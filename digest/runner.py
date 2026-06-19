"""M12.5 调度入口: run_digest 串联 画像 -> 拉新 -> 排序去重 -> 飞书推送 -> 落库。

被 POST /digest/run 调用 (cron 定时触发), 也可 CLI 手动跑。
全程优雅降级: 任一主题拉新/排序失败只跳过该主题, 不阻断整体。
"""
from __future__ import annotations

from config import settings
from core.obs import log_event
from digest import rank, store
from digest.arxiv_client import search_arxiv_papers
from digest.profile import build_profile


def run_digest(dry_run: bool = False) -> dict:
    """执行一次每日论文速递。

    dry_run=True 时只生成分组结果、不推送飞书也不落库 (供调试/前端预览)。
    返回 {"topics", "groups", "pushed", "ok"} 摘要。
    """
    if not settings.digest_enabled:
        log_event("digest.run.disabled")
        return {"ok": False, "reason": "disabled", "topics": 0, "groups": [], "pushed": 0}

    topics = build_profile()
    if not topics:
        log_event("digest.run.no_topics")
        return {"ok": False, "reason": "no_interest_signals", "topics": 0, "groups": [], "pushed": 0}

    categories = [c.strip() for c in settings.digest_arxiv_categories.split(",") if c.strip()]
    pushed = store.pushed_ids()
    local_titles = rank._local_titles()

    groups: list[dict] = []
    to_mark: list[tuple[str, str, str]] = []
    total = 0
    for topic in topics:
        if total >= settings.digest_total_max:
            break
        try:
            candidates = search_arxiv_papers(
                topic.query,
                max_results=settings.digest_fetch_per_topic,
                categories=categories or None,
                days_back=settings.digest_days_back,
            )
            ranked = rank.rank_topic(
                topic, candidates,
                pushed=pushed, local_titles=local_titles,
            )
        except Exception as exc:  # noqa: BLE001  单主题失败不阻断整体
            log_event("digest.run.topic_failed", level="WARNING",
                      topic=topic.name, error=str(exc))
            continue
        if not ranked:
            continue

        papers_out: list[dict] = []
        for paper, reason in ranked:
            if total >= settings.digest_total_max:
                break
            papers_out.append({
                "title": paper.title, "url": paper.url,
                "authors": paper.authors, "reason": reason,
                "arxiv_id": paper.arxiv_id,
            })
            pushed.add(paper.arxiv_id)  # 同次运行内跨主题去重
            to_mark.append((paper.arxiv_id, paper.title, topic.name))
            total += 1
        if papers_out:
            groups.append({
                "name": topic.name, "query": topic.query,
                "reason": topic.reason, "papers": papers_out,
            })

    if dry_run:
        log_event("digest.run.dry", topics=len(topics), groups=len(groups), papers=total)
        return {"ok": True, "dry_run": True, "topics": len(topics),
                "groups": groups, "pushed": 0}

    sent = False
    if groups:
        from interfaces import feishu
        sent = feishu.notify_daily_digest(groups)
        store.mark_pushed(to_mark)

    log_event("digest.run.done", topics=len(topics), groups=len(groups),
              papers=total, sent=sent)
    return {"ok": True, "topics": len(topics), "groups": groups,
            "pushed": total, "sent": sent}
