"""IntentRouter: 规则版意图路由 (MVP, 无 LLM)。

用关键词/句式规则分类, 默认落 knowledge_qa。不引入 LLM 不确定性。

意图:
- preference_write: 写偏好 (如"以后回答都用中文")
- procedure_write:  写规则 (如"记住: 比较论文先列方法再列指标")
- memory_qa:        查我记过什么
- knowledge_qa:     库内知识问答 (默认)
- hybrid:           既问知识又含偏好 (检索 + 合成 + 顺带写记忆)
"""
from __future__ import annotations

import re

KNOWLEDGE_QA = "knowledge_qa"
MEMORY_QA = "memory_qa"
PREFERENCE_WRITE = "preference_write"
PROCEDURE_WRITE = "procedure_write"
HYBRID = "hybrid"

# 显式"记住/以后/请记录"等写入信号。
_REMEMBER_RE = re.compile(r"记住|记一下|记录一下|以后|从现在起|今后|默认就|请记得|帮我记")
# 规则类信号 (做事步骤/流程)。
_PROCEDURE_RE = re.compile(r"步骤|流程|先.*再|顺序|每次.*都|回答时|分析时|比较.*时")
# 偏好类信号 (风格/口味)。
_PREFERENCE_RE = re.compile(r"偏好|喜欢|倾向|用中文|用英文|简洁|详细|只看|只要|不要太|风格")
# 查记忆。
_MEMORY_QA_RE = re.compile(r"我.*(说过|提过|偏好|要求|设置).*(什么|哪些|吗)|我的偏好|我之前")
# 知识问答信号 (问库内论文)。
_KNOWLEDGE_RE = re.compile(r"论文|文献|方法|模型|区别|对比|哪些|怎么|如何|是什么|为什么|实验|效果")
# 疑问句标记: 用于区分"既写又问"(hybrid) 与"只是规则里恰好含知识词"。
_QUESTION_RE = re.compile(r"[?？]|吗|呢|哪些|哪个|什么|如何|怎么|为什么|是不是")


def route(question: str) -> str:
    """返回意图字符串。"""
    q = (question or "").strip()
    if not q:
        return KNOWLEDGE_QA

    has_remember = bool(_REMEMBER_RE.search(q))
    has_knowledge = bool(_KNOWLEDGE_RE.search(q))
    has_question = bool(_QUESTION_RE.search(q))
    has_write = has_remember or bool(_PROCEDURE_RE.search(q)) or bool(_PREFERENCE_RE.search(q))

    # 查记忆 (排在写之前: "我之前说的偏好是什么" 不应被当成写)
    if _MEMORY_QA_RE.search(q):
        return MEMORY_QA

    if has_write:
        # 既含写信号又"确实在问问题" (有疑问标记) -> hybrid; 否则纯写记忆。
        # 仅靠知识词不够 (规则内容如"先列方法再列指标"会误触发)。
        if has_remember and has_knowledge and has_question:
            return HYBRID
        return PROCEDURE_WRITE if _PROCEDURE_RE.search(q) else PREFERENCE_WRITE

    return KNOWLEDGE_QA
