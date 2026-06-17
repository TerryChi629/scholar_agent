"""Hybrid 检索: 向量 + BM25, 用 RRF 融合。

流程:
1. 向量召回 (语义相似) -> 候选 A
2. BM25 关键词召回 (字面匹配) -> 候选 B
3. 用 RRF (Reciprocal Rank Fusion) 融合两路排名 -> 最终 top_k

RRF 只看"排名位次"而非各自分数量纲, 天然规避向量距离与 BM25 分数不可比的问题。
rerank (cross-encoder) 留待后续, 不在 M1 范围。
"""
from __future__ import annotations

import re
import threading
import time

from config import settings
from rag.store import Chunk, get_store

# RRF 平滑常数 (经验值 60): 越大则高位次的优势越平缓。
_RRF_K = 60

_TOKEN_RE = re.compile(r"[A-Za-z0-9]+")

# —— M4 检索结果 TTL 缓存 (进程内, 线程安全): 相同查询短期内直接复用 ——
_cache_lock = threading.Lock()
_retrieval_cache: dict[tuple, tuple[float, list[Chunk]]] = {}


def _tokenize(text: str) -> list[str]:
    """英文为主的简单分词: 取字母数字串并小写。"""
    return [t.lower() for t in _TOKEN_RE.findall(text)]


def _bm25_search(query: str, corpus: list[Chunk], top_k: int) -> list[Chunk]:
    """对给定语料做 BM25 关键词召回, 返回按分数排序的 top_k。"""
    from rank_bm25 import BM25Okapi

    tokenized = [_tokenize(c.text) for c in corpus]
    bm25 = BM25Okapi(tokenized)
    scores = bm25.get_scores(_tokenize(query))
    ranked = sorted(zip(corpus, scores), key=lambda x: x[1], reverse=True)
    return [c for c, _ in ranked[:top_k]]


def _rrf_fuse(rankings: list[list[Chunk]], top_k: int) -> list[Chunk]:
    """对多路排名做 RRF 融合。score = Σ 1/(k + rank)。"""
    scores: dict[str, float] = {}
    pool: dict[str, Chunk] = {}
    for ranking in rankings:
        for rank, chunk in enumerate(ranking):
            scores[chunk.chunk_id] = scores.get(chunk.chunk_id, 0.0) + 1.0 / (_RRF_K + rank)
            pool.setdefault(chunk.chunk_id, chunk)
    ordered = sorted(scores.items(), key=lambda x: x[1], reverse=True)
    out: list[Chunk] = []
    for cid, score in ordered[:top_k]:
        chunk = pool[cid]
        chunk.score = round(score, 6)  # 融合分写回, 便于上层展示/排序
        out.append(chunk)
    return out


def hybrid_search(
    query: str, top_k: int = 8, year_min: int | None = None, paper_id: str | None = None
) -> list[Chunk]:
    """混合检索: 向量召回 + BM25 召回, RRF 融合后返回 top_k。

    paper_id 不为空时, 只在该论文范围内检索 (供 Reader 精读单篇, 防跨篇串味)。
    相同查询参数在 TTL 内命中进程内缓存, 省 embedding + 检索开销。
    """
    cache_key = (query, top_k, year_min, paper_id)
    if settings.cache_enabled:
        ttl = settings.retrieval_cache_ttl
        with _cache_lock:
            hit = _retrieval_cache.get(cache_key)
            if hit and (time.time() - hit[0]) < ttl:
                return hit[1]

    result = _hybrid_search_uncached(query, top_k, year_min, paper_id)

    if settings.cache_enabled:
        with _cache_lock:
            _retrieval_cache[cache_key] = (time.time(), result)
    return result


def _hybrid_search_uncached(
    query: str, top_k: int, year_min: int | None, paper_id: str | None
) -> list[Chunk]:
    conds: list[dict] = []
    if year_min is not None:
        conds.append({"year": {"$gte": year_min}})
    if paper_id:
        conds.append({"paper_id": {"$eq": paper_id}})
    where = None
    if len(conds) == 1:
        where = conds[0]
    elif len(conds) > 1:
        where = {"$and": conds}
    store = get_store()

    # 两路各自多召回一些 (取 top_k 的数倍), 再融合裁剪, 提升召回覆盖。
    fetch_n = max(top_k * 3, top_k)
    vector_hits = store.query(query, top_k=fetch_n, where=where)

    corpus = store.all_chunks(where=where)
    bm25_hits = _bm25_search(query, corpus, top_k=fetch_n) if corpus else []

    if not bm25_hits:  # 库为空或仅向量可用时, 退化为纯向量结果。
        return vector_hits[:top_k]
    return _rrf_fuse([vector_hits, bm25_hits], top_k=top_k)
