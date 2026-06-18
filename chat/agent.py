"""ChatAgent: 对话式 RAG 主入口 (M8)。

不继承 BaseAgent (见 CLAUDE.md M8.0 第 5 条): 输入裸 question、确定性一次合成、
直接返回 ChatResult。只复用 core 底座函数 (run_loop 仅慢路径、llm、obs)。

快慢双路径:
- 快路径 (_answer_fast, v1 实现): 检索 -> 确定性组织证据 -> 一次 LLM 合成。
- 慢路径 (_answer_loop, 预留): 多跳/需追问库再查时复用 run_loop, chat 专属精简工具集。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from core.obs import log_event
from chat import intent as intent_mod
from chat import memory_writer
from chat import session as session_mod
from chat.retriever import retrieve
from chat.synthesizer import synthesize


@dataclass
class ChatResult:
    answer: str
    evidence: list = field(default_factory=list)   # 证据编号映射 (供回溯)
    used_memory: list = field(default_factory=list)  # 命中并注入的记忆条目
    usage: dict = field(default_factory=dict)
    intent: str = ""

    def to_dict(self) -> dict:
        return {
            "answer": self.answer, "evidence": self.evidence,
            "used_memory": self.used_memory, "usage": self.usage,
            "intent": self.intent,
        }


class ChatAgent:
    """本地知识库对话问答。RAG 主导事实, memory2 仅辅助偏好/规则。"""

    def answer(self, question: str, task_id: str | None = None,
               session_id: str | None = None) -> ChatResult:
        question = (question or "").strip()
        if not question:
            return ChatResult(answer="请输入问题。")
        intent = intent_mod.route(question)
        log_event("chat.route", intent=intent, task_id=task_id, session_id=session_id)

        result = self._dispatch(question, intent, task_id, session_id)

        # 短期会话记忆: 记录本轮问答, 超窗旧轮压成滚动摘要 (指代消解/追问用)。
        if session_id:
            session_mod.append_turn(session_id, "user", question)
            session_mod.append_turn(session_id, "assistant", result.answer,
                                    tokens=(result.usage or {}).get("total_tokens", 0))
            session_mod.maybe_summarize(session_id)
        return result

    def _dispatch(self, question: str, intent: str, task_id: str | None,
                  session_id: str | None) -> ChatResult:
        # 纯写记忆: 抽取写入后回执, 不检索。
        if intent in (intent_mod.PREFERENCE_WRITE, intent_mod.PROCEDURE_WRITE):
            res = memory_writer.write_from_intent(intent, question)
            label = "规则" if intent == intent_mod.PROCEDURE_WRITE else "偏好"
            verb = {"added": "已记住", "reinforced": "已确认(强化)",
                    "superseded": "已更新", "ignored": "未记录"}.get(res["action"], "已处理")
            return ChatResult(answer=f"{verb}这条{label}。", intent=intent)

        # 查记忆: 只列已记内容, 不检索论文。
        if intent == intent_mod.MEMORY_QA:
            return self._answer_memory_qa(question, intent)

        # knowledge_qa / hybrid: 走快路径
        result = self._answer_fast(question, intent, session_id)
        # hybrid: 顺带写一条记忆
        if intent == intent_mod.HYBRID:
            memory_writer.write_from_intent("preference_write", question)
        return result

    # —— 快路径 (v1) ——
    def _answer_fast(self, question: str, intent: str,
                     session_id: str | None = None) -> ChatResult:
        from memory2 import recall, build_injection_block

        session_block = session_mod.build_context_block(session_id) if session_id else ""
        # 追问指代消解: 有会话历史时, 把口语化追问改写成自包含检索式 (否则"它的损失函数"召不回)。
        search_query = self._contextualize_query(question, session_block) if session_block else question

        evidences = retrieve(search_query, top_k=8)
        mem_items = recall(question, top_k=5)
        memory_block = build_injection_block(mem_items)

        answer, ref_map, usage = synthesize(question, evidences, memory_block, session_block)
        return ChatResult(
            answer=answer, evidence=ref_map,
            used_memory=[{"category": m.category, "content": m.content} for m in mem_items],
            usage=usage, intent=intent,
        )

    @staticmethod
    def _contextualize_query(question: str, session_block: str) -> str:
        """把含指代的追问改写成自包含检索式 (low 档 LLM); 失败回退原问。"""
        from config import settings
        from core.llm import get_llm

        try:
            provider, model, _, _ = settings.resolve_agent_model("chat")
            sys = ("根据会话上下文, 把用户最新的问题改写成一句自包含、可独立检索的问题 "
                   "(补全指代如'它/这个/上面那个'指向的具体对象)。只输出改写后的问题本身。")
            text = get_llm().chat_text(
                [{"role": "system", "content": sys},
                 {"role": "user", "content": f"{session_block}\n\n最新问题: {question}"}],
                temperature=0.0, provider=provider, model=model,
            )
            rewritten = " ".join((text or "").split())
            return rewritten or question
        except Exception as exc:  # noqa: BLE001  改写失败不阻断
            log_event("chat.contextualize_failed", level="WARNING", error=str(exc))
            return question

    def _answer_memory_qa(self, question: str, intent: str) -> ChatResult:
        from memory2 import recall

        items = recall(question, top_k=10)
        if not items:
            return ChatResult(answer="还没有记录任何偏好或规则。", intent=intent)
        lines = [f"- [{m.category}] {m.content}" for m in items]
        return ChatResult(
            answer="已记录的偏好/规则：\n" + "\n".join(lines),
            used_memory=[{"category": m.category, "content": m.content} for m in items],
            intent=intent,
        )

    # —— 慢路径 (后续阶段预留) ——
    def _answer_loop(self, question: str, task_id: str | None = None) -> ChatResult:
        """复杂多跳问答: 复用 core.run_loop, chat 专属精简工具集 (仅 rag_query)。

        v1 暂未实现, 回退快路径; 后续阶段填充, 升级时无需回头继承 BaseAgent。
        """
        log_event("chat.loop_fallback", level="WARNING")
        return self._answer_fast(question, intent_mod.KNOWLEDGE_QA)
