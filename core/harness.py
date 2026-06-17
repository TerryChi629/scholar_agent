"""Harness 工程外壳: 上下文管理 / 大输出落盘 / 危险操作确认 / 会话恢复。

这是后端工程能力的集中展示区。当前为骨架, 关键方法已留 TODO。
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from config import settings
from core.blackboard import Blackboard


# ============ 会话持久化 (断点恢复) ============

def _conn() -> sqlite3.Connection:
    settings.ensure_dirs()
    conn = sqlite3.connect(settings.sqlite_path)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS tasks (
            task_id TEXT PRIMARY KEY,
            topic   TEXT,
            status  TEXT,
            blackboard TEXT,
            updated_at REAL
        )"""
    )
    return conn


def save_checkpoint(bb: Blackboard) -> None:
    """每个关键步骤后调用, 把黑板存盘。"""
    conn = _conn()
    with conn:
        conn.execute(
            "REPLACE INTO tasks (task_id, topic, status, blackboard, updated_at) VALUES (?,?,?,?,?)",
            (bb.task_id, bb.topic, bb.status, bb.to_json(), time.time()),
        )
    conn.close()


def load_checkpoint(task_id: str) -> Blackboard | None:
    conn = _conn()
    row = conn.execute("SELECT blackboard FROM tasks WHERE task_id=?", (task_id,)).fetchone()
    conn.close()
    return Blackboard.from_json(row[0]) if row else None


def list_tasks() -> list[tuple[str, str, str]]:
    conn = _conn()
    rows = conn.execute("SELECT task_id, topic, status FROM tasks ORDER BY updated_at DESC").fetchall()
    conn.close()
    return rows


# ============ 大输出落盘 ============

def persist_if_large(content: str, tag: str, threshold: int = 4000) -> str:
    """超长内容落盘, 返回提示 (路径 + 摘要); 否则原样返回。

    TODO(Trae): 接入 agent_loop._stringify, 让工具大输出自动走这里。
    """
    if len(content) <= threshold:
        return content
    settings.ensure_dirs()
    path = settings.storage_dir / f"{tag}_{int(time.time())}.txt"
    Path(path).write_text(content, encoding="utf-8")
    return f"[大输出已落盘: {path}]\n摘要(前500字):\n{content[:500]}"


# ============ 上下文压缩 ============

def compress_context(messages: list[dict]) -> list[dict]:
    """超出 token 预算时压缩历史。

    TODO(Trae): 1) 估算 token; 2) 旧轮次摘要化; 3) 保留 system + 最近 N 轮。
    """
    # 占位: 暂不压缩
    return messages


# ============ 危险操作确认 ============

def confirm(action: str, auto_yes: bool = False) -> bool:
    """不可逆操作前置确认 (写库 / 写文件 / 外部网络)。

    CLI 下交互询问; API 下应返回待确认态 (此处简化)。
    """
    if auto_yes:
        return True
    ans = input(f"[确认] 即将执行: {action} —— 继续? [y/N] ").strip().lower()
    return ans == "y"
