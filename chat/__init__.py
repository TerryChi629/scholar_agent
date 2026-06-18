"""chat: ChatAgent 对话式 RAG (M8)。

以本地知识库为事实来源的即问即答, 配 memory2 轻量记忆。
不继承 BaseAgent, 只复用 core 底座函数 (见 CLAUDE.md M8.0 第 5 条)。
"""
from __future__ import annotations

from chat.agent import ChatAgent, ChatResult

__all__ = ["ChatAgent", "ChatResult"]
