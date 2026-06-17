"""Synthesizer Agent: 卡片 -> 立场图谱 + 综述初稿。"""
from __future__ import annotations

from agents.base import BaseAgent, load_skill
from core.blackboard import Blackboard


class SynthesizerAgent(BaseAgent):
    name = "synthesizer"
    tools = ["cluster_cards", "build_graph", "detect_gaps", "export_md", "export_graph_html"]

    @property
    def system_prompt(self) -> str:  # type: ignore[override]
        base = (
            "你是科研综述专家。基于已有论文卡片, 把它们组织成立场图谱: "
            "识别方法流派、观点分歧, 给每条关系写明 rationale 与 evidence, "
            "并指出研究空白。\n"
            "工作流程: 先调用 build_graph(topic) 构建图谱 (它会自动聚类、连边、找空白并写回黑板), "
            "再依据图谱撰写章节化综述初稿, 最后调用 export_md(title, content) 落盘。\n"
            "综述必须基于卡片中的真实信息, 引用论文时使用其真实标题, 禁止编造不存在的结论。"
        )
        skill = load_skill("stance_graph")  # 注入 SOP
        return f"{base}\n\n--- 技能说明 ---\n{skill}" if skill else base

    def build_user_prompt(self, bb: Blackboard) -> str:
        lines = []
        for pid, c in bb.cards.items():
            lines.append(
                f"- [{pid}] {c.title or '?'} ({c.year or '?'}) | 流派: {c.method_family or '?'} | "
                f"核心主张: {c.core_claim or '?'} | 对立: {', '.join(c.opposes) or '无'}"
            )
        cards_brief = "\n".join(lines) or "(无卡片)"
        return (
            f"研究方向: {bb.topic}\n\n已有论文卡片:\n{cards_brief}\n\n"
            f"请先 build_graph 构建立场图谱, 再撰写综述初稿并 export_md 落盘。"
        )

    def apply_result(self, bb, result) -> None:
        """兜底: 确保图谱已建、综述已落盘且内容有效 (LLM 可能漏调工具或写占位内容)。"""
        from tools import build_graph, export_md

        # 1) 图谱兜底: LLM 没成功建图就确定性补建
        if bb.graph is None or not bb.graph.nodes:
            build_graph(bb.topic)

        # 2) 综述兜底: 评估 LLM 产出质量, 不达标则用确定性综述骨架覆盖。
        #    判据: LLM 最终文本 (或已落盘的 md) 长度过短 / 含占位语, 视为无效。
        llm_text = result.final_text.strip()
        existing = self._existing_md_text(bb)
        best = max([llm_text, existing], key=len)
        if not self._looks_valid(best):
            best = self._fallback_review(bb)
        export_md(f"review_{bb.topic}", best)  # 始终落盘一份有效综述 (覆盖占位产物)

    @staticmethod
    def _existing_md_text(bb: Blackboard) -> str:
        """读回已登记的 md 产物内容 (LLM 可能已调 export_md)。"""
        from pathlib import Path
        for a in bb.artifacts:
            if str(a).endswith(".md") and Path(a).exists():
                try:
                    return Path(a).read_text(encoding="utf-8").strip()
                except OSError:
                    continue
        return ""

    @staticmethod
    def _looks_valid(text: str) -> bool:
        """综述是否像样: 足够长且不含明显占位语。"""
        if len(text) < 300:
            return False
        placeholders = ("待撰写", "待补充", "todo", "占位", "placeholder", "待完成")
        return not any(p in text.lower() for p in placeholders)

    @staticmethod
    def _fallback_review(bb: Blackboard) -> str:
        """确定性综述骨架: 直接由卡片与图谱拼装, 保证产物始终存在且可回溯。"""
        g = bb.graph
        out = [f"# 综述初稿：{bb.topic}\n", "## 1. 方法流派概览\n"]
        if g:
            for n in g.nodes:
                if n.type == "method_family":
                    titles = ", ".join(bb.cards[m].title or m for m in n.members if m in bb.cards)
                    out.append(f"- **{n.label}**：{titles}")
        out.append("\n## 2. 论文要点\n")
        for pid, c in bb.cards.items():
            out.append(f"### {c.title or pid} ({c.year or '?'})")
            out.append(f"- 核心主张：{c.core_claim or '（未提取）'}")
            out.append(f"- 方法：{c.method or '（未提取）'}")
            if c.key_results:
                out.append(f"- 关键结果：{'; '.join(c.key_results[:3])}")
            out.append("")
        out.append("## 3. 观点分歧与立场对立\n")
        if g:
            for e in g.edges:
                if e.relation == "opposes":
                    src = next((n.label for n in g.nodes if n.id == e.source), e.source)
                    tgt = next((n.label for n in g.nodes if n.id == e.target), e.target)
                    out.append(f"- {src} ↔ {tgt}：{e.rationale}")
        out.append("\n## 4. 研究空白\n")
        for gap in (g.gaps if g else []):
            out.append(f"- {gap}")
        return "\n".join(out)
