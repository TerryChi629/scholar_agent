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


def _short_label(title: str, max_len: int = 18) -> str:
    """把长论文标题确定性地截短为图谱节点短标识 (不调 LLM)。仅作 LLM 取名失败时的兜底。

    规则 (按优先级):
    1) 形如 "OneRec: xxx" / "HSTU - xxx" 的, 取冒号/破折号前的缩写部分。
    2) 否则取首个 token; 过长再按 max_len 截断加省略号。
    完整标题仍保留在节点 tooltip 中, 短标识只为画布可读。
    """
    t = (title or "").strip()
    if not t:
        return "?"
    import re
    # 1) 冒号 / 破折号 前的简称 (常见论文命名: "Name: full title")
    head = re.split(r"[:：\-—]", t, maxsplit=1)[0].strip()
    if head and head != t and len(head) <= max_len:
        return head
    # 2) 标题本身够短直接用
    if len(t) <= max_len:
        return t
    # 3) 取首词; 仍超长则硬截断
    first = t.split()[0]
    if len(first) <= max_len:
        return first
    return t[:max_len].rstrip() + "…"


def _short_labels(titles: list[str]) -> dict[str, str]:
    """为一批论文标题生成简短、可辨识的图谱节点名 (LLM, low 档, 带持久化缓存)。

    省 token 策略: 按标题持久化缓存 (SQLite), 同一标题只烧一次; 整批未命中的一次
    性请求 (一轮对话出全部)。LLM 不可用 / 解析失败时逐条回退确定性 _short_label。
    返回 {原始标题: 短名}。
    """
    from core.label_cache import get_cached, put_cached

    uniq = [t for t in {(t or "").strip() for t in titles} if t]
    result: dict[str, str] = {}
    miss: list[str] = []
    for t in uniq:
        cached = get_cached(t)
        if cached:
            result[t] = cached
        else:
            miss.append(t)

    if miss:
        llm_named = _llm_name_titles(miss)
        new_pairs = []
        for t in miss:
            name = (llm_named.get(t) or "").strip() or _short_label(t)
            result[t] = name
            new_pairs.append((t, name))
        put_cached(new_pairs)
    return result


def _llm_name_titles(titles: list[str]) -> dict[str, str]:
    """调 LLM (low 档) 为论文标题批量起短名。失败返回空 dict (调用方兜底)。"""
    import json
    from config import settings
    from core.llm import get_llm
    from core.obs import log_event

    provider, model, _, _ = settings.resolve_agent_model("retriever")  # 复用 low 档(最便宜)
    numbered = "\n".join(f"{i+1}. {t}" for i, t in enumerate(titles))
    prompt = (
        "你是论文图谱的标注助手。下面是若干论文标题, 请为每篇起一个简短、可辨识的中文/英文短名, "
        "用于知识图谱节点显示。要求: 优先用论文公认简称 (如 TIGER、HSTU、OneRec); 无公认简称时用 "
        "2-6 字概括其核心方法 (如「流式向量量化检索」「序列转导推荐」); 每个短名不超过 12 个字符, "
        "禁止照抄完整标题。仅输出 JSON 对象, key 为序号字符串, value 为短名, 不要其他文字。\n\n"
        f"{numbered}"
    )
    try:
        text = get_llm().chat_text(
            [{"role": "user", "content": prompt}],
            temperature=0.2, provider=provider, model=model,
        )
        # 容错: 剥离可能的 ```json 包裹
        s = text.strip()
        if s.startswith("```"):
            s = s.split("```", 2)[1] if "```" in s[3:] else s.strip("`")
            s = s[4:] if s.lower().startswith("json") else s
        data = json.loads(s)
        out: dict[str, str] = {}
        for i, t in enumerate(titles):
            v = data.get(str(i + 1))
            if isinstance(v, str) and v.strip():
                out[t] = v.strip()[:16]
        return out
    except Exception as exc:  # noqa: BLE001
        log_event("graph.label_llm_failed", level="WARNING", error=str(exc))
        return {}


def _spans_to_quotes(spans) -> list:
    """把 evidence_spans 规整为 quote 字符串列表。

    Reader 输出的 span 可能是 {quote, page, ...} / {text, source, ...} dict,
    也可能是裸字符串, 这里统一兼容。历史任务里曾出现 text=原文、quote=中文译文
    或只有 text 的情况, 回查应优先使用可回溯原文。
    """
    out: list[str] = []
    for s in spans or []:
        if isinstance(s, dict):
            q = (s.get("quote") or "").strip()
            text = (s.get("text") or "").strip()
            if text and (not q or (_has_cjk(q) and not _has_cjk(text))):
                q = text
        else:
            q = str(s).strip()
        if q:
            out.append(q)
    return out


def _has_cjk(text: str) -> bool:
    import re
    return bool(re.search(r"[\u4e00-\u9fff]", text or ""))


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
    # 论文节点用 LLM 起的短名做 label (避免长标题互相重叠), 全名进 hover tooltip。
    # 批量取名 + 持久化缓存, 同标题不重复烧 token; LLM 失败逐条回退确定性截短。
    paper_titles = [n.label for n in graph.nodes if n.type == "paper" and n.label]
    short_map = _short_labels(paper_titles) if paper_titles else {}
    nodes = []
    for n in graph.nodes:
        is_paper = n.type == "paper"
        label = short_map.get(n.label) or (_short_label(n.label) if is_paper else n.label)
        title_bits = [f"类型: {n.type}"]
        if is_paper and n.label and label != n.label:
            title_bits.insert(0, n.label)  # tooltip 顶部展示完整标题
        if n.members:
            mem = ", ".join((bb.cards[m].title or m) if (bb and m in bb.cards) else m
                            for m in n.members)
            title_bits.append(f"成员: {mem}")
        nodes.append({
            "id": n.id, "label": label,
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
    nodes:{font:{color:'#c9d1d9',size:14,multi:false},
           widthConstraint:{maximum:140},margin:8},
    edges:{smooth:{type:'dynamic'}},
    physics:{stabilization:true,
             barnesHut:{springLength:220,avoidOverlap:0.6,gravitationalConstant:-8000}},
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


# 综述 HTML 模板: 与立场图谱同款暗色主题, 阅读舒适。正文由 markdown 库渲染后注入。
_REVIEW_HTML = """<!DOCTYPE html>
<html lang="zh"><head><meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>综述 · __TITLE__</title>
<style>
  body{margin:0;background:#0d1117;color:#c9d1d9;
       font-family:-apple-system,Segoe UI,Helvetica,Arial,"PingFang SC","Microsoft YaHei",sans-serif;
       line-height:1.8;font-size:16px}
  .wrap{max-width:860px;margin:0 auto;padding:40px 28px 80px}
  h1{font-size:28px;border-bottom:1px solid #21262d;padding-bottom:14px;margin-top:8px}
  h2{font-size:21px;margin-top:36px;color:#79c0ff}
  h3{font-size:17px;margin-top:26px;color:#a5d6ff}
  p{margin:14px 0}
  ul,ol{padding-left:24px}
  li{margin:6px 0}
  strong{color:#e6edf3}
  a{color:#58a6ff}
  blockquote{margin:18px 0;padding:8px 16px;border-left:3px solid #30363d;
             background:#161b22;color:#8b949e;border-radius:0 6px 6px 0}
  code{background:#161b22;padding:2px 6px;border-radius:4px;font-size:14px}
  hr{border:none;border-top:1px solid #21262d;margin:32px 0}
  table{border-collapse:collapse;margin:18px 0;width:100%}
  th,td{border:1px solid #30363d;padding:8px 12px;text-align:left}
  th{background:#161b22}
</style></head>
<body><div class="wrap">
__BODY__
</div></body></html>"""


@tool
def export_review_html(title: str, content: str) -> str:
    """把综述 markdown 正文渲染为带样式的 HTML 文件, 返回路径。写操作。

    确定性渲染: 用 python-markdown 把 LLM 已产出的 markdown 转 HTML, 套暗色阅读主题
    (与立场图谱同款), 不调用 LLM、零额外 token。落盘后登记到 bb.artifacts。
    """
    import markdown as md
    from pathlib import Path
    from config import settings
    from core.run_context import get_active_blackboard

    settings.ensure_dirs()
    safe = _slug(title)[:80] or "review"
    body = md.markdown(content or "", extensions=["extra", "sane_lists", "nl2br"])
    # 取首个 # 标题作为 <title>; 无则用传入 title
    page_title = (title or "综述").strip()
    page = _REVIEW_HTML.replace("__TITLE__", page_title).replace("__BODY__", body)

    path = settings.storage_dir / f"{safe}.html"
    Path(path).write_text(page, encoding="utf-8")
    bb = get_active_blackboard()
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
