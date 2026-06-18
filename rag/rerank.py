"""精排 (rerank) + 多样性去冗余 (MMR) —— M9 检索纵深。

两个独立但同属"召回后重排"环节的能力:

1. rerank(query, chunks): 对融合召回的候选做"真·相关性精排"
   - 优先本地 cross-encoder (bge-reranker, query×doc 交叉编码, 比向量召回精准);
   - 模型不可用 (未装 sentence-transformers / 下载失败) 时, 回退 LLM listwise 打分;
   - 两者都不可用时, 原样返回 (退化为上游 RRF 排序), 绝不阻断检索。

2. mmr(query_vec, chunks): Maximal Marginal Relevance 去冗余
   - 在"与 query 相关"和"与已选片段不重复"之间平衡, 消除 top-k 里近乎重复的片段;
   - 纯确定性, 复用 embedding, 不调任何外部模型。
"""
from __future__ import annotations

import threading

from config import settings
from core.obs import log_event
from rag.store import Chunk

# —— cross-encoder 单例 (懒加载, 线程安全) ——
_ce_lock = threading.Lock()
_ce_model = None          # 加载成功的 CrossEncoder
_ce_unavailable = False   # 加载失败标志: 失败一次后不再反复尝试 (省时)


def _get_cross_encoder():
    """懒加载本地 cross-encoder。不可用返回 None (调用方回退 LLM/原序)。"""
    global _ce_model, _ce_unavailable
    if _ce_model is not None:
        return _ce_model
    if _ce_unavailable:
        return None
    with _ce_lock:
        if _ce_model is not None:
            return _ce_model
        if _ce_unavailable:
            return None
        try:
            from sentence_transformers import CrossEncoder
            _ce_model = CrossEncoder(settings.rerank_model)
            log_event("rerank.ce_loaded", model=settings.rerank_model)
            return _ce_model
        except Exception as exc:  # noqa: BLE001  没装/下载失败 -> 标记不可用, 回退
            _ce_unavailable = True
            log_event("rerank.ce_unavailable", level="WARNING", error=str(exc))
            return None


def _rerank_cross_encoder(query: str, chunks: list[Chunk]) -> list[Chunk] | None:
    """cross-encoder 精排。模型不可用返回 None。"""
    model = _get_cross_encoder()
    if model is None:
        return None
    pairs = [(query, c.text) for c in chunks]
    try:
        scores = model.predict(pairs)
    except Exception as exc:  # noqa: BLE001  推理异常也回退
        log_event("rerank.ce_predict_failed", level="WARNING", error=str(exc))
        return None
    for c, s in zip(chunks, scores):
        c.score = round(float(s), 6)
    chunks.sort(key=lambda x: x.score, reverse=True)
    log_event("rerank.ce", n=len(chunks))
    return chunks


def _rerank_llm(query: str, chunks: list[Chunk]) -> list[Chunk] | None:
    """LLM listwise 兜底精排: 让 low 档模型给每个候选 0-10 相关性打分。

    失败返回 None (调用方退化为原序)。只用于 cross-encoder 不可用时。
    """
    if not settings.rerank_llm_fallback or not chunks:
        return None
    import json
    import re
    from core.llm import get_llm

    listing = "\n".join(f"[{i}] {c.text[:280]}" for i, c in enumerate(chunks))
    prompt = (
        "你是检索精排器。给定一个查询和若干候选片段, 为每个片段相对查询的相关性打分 "
        "(0-10, 10 最相关)。只输出 JSON 对象, key 为片段序号字符串, value 为分数, 无解释。\n\n"
        f"查询: {query}\n\n候选:\n{listing}"
    )
    try:
        provider, model, _, _ = settings.resolve_agent_model("retriever")  # low 档
        text = get_llm().chat_text(
            [{"role": "user", "content": prompt}],
            temperature=0.0, provider=provider, model=model,
        )
        m = re.search(r"\{.*\}", text, re.DOTALL)
        data = json.loads(m.group(0)) if m else {}
        for i, c in enumerate(chunks):
            v = data.get(str(i))
            if isinstance(v, (int, float)):
                c.score = round(float(v), 6)
        chunks.sort(key=lambda x: x.score, reverse=True)
        log_event("rerank.llm", n=len(chunks))
        return chunks
    except Exception as exc:  # noqa: BLE001
        log_event("rerank.llm_failed", level="WARNING", error=str(exc))
        return None


def rerank(query: str, chunks: list[Chunk]) -> list[Chunk]:
    """对候选做精排: cross-encoder 优先, 失败回退 LLM, 再失败保持原序。"""
    if not settings.rerank_enabled or len(chunks) <= 1:
        return chunks
    ranked = _rerank_cross_encoder(query, chunks)
    if ranked is not None:
        return ranked
    ranked = _rerank_llm(query, chunks)
    if ranked is not None:
        return ranked
    return chunks  # 全不可用: 退化为上游排序


# —— MMR 多样性去冗余 ——
def _cosine(a: list[float], b: list[float]) -> float:
    s = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    return s / (na * nb) if na and nb else 0.0


def mmr(query: str, chunks: list[Chunk], top_k: int,
        lambda_mult: float | None = None) -> list[Chunk]:
    """Maximal Marginal Relevance: 在相关性与多样性间平衡, 去掉近乎重复的片段。

    复用 embedding 计算片段间相似度; query 相关性用片段当前 score (上游精排/融合分)
    归一化近似。embedding 失败时直接返回前 top_k (不阻断)。
    """
    if not settings.mmr_enabled or len(chunks) <= top_k:
        return chunks[:top_k]
    lam = settings.mmr_lambda if lambda_mult is None else lambda_mult
    try:
        from core.llm import get_embedder
        vecs = get_embedder().embed([c.text for c in chunks])
    except Exception as exc:  # noqa: BLE001  embedding 不可用 -> 退化为按 score 截断
        log_event("mmr.embed_failed", level="WARNING", error=str(exc))
        return chunks[:top_k]

    # query 相关性: 用片段 score 归一到 [0,1] (上游已精排, score 越大越相关)。
    scores = [c.score for c in chunks]
    smin, smax = min(scores), max(scores)
    rng = (smax - smin) or 1.0
    rel = [(s - smin) / rng for s in scores]

    selected: list[int] = []
    candidates = list(range(len(chunks)))
    while candidates and len(selected) < top_k:
        best_i, best_val = None, -1e9
        for i in candidates:
            max_sim = max((_cosine(vecs[i], vecs[j]) for j in selected), default=0.0)
            val = lam * rel[i] - (1 - lam) * max_sim
            if val > best_val:
                best_val, best_i = val, i
        selected.append(best_i)
        candidates.remove(best_i)
    log_event("mmr.select", kept=len(selected), lam=lam)
    return [chunks[i] for i in selected]
