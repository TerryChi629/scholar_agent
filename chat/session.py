"""chat 短期(会话)记忆: 滑动窗口 + 摘要压缩。

与 memory2 (长期 preference/procedure) 分工:
- memory2 = 跨会话长期记忆, 承载用户偏好/规则。
- session  = 单会话短期记忆, 承载多轮对话历史, 用于指代消解 / 追问 / 避免重复检索。
  生命周期 = 一次会话; 不承载论文事实 (事实只来自 RAG 证据)。

存储 (复用 settings.sqlite_path, 不同表):
- chat_sessions: 每会话一行, 存滚动摘要 (summary) + 已摘要到第几轮 (summarized_upto)。
- chat_turns:    每轮一行 (role + content), 按 seq 递增。

注入策略:
- 取最近 window_turns 轮原文; 更早的轮被压成 summary (用 low 档 LLM)。
- 注入块 = [会话摘要] + [最近N轮对话], 供合成端做指代消解。
"""
from __future__ import annotations

import json
import sqlite3
import time

from config import settings
from core.llm import get_llm
from core.obs import log_event


def _conn() -> sqlite3.Connection:
    settings.ensure_dirs()
    conn = sqlite3.connect(settings.sqlite_path)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS chat_sessions (
            session_id TEXT PRIMARY KEY,
            summary TEXT,
            summarized_upto INTEGER,
            title TEXT,
            total_tokens INTEGER DEFAULT 0,
            created_at REAL,
            updated_at REAL
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS chat_turns (
            session_id TEXT,
            seq INTEGER,
            role TEXT,
            content TEXT,
            tokens INTEGER DEFAULT 0,
            evidence_json TEXT,
            created_at REAL
        )"""
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_chat_turns_sess ON chat_turns(session_id, seq)"
    )
    # 兼容旧库 (M10 之前无 title/total_tokens/tokens 列): 缺失则补列。
    _ensure_column(conn, "chat_sessions", "title", "TEXT")
    _ensure_column(conn, "chat_sessions", "total_tokens", "INTEGER DEFAULT 0")
    _ensure_column(conn, "chat_turns", "tokens", "INTEGER DEFAULT 0")
    _ensure_column(conn, "chat_turns", "evidence_json", "TEXT")
    return conn


def _ensure_column(conn: sqlite3.Connection, table: str, col: str, decl: str) -> None:
    cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if col not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")


def _next_seq(conn: sqlite3.Connection, session_id: str) -> int:
    row = conn.execute(
        "SELECT MAX(seq) FROM chat_turns WHERE session_id=?", (session_id,)
    ).fetchone()
    return (row[0] + 1) if row and row[0] is not None else 0


def append_turn(session_id: str, role: str, content: str, tokens: int = 0,
                evidence: list | None = None) -> None:
    """追加一轮对话。tokens/evidence 主要用于 assistant 轮的 token 与证据回看。"""
    if not settings.chat_session_enabled or not (session_id or "").strip():
        return
    content = (content or "").strip()
    if not content:
        return
    conn = _conn()
    now = time.time()
    with conn:
        seq = _next_seq(conn, session_id)
        ev_json = json.dumps(evidence or [], ensure_ascii=False) if evidence else None
        conn.execute(
            """INSERT INTO chat_turns
               (session_id, seq, role, content, tokens, evidence_json, created_at)
               VALUES (?,?,?,?,?,?,?)""",
            (session_id, seq, role, content, int(tokens or 0), ev_json, now),
        )
        # 标题取首条 user 问题 (截断); total_tokens 累加。
        title = content[:40] if (role == "user" and seq == 0) else None
        conn.execute(
            """INSERT INTO chat_sessions (session_id, summary, summarized_upto, title, total_tokens, created_at, updated_at)
               VALUES (?, '', -1, ?, ?, ?, ?)
               ON CONFLICT(session_id) DO UPDATE SET
                 updated_at=excluded.updated_at,
                 total_tokens=COALESCE(total_tokens,0)+?,
                 title=COALESCE(title, ?)""",
            (session_id, title, int(tokens or 0), now, now, int(tokens or 0), title),
        )
    conn.close()


def _load(session_id: str) -> tuple[str, int, list[tuple]]:
    """返回 (summary, summarized_upto, turns) ; turns=[(seq, role, content), ...] 按 seq 升序。"""
    conn = _conn()
    srow = conn.execute(
        "SELECT summary, summarized_upto FROM chat_sessions WHERE session_id=?",
        (session_id,),
    ).fetchone()
    turns = conn.execute(
        "SELECT seq, role, content FROM chat_turns WHERE session_id=? ORDER BY seq ASC",
        (session_id,),
    ).fetchall()
    conn.close()
    summary = srow[0] if srow else ""
    upto = srow[1] if srow else -1
    return summary or "", upto if upto is not None else -1, turns


def build_context_block(session_id: str) -> str:
    """组织会话上下文注入块: [会话摘要] + [最近N轮对话]。无历史返回空串。"""
    if not settings.chat_session_enabled or not (session_id or "").strip():
        return ""
    summary, _, turns = _load(session_id)
    if not turns and not summary:
        return ""

    window = max(settings.chat_session_window_turns, 1) * 2  # 1 轮 = 1 user + 1 assistant
    recent = turns[-window:]
    lines: list[str] = []
    if summary:
        lines.append(f"[早前对话摘要] {summary}")
    for _, role, content in recent:
        who = "用户" if role == "user" else "助手"
        lines.append(f"{who}: {content}")
    if not lines:
        return ""
    return (
        "【当前会话上下文】(仅用于理解指代/追问, 不得作为论文事实来源):\n"
        + "\n".join(lines)
    )


_SUMMARY_SYS = (
    "把下面的多轮对话压缩成一段简洁的会话摘要 (3-5 句), 保留: 用户问过的核心主题、"
    "已给出的关键结论指向、未尽的追问线索。只输出摘要本身, 不要解释。"
)


def maybe_summarize(session_id: str) -> None:
    """超窗的旧轮压成滚动摘要 (low 档 LLM); 失败静默跳过, 不阻断主链路。"""
    if not settings.chat_session_enabled or not (session_id or "").strip():
        return
    summary, upto, turns = _load(session_id)
    window = max(settings.chat_session_window_turns, 1) * 2
    # 只在历史超过窗口时压缩; 待压缩范围 = (upto, len-window]
    if len(turns) <= window:
        return
    cut = len(turns) - window  # 这之前 (seq < turns[cut].seq) 的轮要进摘要
    pending = [t for t in turns[:cut] if t[0] > upto]
    if not pending:
        return

    convo = "\n".join(
        f"{'用户' if r == 'user' else '助手'}: {c}" for _, r, c in pending
    )
    base = f"已有摘要: {summary}\n\n新增对话:\n{convo}" if summary else convo
    try:
        provider, model, _, _ = settings.resolve_agent_model("chat")
        new_summary = get_llm().chat_text(
            [{"role": "system", "content": _SUMMARY_SYS},
             {"role": "user", "content": base}],
            temperature=0.0, provider=provider, model=model,
        )
        new_summary = " ".join((new_summary or "").split())
        if not new_summary:
            return
        new_upto = pending[-1][0]
        conn = _conn()
        with conn:
            conn.execute(
                "UPDATE chat_sessions SET summary=?, summarized_upto=?, updated_at=? WHERE session_id=?",
                (new_summary, new_upto, time.time(), session_id),
            )
        conn.close()
        log_event("chat.session.summarized", session_id=session_id, upto=new_upto)
    except Exception as exc:  # noqa: BLE001  摘要失败不阻断对话
        log_event("chat.session.summarize_failed", level="WARNING", error=str(exc))


# —— 会话记录查询 (供前端历史侧栏 / token 统计) ——

def prune_old_sessions(keep: int = 10) -> int:
    """保留最近 keep 个会话, 删除更早会话及其 turns。返回删除的会话数。"""
    keep = max(int(keep or 10), 1)
    conn = _conn()
    rows = conn.execute(
        "SELECT session_id FROM chat_sessions ORDER BY updated_at DESC LIMIT -1 OFFSET ?",
        (keep,),
    ).fetchall()
    ids = [r[0] for r in rows]
    if not ids:
        conn.close()
        return 0
    with conn:
        conn.executemany("DELETE FROM chat_turns WHERE session_id=?", [(i,) for i in ids])
        conn.executemany("DELETE FROM chat_sessions WHERE session_id=?", [(i,) for i in ids])
    conn.close()
    return len(ids)


def list_sessions(limit: int = 10) -> list[dict]:
    """列出全部会话 (按最近更新倒序), 含标题/轮数/累计 token。"""
    prune_old_sessions(keep=10)
    conn = _conn()
    rows = conn.execute(
        """SELECT s.session_id, s.title, s.total_tokens, s.created_at, s.updated_at,
                  (SELECT COUNT(*) FROM chat_turns t WHERE t.session_id = s.session_id) AS turns
           FROM chat_sessions s
           ORDER BY s.updated_at DESC LIMIT ?""",
        (int(limit),),
    ).fetchall()
    conn.close()
    return [
        {
            "session_id": r[0],
            "title": r[1] or "(未命名会话)",
            "total_tokens": r[2] or 0,
            "created_at": r[3],
            "updated_at": r[4],
            "turns": r[5] or 0,
        }
        for r in rows
    ]


def rename_session(session_id: str, title: str) -> bool:
    """重命名会话。返回是否命中会话。"""
    title = " ".join((title or "").split())[:60]
    if not session_id or not title:
        return False
    conn = _conn()
    with conn:
        cur = conn.execute(
            "UPDATE chat_sessions SET title=?, updated_at=? WHERE session_id=?",
            (title, time.time(), session_id),
        )
    conn.close()
    return cur.rowcount > 0


def delete_session(session_id: str) -> bool:
    """删除单个会话及其全部 turns。返回是否命中会话。"""
    if not session_id:
        return False
    conn = _conn()
    exists = conn.execute(
        "SELECT 1 FROM chat_sessions WHERE session_id=? LIMIT 1",
        (session_id,),
    ).fetchone()
    with conn:
        conn.execute("DELETE FROM chat_turns WHERE session_id=?", (session_id,))
        conn.execute("DELETE FROM chat_sessions WHERE session_id=?", (session_id,))
    conn.close()
    return bool(exists)


def get_session(session_id: str) -> dict:
    """返回单个会话的完整对话记录 (含每轮 token)。"""
    conn = _conn()
    srow = conn.execute(
        "SELECT title, total_tokens, summary, created_at, updated_at FROM chat_sessions WHERE session_id=?",
        (session_id,),
    ).fetchone()
    turns = conn.execute(
        "SELECT seq, role, content, tokens, evidence_json, created_at FROM chat_turns WHERE session_id=? ORDER BY seq ASC",
        (session_id,),
    ).fetchall()
    conn.close()
    if not srow and not turns:
        return {}
    return {
        "session_id": session_id,
        "title": (srow[0] if srow else None) or "(未命名会话)",
        "total_tokens": (srow[1] if srow else 0) or 0,
        "summary": (srow[2] if srow else "") or "",
        "created_at": srow[3] if srow else None,
        "updated_at": srow[4] if srow else None,
        "turns": [
            {
                "seq": t[0],
                "role": t[1],
                "content": t[2],
                "tokens": t[3] or 0,
                "evidence": _parse_json_list(t[4]),
                "created_at": t[5],
            }
            for t in turns
        ],
    }


def _parse_json_list(raw: str | None) -> list:
    if not raw:
        return []
    try:
        data = json.loads(raw)
        return data if isinstance(data, list) else []
    except Exception:  # noqa: BLE001  历史坏数据不影响会话回看
        return []
