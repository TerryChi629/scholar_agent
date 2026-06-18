"""chat 检索: 复用 rag.hybrid_search, 按 paper 聚合证据。

确定性组织: 把 chunk 级命中按 paper_id 聚合 (每篇取最佳若干片段), 既保证候选覆盖
多篇论文 (不被单篇 chunk 挤占), 又给合成端结构化的"按论文分组"证据。
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class PaperEvidence:
    paper_id: str
    title: str
    year: int
    best_score: float
    spans: list[dict] = field(default_factory=list)  # [{text, section, page, score}]


def retrieve(query: str, top_k: int = 8, per_paper: int = 2,
             year_min: int | None = None) -> list[PaperEvidence]:
    """检索并按 paper 聚合。返回按最佳片段得分降序的 PaperEvidence 列表。"""
    from rag.retrieve import hybrid_search

    hits = hybrid_search(query, top_k=top_k, year_min=year_min)
    groups: dict[str, PaperEvidence] = {}
    for h in hits:
        meta = h.metadata or {}
        pid = h.paper_id or meta.get("paper_id", "")
        ev = groups.get(pid)
        if ev is None:
            ev = PaperEvidence(
                paper_id=pid,
                title=meta.get("title", "") or "?",
                year=int(meta.get("year", 0) or 0),
                best_score=h.score,
            )
            groups[pid] = ev
        if len(ev.spans) < per_paper:
            ev.spans.append({
                "text": h.text,
                # 精确命中文本: 父文档扩展后 h.text 是合并上下文, 回溯/引用用原命中片段更准。
                "hit_text": meta.get("hit_text", h.text),
                "section": meta.get("section", ""),
                "page": meta.get("page", 0),
                "score": round(h.score, 4),
            })
        ev.best_score = max(ev.best_score, h.score)
    return sorted(groups.values(), key=lambda e: e.best_score, reverse=True)
