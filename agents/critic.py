"""Critic Agent: 质量闸门。引用核对 + 矛盾检查 + 完整性, 决定是否打回。

设计: 不只是"问 LLM 通不通过", 而是先做**确定性回查** (守住防幻觉红线):
1) 引用真实性: 卡片 evidence_spans 的 quote 必须能在向量库该论文内检索到。
2) 关系证据: 图谱 opposes 边的 evidence 是否非空。
3) 完整性: 候选论文是否都生成了卡片。
确定性检查发现的硬伤直接判不通过并给出结构化反馈, 供 Orchestrator 带反馈重调度。
"""
from __future__ import annotations

from agents.base import BaseAgent
from core.blackboard import Blackboard


class CriticAgent(BaseAgent):
    name = "critic"
    system_prompt = (
        "你是严格的审稿人。检查产出的质量:\n"
        "1) 引用真实性: 每条引用是否能在论文卡片的 evidence_spans 中找到来源 (防幻觉)。\n"
        "2) 逻辑一致性: 图谱里的对立/演进关系是否有证据支撑。\n"
        "3) 完整性: 是否遗漏重要候选论文。\n"
        "输出 JSON: {passed: bool, issues: [...], suggestions: [...]}"
    )
    tools = ["rag_query"]

    def review(self, bb: Blackboard, on_step=None) -> bool:
        """确定性回查 + 反馈。返回是否通过; 不通过时写 critic_feedback 供重调度。"""
        issues: list[str] = []
        suggestions: list[str] = []

        # —— 1) 完整性: 候选论文是否都有卡片 ——
        missing = [pid for pid in bb.candidates if pid not in bb.cards]
        if missing:
            issues.append(f"完整性: {len(missing)} 篇候选论文缺少精读卡片: {missing}")
            suggestions.append("对缺失论文重新运行 Reader 精读。")

        # —— 2) 引用真实性: evidence_spans 的 quote 能否在该论文内检索到 ——
        unverifiable = self._verify_evidence(bb)
        if unverifiable:
            issues.append(
                f"引用真实性: {len(unverifiable)} 条 evidence_spans 无法在原文中回溯 (疑似幻觉): "
                f"{unverifiable[:3]}{'...' if len(unverifiable) > 3 else ''}"
            )
            suggestions.append("对无法回溯引用的论文重新精读, 要求 quote 必须摘自原文。")

        # —— 3) 关系证据: 图谱 opposes 边是否有证据 ——
        if bb.graph:
            no_ev = [f"{e.source}->{e.target}" for e in bb.graph.edges
                     if e.relation == "opposes" and not e.evidence]
            if no_ev:
                issues.append(f"关系证据: {len(no_ev)} 条 opposes 边缺少 evidence 支撑。")
                suggestions.append("为对立关系补充来自 evidence_spans 的引用, 否则降级为'可能相关'。")
        else:
            issues.append("完整性: 尚未生成立场图谱。")
            suggestions.append("运行 Synthesizer 的 build_graph。")

        passed = len(issues) == 0
        verdict = {"passed": passed, "issues": issues, "suggestions": suggestions,
                   "method": "deterministic"}
        bb.critic_feedback.append(verdict)
        if on_step:
            on_step({"round": 0, "type": "final",
                     "text": f"Critic 判定: {'通过' if passed else '打回'} | issues={len(issues)}"})
        return passed

    @staticmethod
    def _verify_evidence(bb: Blackboard, max_per_card: int = 3) -> list[str]:
        """回查每张卡片的 evidence_spans: quote 能否在库内该论文检索命中。

        返回无法回溯的 quote 列表 (截断的描述)。检索命中判据: 取该论文 top1 片段,
        与 quote 有足够的词重叠 (宽松匹配, 避免标点/分块差异误杀)。
        """
        from rag.retrieve import hybrid_search
        from tools import _spans_to_quotes

        bad: list[str] = []
        for pid, card in bb.cards.items():
            for quote in _spans_to_quotes(card.evidence_spans)[:max_per_card]:
                if len(quote) < 8:  # 太短不具回溯意义, 跳过
                    continue
                hits = hybrid_search(quote, top_k=1, paper_id=pid)
                if not hits or not _overlap_ok(quote, hits[0].text):
                    bad.append(quote[:50])
        return bad


def _overlap_ok(quote: str, source: str, ratio: float = 0.5) -> bool:
    """宽松判断 quote 是否源自 source: 词集合重叠比是否达阈值。"""
    import re
    qw = set(re.findall(r"[a-zA-Z0-9\u4e00-\u9fff]+", quote.lower()))
    sw = set(re.findall(r"[a-zA-Z0-9\u4e00-\u9fff]+", source.lower()))
    if not qw:
        return False
    return len(qw & sw) / len(qw) >= ratio
