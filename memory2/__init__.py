"""memory2: ChatAgent 轻量长期记忆子系统 (M8)。

与 memory/ (card memory, 服务 ReaderAgent) 并存、互不干扰。只承载用户的
preference / procedure 两类记忆 (薄版); event / profile 列为后续阶段。

红线 (见 CLAUDE.md M8):
- 记忆不替代 RAG 证据: 论文事实只来自 hybrid_search, memory 仅承载偏好/规则。
- 不引入 LLM 黑盒判断: 去重/强化/取代均走确定性规则 + 向量相似度阈值。
"""
from __future__ import annotations

from memory2.models import MemoryItem, PROCEDURE, PREFERENCE, CATEGORIES
from memory2.memorizer import remember
from memory2.retriever import recall
from memory2.injection import build_injection_block

__all__ = [
    "MemoryItem", "PROCEDURE", "PREFERENCE", "CATEGORIES",
    "remember", "recall", "build_injection_block",
]
