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
    若处于 Reader 精读上下文且本次未显式传 paper_id, 会自动注入当前精读的 paper_id
    (兜底防串味: 模型偶尔漏传参时仍锁定目标论文, 而非报错打断)。
    """
    from rag.retrieve import hybrid_search
    from core.run_context import get_reader_paper_id

    pid = paper_id or None
    if pid is None:
        injected = get_reader_paper_id()
        if injected:
            pid = injected
            from core.obs import log_event
            log_event("rag_query.inject_paper_id", paper_id=pid)
    ymin = year_min or None
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
    """把研究方向扩展为同义/相关检索词, 提高召回。Retriever 使用。

    用 LLM 围绕给定 topic 动态生成 5-8 个检索查询 (中文表述 + 英文术语 + 核心方法词),
    严格约束不得偏离原主题、不得引入无关领域词 (防主题漂移)。生成失败或为空时回退
    为 [topic]; 结果强制包含原 topic 并去重。绝不硬编码任何示例主题词。
    """
    topic = (topic or "").strip()
    if not topic:
        return []
    from core.llm import get_llm
    from core.obs import log_event

    sys = (
        "你是学术检索查询扩展助手。针对用户给定的研究方向, 生成 5-8 个用于本地论文库"
        "检索的查询词/短语, 要求: 覆盖中文表述、对应英文术语、核心方法名; 每条都必须"
        "紧扣原方向, 严禁引入与原方向无关的领域或泛化主题 (防止主题漂移)。"
        "只输出一个 JSON 字符串数组, 不要任何解释。"
    )
    try:
        text = get_llm().chat_text(
            [{"role": "system", "content": sys},
             {"role": "user", "content": f"研究方向: {topic}"}],
            temperature=0.2,
        )
        import json as _json
        import re as _re
        m = _re.search(r"\[.*\]", text, _re.DOTALL)
        arr = _json.loads(m.group(0)) if m else []
        queries = [str(q).strip() for q in arr if isinstance(q, (str, int, float)) and str(q).strip()]
    except Exception as exc:  # noqa: BLE001  扩展失败不应阻断检索, 降级为原 topic
        log_event("expand_query.fallback", level="WARNING", error=str(exc))
        queries = []

    # 强制包含原 topic, 去重保序, 控制上限。
    out: list[str] = []
    seen: set[str] = set()
    for q in [topic, *queries]:
        key = q.lower()
        if key not in seen:
            seen.add(key)
            out.append(q)
    return out[:8] if len(out) > 1 else [topic]


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

    # 2) opposes 边: 解析每张卡片的 opposes 字段, 匹配到具体论文或流派。
    #    匹配不到库内真实节点的 opposes 直接丢弃 (不再生成"一句话外部观点"噪声节点),
    #    确保每条对立边都连接图中真实存在的论文/流派, 保持图谱干净可解读。
    title_to_id = {(card.title or "").lower(): pid for pid, card in bb.cards.items() if card.title}
    label_to_id = {n.label.lower(): n.id for n in nodes if n.type == "method_family"}

    for pid, card in bb.cards.items():
        for opp in card.opposes or []:
            opp_l = str(opp).lower().strip()
            if not opp_l:
                continue
            target = None
            for t, tid in title_to_id.items():  # 先尝试匹配论文标题
                if tid != pid and (opp_l in t or t in opp_l):
                    target = tid
                    break
            if target is None:  # 再尝试匹配流派
                for lab, lid in label_to_id.items():
                    if opp_l in lab or lab in opp_l:
                        target = lid
                        break
            if target is None:  # 匹配不到库内真实节点 -> 丢弃, 不制造噪声
                continue
            ev = _spans_to_quotes(card.evidence_spans)[:1]
            tgt_label = next((n.label for n in nodes if n.id == target), target)
            edges.append(StanceEdge(
                source=pid,
                target=target,
                relation="opposes",
                rationale=f"{card.title or pid} 与「{tgt_label}」在方法路线上存在分歧",
                evidence=ev,
            ))

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

    # 1) 孤立流派 (仅一篇论文): 聚合为一条, 避免每篇各自成派时刷屏。
    fam_count: Counter = Counter(c.method_family or "未分类" for c in bb.cards.values())
    solo = [fam for fam, n in fam_count.items() if n == 1 and fam != "未分类"]
    if len(solo) == 1:
        gaps.append(f"方法流派「{solo[0]}」仅有单篇代表, 缺乏横向对比与复现验证。")
    elif len(solo) > 1:
        gaps.append(
            f"共有 {len(solo)} 个方法流派各仅一篇代表 ({', '.join(solo)}), "
            f"流派间缺乏横向对比与复现验证, 整体呈现「百花齐放但少交叉印证」的格局。"
        )

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
def export_graph_html(topic: str) -> str:
    """把黑板上的立场图谱渲染为可交互 HTML (vis-network)。写操作。

    确定性可视化: 直接读黑板 bb.graph (build_graph 已写回的真实结果), 不让 LLM
    生成图结构。节点按类型着色 (流派/论文/外部观点), 边按关系着色 (opposes 红/
    supports 绿), 悬停显示 rationale 与证据。落盘后登记到 bb.artifacts。
    """
    import html
    import json
    from pathlib import Path
    from config import settings
    from core.run_context import get_active_blackboard

    bb = get_active_blackboard()
    graph = getattr(bb, "graph", None) if bb is not None else None
    if graph is None or not graph.nodes:
        return "[无图谱可导出: 请先 build_graph]"

    color_by_type = {"method_family": "#4C9AFF", "paper": "#79F2C0",
                     "viewpoint": "#FFAB00"}
    shape_by_type = {"method_family": "diamond", "paper": "dot", "viewpoint": "triangle"}
    nodes = []
    for n in graph.nodes:
        title_bits = [f"类型: {n.type}"]
        if n.members:
            mem = ", ".join((bb.cards[m].title or m) if (bb and m in bb.cards) else m
                            for m in n.members)
            title_bits.append(f"成员: {mem}")
        nodes.append({
            "id": n.id, "label": n.label,
            "color": color_by_type.get(n.type, "#C1C7D0"),
            "shape": shape_by_type.get(n.type, "dot"),
            "title": " | ".join(title_bits),
        })

    edge_color = {"opposes": "#FF5630", "supports": "#36B37E",
                  "extends": "#6554C0", "evolves_to": "#00B8D9"}
    edges = []
    for e in graph.edges:
        tip = e.rationale or e.relation
        if e.evidence:
            tip += "\n证据: " + " / ".join(str(x) for x in e.evidence[:2])
        edges.append({
            "from": e.source, "to": e.target, "label": e.relation,
            "arrows": "to", "color": {"color": edge_color.get(e.relation, "#8993A4")},
            "title": tip, "font": {"size": 10, "align": "middle"},
        })

    gaps_html = "".join(f"<li>{html.escape(str(g))}</li>" for g in (graph.gaps or []))
    page = """<!DOCTYPE html>
<html lang="zh"><head><meta charset="utf-8"/>
<title>立场图谱 · __TOPIC__</title>
<script src="https://unpkg.com/vis-network/standalone/umd/vis-network.min.js"></script>
<style>
  body{margin:0;font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;background:#0d1117;color:#c9d1d9}
  header{padding:12px 20px;border-bottom:1px solid #21262d}
  h1{font-size:18px;margin:0}
  #net{width:100%;height:72vh;border-bottom:1px solid #21262d}
  .legend{font-size:12px;color:#8b949e;padding:8px 20px}
  .legend span{margin-right:16px}
  .gaps{padding:8px 20px}
  .gaps h2{font-size:14px}
  .dot{display:inline-block;width:10px;height:10px;border-radius:50%;margin-right:4px;vertical-align:middle}
</style></head>
<body>
<header><h1>立场图谱 · __TOPIC__</h1></header>
<div class="legend">
  <span><i class="dot" style="background:#4C9AFF"></i>方法流派</span>
  <span><i class="dot" style="background:#79F2C0"></i>论文</span>
  <span><i class="dot" style="background:#FFAB00"></i>外部观点</span>
  <span style="color:#FF5630">— opposes</span>
  <span style="color:#36B37E">— supports</span>
</div>
<div id="net"></div>
<div class="gaps"><h2>研究空白</h2><ul>__GAPS__</ul></div>
<script>
  const nodes=new vis.DataSet(__NODES__);
  const edges=new vis.DataSet(__EDGES__);
  new vis.Network(document.getElementById('net'),{nodes,edges},{
    nodes:{font:{color:'#c9d1d9'}},
    physics:{stabilization:true,barnesHut:{springLength:160}},
    interaction:{hover:true,tooltipDelay:120}
  });
</script>
</body></html>"""
    page = (page.replace("__TOPIC__", html.escape(topic or graph.topic or ""))
                .replace("__NODES__", json.dumps(nodes, ensure_ascii=False))
                .replace("__EDGES__", json.dumps(edges, ensure_ascii=False))
                .replace("__GAPS__", gaps_html or "<li>（未识别）</li>"))

    settings.ensure_dirs()
    path = settings.storage_dir / f"{_slug(topic or graph.topic)}_graph.html"
    Path(path).write_text(page, encoding="utf-8")
    if bb is not None and str(path) not in bb.artifacts:
        bb.artifacts.append(str(path))
    return str(path)


@tool
def fs_list_dir(root_dir: str, sub_path: str = ".") -> list:
    """通过 MCP filesystem server 列出目录内容 (协议化能力接入示例)。

    经官方 @modelcontextprotocol/server-filesystem 访问, root_dir 为授权根目录,
    sub_path 为其下相对/绝对子路径。需本机已安装 node/npx。
    """
    from mcp_clients import mcp_list_dir

    try:
        return mcp_list_dir(root_dir, sub_path)
    except Exception as exc:  # noqa: BLE001
        return [f"[mcp error] {exc}"]


@tool
def fs_read_file(root_dir: str, file_path: str) -> str:
    """通过 MCP filesystem server 读取文本文件内容 (协议化能力接入示例)。

    经官方 filesystem-mcp 在授权 root_dir 内读取 file_path。需本机已安装 node/npx。
    """
    from mcp_clients import mcp_read_file

    try:
        return mcp_read_file(root_dir, file_path)
    except Exception as exc:  # noqa: BLE001
        return f"[mcp error] {exc}"
