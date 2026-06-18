"""长期记忆 (SQLite): 卡片级语义记忆。

跨任务复用已精读论文的 topic 无关字段, 避免同一篇论文换 topic 就从头精读 (省 token)。

关键设计 (见 CLAUDE.md 第 10 节):
- **分字段复用**: 只持久化 topic 无关字段 (论文固有属性); stance_tags/opposes 这类
  topic 相关字段不入库, 命中后按新 topic 重新抽取, 避免"A topic 立场套到 B topic"。
- **质量闸门**: 由 Orchestrator 在 Critic 通过后才写入 (见 orchestrator), 杜绝低质卡片
  被缓存固化。本模块只负责存取, 不做质量判断。
"""
from __future__ import annotations

import json
import sqlite3
import time

from config import settings

# topic 无关字段 (论文固有属性): 可安全跨任务复用。
# 刻意排除 stance_tags / opposes (随研究方向变化, 命中后按新 topic 重抽)。
TOPIC_INVARIANT_FIELDS = (
    "title", "authors", "year", "venue", "core_claim", "method",
    "method_family", "key_results", "limitations", "evidence_spans",
)


def _conn() -> sqlite3.Connection:
    settings.ensure_dirs()
    conn = sqlite3.connect(settings.sqlite_path)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS memory_cards (
            paper_id TEXT PRIMARY KEY, card_json TEXT, updated_at REAL
        )"""
    )
    return conn


def remember_card(paper_id: str, fields: dict) -> None:
    """持久化某篇论文的 topic 无关字段 (REPLACE 语义, 幂等覆盖)。

    fields 应只含 TOPIC_INVARIANT_FIELDS; 这里再做一次过滤兜底, 防止误存 stance。
    """
    invariant = {k: v for k, v in (fields or {}).items() if k in TOPIC_INVARIANT_FIELDS}
    conn = _conn()
    with conn:
        conn.execute(
            "REPLACE INTO memory_cards VALUES (?,?,?)",
            (paper_id, json.dumps(invariant, ensure_ascii=False, default=str), time.time()),
        )
    conn.close()


def recall_card(paper_id: str) -> dict | None:
    """召回某篇论文的 topic 无关字段; 未命中返回 None。"""
    conn = _conn()
    row = conn.execute(
        "SELECT card_json FROM memory_cards WHERE paper_id=?", (paper_id,)
    ).fetchone()
    conn.close()
    if not row:
        return None
    try:
        data = json.loads(row[0])
        return data if isinstance(data, dict) else None
    except (json.JSONDecodeError, TypeError):
        return None
