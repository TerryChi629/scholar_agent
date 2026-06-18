"""memory_writer: 从用户输入抽取 preference/procedure 并写入 memory2。

抽取走 low 档 LLM (把口语化指令规整成一句陈述), 失败则降级为直接用原文。
category 由 IntentRouter 已判定 (preference_write / procedure_write), 这里只负责
规整内容 + 调 memory2.remember。
"""
from __future__ import annotations

from config import settings
from core.llm import get_llm
from core.obs import log_event
from memory2 import remember
from memory2.models import PREFERENCE, PROCEDURE

_EXTRACT_SYS = (
    "把用户这句话里要长期记住的偏好或规则, 提炼成一句简洁、自包含的陈述句 "
    "(去掉'记住''以后'等口语前缀, 保留可执行的核心)。只输出这句话本身, 不要解释。"
)


def _extract_content(raw: str) -> str:
    """用 low 档 LLM 规整成一句陈述; 失败回退原文 (去常见前缀)。"""
    try:
        provider, model, _, _ = settings.resolve_agent_model("chat")
        text = get_llm().chat_text(
            [{"role": "system", "content": _EXTRACT_SYS},
             {"role": "user", "content": raw}],
            temperature=0.0, provider=provider, model=model,
        )
        cleaned = " ".join((text or "").split())
        return cleaned or raw.strip()
    except Exception as exc:  # noqa: BLE001  抽取失败不应阻断, 回退原文
        log_event("memory2.extract_failed", level="WARNING", error=str(exc))
        return raw.strip()


def write_from_intent(intent: str, question: str) -> dict:
    """按意图写入一条记忆, 返回 memory2.remember 的结果 (含 action)。"""
    category = PROCEDURE if intent == "procedure_write" else PREFERENCE
    content = _extract_content(question)
    return remember(category, content)
