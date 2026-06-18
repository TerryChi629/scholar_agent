"""论文标题 -> 图谱短名的持久化缓存 (SQLite)。

图谱渲染时用 LLM 为长论文标题起简短可辨识的节点名, 同一标题只需起一次。
缓存按标题 hash 命中, 跨任务复用, 重渲染零额外 token。
"""
from __future__ import annotations

import hashlib
import sqlite3
import threading

from config import settings

_lock = threading.Lock()
_initialized = False


def _conn() -> sqlite3.Connection:
    global _initialized
    settings.ensure_dirs()
    conn = sqlite3.connect(settings.sqlite_path)
    if not _initialized:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS label_cache (key TEXT PRIMARY KEY, label TEXT)"
        )
        _initialized = True
    return conn


def _key(title: str) -> str:
    return hashlib.sha256((title or "").strip().encode("utf-8")).hexdigest()


def get_cached(title: str) -> str | None:
    if not settings.cache_enabled or not title:
        return None
    with _lock:
        conn = _conn()
        row = conn.execute(
            "SELECT label FROM label_cache WHERE key=?", (_key(title),)
        ).fetchone()
        conn.close()
    return row[0] if row else None


def put_cached(pairs: list[tuple[str, str]]) -> None:
    if not settings.cache_enabled or not pairs:
        return
    with _lock:
        conn = _conn()
        with conn:
            conn.executemany(
                "REPLACE INTO label_cache (key, label) VALUES (?,?)",
                [(_key(t), label) for t, label in pairs],
            )
        conn.close()
