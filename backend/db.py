import json
import os
import time
from datetime import datetime

import psycopg
from psycopg.rows import dict_row

from build import BOX, clean_scan

SCHEMA = """
CREATE TABLE IF NOT EXISTS ods_scan (
    id BIGSERIAL PRIMARY KEY,
    biz_date DATE NOT NULL,
    terminal_code TEXT NOT NULL,
    raw_payload JSONB NOT NULL,
    landed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS dwd_move (
    id BIGSERIAL PRIMARY KEY,
    biz_date DATE NOT NULL,
    source_id BIGINT NOT NULL REFERENCES ods_scan(id),
    terminal_code TEXT NOT NULL,
    container_no TEXT NOT NULL,
    event_kind TEXT NOT NULL,
    event_at TIMESTAMP NOT NULL,
    voided_at TIMESTAMPTZ
);

ALTER TABLE dwd_move ADD COLUMN IF NOT EXISTS voided_at TIMESTAMPTZ;

CREATE TABLE IF NOT EXISTS build_runs (
    id BIGSERIAL PRIMARY KEY,
    biz_date DATE NOT NULL,
    accepted_count INTEGER NOT NULL,
    rejected_count INTEGER NOT NULL,
    built_by TEXT NOT NULL,
    built_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS build_rejects (
    id BIGSERIAL PRIMARY KEY,
    run_id BIGINT NOT NULL REFERENCES build_runs(id),
    source_id BIGINT NOT NULL REFERENCES ods_scan(id),
    reason TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS void_settings (
    biz_date DATE PRIMARY KEY,
    policy TEXT NOT NULL CHECK (policy IN ('tombstone', 'erase')),
    locked_by TEXT NOT NULL,
    locked_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS revocation_queue (
    id BIGSERIAL PRIMARY KEY,
    biz_date DATE NOT NULL,
    source_id BIGINT NOT NULL UNIQUE REFERENCES ods_scan(id),
    container_no TEXT NOT NULL,
    policy TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    enqueued_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    settled_at TIMESTAMPTZ,
    reversal_id BIGINT
);

CREATE TABLE IF NOT EXISTS reversals (
    id BIGSERIAL PRIMARY KEY,
    biz_date DATE NOT NULL,
    container_no TEXT NOT NULL,
    policy TEXT NOT NULL,
    result TEXT NOT NULL,
    note TEXT,
    operator TEXT NOT NULL,
    affected_count INTEGER NOT NULL DEFAULT 0,
    before_rows JSONB NOT NULL DEFAULT '[]'::jsonb,
    after_rows JSONB NOT NULL DEFAULT '[]'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""

POLICY_NAMES = {"tombstone": "打墓碑留行", "erase": "抹行留原文"}


class VoidError(Exception):
    def __init__(self, detail, status=409):
        super().__init__(detail)
        self.detail = detail
        self.status = status


def connect():
    return psycopg.connect(os.environ["DATABASE_URL"], row_factory=dict_row)


def init_db():
    last_error = None
    for _ in range(30):
        try:
            with connect() as conn:
                conn.execute(SCHEMA)
                count = conn.execute("SELECT COUNT(*) AS n FROM ods_scan").fetchone()["n"]
                if count == 0:
                    _seed(conn)
                conn.commit()
            return
        except psycopg.OperationalError as exc:
            last_error = exc
            time.sleep(1)
    raise last_error


def _land(conn, biz_date, terminal, payload):
    conn.execute(
        """
        INSERT INTO ods_scan (biz_date, terminal_code, raw_payload)
        VALUES (%s, %s, %s::jsonb)
        """,
        (biz_date, terminal, json.dumps(payload, ensure_ascii=False)),
    )


def _seed(conn):
    built = [
        {"scan": "IN", "box": "CSNU1029384", "at": "2026-11-11T07:40:00", "gate": "T1"},
        {"scan": "OUT", "box": "CSNU1029384", "at": "2026-11-11T16:05:00", "gate": "T1"},
    ]
    waiting = [
        {"scan": "IN", "box": "TCLU5566778", "at": "2026-11-12T09:15:00", "gate": "T2"},
        {"scan": "XX", "box": "TCLU5566778", "at": "2026-11-12T11:00:00", "gate": "T2"},
        {"scan": "RS", "box": "BAD", "at": "2026-11-12T12:00:00", "gate": "T2"},
        {"scan": "OUT", "box": "TCLU5566778", "at": "2026-11-12T13:20:00", "gate": "T2", "revoke": "tombstone"},
        {"scan": "OUT", "box": "TCLU5566778", "at": "2026-11-12T13:25:00", "gate": "T2", "revoke": "erase"},
    ]
    for payload in built:
        _land(conn, "2026-11-11", payload["gate"], payload)
    for payload in waiting:
        _land(conn, "2026-11-12", payload["gate"], payload)
    rebuild_day(conn, "2026-11-11", "admin")
    # 贴源后又来的撤销扫描：明细已收，口径未定，这一箱先不准清洗
    _land(
        conn,
        "2026-11-11",
        "T1",
        {"scan": "OUT", "box": "CSNU1029384", "at": "2026-11-11T18:40:00", "gate": "T1", "revoke": "erase"},
    )


def rebuild_day(conn, biz_date: str, username: str):
    conn.execute("DELETE FROM dwd_move WHERE biz_date = %s", (biz_date,))
    rows = conn.execute(
        "SELECT id, terminal_code, raw_payload FROM ods_scan WHERE biz_date = %s ORDER BY id",
        (biz_date,),
    ).fetchall()
    setting = conn.execute(
        "SELECT policy FROM void_settings WHERE biz_date = %s",
        (biz_date,),
    ).fetchone()
    policy = setting["policy"] if setting else None
    run = conn.execute(
        """
        INSERT INTO build_runs (biz_date, accepted_count, rejected_count, built_by)
        VALUES (%s, 0, 0, %s)
        RETURNING id
        """,
        (biz_date, username),
    ).fetchone()
    accepted = 0
    rejected = 0
    parsed = []
    for row in rows:
        payload = row["raw_payload"]
        if isinstance(payload, str):
            payload = json.loads(payload)
        marker = str(payload.get("revoke") or "").strip().lower()
        box = str(payload.get("box") or "").strip().upper()
        parsed.append((row, payload, marker, box))
    revoked_boxes = {box for _, _, marker, box in parsed if marker and BOX.match(box)}
    for row, payload, marker, box in parsed:
        reason = None
        if policy is None and box in revoked_boxes:
            reason = "作废还没收束，这一箱不准清洗"
        elif marker:
            if not BOX.match(box):
                reason = "箱号不是四位字母加七位数字"
            else:
                try:
                    datetime.fromisoformat(str(payload.get("at") or "").strip())
                except ValueError:
                    reason = "扫描时间无法读取"
            if reason is None and marker != policy:
                reason = "作废口径偏了"
            if reason is None:
                conn.execute(
                    """
                    INSERT INTO revocation_queue (biz_date, source_id, container_no, policy)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (source_id) DO NOTHING
                    """,
                    (biz_date, row["id"], box, marker),
                )
                accepted += 1
                continue
        else:
            cleaned, reason = clean_scan(payload)
            if reason is None:
                conn.execute(
                    """
                    INSERT INTO dwd_move (biz_date, source_id, terminal_code, container_no, event_kind, event_at)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    """,
                    (
                        biz_date,
                        row["id"],
                        row["terminal_code"],
                        cleaned["container_no"],
                        cleaned["event_kind"],
                        cleaned["event_at"],
                    ),
                )
                accepted += 1
                continue
        conn.execute(
            "INSERT INTO build_rejects (run_id, source_id, reason) VALUES (%s, %s, %s)",
            (run["id"], row["id"], reason),
        )
        rejected += 1
    # 已落账的冲销不能被清洗冲掉：墓碑补打，抹行补抹
    for rev in conn.execute(
        "SELECT container_no, policy FROM reversals WHERE biz_date = %s AND result = 'done'",
        (biz_date,),
    ).fetchall():
        if rev["policy"] == "tombstone":
            conn.execute(
                """
                UPDATE dwd_move SET voided_at = COALESCE(voided_at, NOW())
                WHERE biz_date = %s AND container_no = %s
                """,
                (biz_date, rev["container_no"]),
            )
        else:
            conn.execute(
                "DELETE FROM dwd_move WHERE biz_date = %s AND container_no = %s",
                (biz_date, rev["container_no"]),
            )
    conn.execute(
        "UPDATE build_runs SET accepted_count = %s, rejected_count = %s WHERE id = %s",
        (accepted, rejected, run["id"]),
    )
    return run["id"]


def _snapshot(conn, biz_date, box):
    rows = conn.execute(
        """
        SELECT source_id, event_kind, event_at, voided_at
        FROM dwd_move
        WHERE biz_date = %s AND container_no = %s
        ORDER BY event_at, id
        """,
        (biz_date, box),
    ).fetchall()
    snap = []
    for row in rows:
        snap.append(
            {
                "source_id": row["source_id"],
                "event_kind": row["event_kind"],
                "event_at": row["event_at"].strftime("%Y-%m-%d %H:%M") if row["event_at"] else None,
                "voided": row["voided_at"] is not None,
            }
        )
    return snap


def _record_contention(conn, biz_date, box, policy, username, note):
    conn.execute(
        """
        INSERT INTO reversals (biz_date, container_no, policy, result, note, operator)
        VALUES (%s, %s, %s, 'contention', %s, %s)
        """,
        (biz_date, box, policy, note, username),
    )


def apply_reversal(conn, biz_date: str, box: str, policy: str, username: str):
    setting = conn.execute(
        "SELECT policy FROM void_settings WHERE biz_date = %s",
        (biz_date,),
    ).fetchone()
    if not setting:
        raise VoidError("先定作废口径，再冲销")
    # 抢段加锁：同一箱号同一天同一时刻只放行一路冲销
    conn.execute(
        "SELECT pg_advisory_xact_lock(hashtext(%s)::bigint)",
        (f"{biz_date}:{box}",),
    )
    done = conn.execute(
        """
        SELECT id FROM reversals
        WHERE biz_date = %s AND container_no = %s AND result = 'done'
        """,
        (biz_date, box),
    ).fetchone()
    if done:
        _record_contention(conn, biz_date, box, policy, username, "这一箱今天已经冲销落账")
        return {"result": "contention", "detail": "冲销争用：这一箱今天已经冲销落账"}
    if policy != setting["policy"]:
        _record_contention(
            conn,
            biz_date,
            box,
            policy,
            username,
            f"这一路带的口径是{POLICY_NAMES.get(policy, policy)}，与作废设置不合",
        )
        return {"result": "contention", "detail": "冲销争用：口径与作废设置不合"}
    entry = conn.execute(
        """
        SELECT id FROM revocation_queue
        WHERE biz_date = %s AND container_no = %s AND status = 'pending'
        ORDER BY id LIMIT 1
        """,
        (biz_date, box),
    ).fetchone()
    if not entry:
        raise VoidError("这一箱没有待冲销的撤销", 404)
    before = _snapshot(conn, biz_date, box)
    if setting["policy"] == "tombstone":
        conn.execute(
            """
            UPDATE dwd_move SET voided_at = NOW()
            WHERE biz_date = %s AND container_no = %s AND voided_at IS NULL
            """,
            (biz_date, box),
        )
    else:
        conn.execute(
            "DELETE FROM dwd_move WHERE biz_date = %s AND container_no = %s",
            (biz_date, box),
        )
    after = _snapshot(conn, biz_date, box)
    reversal = conn.execute(
        """
        INSERT INTO reversals (biz_date, container_no, policy, result, operator, affected_count, before_rows, after_rows)
        VALUES (%s, %s, %s, 'done', %s, %s, %s::jsonb, %s::jsonb)
        RETURNING id
        """,
        (
            biz_date,
            box,
            setting["policy"],
            username,
            len(before),
            json.dumps(before, ensure_ascii=False),
            json.dumps(after, ensure_ascii=False),
        ),
    ).fetchone()
    conn.execute(
        """
        UPDATE revocation_queue
        SET status = 'done', settled_at = NOW(), reversal_id = %s
        WHERE id = %s
        """,
        (reversal["id"], entry["id"]),
    )
    return {"result": "done", "reversal_id": reversal["id"]}
