"""AnswerSynthesizer: 确定性组织证据 + 一次 LLM 合成。

主链路核心: 证据排版完全确定性 (编号、来源、页码由代码拼), LLM 只负责"基于给定
证据写答案", 不让它编引用。一次 chat_text 调用, 不进 ReAct loop。
"""
from __future__ import annotations

from config import settings
from core.llm import get_llm
from chat.retriever import PaperEvidence

_SYS = (
    "你是本地论文库的研究助手。基于【证据】产出一份结构化、总结性的回答, 而非罗列或复述原文片段。\n"
    "输出用 Markdown, 遵循以下结构:\n"
    "1. 先给一段凝练总述 (2-4 句), 直接回答问题核心。\n"
    "2. 再分点展开关键技术 / 方法 / 对比, 每个要点末尾用 [n] 标注其证据来源编号。\n"
    "3. 若问题涉及可形式化的内容 (如建模目标、损失函数、概率分解、注意力机制等), 用 LaTeX 写出关键公式: "
    "行内用 $...$, 独立公式用 $$...$$。仅在证据中有明确依据时给出, 公式后标 [n]。\n"
    "4. 若有助于理解 (方法流程 / 模块关系 / 方法演进脉络), 用一个 Mermaid 图概括, 用 ```mermaid 代码块包裹 "
    "(如 graph LR 或 flowchart TD)。图中节点只能反映证据里出现的结构, 不要杜撰。\n"
    "   Mermaid 语法严格要求 (否则渲染失败): 节点标签一律用双引号包裹, 如 A[\"标签文本\"]; "
    "标签内禁止出现未转义的括号 ()、逗号、分号、引号、$、<br> 等特殊字符 (改用中文顿号或空格分隔); "
    "节点 id 只用英文字母数字。\n"
    "强约束 (防幻觉):\n"
    "- 严禁编造证据中没有的论文 / 数字 / 结论 / 公式 / 结构; 公式与图都必须能在证据中找到依据。\n"
    "- 证据无法支撑公式或图时, 就不要给, 宁可只用文字; 证据完全未覆盖问题时明确说'库内未找到'。\n"
    "- 若给出【用户偏好/规则】, 只用于调整回答风格与组织方式, 不得用它替代或编造论文事实。"
)


def _format_evidence(evidences: list[PaperEvidence]) -> tuple[str, list[dict]]:
    """把聚合证据渲染成带编号的文本块, 同时返回编号映射 (供前端回溯)。"""
    lines: list[str] = []
    ref_map: list[dict] = []
    n = 0
    for ev in evidences:
        for sp in ev.spans:
            n += 1
            sec = f"·{sp['section']}" if sp.get("section") else ""
            lines.append(
                f"[{n}] 《{ev.title}》({ev.year}){sec} p{sp.get('page', '?')}: {sp['text']}"
            )
            ref_map.append({
                "ref": n, "paper_id": ev.paper_id, "title": ev.title,
                "year": ev.year, "section": sp.get("section", ""),
                "page": sp.get("page", 0), "score": sp.get("score", 0.0),
                # 引用回溯用精确命中片段 (非父文档扩展后的合并文本), 更可定位。
                "quote": sp.get("hit_text") or sp["text"],
            })
    return "\n\n".join(lines), ref_map


def synthesize(question: str, evidences: list[PaperEvidence],
               memory_block: str = "", session_block: str = "") -> tuple[str, list[dict], dict]:
    """合成答案。返回 (answer_text, evidence_ref_map, usage)。

    session_block: 当前会话上下文 (多轮历史/摘要), 仅供指代消解, 不作事实来源。
    """
    ev_text, ref_map = _format_evidence(evidences)
    if not ev_text:
        return ("库内未找到与该问题相关的内容。", [], {})

    parts = [f"问题: {question}", ""]
    if session_block:
        parts += [session_block, ""]
    if memory_block:
        parts += [memory_block, ""]
    parts += ["【证据】", ev_text, "",
              "请基于以上证据产出结构化总结性回答: 先总述, 再分点(每点末尾标 [编号]); "
              "可形式化处用 LaTeX 公式, 有助理解处给一个 Mermaid 图, 公式/图均须有证据依据。"]
    user_prompt = "\n".join(parts)

    provider, model, _, _ = settings.resolve_agent_model("chat")
    llm = get_llm()
    resp = llm.chat(
        [{"role": "system", "content": _SYS}, {"role": "user", "content": user_prompt}],
        temperature=0.2, provider=provider, model=model,
    )
    answer = resp.choices[0].message.content or ""
    u = getattr(resp, "usage", None)
    usage = {
        "prompt_tokens": getattr(u, "prompt_tokens", 0) or 0,
        "completion_tokens": getattr(u, "completion_tokens", 0) or 0,
        "total_tokens": getattr(u, "total_tokens", 0) or 0,
    } if u else {}
    return answer, ref_map, usage
