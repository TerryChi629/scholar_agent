"""M12.1 兴趣画像: 多源长期记忆聚合 + LLM 聚类成兴趣主题。

信号来源 (多源融合, 让 agent "更懂我"):
- 问答记录 (chat_turns 里的 user 轮): 你真实问过的问题, 最强信号。
- 任务主题 (tasks.topic): 你发起过综述的方向。
- 偏好记忆 (memory2 preference): 你显式说过的偏好/关注点。
- 卡片记忆 (memory_cards 的 method_family): 你精读过论文的方法族。

每条信号带时间戳, 用 recency 衰减加权 (复用 memory2 的 half-life 思路: 近期高频排前)。
聚合后用一次 low 档 LLM 把原始信号聚类成 3-5 个稳定主题, 每个主题给出:
- name: 中文主题名
- query: 英文检索词 (直接喂 arXiv)
- reason: 来源溯源 (例如 "因为你最近问过 X / 综述过 Y"), 用于推送卡片的推荐理由。

LLM 失败时回退: 直接用权重最高的若干原始信号作为主题 (query 退化为原文)。
"""
from __future__ import annotations

import json
import math
import re
import sqlite3
import time
from dataclasses import dataclass, field

from config import settings
from core.obs import log_event


@dataclass
class InterestSignal:
    """一条兴趣信号 (聚类前的原子素材)。"""
    text: str
    source: str          # qa | task | preference | card
    ts: float            # 最近一次出现时间
    weight: float = 1.0  # recency 加权后的权重


@dataclass
class InterestTopic:
    """聚类后的兴趣主题 (画像的最终产物)。"""
    name: str
    query: str
    reason: str
    sources: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"name": self.name, "query": self.query,
                "reason": self.reason, "sources": self.sources}


# 各来源的基础权重: 用户自定义置顶 > 真实问过的问题 > 发起过的任务 > 偏好 > 卡片。
_SOURCE_WEIGHT = {"custom": 1.5, "qa": 1.0, "task": 0.9, "preference": 0.6, "card": 0.5}


def _recency(ts: float, now: float) -> float:
    """recency 衰减, 复用 memory2 half-life 思路 (近期权重高)。"""
    half = max(settings.memory2_half_life_days, 1.0)
    age_days = max(now - (ts or now), 0.0) / 86400.0
    return 0.5 ** (age_days / half)


def _collect_qa(conn: sqlite3.Connection, since: float) -> list[InterestSignal]:
    """问答记录: 取窗口内 user 轮的问题文本。"""
    try:
        rows = conn.execute(
            "SELECT content, created_at FROM chat_turns "
            "WHERE role='user' AND created_at>=? ORDER BY created_at DESC",
            (since,),
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    out: list[InterestSignal] = []
    for content, ts in rows:
        text = (content or "").strip()
        if len(text) >= 4:
            out.append(InterestSignal(text=text[:200], source="qa", ts=ts or 0.0))
    return out


def _collect_tasks(conn: sqlite3.Connection, since: float) -> list[InterestSignal]:
    """任务主题: 取窗口内发起过的综述 topic。"""
    try:
        rows = conn.execute(
            "SELECT topic, updated_at FROM tasks WHERE updated_at>=? ORDER BY updated_at DESC",
            (since,),
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    out: list[InterestSignal] = []
    for topic, ts in rows:
        text = (topic or "").strip()
        if text:
            out.append(InterestSignal(text=text[:200], source="task", ts=ts or 0.0))
    return out


def _collect_preferences(now: float) -> list[InterestSignal]:
    """偏好记忆: memory2 的 preference 活跃条目 (freq 越高越久用越重)。"""
    if not settings.memory2_enabled:
        return []
    try:
        from memory2 import store as m2store
        items = m2store.active_items("preference")
    except Exception as exc:  # noqa: BLE001  记忆不可用不阻断画像
        log_event("digest.profile.pref_failed", level="WARNING", error=str(exc))
        return []
    out: list[InterestSignal] = []
    for it in items:
        text = (it.content or "").strip()
        if text:
            sig = InterestSignal(text=text[:200], source="preference",
                                 ts=it.last_used_at or it.created_at or now)
            sig.weight = math.log1p(max(it.freq, 1))  # freq 体现强度
            out.append(sig)
    return out


def _collect_cards(conn: sqlite3.Connection, now: float) -> list[InterestSignal]:
    """卡片记忆: 已精读论文的 method_family (你深入读过的方法方向)。"""
    try:
        rows = conn.execute(
            "SELECT card_json, updated_at FROM memory_cards ORDER BY updated_at DESC LIMIT 200"
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    fam_ts: dict[str, float] = {}
    fam_cnt: dict[str, int] = {}
    for card_json, ts in rows:
        try:
            fam = (json.loads(card_json) or {}).get("method_family", "")
        except (json.JSONDecodeError, TypeError):
            fam = ""
        fam = (fam or "").strip()
        if not fam or fam == "未分类":
            continue
        fam_ts[fam] = max(fam_ts.get(fam, 0.0), ts or 0.0)
        fam_cnt[fam] = fam_cnt.get(fam, 0) + 1
    out: list[InterestSignal] = []
    for fam, ts in fam_ts.items():
        sig = InterestSignal(text=fam[:120], source="card", ts=ts or now)
        sig.weight = math.log1p(fam_cnt[fam])
        out.append(sig)
    return out


def _collect_custom(now: float) -> list[InterestSignal]:
    """自定义兴趣: 用户在前端自然语言补充的兴趣 (最高权重, 视为近期信号)。"""
    try:
        from digest import store
        items = store.list_interests()
    except Exception as exc:  # noqa: BLE001  自定义表不可用不阻断画像
        log_event("digest.profile.custom_failed", level="WARNING", error=str(exc))
        return []
    out: list[InterestSignal] = []
    for it in items:
        text = (it.get("text") or "").strip()
        if text:
            out.append(InterestSignal(text=text[:200], source="custom",
                                      ts=it.get("created_at") or now))
    return out


def collect_signals() -> list[InterestSignal]:
    """聚合多源兴趣信号并按 recency × 来源权重打分排序 (降序)。"""
    now = time.time()
    since = now - settings.digest_profile_days * 86400.0
    settings.ensure_dirs()
    conn = sqlite3.connect(settings.sqlite_path)
    try:
        signals = (
            _collect_qa(conn, since)
            + _collect_tasks(conn, since)
            + _collect_cards(conn, now)
        )
    finally:
        conn.close()
    signals += _collect_preferences(now)
    signals += _collect_custom(now)

    # 最终权重 = 来源基础权重 × recency × 条目自带强度。
    for s in signals:
        s.weight = _SOURCE_WEIGHT.get(s.source, 0.5) * _recency(s.ts, now) * (s.weight or 1.0)
    signals.sort(key=lambda s: s.weight, reverse=True)
    return signals


_CLUSTER_SYS = (
    "你是科研兴趣分析助手。下面是用户近期的提问、综述方向、偏好与精读方法 (按重要度排序)。"
    "请据此归纳出该用户最核心的 {n} 个研究兴趣主题, 用于每天去 arXiv 找最新论文。\n"
    "对每个主题输出: name (中文主题名), query (用于 arXiv 检索的英文关键词, 含核心术语), "
    "reason (一句话说明为什么判断用户关注它, 要引用其具体提问/方向, 体现'懂他')。\n"
    "只输出 JSON 数组, 每个元素是 {{\"name\":..,\"query\":..,\"reason\":..}}, 不要解释。"
)


def _cluster_with_llm(signals: list[InterestSignal], n: int) -> list[InterestTopic]:
    """用 low 档 LLM 把信号聚类成 n 个兴趣主题。失败抛异常 (调用方回退)。"""
    from core.llm import get_llm

    listing = "\n".join(
        f"[{s.source}] {s.text}" for s in signals[:40]  # 控 token: 取权重最高的 40 条
    )
    provider, model, _, _ = settings.resolve_agent_model("chat")  # low 档
    text = get_llm().chat_text(
        [{"role": "system", "content": _CLUSTER_SYS.format(n=n)},
         {"role": "user", "content": listing}],
        temperature=0.3, provider=provider, model=model,
    )
    m = re.search(r"\[.*\]", text, re.DOTALL)
    data = json.loads(m.group(0)) if m else []
    topics: list[InterestTopic] = []
    for d in data:
        if not isinstance(d, dict):
            continue
        name = str(d.get("name", "")).strip()
        query = str(d.get("query", "")).strip()
        reason = str(d.get("reason", "")).strip()
        if name and query:
            topics.append(InterestTopic(name=name, query=query, reason=reason))
    return topics[:n]


def _fallback_topics(signals: list[InterestSignal], n: int) -> list[InterestTopic]:
    """LLM 不可用时的兜底: 直接用权重最高的去重信号作为主题 (query 退化为原文)。"""
    topics: list[InterestTopic] = []
    seen: set[str] = set()
    for s in signals:
        key = s.text.lower()[:40]
        if key in seen:
            continue
        seen.add(key)
        topics.append(InterestTopic(
            name=s.text[:30], query=s.text,
            reason=f"来自你的{ {'qa': '提问', 'task': '综述方向', 'preference': '偏好', 'card': '精读方法'}.get(s.source, '记录') }",
            sources=[s.source],
        ))
        if len(topics) >= n:
            break
    return topics


def build_profile(topics_max: int | None = None) -> list[InterestTopic]:
    """构建兴趣画像: 聚合信号 -> LLM 聚类 (失败回退) -> 返回兴趣主题列表。"""
    n = topics_max or settings.digest_topics_max
    signals = collect_signals()
    if not signals:
        log_event("digest.profile.empty")
        return []
    # 给每个主题标注其覆盖到的来源 (用于卡片溯源)。
    src_set = sorted({s.source for s in signals})
    try:
        topics = _cluster_with_llm(signals, n)
        if topics:
            for t in topics:
                if not t.sources:
                    t.sources = src_set
            log_event("digest.profile.built", topics=len(topics), signals=len(signals))
            return topics
    except Exception as exc:  # noqa: BLE001  聚类失败回退确定性兜底
        log_event("digest.profile.cluster_failed", level="WARNING", error=str(exc))
    return _fallback_topics(signals, n)


_SOURCE_LABEL = {"custom": "自定义兴趣", "qa": "提问记录", "task": "综述方向",
                 "preference": "偏好记忆", "card": "精读方法"}


def profile_summary(topics_max: int | None = None, top_signals: int = 12) -> dict:
    """构建供前端展示的画像摘要: 主题 + 来源分布 + 权重最高的信号 (体现'懂他')。"""
    signals = collect_signals()
    by_source: dict[str, int] = {}
    for s in signals:
        by_source[s.source] = by_source.get(s.source, 0) + 1
    topics = build_profile(topics_max) if signals else []
    return {
        "topics": [t.to_dict() for t in topics],
        "signal_count": len(signals),
        "sources": [
            {"source": src, "label": _SOURCE_LABEL.get(src, src), "count": cnt}
            for src, cnt in sorted(by_source.items(), key=lambda kv: -kv[1])
        ],
        "top_signals": [
            {"source": s.source, "label": _SOURCE_LABEL.get(s.source, s.source),
             "text": s.text, "weight": round(s.weight, 4)}
            for s in signals[:top_signals]
        ],
    }
