"""Retriever Agent: 检索策略 + RAG 召回, 不足时补外部。"""
from __future__ import annotations

import re

from agents.base import BaseAgent
from core.blackboard import Blackboard

# 库内 paper_id 形如 12 位十六进制 (见 rag/ingest.py::_paper_id)。
_PAPER_ID_RE = re.compile(r"\b[0-9a-f]{12}\b")


class RetrieverAgent(BaseAgent):
    name = "retriever"
    system_prompt = (
        "你是文献检索专家。给定研究方向, 你的目标是召回最相关的论文集合。\n"
        "策略: 先用 expand_query 扩展检索词, 再用 rag_query 查本地库; "
        "若本地结果不足, 用 search_arxiv 补充。\n"
        "最终请明确列出候选论文的 paper_id (来自 rag_query 返回结果), 每行一个。"
    )
    tools = ["expand_query", "rag_query", "search_arxiv", "rag_ingest"]

    def build_user_prompt(self, bb: Blackboard) -> str:
        return f"研究方向: {bb.topic}\n请召回相关论文, 输出候选 paper_id 列表。"

    def apply_result(self, bb, result) -> None:
        """提取候选 paper_id, 校验真实存在后去重写入 bb.candidates。

        候选来源 = 模型输出/trace 中点名的 paper_id ∪ 对 topic 检索后按论文聚合的结果。
        - 防幻觉: 只接受向量库里真实存在的 paper_id (LLM 可能编造)。
        - 保召回: 检索返回的是 chunk, 单篇论文 chunk 多会挤占名额; 故按论文聚合,
          以每篇最佳 chunk 得分排序, 保证跨篇对比所需的候选覆盖。
        """
        from rag.store import get_store
        from rag.retrieve import hybrid_search

        max_candidates = 12  # 候选上限 (覆盖当前全库规模; 超此再按相关度截断)
        valid_ids = set(get_store().list_papers().keys())

        # 1) 从模型最终输出 + trace 观测里抓 12 位 hex 候选 (保序)
        text_pool = result.final_text + "\n" + "\n".join(
            step.get("result_preview", "") for step in result.trace
            if step.get("type") == "observe"
        )
        named = [pid for pid in _PAPER_ID_RE.findall(text_pool) if pid in valid_ids]

        # 2) 对 topic 检索一个候选池, 按论文聚合 (取每篇最佳得分) 后排序。
        #    只为聚合出候选 paper_id, 不需要全库规模: top_k 过大会让下游 MMR + 父文档
        #    扩展对接近全库做重活 (实测库大时整步空转数分钟)。按论文数适度放大并设硬上限。
        pool = hybrid_search(bb.topic, top_k=min(max(len(valid_ids) * 4, 60), 150))
        best_score: dict[str, float] = {}
        for h in pool:
            if h.paper_id and h.paper_id > "":
                best_score[h.paper_id] = max(best_score.get(h.paper_id, 0.0), h.score)
        retrieved = sorted(best_score, key=best_score.get, reverse=True)

        # 3) 合并去重保序, 模型点名的优先
        seen: set[str] = set()
        merged: list[str] = []
        for pid in named + retrieved:
            if pid in valid_ids and pid not in seen:
                seen.add(pid)
                merged.append(pid)
        bb.candidates = merged[:max_candidates]
