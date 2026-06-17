"""Tool 实现 + 注册。Agent 只能通过这里的 @tool 影响世界。

注意: import 本模块即完成注册 (装饰器副作用)。
大部分为骨架, 业务逻辑留 TODO 给 Trae 填。
"""
from __future__ import annotations

from core.tool_registry import tool


@tool
def rag_query(query: str, top_k: int = 8, year_min: int = 0, paper_id: str = "") -> list:
    """在本地私有库检索相关片段, 支持按年份/指定论文过滤。Retriever/Reader 使用。

    paper_id 非空时只在该论文内检索 (Reader 精读单篇时务必传入, 避免跨篇串味)。
    """
    from rag.retrieve import hybrid_search

    ymin = year_min or None
    pid = paper_id or None
    hits = hybrid_search(query, top_k=top_k, year_min=ymin, paper_id=pid)
    return [{"paper_id": h.paper_id, "text": h.text[:600], "score": round(h.score, 3),
             "meta": h.metadata} for h in hits]


@tool
def rag_ingest(directory: str) -> dict:
    """把一个本地目录下的 PDF 入库 (解析+分块+embedding)。写操作。"""
    from rag.ingest import ingest_dir

    return ingest_dir(directory)


@tool
def search_arxiv(query: str, max_results: int = 5) -> list:
    """检索 arXiv 补充外部文献。Retriever 在本地库不足时调用。

    TODO(Trae): 用 arxiv 库实现; 可下载 PDF 后交 rag_ingest 入库。
    """
    return [{"title": f"[TODO] arxiv 结果 for: {query}", "id": "placeholder"}]


@tool
def expand_query(topic: str) -> list:
    """把研究方向扩展为同义/相关检索词, 提高召回。

    TODO(Trae): 用 LLM 生成 5-8 个相关 query。
    """
    return [topic]


def _slug(text: str) -> str:
    """把任意标签规整为稳定的节点 id 片段。"""
    import re
    s = re.sub(r"[^0-9a-zA-Z\u4e00-\u9fff]+", "_", (text or "").strip().lower())
    return s.strip("_") or "unknown"


def _spans_to_quotes(spans) -> list:
    """把 evidence_spans 规整为 quote 字符串列表。

    Reader 输出的 span 可能是 {quote, page, ...} dict, 也可能是裸字符串, 这里统一兼容。
    """
    out: list[str] = []
    for s in spans or []:
        if isinstance(s, dict):
            q = (s.get("quote") or "").strip()
        else:
            q = str(s).strip()
        if q:
            out.append(q)
    return out


@tool
def cluster_cards(paper_ids: list = None) -> dict:
    """把论文卡片按 method_family 聚类, 供建图。Synthesizer 使用。

    确定性逻辑: 读黑板卡片的 method_family 字段归簇 (不依赖 LLM, 杜绝幻觉)。
    paper_ids 为空时默认对黑板上全部卡片聚类。
    """
    from core.run_context import get_active_blackboard

    bb = get_active_blackboard()
    if bb is None:
        return {"clusters": [], "note": "无活动任务"}
    ids = paper_ids or list(bb.cards.keys())
    clusters: dict[str, dict] = {}
    for pid in ids:
        card = bb.cards.get(pid)
        if not card:
            continue
        fam = card.method_family or "未分类"
        key = _slug(fam)
        clusters.setdefault(key, {"label": fam, "members": []})["members"].append(pid)
    return {"clusters": [
        {"id": f"family::{k}", "label": v["label"], "members": v["members"]}
        for k, v in clusters.items()
    ]}


@tool
def build_graph(topic: str) -> dict:
    """基于卡片的 method_family 聚类与 opposes 关系构建立场图谱, 写回黑板。

    确定性建图 (防幻觉):
    - 节点: 每个 method_family 簇 + 每篇论文。
    - 边: 依据卡片 opposes 字段连 opposes 边; 同簇内论文连 supports 边。
      每条边写 rationale, 并尽量从被指向论文的 evidence_spans 取证据。
    """
    from core.run_context import get_active_blackboard
    from core.blackboard import StanceGraph, StanceNode, StanceEdge

    bb = get_active_blackboard()
    if bb is None or not bb.cards:
        return {"nodes": [], "edges": [], "note": "无卡片可建图"}

    cl = cluster_cards(list(bb.cards.keys()))["clusters"]
    nodes: list[StanceNode] = []
    edges: list[StanceEdge] = []

    # 1) 方法流派节点 + 论文节点
    for c in cl:
        nodes.append(StanceNode(id=c["id"], label=c["label"], type="method_family",
                                members=c["members"]))
    for pid, card in bb.cards.items():
        nodes.append(StanceNode(id=pid, label=card.title or pid, type="paper"))

    # 论文 -> 所属流派的归属边 (belongs_to 用 supports 表达同阵营)
    for c in cl:
        for pid in c["members"]:
            edges.append(StanceEdge(source=pid, target=c["id"], relation="supports",
                                    rationale=f"{bb.cards[pid].title or pid} 属于 {c['label']} 流派"))

    # 2) opposes 边: 解析每张卡片的 opposes 字段, 匹配到具体论文或流派
    title_to_id = {(card.title or "").lower(): pid for pid, card in bb.cards.items() if card.title}
    label_to_id = {n.label.lower(): n.id for n in nodes if n.type == "method_family"}

    for pid, card in bb.cards.items():
        for opp in card.opposes or []:
            opp_l = str(opp).lower().strip()
            target = None
            for t, tid in title_to_id.items():  # 先尝试匹配论文标题
                if opp_l and (opp_l in t or t in opp_l):
                    target = tid
                    break
            if target is None:  # 再尝试匹配流派
                for lab, lid in label_to_id.items():
                    if opp_l and (opp_l in lab or lab in opp_l):
                        target = lid
                        break
            ev = _spans_to_quotes(card.evidence_spans)[:1]
            edges.append(StanceEdge(
                source=pid,
                target=target or f"viewpoint::{_slug(str(opp))}",
                relation="opposes",
                rationale=f"{card.title or pid} 在卡片中声明与「{opp}」对立",
                evidence=ev,
            ))
            if target is None:  # 外部观点节点 (库内无对应论文)
                nodes.append(StanceNode(id=f"viewpoint::{_slug(str(opp))}",
                                        label=str(opp), type="viewpoint"))

    # 去重节点 (viewpoint 可能重复)
    seen: set[str] = set()
    uniq_nodes = [n for n in nodes if not (n.id in seen or seen.add(n.id))]

    graph = StanceGraph(topic=topic, nodes=uniq_nodes, edges=edges)
    graph.gaps = detect_gaps(topic)
    bb.graph = graph  # 写回黑板 (确定性结果, 非 LLM 生成)
    return {"nodes": len(uniq_nodes), "edges": len(edges), "gaps": len(graph.gaps),
            "note": "图谱已写回黑板"}


@tool
def detect_gaps(topic: str) -> list:
    """基于卡片识别研究空白 (确定性启发式)。Synthesizer 使用。

    启发式: 1) 只被一篇论文采用的孤立方法流派; 2) 卡片中频繁出现但未被解决的
    limitations; 3) 存在对立但无演进 (evolves_to) 衔接的争议点。
    """
    from core.run_context import get_active_blackboard
    from collections import Counter

    bb = get_active_blackboard()
    if bb is None or not bb.cards:
        return []
    gaps: list[str] = []

    # 1) 孤立流派 (仅一篇论文)
    fam_count: Counter = Counter(c.method_family or "未分类" for c in bb.cards.values())
    for fam, n in fam_count.items():
        if n == 1 and fam != "未分类":
            gaps.append(f"方法流派「{fam}」仅有单篇代表, 缺乏横向对比与复现验证。")

    # 2) 高频未解决的局限
    lim_count: Counter = Counter()
    for c in bb.cards.values():
        for lim in c.limitations or []:
            lim_count[str(lim).strip()] += 1
    for lim, n in lim_count.most_common(3):
        if n >= 2 and lim:
            gaps.append(f"多篇论文共同指出但未解决的局限: {lim}")

    # 3) 有对立无演进
    if any(c.opposes for c in bb.cards.values()):
        gaps.append("存在方法/观点对立, 但卡片中尚无明确的演进 (evolves_to) 路径衔接, 是潜在的研究空白。")

    return gaps or ["卡片信息有限, 未能自动识别明确研究空白。"]


@tool
def export_md(title: str, content: str) -> str:
    """把综述初稿导出为 Markdown 文件, 返回路径。写操作。

    落盘后把路径登记到当前任务黑板的 artifacts, 便于产物追踪。
    """
    from pathlib import Path
    from config import settings
    from core.run_context import get_active_blackboard

    settings.ensure_dirs()
    safe = _slug(title)[:80] or "review"
    path = settings.storage_dir / f"{safe}.md"
    Path(path).write_text(content, encoding="utf-8")
    bb = get_active_blackboard()
    if bb is not None and str(path) not in bb.artifacts:
        bb.artifacts.append(str(path))
    return str(path)


@tool
def export_graph_html(topic: str, graph_json: str) -> str:
    """把立场图谱导出为静态 HTML (可视化)。写操作。

    TODO(Trae): 用 vis-network / mermaid 渲染 graph_json。
    """
    from pathlib import Path
    from config import settings

    settings.ensure_dirs()
    path = settings.storage_dir / f"{topic}_graph.html"
    Path(path).write_text(f"<!-- TODO 可视化 -->\n<pre>{graph_json}</pre>", encoding="utf-8")
    return str(path)
