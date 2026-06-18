"""memory2 注入: 把召回的记忆条目组织成给合成端的注入文本块。

优先级: procedure > preference。低置信 (freq=1) 条目显式标注, 让合成端可酌情忽略。
注入块只承载"用户偏好/规则", 绝不承载论文事实 (事实只来自 RAG 证据)。
"""
from __future__ import annotations

from memory2.models import PREFERENCE, PROCEDURE, MemoryItem

# 注入优先级: 数字越小越靠前。
_ORDER = {PROCEDURE: 0, PREFERENCE: 1}
_LABEL = {PROCEDURE: "规则", PREFERENCE: "偏好"}


def build_injection_block(items: list[MemoryItem]) -> str:
    """把记忆条目排序后渲染成注入文本; 无条目返回空串。"""
    if not items:
        return ""
    ordered = sorted(items, key=lambda it: (_ORDER.get(it.category, 9)))
    lines: list[str] = []
    for it in ordered:
        tag = _LABEL.get(it.category, it.category)
        low_conf = "（低置信，仅供参考）" if it.freq <= 1 else ""
        lines.append(f"- [{tag}]{low_conf} {it.content}")
    return "已知用户偏好/规则（仅作风格与组织参考，不得用于编造论文事实）：\n" + "\n".join(lines)
