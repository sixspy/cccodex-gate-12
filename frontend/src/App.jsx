import { useEffect, useState } from "react";

function loadProfile() {
  try {
    return JSON.parse(localStorage.getItem("lake.profile") || "null");
  } catch {
    return null;
  }
}

async function request(path, options = {}) {
  const token = localStorage.getItem("lake.token") || "";
  const headers = { ...(options.headers || {}) };
  if (token) headers.Authorization = `Bearer ${token}`;
  const response = await fetch(path, { ...options, headers });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(data.detail || "请求没有完成");
  }
  return data;
}

export default function App() {
  const [profile, setProfile] = useState(loadProfile);
  const [username, setUsername] = useState("admin");
  const [password, setPassword] = useState("123456");
  const [loginError, setLoginError] = useState("");
  const [dates, setDates] = useState([]);
  const [detail, setDetail] = useState(null);
  const [voidInfo, setVoidInfo] = useState(null);
  const [formError, setFormError] = useState("");
  const [busy, setBusy] = useState(false);

  async function loadDay(bizDate) {
    const [day, voided] = await Promise.all([
      request(`/api/dates/${bizDate}`),
      request(`/api/dates/${bizDate}/void`),
    ]);
    setDetail(day);
    setVoidInfo(voided);
  }

  async function refresh(prefer) {
    const rows = await request("/api/dates");
    setDates(rows);
    const target = prefer || detail?.biz_date || rows[0]?.biz_date;
    if (target) await loadDay(target);
  }

  useEffect(() => {
    if (!profile) return;
    refresh().catch(() => {
      localStorage.removeItem("lake.token");
      localStorage.removeItem("lake.profile");
      setProfile(null);
    });
  }, [profile]);

  async function submitLogin(event) {
    event.preventDefault();
    setLoginError("");
    try {
      const data = await request("/api/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ username, password }),
      });
      localStorage.setItem("lake.token", data.access_token);
      const next = { username: data.username, role: data.role, title: data.title };
      localStorage.setItem("lake.profile", JSON.stringify(next));
      setProfile(next);
    } catch (error) {
      setLoginError(error.message);
    }
  }

  async function buildDay() {
    if (!detail) return;
    setBusy(true);
    setFormError("");
    try {
      const data = await request(`/api/dates/${detail.biz_date}/build`, { method: "POST" });
      setDetail(data);
      setVoidInfo(await request(`/api/dates/${detail.biz_date}/void`));
      setDates(await request("/api/dates"));
    } catch (error) {
      setFormError(error.message);
    } finally {
      setBusy(false);
    }
  }

  async function setPolicy(policy) {
    if (!detail) return;
    setBusy(true);
    setFormError("");
    try {
      setVoidInfo(
        await request(`/api/dates/${detail.biz_date}/void-setting`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ policy }),
        })
      );
    } catch (error) {
      setFormError(error.message);
    } finally {
      setBusy(false);
    }
  }

  async function reverseEntry(entry) {
    if (!detail) return;
    setBusy(true);
    setFormError("");
    try {
      setVoidInfo(
        await request(`/api/dates/${detail.biz_date}/reverse`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ container_no: entry.container_no, policy: entry.policy }),
        })
      );
      setDetail(await request(`/api/dates/${detail.biz_date}`));
    } catch (error) {
      setFormError(error.message);
      setVoidInfo(await request(`/api/dates/${detail.biz_date}/void`));
    } finally {
      setBusy(false);
    }
  }

  function logout() {
    localStorage.removeItem("lake.token");
    localStorage.removeItem("lake.profile");
    setProfile(null);
    setDetail(null);
    setVoidInfo(null);
  }

  if (!profile) {
    return (
      <main className="sheet gate">
        <p className="kicker">贴源层 → 明细层</p>
        <h1>闸口事件湖仓台</h1>
        <p>扫描原文先落贴源。只有字典认得出的事件才收进明细。</p>
        <form onSubmit={submitLogin}>
          <label>用户<input value={username} onChange={(event) => setUsername(event.target.value)} /></label>
          <label>口令<input type="password" value={password} onChange={(event) => setPassword(event.target.value)} /></label>
          <button>打开这一层</button>
          {loginError ? <p className="warn">{loginError}</p> : null}
        </form>
      </main>
    );
  }

  return (
    <>
      <header className="bar">
        <div>
          <p className="kicker">闸口扫描 · 分层</p>
          <h1>闸口事件湖仓台</h1>
        </div>
        <div className="who">
          <span>{profile.title} {profile.username}</span>
          <button className="line" onClick={logout}>离开</button>
        </div>
      </header>
      <main className="layout">
        <aside>
          {dates.map((row) => (
            <button
              key={row.biz_date}
              className={detail?.biz_date === row.biz_date ? "day on" : "day"}
              onClick={() => loadDay(row.biz_date)}
            >
              <strong>{row.biz_date}</strong>
              <em>{row.built ? "明细已收" : "尚未构建"}</em>
              {row.pending_revocations ? <em className="pend">撤销待办 {row.pending_revocations}</em> : null}
              <span>贴源 {row.ods_count} 条 · 明细 {row.dwd_count} 条</span>
            </button>
          ))}
        </aside>
        {detail ? (
          <section className="sheet">
            <div className="split-head">
              <h2>{detail.biz_date}</h2>
              <button disabled={profile.role !== "writer" || busy} onClick={buildDay}>
                按字典构建明细层
              </button>
            </div>
            <p className="note">IN 进闸，OUT 出闸，RS 倒箱。箱号要是四位字母加七位数字。贴源里带 revoke 标记的是撤销扫描。</p>
            {profile.role !== "writer" ? <p className="note">核对员不能重新构建，也不能定口径、冲销。</p> : null}
            {formError ? <p className="warn">{formError}</p> : null}
            <div className="layers">
              <article>
                <h3>贴源层</h3>
                <ul>
                  {detail.ods.map((row) => (
                    <li key={row.id}>
                      <span>{row.terminal_code}</span>
                      <code>{JSON.stringify(row.raw_payload)}</code>
                    </li>
                  ))}
                </ul>
              </article>
              <article>
                <h3>明细层</h3>
                {detail.moves.length ? (
                  <table>
                    <thead>
                      <tr><th>来源</th><th>箱号</th><th>事件</th><th>时间</th><th>留痕</th></tr>
                    </thead>
                    <tbody>
                      {detail.moves.map((row) => (
                        <tr key={`${row.source_id}-${row.event_kind}`} className={row.voided_at ? "voided" : ""}>
                          <td>{row.source_id}</td>
                          <td>{row.container_no}</td>
                          <td>{row.event_kind}</td>
                          <td>{row.event_at}</td>
                          <td className="mark">{row.voided_at ? <em className="tomb">已打墓碑</em> : "—"}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                ) : <p>这一天的明细还是空的。</p>}
                <h3>剔除</h3>
                <ul>
                  {detail.rejects.map((row) => (
                    <li key={row.source_id}><span>来源 {row.source_id}</span><em>{row.reason}</em></li>
                  ))}
                  {!detail.rejects.length ? <li>没有被剔除的扫描</li> : null}
                </ul>
              </article>
            </div>
            {voidInfo ? (
              <div className="void">
                <article className="panel">
                  <h3>作废设置</h3>
                  {voidInfo.setting ? (
                    <p className="note">
                      口径已收束：<strong>{voidInfo.setting.policy_name}</strong>
                      （{voidInfo.setting.locked_by} 定于 {voidInfo.setting.locked_at}），当天禁止改口。
                    </p>
                  ) : (
                    <>
                      <p className="note">
                        口径二选一，落进设置即收束：在明细上打墓碑留行不删，或把明细行清掉只留贴源撤销原文。
                      </p>
                      {profile.role === "writer" ? (
                        <div className="row">
                          <button disabled={busy} onClick={() => setPolicy("tombstone")}>打墓碑留行不删</button>
                          <button disabled={busy} className="danger" onClick={() => setPolicy("erase")}>清掉明细行留原文</button>
                        </div>
                      ) : (
                        <p className="note">核对员不能定口径。</p>
                      )}
                    </>
                  )}
                </article>
                <article className="panel">
                  <h3>撤销队列</h3>
                  {voidInfo.queue.length ? (
                    <table>
                      <thead>
                        <tr><th>来源</th><th>箱号</th><th>口径</th><th>状态</th><th></th></tr>
                      </thead>
                      <tbody>
                        {voidInfo.queue.map((entry) => (
                          <tr key={entry.id}>
                            <td>{entry.source_id}</td>
                            <td>{entry.container_no}</td>
                            <td>{entry.policy_name}</td>
                            <td>{entry.status === "pending" ? "待冲销" : `已冲销 ${entry.settled_at || ""}`}</td>
                            <td>
                              {entry.status === "pending" && profile.role === "writer" ? (
                                <button disabled={busy} onClick={() => reverseEntry(entry)}>冲销</button>
                              ) : null}
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  ) : (
                    <p className="note">
                      {voidInfo.setting
                        ? "队列是空的。重新构建后，与作废设置合口径的撤销扫描才会进队列。"
                        : "队列是空的。缺撤销标记的扫描不进撤销队列。"}
                    </p>
                  )}
                </article>
                <article className="panel">
                  <h3>墓碑列表</h3>
                  {voidInfo.tombstones.length ? (
                    <table>
                      <thead>
                        <tr><th>箱子</th><th>留痕次数</th><th>抹行次数</th><th>口径</th></tr>
                      </thead>
                      <tbody>
                        {voidInfo.tombstones.map((row) => (
                          <tr key={row.container_no}>
                            <td>{row.container_no}</td>
                            <td>{row.tombstoned}</td>
                            <td>{row.erased}</td>
                            <td>{row.policy_name}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  ) : (
                    <p className="note">还没有冲销落账。</p>
                  )}
                </article>
                <article className="panel">
                  <h3>冲销详情</h3>
                  {voidInfo.reversals.length ? (
                    voidInfo.reversals.map((rev) => (
                      <div key={rev.id} className="rev">
                        {rev.result === "contention" ? (
                          <p className="warn">
                            冲销争用 · {rev.container_no} · {rev.operator} · {rev.created_at}：
                            {rev.note}（这一路带的口径：{rev.policy_name}）
                          </p>
                        ) : (
                          <>
                            <p className="note">
                              {rev.container_no} · {rev.policy_name} · {rev.operator} · {rev.created_at} · 影响 {rev.affected_count} 行
                            </p>
                            <div className="cmp">
                              <div>
                                <h4>明细原行</h4>
                                <ul>
                                  {rev.before_rows.map((line) => (
                                    <li key={`b-${rev.id}-${line.source_id}`}>
                                      <span>来源 {line.source_id} · {line.event_kind} · {line.event_at}</span>
                                    </li>
                                  ))}
                                  {!rev.before_rows.length ? <li><span>原行是空的</span></li> : null}
                                </ul>
                              </div>
                              <div>
                                <h4>撤销后的行</h4>
                                {rev.policy === "erase" ? (
                                  <ul>
                                    {rev.before_rows.map((line) => (
                                      <li key={`a-${rev.id}-${line.source_id}`} className="erased">
                                        <span>
                                          <s>来源 {line.source_id} · {line.event_kind} · {line.event_at}</s>{" "}
                                          <em className="tomb">已抹掉</em>
                                        </span>
                                      </li>
                                    ))}
                                    {!rev.before_rows.length ? <li><span>没有可抹的行</span></li> : null}
                                  </ul>
                                ) : (
                                  <ul>
                                    {rev.after_rows.map((line) => (
                                      <li key={`a-${rev.id}-${line.source_id}`} className={line.voided ? "erased" : ""}>
                                        <span>
                                          来源 {line.source_id} · {line.event_kind} · {line.event_at}{" "}
                                          {line.voided ? <em className="tomb">已打墓碑</em> : null}
                                        </span>
                                      </li>
                                    ))}
                                    {!rev.after_rows.length ? <li><span>撤销后没有行</span></li> : null}
                                  </ul>
                                )}
                              </div>
                            </div>
                          </>
                        )}
                      </div>
                    ))
                  ) : (
                    <p className="note">还没有冲销记录。</p>
                  )}
                </article>
              </div>
            ) : null}
          </section>
        ) : null}
      </main>
    </>
  );
}
