"""Synthesizer Agent: 卡片 -> 立场图谱 + 综述初稿。"""
from __future__ import annotations

from agents.base import BaseAgent, load_skill
from core.blackboard import Blackboard


class SynthesizerAgent(BaseAgent):
    name = "synthesizer"
    # 落盘 (export_md / export_graph_html) 不交给 LLM, 改由 apply_result 确定性执行,
    # 避免 LLM 误调产生占位/重复产物 (历史 bug: LLM 调 export_md 写入「待撰写」)。
    tools = ["cluster_cards", "build_graph", "detect_gaps"]

    @property
    def system_prompt(self) -> str:  # type: ignore[override]
        base = (
            "你是科研综述专家。基于已有论文卡片, 把它们组织成立场图谱: "
            "识别方法流派、观点分歧, 给每条关系写明 rationale 与 evidence, "
            "并指出研究空白。\n"
            "工作流程: 先调用 build_graph(topic) 构建图谱 (它会自动聚类、连边、找空白并写回黑板), "
            "再依据图谱在你的最终回复中直接输出完整的章节化综述正文 (无需调用任何落盘工具, "
            "系统会自动保存你输出的正文)。\n"
            "综述写作要求:\n"
            "- 全文用简体中文撰写, 段落连贯成文 (而非罗列字段), 章节包含: 摘要、"
            "方法范式概览、代表工作剖析、立场分歧、研究空白与展望。\n"
            "- 引用论文时使用其真实标题; 专有名词/缩写 (如 TIGER、HSTU、Semantic ID) 可保留英文。\n"
            "- 最终回复必须是完整成文的综述正文, 长度不少于 800 字, "
            "严禁输出「待撰写」「待补充」等占位内容。\n"
            "- 必须基于卡片中的真实信息, 禁止编造不存在的结论。"
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
        from tools import build_graph, export_md, export_graph_html, export_review_html

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
        # markdown 底稿 (供人工精修) + 美化 HTML (供飞书展示/阅读), 同源不重复生成正文
        export_md(f"review_{bb.topic}", best)
        export_review_html(f"review_{bb.topic}", best)

        # 3) 可视化兜底: 始终从黑板图谱导出可交互 HTML
        export_graph_html(bb.topic)

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
        """确定性综述骨架: 直接由卡片与图谱拼装, 保证产物始终存在且可回溯。

        采用连贯中文叙述 (而非字段罗列), 每节用过渡句串联, 引用论文用真实标题。
        """
        g = bb.graph
        topic = bb.topic
        n_paper = len(bb.cards)
        families = [n for n in (g.nodes if g else []) if n.type == "method_family"]
        opp_edges = [e for e in (g.edges if g else []) if e.relation == "opposes"]

        out: list[str] = [f"# 综述初稿：{topic}\n"]

        # 摘要段
        out.append("## 摘要\n")
        fam_names = "、".join(f.label for f in families) or "若干方法范式"
        out.append(
            f"本综述围绕「{topic}」, 系统梳理了库内 {n_paper} 篇代表性工作。"
            f"这些工作大致可归纳为 {len(families)} 类方法范式: {fam_names}。"
            f"下文先概览各范式的代表方法, 再逐篇剖析其核心主张与方法路线, "
            f"继而讨论它们之间的立场分歧, 最后指出当前研究的空白与潜在方向。\n"
        )

        # 1. 方法流派概览
        out.append("## 1. 方法范式概览\n")
        if families:
            for f in families:
                titles = "、".join(bb.cards[m].title or m for m in f.members if m in bb.cards)
                out.append(f"- **{f.label}**：代表工作为 {titles}。")
        else:
            out.append("- 库内论文暂未形成清晰的方法范式聚类。")
        out.append("")

        # 2. 各篇剖析
        out.append("## 2. 代表工作剖析\n")
        for pid, c in bb.cards.items():
            out.append(f"### {c.title or pid}（{c.year or '年份未知'}）")
            claim = c.core_claim or "（卡片未提取到核心主张）"
            method = c.method or "（卡片未提取到方法描述）"
            out.append(f"该工作的核心主张是：{claim}")
            out.append(f"在方法上，{method}")
            if c.key_results:
                out.append(f"其报告的关键结果包括：{'；'.join(c.key_results[:3])}。")
            out.append("")

        # 3. 立场分歧
        out.append("## 3. 立场分歧与方法路线之争\n")
        if opp_edges:
            out.append("从各工作的对比表述中, 可以识别出如下方法路线上的分歧：")
            for e in opp_edges:
                src = next((n.label for n in g.nodes if n.id == e.source), e.source)
                tgt = next((n.label for n in g.nodes if n.id == e.target), e.target)
                line = f"- **{src}** 与 **{tgt}** 在方法路线上存在分歧"
                if e.evidence:
                    line += f"（佐证片段：{e.evidence[0][:80]}…）"
                out.append(line + "。")
        else:
            out.append(
                "在现有卡片中, 各工作更多是并行探索而非正面交锋, 尚未识别出明确的"
                "方法路线对立——这本身也提示该方向仍处于「各自开疆拓土」的早期阶段。"
            )
        out.append("")

        # 4. 研究空白
        out.append("## 4. 研究空白与展望\n")
        gaps = (g.gaps if g else []) or ["卡片信息有限, 未能自动识别明确研究空白。"]
        for gap in gaps:
            out.append(f"- {gap}")
        out.append(
            "\n> 注：本初稿由确定性骨架基于论文卡片与立场图谱拼装而成, 所有论点均可"
            "回溯到库内真实论文, 可作为人工精修综述的可靠底稿。"
        )
        return "\n".join(out)
