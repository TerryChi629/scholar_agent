"""memory2 召回: 向量全扫 + 关键词 RRF + hotness 融合。

条目量少 (用户偏好/规则), 无需 ANN, 直接全扫算相似度。
- 向量路: query 与各条目 content 的 cosine 排名。
- 关键词路: ASCII token + 中文 bigram 命中数排名 (复用 retrieve 的分词思路)。
- RRF 融合两路 (K=60, 关键词权重 0.5)。
- hotness: freq × recency (half_life 天), final = 0.85*retrieval + 0.15*hotness。
"""
from __future__ import annotations

import math
import re
import time

from config import settings
from core.llm import get_embedder
from memory2 import store
from memory2.models import MemoryItem

_RRF_K = 60
_KEYWORD_WEIGHT = 0.5
_RETRIEVAL_W = 0.85
_HOTNESS_W = 0.15

_TOKEN_RE = re.compile(r"[A-Za-z0-9]+|[\u4e00-\u9fff]")
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")


def _tokenize(text: str) -> list[str]:
    """中英混合分词: 英文/数字按串小写; 中文相邻二元组 (保留单字兜底)。"""
    if not text:
        return []
    raw: list[str] = []
    for m in _TOKEN_RE.finditer(text):
        tok = m.group(0)
        raw.append(tok if _CJK_RE.match(tok) else tok.lower())
    out: list[str] = []
    for i, cur in enumerate(raw):
        if _CJK_RE.match(cur):
            out.append(cur)
            if i + 1 < len(raw) and _CJK_RE.match(raw[i + 1]):
                out.append(cur + raw[i + 1])
        else:
            out.append(cur)
    return out


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    return dot / (na * nb) if na and nb else 0.0


def _hotness(item: MemoryItem, now: float) -> float:
    """freq × recency 衰减; recency = 0.5^(age_days / half_life), 归一到 [0,1) 量级。"""
    half = max(settings.memory2_half_life_days, 1.0)
    age_days = max(now - (item.last_used_at or item.created_at or now), 0.0) / 86400.0
    recency = 0.5 ** (age_days / half)
    # freq 用 log 压缩, 避免高频条目碾压; 再乘 recency。
    return math.log1p(item.freq) * recency


def recall(query: str, category: str | None = None, top_k: int = 5) -> list[MemoryItem]:
    """召回与 query 最相关的活跃记忆条目 (融合分降序, 命中后刷新 last_used_at)。"""
    if not settings.memory2_enabled or not (query or "").strip():
        return []
    items = store.active_items(category)
    if not items:
        return []

    # 向量路排名
    emb = get_embedder()
    qvec = emb.embed([query])[0]
    item_vecs = emb.embed([it.content for it in items])
    vec_ranked = sorted(
        range(len(items)),
        key=lambda i: _cosine(qvec, item_vecs[i]),
        reverse=True,
    )

    # 关键词路排名 (命中 token 数; 0 命中的排末尾)
    qtokens = set(_tokenize(query))
    kw_hits = [len(qtokens & set(_tokenize(it.content))) for it in items]
    kw_ranked = sorted(range(len(items)), key=lambda i: kw_hits[i], reverse=True)

    # RRF 融合 (关键词路加权)
    scores: dict[int, float] = {}
    for rank, i in enumerate(vec_ranked):
        scores[i] = scores.get(i, 0.0) + 1.0 / (_RRF_K + rank)
    for rank, i in enumerate(kw_ranked):
        if kw_hits[i] > 0:  # 0 命中不贡献关键词分
            scores[i] = scores.get(i, 0.0) + _KEYWORD_WEIGHT / (_RRF_K + rank)

    # 叠加 hotness。retrieval (RRF) 与 hotness 量纲不同, 各自归一到 [0,1] 再加权,
    # 否则 0.85/0.15 的权重无意义 (RRF 量级 ~0.02, hotness ~1)。
    now = time.time()
    max_retr = max(scores.values(), default=0.0) or 1.0
    hot_raw = {i: _hotness(items[i], now) for i in scores}
    max_hot = max(hot_raw.values(), default=0.0) or 1.0
    final: list[tuple[int, float]] = []
    for i, retr in scores.items():
        norm_retr = retr / max_retr
        norm_hot = hot_raw[i] / max_hot
        final.append((i, _RETRIEVAL_W * norm_retr + _HOTNESS_W * norm_hot))
    final.sort(key=lambda x: x[1], reverse=True)

    picked = [items[i] for i, _ in final[:top_k]]
    for it in picked:
        store.touch(it.id)
    return picked
