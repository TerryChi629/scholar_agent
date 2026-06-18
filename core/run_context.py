"""当前任务黑板的进程内上下文持有器。

工具 (tools/__init__.py) 是无状态函数, 只接收 LLM 给的参数, 拿不到黑板。
但 cluster_cards / build_graph / detect_gaps 等工具必须读取黑板上的卡片来
做确定性计算 (而非把整堆卡片塞进 LLM 上下文 —— 那会撑爆上下文且诱发幻觉)。

约定: Orchestrator/Agent 在运行某任务前用 set_active_blackboard 注册当前黑板,
工具内用 get_active_blackboard 读取。map 流程中 Synthesizer 在主线程顺序运行,
故用简单的模块级持有器即可 (Reader 并行不依赖此机制, 只用 rag_query)。
"""
from __future__ import annotations

import threading

from core.blackboard import Blackboard

_active: Blackboard | None = None

# Reader 并行精读时, 各线程"当前锁定的 paper_id"。用 thread-local 隔离, 供 rag_query
# 在模型漏传 paper_id 时自动注入, 杜绝跨篇串味 (方案 D: 自动注入而非报错打断)。
_reader_ctx = threading.local()


def set_active_blackboard(bb: Blackboard | None) -> None:
    global _active
    _active = bb


def get_active_blackboard() -> Blackboard | None:
    return _active


def set_reader_paper_id(paper_id: str | None) -> None:
    """登记当前线程正在精读的 paper_id (None 表示清除)。"""
    _reader_ctx.paper_id = paper_id


def get_reader_paper_id() -> str | None:
    """取当前线程正在精读的 paper_id; 非 Reader 上下文返回 None。"""
    return getattr(_reader_ctx, "paper_id", None)
