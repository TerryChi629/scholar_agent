"""M12.3 去重存储 + 推送记录 + 自定义兴趣 (供前端速递工作区)。

复用 settings.sqlite_path (与 card memory / memory2 同库, 不同表)。
- digest_pushed: 已推论文 (去重 + 历史记录)。
- digest_interests: 用户自然语言添加的自定义兴趣 (并入画像, 高权重置顶)。
"""
from __future__ import annotations

import sqlite3
import time
import uuid

from config import settings


def _conn() -> sqlite3.Connection:
    settings.ensure_dirs()
    conn = sqlite3.connect(settings.sqlite_path)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS digest_pushed (
            arxiv_id TEXT PRIMARY KEY,
            title TEXT,
            topic TEXT,
            pushed_at REAL
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS digest_interests (
            id TEXT PRIMARY KEY,
            text TEXT,
            created_at REAL
        )"""
    )
    return conn


def pushed_ids() -> set[str]:
    """返回所有已推送过的 arXiv id 集合。"""
    conn = _conn()
    try:
        rows = conn.execute("SELECT arxiv_id FROM digest_pushed").fetchall()
    finally:
        conn.close()
    return {r[0] for r in rows if r[0]}


def mark_pushed(items: list[tuple[str, str, str]]) -> None:
    """落库已推送论文。items: [(arxiv_id, title, topic), ...]。"""
    if not items:
        return
    now = time.time()
    conn = _conn()
    try:
        with conn:
            conn.executemany(
                "INSERT OR REPLACE INTO digest_pushed (arxiv_id, title, topic, pushed_at) "
                "VALUES (?,?,?,?)",
                [(aid, title, topic, now) for aid, title, topic in items],
            )
    finally:
        conn.close()


def list_pushed(limit: int = 100) -> list[dict]:
    """按推送时间倒序列出历史推送记录 (供前端推送记录页)。"""
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT arxiv_id, title, topic, pushed_at FROM digest_pushed "
            "ORDER BY pushed_at DESC LIMIT ?",
            (max(1, int(limit)),),
        ).fetchall()
    finally:
        conn.close()
    return [
        {"arxiv_id": aid, "title": title, "topic": topic,
         "url": f"https://arxiv.org/abs/{aid}", "pushed_at": ts}
        for aid, title, topic, ts in rows
    ]


# —— 自定义兴趣 (用户自然语言补充, 并入画像) ——

def add_interest(text: str) -> dict:
    """新增一条自定义兴趣, 返回该条记录。"""
    text = (text or "").strip()
    if not text:
        raise ValueError("interest text is empty")
    item = {"id": uuid.uuid4().hex[:12], "text": text[:300], "created_at": time.time()}
    conn = _conn()
    try:
        with conn:
            conn.execute(
                "INSERT INTO digest_interests (id, text, created_at) VALUES (?,?,?)",
                (item["id"], item["text"], item["created_at"]),
            )
    finally:
        conn.close()
    return item


def list_interests() -> list[dict]:
    """列出全部自定义兴趣 (按添加时间倒序)。"""
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT id, text, created_at FROM digest_interests ORDER BY created_at DESC"
        ).fetchall()
    finally:
        conn.close()
    return [{"id": i, "text": t, "created_at": ts} for i, t, ts in rows]


def delete_interest(interest_id: str) -> bool:
    """删除一条自定义兴趣, 返回是否删除成功。"""
    conn = _conn()
    try:
        with conn:
            cur = conn.execute(
                "DELETE FROM digest_interests WHERE id=?", (interest_id,)
            )
        return cur.rowcount > 0
    finally:
        conn.close()

