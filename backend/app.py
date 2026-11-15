import os

from flask import Flask, jsonify, request
from psycopg.errors import UniqueViolation

from auth import authenticate, issue_token, read_user
from build import BOX
from db import POLICY_NAMES, VoidError, apply_reversal, connect, init_db, rebuild_day

app = Flask(__name__)
app.json.ensure_ascii = False


def _stamp(value):
    if value is None:
        return None
    return value.isoformat(sep=" ", timespec="minutes")


def _user():
    return read_user(request.headers.get("Authorization"))


@app.get("/api/health")
def health():
    with connect() as conn:
        conn.execute("SELECT 1")
    return jsonify({"status": "ok", "service": "gate-event-lake"})


@app.post("/api/login")
def login():
    body = request.get_json(silent=True) or {}
    user = authenticate(str(body.get("username", "")).strip(), str(body.get("password", "")))
    if not user:
        return jsonify({"detail": "用户名或密码错误"}), 401
    return jsonify(
        {
            "access_token": issue_token(user),
            "username": user["username"],
            "role": user["role"],
            "title": user["title"],
        }
    )


@app.get("/api/dates")
def list_dates():
    if not _user():
        return jsonify({"detail": "未登录"}), 401
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT d.biz_date,
                   (SELECT COUNT(*) FROM ods_scan o WHERE o.biz_date = d.biz_date) AS ods_count,
                   (SELECT COUNT(*) FROM dwd_move m WHERE m.biz_date = d.biz_date) AS dwd_count,
                   (SELECT COUNT(*) FROM revocation_queue q
                    WHERE q.biz_date = d.biz_date AND q.status = 'pending') AS pending_revocations,
                   EXISTS(SELECT 1 FROM build_runs r WHERE r.biz_date = d.biz_date) AS built
            FROM (SELECT DISTINCT biz_date FROM ods_scan) d
            ORDER BY d.biz_date
            """
        ).fetchall()
    for row in rows:
        row["biz_date"] = row["biz_date"].isoformat()
    return jsonify(rows)


@app.get("/api/dates/<biz_date>")
def date_detail(biz_date):
    if not _user():
        return jsonify({"detail": "未登录"}), 401
    with connect() as conn:
        ods = conn.execute(
            """
            SELECT id, terminal_code, raw_payload
            FROM ods_scan WHERE biz_date = %s ORDER BY id
            """,
            (biz_date,),
        ).fetchall()
        moves = conn.execute(
            """
            SELECT source_id, terminal_code, container_no, event_kind, event_at, voided_at
            FROM dwd_move WHERE biz_date = %s ORDER BY event_at, id
            """,
            (biz_date,),
        ).fetchall()
        run = conn.execute(
            """
            SELECT id, accepted_count, rejected_count, built_by, built_at
            FROM build_runs WHERE biz_date = %s ORDER BY id DESC LIMIT 1
            """,
            (biz_date,),
        ).fetchone()
        rejects = []
        if run:
            rejects = conn.execute(
                """
                SELECT source_id, reason FROM build_rejects
                WHERE run_id = %s ORDER BY source_id
                """,
                (run["id"],),
            ).fetchall()
    if not ods:
        return jsonify({"detail": "这一天没有贴源扫描"}), 404
    for move in moves:
        move["event_at"] = _stamp(move["event_at"])
        move["voided_at"] = _stamp(move["voided_at"])
    if run:
        run["built_at"] = _stamp(run["built_at"])
    return jsonify({"biz_date": biz_date, "ods": ods, "moves": moves, "run": run, "rejects": rejects})


@app.post("/api/dates/<biz_date>/build")
def build_date(biz_date):
    user = _user()
    if not user:
        return jsonify({"detail": "未登录"}), 401
    if user["role"] != "writer":
        return jsonify({"detail": "核对员不能构建明细层"}), 403
    with connect() as conn:
        exists = conn.execute(
            "SELECT 1 FROM ods_scan WHERE biz_date = %s LIMIT 1",
            (biz_date,),
        ).fetchone()
        if not exists:
            return jsonify({"detail": "这一天没有贴源扫描"}), 404
        rebuild_day(conn, biz_date, user["username"])
        conn.commit()
    return date_detail(biz_date)


def _void_payload(conn, biz_date):
    setting = conn.execute(
        "SELECT policy, locked_by, locked_at FROM void_settings WHERE biz_date = %s",
        (biz_date,),
    ).fetchone()
    queue = conn.execute(
        """
        SELECT id, source_id, container_no, policy, status, enqueued_at, settled_at
        FROM revocation_queue WHERE biz_date = %s ORDER BY id
        """,
        (biz_date,),
    ).fetchall()
    tombstones = conn.execute(
        """
        SELECT container_no,
               SUM(CASE WHEN policy = 'tombstone' THEN affected_count ELSE 0 END) AS tombstoned,
               SUM(CASE WHEN policy = 'erase' THEN affected_count ELSE 0 END) AS erased,
               MIN(policy) AS policy
        FROM reversals
        WHERE biz_date = %s AND result = 'done'
        GROUP BY container_no
        ORDER BY container_no
        """,
        (biz_date,),
    ).fetchall()
    reversals = conn.execute(
        """
        SELECT id, container_no, policy, result, note, operator, affected_count,
               before_rows, after_rows, created_at
        FROM reversals WHERE biz_date = %s ORDER BY id DESC
        """,
        (biz_date,),
    ).fetchall()
    if setting:
        setting["policy_name"] = POLICY_NAMES[setting["policy"]]
        setting["locked_at"] = _stamp(setting["locked_at"])
    for entry in queue:
        entry["policy_name"] = POLICY_NAMES.get(entry["policy"], entry["policy"])
        entry["enqueued_at"] = _stamp(entry["enqueued_at"])
        entry["settled_at"] = _stamp(entry["settled_at"])
    for row in tombstones:
        row["policy_name"] = POLICY_NAMES.get(row["policy"], row["policy"])
    for rev in reversals:
        rev["policy_name"] = POLICY_NAMES.get(rev["policy"], rev["policy"])
        rev["created_at"] = _stamp(rev["created_at"])
    return {"setting": setting, "queue": queue, "tombstones": tombstones, "reversals": reversals}


@app.get("/api/dates/<biz_date>/void")
def void_info(biz_date):
    if not _user():
        return jsonify({"detail": "未登录"}), 401
    with connect() as conn:
        return jsonify(_void_payload(conn, biz_date))


@app.post("/api/dates/<biz_date>/void-setting")
def set_void_setting(biz_date):
    user = _user()
    if not user:
        return jsonify({"detail": "未登录"}), 401
    if user["role"] != "writer":
        return jsonify({"detail": "核对员不能定作废口径"}), 403
    body = request.get_json(silent=True) or {}
    policy = str(body.get("policy", "")).strip().lower()
    if policy not in POLICY_NAMES:
        return jsonify({"detail": "作废口径只能二选一：打墓碑留行或抹行留原文"}), 400
    with connect() as conn:
        exists = conn.execute(
            "SELECT 1 FROM void_settings WHERE biz_date = %s",
            (biz_date,),
        ).fetchone()
        if exists:
            return jsonify({"detail": "口径已收束，当天禁止改口"}), 409
        try:
            conn.execute(
                "INSERT INTO void_settings (biz_date, policy, locked_by) VALUES (%s, %s, %s)",
                (biz_date, policy, user["username"]),
            )
        except UniqueViolation:
            conn.rollback()
            return jsonify({"detail": "口径已收束，当天禁止改口"}), 409
        conn.commit()
        return jsonify(_void_payload(conn, biz_date))


@app.post("/api/dates/<biz_date>/reverse")
def reverse_container(biz_date):
    user = _user()
    if not user:
        return jsonify({"detail": "未登录"}), 401
    if user["role"] != "writer":
        return jsonify({"detail": "核对员不能冲销"}), 403
    body = request.get_json(silent=True) or {}
    box = str(body.get("container_no", "")).strip().upper()
    policy = str(body.get("policy", "")).strip().lower()
    if not BOX.match(box):
        return jsonify({"detail": "箱号不是四位字母加七位数字"}), 400
    if policy not in POLICY_NAMES:
        return jsonify({"detail": "冲销要带作废口径"}), 400
    try:
        with connect() as conn:
            outcome = apply_reversal(conn, biz_date, box, policy, user["username"])
            conn.commit()
    except VoidError as exc:
        return jsonify({"detail": exc.detail}), exc.status
    if outcome["result"] == "contention":
        return jsonify({"detail": outcome["detail"]}), 409
    with connect() as conn:
        return jsonify(_void_payload(conn, biz_date))


init_db()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
