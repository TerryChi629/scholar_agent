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

# 英文/数字串 + 单个 CJK 字。CJK 单字单独捕获, 再在 _tokenize 内拼成 bigram,
# 让中文 query 在 BM25 路真正生效 (此前只取英文数字, 中文 query 完全失效)。
_TOKEN_RE = re.compile(r"[A-Za-z0-9]+|[\u4e00-\u9fff]")
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")

# F: 极轻量规则 rerank 的弱加分系数。刻意压得很小, 只在 RRF 分数接近时起决胜微调,
# 不颠覆语义/字面融合的主排序。
_RERANK_SECTION_BONUS = 0.05   # 命中核心章节的弱加分
_RERANK_KEYWORD_BONUS = 0.01   # 每个 query 关键词在片段命中的弱加分 (有上限)
_RERANK_KEYWORD_CAP = 0.05     # 关键词加分上限
_CORE_SECTION_RE = re.compile(
    r"abstract|method|approach|model|framework|experiment|evaluation|result",
    re.IGNORECASE,
)

# —— M4 检索结果 TTL 缓存 (进程内, 线程安全): 相同查询短期内直接复用 ——
_cache_lock = threading.Lock()
_retrieval_cache: dict[tuple, tuple[float, list[Chunk]]] = {}

# 中文 query -> 英文术语 的翻译缓存 (进程内): 同一中文 query 只烧一次 LLM。
_qtrans_lock = threading.Lock()
_qtrans_cache: dict[str, str] = {}


def _has_cjk(text: str) -> bool:
    return bool(_CJK_RE.search(text or ""))


def _bm25_query(query: str) -> str:
    """为 BM25 关键词召回准备 query 文本。

    BM25 语料目前以英文论文为主, 中文 query 的字面 (bigram) 几乎无法命中, 会让 BM25
    这条腿空转、hybrid 退化为单腿向量。故含中文时用 low 档 LLM 把 query 译成英文术语
    (含核心方法名), 拼接在原 query 之后一并喂给 BM25 (原 query 保留, 兼容中英混排)。
    向量召回仍用原始 query (跨语种语义), 互不影响。结果按 query 缓存, 控 token。
    """
    if not _has_cjk(query):
        return query
    with _qtrans_lock:
        cached = _qtrans_cache.get(query)
    if cached is not None:
        return cached
    en = _llm_translate_terms(query)
    merged = f"{query} {en}".strip() if en else query
    with _qtrans_lock:
        _qtrans_cache[query] = merged
    return merged


def _llm_translate_terms(query: str) -> str:
    """调 low 档 LLM 把中文检索 query 译成英文术语串。失败返回空串 (调用方回退原 query)。"""
    from config import settings
    from core.llm import get_llm
    from core.obs import log_event

    try:
        provider, model, _, _ = settings.resolve_agent_model("retriever")  # 复用 low 档
        prompt = (
            "把下面的中文学术检索词翻译成英文检索关键词, 用于在英文论文库做 BM25 关键词匹配。"
            "要求: 给出对应的英文术语 + 该领域常用同义表达 / 缩写 (如有), 用空格分隔, "
            "只输出英文关键词本身, 不要解释、不要标点。\n\n"
            f"检索词: {query}"
        )
        text = get_llm().chat_text(
            [{"role": "user", "content": prompt}],
            temperature=0.0, provider=provider, model=model,
        )
        # 只保留英文/数字/空格, 去掉可能混入的中文或多余符号。
        cleaned = re.sub(r"[^A-Za-z0-9 \-]", " ", text or "")
        return " ".join(cleaned.split())[:200]
    except Exception as exc:  # noqa: BLE001  翻译失败不应阻断检索, 回退原 query
        log_event("bm25.translate_failed", level="WARNING", error=str(exc))
        return ""


def _tokenize(text: str) -> list[str]:
    """中英混合分词。英文/数字按串取并小写; 中文按相邻二元组 (bigram) 切分。

    中文无空格分词, 单字区分度低、bigram 又能在无外部分词器 (如 jieba) 下稳定逼近
    词级匹配, 故对连续中文取相邻二字组合 (单字也保留作兜底), 让中文 query 在 BM25
    关键词召回真正生效。
    """
    if not text:
        return []
    tokens: list[str] = []
    for m in _TOKEN_RE.finditer(text):
        tok = m.group(0)
        if _CJK_RE.match(tok):  # 单个 CJK 字: 暂存, 下面拼 bigram
            tokens.append(tok)
        else:
            tokens.append(tok.lower())
    # 把相邻的 CJK 单字拼成 bigram (保留单字兜底)。
    out: list[str] = []
    i = 0
    n = len(tokens)
    while i < n:
        cur = tokens[i]
        if _CJK_RE.match(cur):
            out.append(cur)  # 单字兜底
            if i + 1 < n and _CJK_RE.match(tokens[i + 1]):
                out.append(cur + tokens[i + 1])  # 相邻二元组
        else:
            out.append(cur)
        i += 1
    return out


def _bm25_search(query: str, corpus: list[Chunk], top_k: int) -> list[Chunk]:
    """对给定语料做 BM25 关键词召回, 返回按分数排序的 top_k。"""
    from rank_bm25 import BM25Okapi

    tokenized = [_tokenize(c.text) for c in corpus]
    bm25 = BM25Okapi(tokenized)
    scores = bm25.get_scores(_tokenize(query))
    ranked = sorted(zip(corpus, scores), key=lambda x: x[1], reverse=True)
    return [c for c, _ in ranked[:top_k]]


def _bm25_recall(query: str, top_k: int, paper_id: str | None,
                 year_min: int | None, corpus: list[Chunk]) -> list[Chunk]:
    """BM25 召回: 优先持久化索引 (不每查重建), 回退按 corpus 现场建。

    向量用原始 query (跨语种语义); BM25 对中文 query 先补英文术语再字面匹配。
    """
    bm25_q = _bm25_query(query)
    if settings.bm25_persist_enabled:
        try:
            from rag.bm25_index import get_index
            return get_index().search(_tokenize(bm25_q), top_k=top_k,
                                      where_paper_id=paper_id, year_min=year_min)
        except Exception as exc:  # noqa: BLE001  索引异常 -> 回退现场重建
            from core.obs import log_event
            log_event("bm25.index_fallback", level="WARNING", error=str(exc))
    return _bm25_search(bm25_q, corpus, top_k=top_k) if corpus else []


def _expand_context(chunks: list[Chunk]) -> list[Chunk]:
    """父文档/邻居窗口扩展: 把每个命中 chunk 与同篇相邻 chunk 拼回更完整上下文。

    命中片段常只是答案的一段, 拼回前后 chunk 给合成端更连贯的上下文。原命中文本存入
    metadata['hit_text'] (供精确回溯/引用), chunk.text 替换为扩展后的合并文本。
    去重: 同篇内被合并的邻居不再单独出现 (避免重复上下文挤占)。
    """
    if not settings.context_expand_enabled or not chunks:
        return chunks
    store = get_store()
    window = settings.context_expand_window
    out: list[Chunk] = []
    covered: set[str] = set()  # 已被某次扩展吸收的 chunk_id
    for c in chunks:
        if c.chunk_id in covered:
            continue
        meta = c.metadata or {}
        idx = meta.get("chunk_index")
        pid = c.paper_id or meta.get("paper_id", "")
        if idx is None or not pid:
            out.append(c)
            continue
        neighbors = store.neighbors(pid, int(idx), window=window)
        if len(neighbors) <= 1:
            out.append(c)
            continue
        merged = " ".join(n.text for n in neighbors if n.text).strip()
        for n in neighbors:
            covered.add(n.chunk_id)
        new_meta = {**meta, "hit_text": c.text, "expanded": True}
        out.append(Chunk(chunk_id=c.chunk_id, paper_id=pid, text=merged,
                         metadata=new_meta, score=c.score))
    return out


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


def _rule_rerank(query: str, chunks: list[Chunk]) -> list[Chunk]:
    """极轻量规则 rerank: 在 RRF 融合分基础上叠加弱加分后重排。

    确定性、无模型、无额外 IO:
    - 核心章节 (method/experiment/abstract...) 命中给小幅加分;
    - query 关键词在片段文本命中按命中数小幅加分 (有上限)。
    加分系数远小于 RRF 分量级差, 只在并列/接近时决胜, 不颠覆主排序。
    """
    qtokens = set(_tokenize(query))
    for c in chunks:
        bonus = 0.0
        section = (c.metadata or {}).get("section", "") or ""
        if _CORE_SECTION_RE.search(section):
            bonus += _RERANK_SECTION_BONUS
        if qtokens:
            ctokens = set(_tokenize(c.text))
            hit = len(qtokens & ctokens)
            bonus += min(hit * _RERANK_KEYWORD_BONUS, _RERANK_KEYWORD_CAP)
        c.score = round(c.score + bonus, 6)
    chunks.sort(key=lambda x: x.score, reverse=True)
    return chunks


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
    """多阶段检索管线 (M9):

    召回(向量+BM25) -> RRF 融合 -> 规则 rerank -> cross-encoder/LLM 精排
    -> MMR 去冗余 -> 父文档扩展 -> top_k。
    各阶段均可通过 config 开关关闭, 关闭后退化为上游结果 (向后兼容)。
    """
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

    # 召回阶段多取候选 (取 max(top_k×3, rerank_top_n)), 给精排足够池子, 最后裁到 top_k。
    fetch_n = max(top_k * 3, settings.rerank_top_n if settings.rerank_enabled else top_k)
    vector_hits = store.query(query, top_k=fetch_n, where=where)

    # BM25 召回: 优先持久化索引; 持久化关闭/异常时回退按 corpus 现场重建。
    corpus = store.all_chunks(where=where) if not settings.bm25_persist_enabled else []
    bm25_hits = _bm25_recall(query, fetch_n, paper_id, year_min, corpus)

    # 1) 融合 (无 BM25 时退化为纯向量)
    if not bm25_hits:
        candidates = _rule_rerank(query, vector_hits[:fetch_n])
    else:
        fused = _rrf_fuse([vector_hits, bm25_hits], top_k=fetch_n)
        candidates = _rule_rerank(query, fused)

    # 2) 精排: cross-encoder 优先, 失败回退 LLM, 再失败保持原序
    from rag.rerank import rerank, mmr
    candidates = rerank(query, candidates)

    # 3) MMR 去冗余 (在精排后的候选上选出多样的 top_k)
    selected = mmr(query, candidates, top_k=top_k)

    # 4) 父文档/邻居窗口扩展 (给合成更完整上下文)
    return _expand_context(selected)
