"""M12.2 arXiv 拉新: 调官方 arXiv Atom API (stdlib, 不引入新依赖)。

用 urllib + xml.etree 解析 Atom feed (与 interfaces/feishu.py 一致的零依赖风格),
不依赖第三方 arxiv 库。支持:
- 关键词检索 (all:query), 可选分类过滤 (cat:cs.IR OR cat:cs.LG ...)。
- 按 submittedDate 倒序 (最新优先)。
- 客户端按回溯天数过滤 (近 N 天提交)。

失败返回空列表 (调用方降级), 不抛出阻断主流程。
"""
from __future__ import annotations

import re
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from xml.etree import ElementTree as ET

from core.obs import log_event

_ARXIV_API = "http://export.arxiv.org/api/query"
_ATOM = "{http://www.w3.org/2005/Atom}"
# AND 词项上限: 超过后近期窗口内几乎必然归零, 取前 N 个核心词兼顾精度与召回。
_MAX_AND_TERMS = 4


@dataclass
class ArxivPaper:
    arxiv_id: str
    title: str
    summary: str
    authors: list[str] = field(default_factory=list)
    categories: list[str] = field(default_factory=list)
    published: str = ""           # ISO 字符串
    published_ts: float = 0.0     # epoch 秒
    url: str = ""

    def to_dict(self) -> dict:
        return {
            "arxiv_id": self.arxiv_id, "title": self.title, "summary": self.summary,
            "authors": self.authors, "categories": self.categories,
            "published": self.published, "url": self.url,
        }


def _build_search_query(query: str, categories: list[str] | None) -> str:
    """构造 arXiv search_query: (all:词1 AND all:词2 ...) AND (cat:.. OR cat:..)。

    arXiv 对整句加引号的精确短语匹配几乎命中不到, 故拆成词项用 AND 连接 (兼顾精度与召回);
    单引号包裹的多词短语 (如 "large language model") 作为一个整体词项保留。
    词项过多时 AND 会过严 (近期窗口内极易归零), 故最多取前 _MAX_AND_TERMS 个核心词。
    """
    terms = [t for t in re.findall(r'"[^"]+"|\S+', query) if t.strip()]
    terms = terms[:_MAX_AND_TERMS]
    parts = [f"all:{t}" for t in terms]
    q = " AND ".join(parts) if parts else f"all:{query}"
    cats = [c.strip() for c in (categories or []) if c.strip()]
    if cats:
        cat_expr = " OR ".join(f"cat:{c}" for c in cats)
        return f"({q}) AND ({cat_expr})"
    return q


def _parse_entry(entry: ET.Element) -> ArxivPaper | None:
    """解析单个 Atom <entry>。字段缺失则尽力填充。"""
    def _text(tag: str) -> str:
        el = entry.find(f"{_ATOM}{tag}")
        return (el.text or "").strip() if el is not None and el.text else ""

    raw_id = _text("id")  # 形如 http://arxiv.org/abs/2401.01234v1
    if not raw_id:
        return None
    arxiv_id = raw_id.rsplit("/", 1)[-1]
    title = " ".join(_text("title").split())
    summary = " ".join(_text("summary").split())
    published = _text("published")
    published_ts = 0.0
    if published:
        try:
            dt = datetime.strptime(published, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            published_ts = dt.timestamp()
        except ValueError:
            published_ts = 0.0
    authors = [
        (a.find(f"{_ATOM}name").text or "").strip()
        for a in entry.findall(f"{_ATOM}author")
        if a.find(f"{_ATOM}name") is not None
    ]
    categories = [
        c.get("term", "") for c in entry.findall(f"{_ATOM}category") if c.get("term")
    ]
    return ArxivPaper(
        arxiv_id=arxiv_id, title=title, summary=summary,
        authors=[a for a in authors if a], categories=categories,
        published=published, published_ts=published_ts,
        url=f"https://arxiv.org/abs/{arxiv_id}",
    )


def search_arxiv_papers(
    query: str,
    max_results: int = 30,
    categories: list[str] | None = None,
    days_back: int | None = None,
) -> list[ArxivPaper]:
    """检索 arXiv 最新论文 (按提交时间倒序)。

    days_back 非空时只保留近 N 天提交的论文。失败返回空列表。
    """
    query = (query or "").strip()
    if not query:
        return []
    params = {
        "search_query": _build_search_query(query, categories),
        "sortBy": "submittedDate",
        "sortOrder": "descending",
        "start": 0,
        "max_results": max(1, int(max_results)),
    }
    url = f"{_ARXIV_API}?{urllib.parse.urlencode(params)}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "ScholarStance/1.0"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read()
    except Exception as exc:  # noqa: BLE001  网络/超时失败降级
        log_event("digest.arxiv.fetch_failed", level="WARNING", query=query, error=str(exc))
        return []

    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        log_event("digest.arxiv.parse_failed", level="WARNING", error=str(exc))
        return []

    papers: list[ArxivPaper] = []
    for entry in root.findall(f"{_ATOM}entry"):
        p = _parse_entry(entry)
        if p:
            papers.append(p)

    if days_back and papers:
        # 逐级放宽窗口: 请求窗口内 0 命中时, 宁可推稍旧但相关的, 也不空推。
        for factor in (1, 3, 15):
            cutoff = time.time() - days_back * factor * 86400.0
            recent = [p for p in papers if not p.published_ts or p.published_ts >= cutoff]
            if recent:
                if factor > 1:
                    log_event("digest.arxiv.window_widened", query=query,
                              days=days_back * factor, n=len(recent))
                papers = recent
                break

    log_event("digest.arxiv.fetched", query=query, n=len(papers))
    return papers
