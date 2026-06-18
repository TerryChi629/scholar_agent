"""memory2 写入: remember() = 精确去重 / reinforcement / 语义 supersede。

写入决策 (确定性, 见 CLAUDE.md M8.4):
1. content_hash 命中已有条目 -> reinforcement (freq+1), 不新增。
2. 与某活跃条目语义相似度 >= supersede 阈值 (默认 0.90) -> supersede (新取代旧)。
3. 0.70-0.90 -> 暂不合并, 直接新增 (避免误并)。
4. 否则直接新增。
"""
from __future__ import annotations

import time
import uuid

from config import settings
from core.llm import get_embedder
from core.obs import log_event
from memory2 import store
from memory2.models import CATEGORIES, MemoryItem, content_hash


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def remember(category: str, content: str) -> dict:
    """写入一条记忆, 返回 {action, id} (action: reinforced/superseded/added/ignored)。"""
    if not settings.memory2_enabled:
        return {"action": "ignored", "id": None}
    content = " ".join((content or "").split())
    if category not in CATEGORIES or not content:
        return {"action": "ignored", "id": None}

    chash = content_hash(category, content)
    exact = store.get_by_hash(chash)
    if exact is not None:  # 1. 精确去重 -> 强化
        store.reinforce(exact.id)
        log_event("memory2.reinforce", category=category, item_id=exact.id)
        return {"action": "reinforced", "id": exact.id}

    # 2/3. 语义判定: 同类活跃条目里找最相似的。
    emb = get_embedder()
    new_vec = emb.embed([content])[0]
    candidates = store.active_items(category)
    best_id, best_sim = None, 0.0
    if candidates:
        vecs = emb.embed([c.content for c in candidates])
        for c, v in zip(candidates, vecs):
            sim = _cosine(new_vec, v)
            if sim > best_sim:
                best_id, best_sim = c.id, sim

    now = time.time()
    new_item = MemoryItem(
        id=uuid.uuid4().hex[:12], category=category, content=content,
        chash=chash, freq=1, created_at=now, last_used_at=now,
    )
    if best_id and best_sim >= settings.memory2_supersede_threshold:
        store.supersede(best_id, new_item, reason=f"sim={best_sim:.3f}")
        log_event("memory2.supersede", category=category, old_id=best_id,
                  new_id=new_item.id, sim=round(best_sim, 3))
        return {"action": "superseded", "id": new_item.id}

    store.insert(new_item)
    log_event("memory2.add", category=category, item_id=new_item.id,
              nearest_sim=round(best_sim, 3))
    return {"action": "added", "id": new_item.id}
