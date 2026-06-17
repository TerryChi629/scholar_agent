"""长期记忆 (SQLite)。

- 已读论文卡片库: 避免重复精读。
- 用户画像: 关注方向 / 常用检索词 / 偏好综述风格 -> 个性化。
TODO(Trae): 实现读写 + 按 topic 相似度召回历史。
"""
from __future__ import annotations

import sqlite3

from config import settings


def _conn() -> sqlite3.Connection:
    settings.ensure_dirs()
    conn = sqlite3.connect(settings.sqlite_path)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS memory_cards (
            paper_id TEXT PRIMARY KEY, card_json TEXT, updated_at REAL
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS user_profile (
            key TEXT PRIMARY KEY, value TEXT
        )"""
    )
    return conn


def remember_card(paper_id: str, card_json: str) -> None:
    import time
    conn = _conn()
    with conn:
        conn.execute("REPLACE INTO memory_cards VALUES (?,?,?)", (paper_id, card_json, time.time()))
    conn.close()


def recall_card(paper_id: str) -> str | None:
    conn = _conn()
    row = conn.execute("SELECT card_json FROM memory_cards WHERE paper_id=?", (paper_id,)).fetchone()
    conn.close()
    return row[0] if row else None

# TODO(Trae): user_profile 读写 + topic 相似度召回。
