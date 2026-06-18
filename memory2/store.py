"""memory2 存储层: SQLite 双表 (memory_items + memory_replacements)。

- memory_items: 记忆条目, chash 唯一索引做精确去重。
- memory_replacements: supersede 审计链 (新条目取代旧条目), 旧条目软删保留。

复用 settings.sqlite_path (与 card memory / embed 缓存同库, 不同表)。
"""
from __future__ import annotations

import sqlite3
import time

from config import settings
from memory2.models import CATEGORIES, MemoryItem


def _conn() -> sqlite3.Connection:
    settings.ensure_dirs()
    conn = sqlite3.connect(settings.sqlite_path)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS memory_items (
            id TEXT PRIMARY KEY,
            category TEXT,
            content TEXT,
            chash TEXT,
            freq INTEGER,
            created_at REAL,
            last_used_at REAL,
            superseded_by TEXT
        )"""
    )
    # chash 唯一索引: 精确去重的硬保证 (同 category+内容只存一条)。
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_memory_chash ON memory_items(chash)"
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS memory_replacements (
            old_id TEXT,
            new_id TEXT,
            reason TEXT,
            created_at REAL
        )"""
    )
    return conn


def insert(item: MemoryItem) -> None:
    conn = _conn()
    with conn:
        conn.execute(
            "INSERT INTO memory_items VALUES (?,?,?,?,?,?,?,?)", item.to_row()
        )
    conn.close()


def get_by_hash(chash: str) -> MemoryItem | None:
    conn = _conn()
    row = conn.execute(
        "SELECT * FROM memory_items WHERE chash=?", (chash,)
    ).fetchone()
    conn.close()
    return MemoryItem.from_row(row) if row else None


def reinforce(item_id: str) -> None:
    """精确命中已有条目: freq+1, 刷新 last_used_at。"""
    conn = _conn()
    with conn:
        conn.execute(
            "UPDATE memory_items SET freq=freq+1, last_used_at=? WHERE id=?",
            (time.time(), item_id),
        )
    conn.close()


def touch(item_id: str) -> None:
    """召回命中后刷新 last_used_at (不增 freq)。"""
    conn = _conn()
    with conn:
        conn.execute(
            "UPDATE memory_items SET last_used_at=? WHERE id=?", (time.time(), item_id)
        )
    conn.close()


def supersede(old_id: str, new_item: MemoryItem, reason: str = "semantic") -> None:
    """新条目取代旧条目: 插入新条目 + 标记旧条目 superseded_by + 记审计。"""
    conn = _conn()
    with conn:
        conn.execute("INSERT INTO memory_items VALUES (?,?,?,?,?,?,?,?)", new_item.to_row())
        conn.execute(
            "UPDATE memory_items SET superseded_by=? WHERE id=?", (new_item.id, old_id)
        )
        conn.execute(
            "INSERT INTO memory_replacements VALUES (?,?,?,?)",
            (old_id, new_item.id, reason, time.time()),
        )
    conn.close()


def active_items(category: str | None = None) -> list[MemoryItem]:
    """取所有未被取代 (superseded_by IS NULL) 的活跃条目, 供召回全扫。"""
    conn = _conn()
    sql = "SELECT * FROM memory_items WHERE superseded_by IS NULL"
    params: tuple = ()
    if category in CATEGORIES:
        sql += " AND category=?"
        params = (category,)
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return [MemoryItem.from_row(r) for r in rows]
