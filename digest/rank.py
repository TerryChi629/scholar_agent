"""M12.3 排序去重: embedding 粗排 + LLM 精排 + 去重。

流程 (针对单个兴趣主题的候选池):
1. 去重: 跳过已推送过的 (digest_pushed) 与本地库已有的 (按标题近似) 论文。
2. 粗排: 用 embedding 算候选摘要与主题语义的 cosine 相似度, 取 top-N。
3. 精排: 用一次 low 档 LLM listwise 重排 top-N, 结合主题意图选出最相关的若干篇,
   并给出一句"为什么推给你"的推荐理由。LLM 失败时回退粗排顺序。
"""
from __future__ import annotations

import json
import math
import re

from config import settings
from core.obs import log_event
from digest import store
from digest.arxiv_client import ArxivPaper
from digest.profile import InterestTopic


def _norm_title(t: str) -> str:
    """标题归一化 (小写 + 去非字母数字) 用于近似去重。"""
    return re.sub(r"[^a-z0-9]+", "", (t or "").lower())


def _local_titles() -> set[str]:
    """本地库已有论文的归一化标题集 (避免推送已读过的)。"""
    try:
        from rag.store import get_store
        papers = get_store().list_papers()
    except Exception as exc:  # noqa: BLE001  向量库不可用不阻断
        log_event("digest.rank.local_failed", level="WARNING", error=str(exc))
        return set()
    return {_norm_title(p.get("title", "")) for p in papers.values() if p.get("title")}


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


def _coarse_rank(topic: InterestTopic, papers: list[ArxivPaper], top_n: int) -> list[ArxivPaper]:
    """embedding 粗排: 按摘要 vs 主题语义相似度降序取 top_n。失败回退原序。"""
    if not papers:
        return []
    try:
        from core.llm import get_embedder
        embedder = get_embedder()
        anchor = f"{topic.name} {topic.query}".strip()
        texts = [anchor] + [f"{p.title}. {p.summary[:500]}" for p in papers]
        vecs = embedder.embed(texts)
        qv, pvs = vecs[0], vecs[1:]
        scored = sorted(
            zip(papers, pvs), key=lambda pair: _cosine(qv, pair[1]), reverse=True
        )
        return [p for p, _ in scored[:top_n]]
    except Exception as exc:  # noqa: BLE001  embedding 失败回退原序
        log_event("digest.rank.coarse_failed", level="WARNING", error=str(exc))
        return papers[:top_n]


_RERANK_SYS = (
    "你是科研论文推荐助手。用户的兴趣主题是「{name}」(检索词: {query})。"
    "下面是若干候选论文 (含序号、标题、摘要)。请挑出与该主题最相关、最值得用户今天读的论文, "
    "最多 {k} 篇, 按推荐优先级排序。\n"
    "对每篇给出 idx (候选序号) 和 reason (一句话推荐理由, 说明它和用户兴趣的关联, 体现'懂他')。\n"
    "只输出 JSON 数组, 元素为 {{\"idx\":整数, \"reason\":\"...\"}}, 不要解释。"
)


def _fine_rank(
    topic: InterestTopic, papers: list[ArxivPaper], k: int
) -> list[tuple[ArxivPaper, str]]:
    """LLM 精排: 返回 [(paper, reason), ...]。失败回退粗排顺序 (理由用主题溯源)。"""
    if not papers:
        return []
    listing = "\n".join(
        f"[{i}] {p.title}\n{p.summary[:300]}" for i, p in enumerate(papers)
    )
    try:
        from core.llm import get_llm
        provider, model, _, _ = settings.resolve_agent_model("chat")  # low 档
        text = get_llm().chat_text(
            [{"role": "system", "content": _RERANK_SYS.format(
                name=topic.name, query=topic.query, k=k)},
             {"role": "user", "content": listing}],
            temperature=0.2, provider=provider, model=model,
        )
        m = re.search(r"\[.*\]", text, re.DOTALL)
        data = json.loads(m.group(0)) if m else []
        out: list[tuple[ArxivPaper, str]] = []
        seen: set[int] = set()
        for d in data:
            if not isinstance(d, dict):
                continue
            idx = d.get("idx")
            if not isinstance(idx, int) or idx < 0 or idx >= len(papers) or idx in seen:
                continue
            seen.add(idx)
            reason = str(d.get("reason", "")).strip() or topic.reason
            out.append((papers[idx], reason))
            if len(out) >= k:
                break
        if out:
            return out
    except Exception as exc:  # noqa: BLE001  精排失败回退
        log_event("digest.rank.fine_failed", level="WARNING", error=str(exc))
    # 回退: 取粗排前 k, 理由用主题溯源。
    return [(p, topic.reason) for p in papers[:k]]


def rank_topic(
    topic: InterestTopic,
    papers: list[ArxivPaper],
    per_topic: int | None = None,
    pushed: set[str] | None = None,
    local_titles: set[str] | None = None,
) -> list[tuple[ArxivPaper, str]]:
    """对单个主题的候选池做 去重 -> 粗排 -> 精排, 返回 [(paper, reason), ...]。"""
    k = per_topic or settings.digest_per_topic
    pushed = pushed if pushed is not None else store.pushed_ids()
    local_titles = local_titles if local_titles is not None else _local_titles()

    # 去重: 跳过已推 (arxiv_id) 与本地库已有 (标题近似)。
    seen_title: set[str] = set()
    candidates: list[ArxivPaper] = []
    for p in papers:
        if p.arxiv_id in pushed:
            continue
        nt = _norm_title(p.title)
        if nt and (nt in local_titles or nt in seen_title):
            continue
        seen_title.add(nt)
        candidates.append(p)
    if not candidates:
        return []

    coarse = _coarse_rank(topic, candidates, settings.digest_rerank_top_n)
    return _fine_rank(topic, coarse, k)
