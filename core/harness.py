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


# ============ 上下文压缩 + token 计数 ============

def estimate_tokens(text: str) -> int:
    """粗略估算 token 数 (不依赖 tiktoken, GLM/DeepSeek 无官方分词器)。

    经验近似: CJK 字符约 1 token/字, 其余 (英文/符号) 约 1 token/4 字符。
    宁可高估 (偏保守), 触发压缩比超限安全。
    """
    if not text:
        return 0
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    other = len(text) - cjk
    return cjk + (other + 3) // 4


def _message_tokens(msg: dict) -> int:
    """单条消息的 token 估算 (含 content + tool_calls 序列化 + 角色固定开销)。"""
    import json as _json

    n = 4  # 每条消息的角色/分隔固定开销
    n += estimate_tokens(str(msg.get("content") or ""))
    for tc in msg.get("tool_calls") or []:
        try:
            n += estimate_tokens(_json.dumps(tc, ensure_ascii=False, default=str))
        except (TypeError, ValueError):
            n += estimate_tokens(str(tc))
    return n


def count_messages_tokens(messages: list[dict]) -> int:
    """整个消息列表的 token 估算。"""
    return sum(_message_tokens(m) for m in messages)


def _is_safe_boundary(msg: dict) -> bool:
    """该消息可作为"保留块"的起点而不破坏 tool_call 配对。

    不能从 role=tool (其对应的 assistant.tool_calls 可能已被裁掉) 或
    携带 tool_calls 的 assistant 中途切入, 否则厂商接口会报配对错误。
    """
    role = msg.get("role")
    if role in ("user", "system"):
        return True
    if role == "assistant" and not msg.get("tool_calls"):
        return True
    return False


def _summarize_dropped(messages: list[dict]) -> str:
    """把被裁掉的旧消息确定性地摘要化 (不调 LLM: 省成本、可复现、防幻觉)。

    保留角色与关键内容片段, 工具调用只留名字与结果概要。
    """
    lines: list[str] = []
    for m in messages:
        role = m.get("role")
        content = str(m.get("content") or "").strip().replace("\n", " ")
        if role == "assistant" and m.get("tool_calls"):
            names = ", ".join(tc.get("function", {}).get("name", "?")
                              for tc in m["tool_calls"])
            lines.append(f"- assistant 调用工具: {names}")
            if content:
                lines.append(f"  想法: {content[:120]}")
        elif role == "tool":
            lines.append(f"- 工具返回: {content[:160]}")
        elif role == "user":
            lines.append(f"- 用户/输入: {content[:160]}")
        elif role == "assistant":
            lines.append(f"- assistant: {content[:200]}")
    return "\n".join(lines)


def compress_context(messages: list[dict], budget: int | None = None) -> list[dict]:
    """超出 token 预算时压缩历史, 保证 tool_call 配对完整。

    策略: 1) 估算总 token; 2) 未超预算原样返回; 3) 超出则保留 system, 从尾部
    保留最近若干轮 (在安全边界切入), 其余旧消息确定性摘要为一条历史摘要消息。
    """
    budget = budget or settings.context_token_budget
    if count_messages_tokens(messages) <= budget:
        return messages
    if not messages:
        return messages

    system = messages[0] if messages[0].get("role") == "system" else None
    body = messages[1:] if system else messages

    # 给 system + 摘要 + 后续生成预留余量, 用预算的一半装最近消息
    keep_budget = max(1, budget // 2)
    acc = 0
    start = len(body)
    for i in range(len(body) - 1, -1, -1):
        acc += _message_tokens(body[i])
        if acc > keep_budget:
            break
        start = i
    # 向后推进到安全边界, 避免从悬空的 tool / tool_calls 中途切入
    while start < len(body) and not _is_safe_boundary(body[start]):
        start += 1

    dropped, kept = body[:start], body[start:]
    out: list[dict] = []
    if system:
        out.append(system)
    if dropped:
        out.append({
            "role": "user",
            "content": "[历史摘要 — 早期轮次已压缩]\n" + _summarize_dropped(dropped),
        })
    out.extend(kept)
    return out


# ============ 危险操作确认 ============

def confirm(action: str, auto_yes: bool = False) -> bool:
    """不可逆操作前置确认 (写库 / 写文件 / 外部网络)。

    CLI 下交互询问; API 下应返回待确认态 (此处简化)。
    """
    if auto_yes:
        return True
    ans = input(f"[确认] 即将执行: {action} —— 继续? [y/N] ").strip().lower()
    return ans == "y"
