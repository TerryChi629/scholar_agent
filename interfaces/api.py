"""FastAPI 接口。任务异步执行, 接口立即返回 task_id。

启动 (本地调试):  uvicorn interfaces.api:app --reload
注意: 项目约定不在沙箱环境起服务; 本文件仅为接口契约骨架。
"""
from __future__ import annotations

import time
import uuid

from fastapi import BackgroundTasks, FastAPI, HTTPException, Query
from pydantic import BaseModel

import tools  # noqa: F401  注册 tool
from core.blackboard import Blackboard, Status
from core.harness import list_tasks, load_checkpoint, save_checkpoint
from core.obs import log_event
from agents.orchestrator import Orchestrator

app = FastAPI(title="ScholarStance API")
_started_at = time.time()


@app.get("/")
def index():
    """前端控制台单页 (web/index.html, FastAPI 直接托管, 零构建链)。"""
    from pathlib import Path
    from fastapi.responses import HTMLResponse, PlainTextResponse

    page = Path(__file__).resolve().parent.parent / "web" / "index.html"
    if not page.is_file():
        return PlainTextResponse("前端页面缺失: web/index.html", status_code=404)
    return HTMLResponse(page.read_text(encoding="utf-8"))


class MapRequest(BaseModel):
    topic: str


class IngestRequest(BaseModel):
    directory: str


class ChatRequest(BaseModel):
    question: str
    task_id: str | None = None
    session_id: str | None = None


class RenameSessionRequest(BaseModel):
    title: str


def _run_map(task_id: str, topic: str) -> None:
    bb = Blackboard(task_id=task_id, topic=topic)
    try:
        Orchestrator().run(bb)
    except Exception as exc:  # noqa: BLE001  失败要落盘为 FAILED, 供查询感知
        bb.status = Status.FAILED.value
        bb.critic_feedback.append({"type": "error", "detail": str(exc)})
        save_checkpoint(bb)
        log_event("task.failed", level="ERROR", task_id=task_id, error=str(exc))


@app.post("/tasks")
def create_task(req: MapRequest, bg: BackgroundTasks):
    task_id = uuid.uuid4().hex[:8]
    bg.add_task(_run_map, task_id, req.topic)
    log_event("task.accepted", task_id=task_id, topic=req.topic)
    return {"task_id": task_id, "status": "accepted"}


@app.get("/tasks")
def list_all_tasks(
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
):
    """分页列出任务 (按更新时间倒序)。"""
    rows = list_tasks()
    total = len(rows)
    page = rows[offset:offset + limit]
    return {
        "total": total, "limit": limit, "offset": offset,
        "items": [{"task_id": t, "topic": top, "status": st} for t, top, st in page],
    }


@app.get("/tasks/{task_id}")
def get_task(task_id: str):
    bb = load_checkpoint(task_id)
    if not bb:
        raise HTTPException(status_code=404, detail="task not found")
    done = bb.status in (Status.DONE.value, Status.FAILED.value)
    error = next((f["detail"] for f in bb.critic_feedback if f.get("type") == "error"), None)
    return {
        "task_id": bb.task_id, "topic": bb.topic, "status": bb.status,
        "done": done, "error": error,
        "cards": len(bb.cards),
        "nodes": len(bb.graph.nodes) if bb.graph else 0,
        "artifacts": bb.artifacts,
        "usage": bb.usage,
        "plan": [
            {"name": s.name, "status": s.status, "note": s.note}
            for s in bb.plan
        ],
        "graph_events": bb.graph_events[-80:],
        "critic_feedback": bb.critic_feedback[-10:],
    }


@app.post("/ingest")
def ingest(req: IngestRequest):
    from rag.ingest import ingest_dir
    return ingest_dir(req.directory)


@app.get("/papers")
def list_papers():
    """库内全部论文 (标题/年份/片段数 + 库统计), 供前端论文库管理页。"""
    from rag.store import get_store
    store = get_store()
    papers = store.list_papers()
    chunk_cnt: dict[str, int] = {}
    for c in store.all_chunks():
        if c.paper_id:
            chunk_cnt[c.paper_id] = chunk_cnt.get(c.paper_id, 0) + 1
    items = [
        {"paper_id": pid, "title": meta.get("title", "") or pid,
         "year": meta.get("year", 0) or 0, "chunks": chunk_cnt.get(pid, 0)}
        for pid, meta in papers.items()
    ]
    items.sort(key=lambda p: (-(p["year"] or 0), p["title"]))
    return {"total": len(items), "chunks": store.count(), "items": items}


@app.delete("/papers/{paper_id}")
def delete_paper(paper_id: str):
    """删除某篇论文的全部 chunk, 并重建 BM25 索引保持同步。"""
    from rag.store import get_store
    removed = get_store().delete_paper(paper_id)
    if not removed:
        raise HTTPException(status_code=404, detail="paper not found")
    try:
        from rag import bm25_index
        bm25_index.reset()  # 语料已变, 清索引下次查询时按新库重建
    except Exception as exc:  # noqa: BLE001  索引重置失败不阻断删除
        log_event("papers.bm25_reset_failed", level="WARNING", error=str(exc))
    log_event("papers.deleted", paper_id=paper_id, chunks=removed)
    return {"ok": True, "paper_id": paper_id, "removed_chunks": removed}


@app.post("/ask")
def ask(q: str):
    from tools import rag_query
    return {"hits": rag_query(q, top_k=5)}


@app.post("/chat")
def chat(req: ChatRequest):
    """对话式 RAG (M8): 检索 + 记忆注入 + 一次合成。返回 ChatResult。"""
    from chat import ChatAgent
    res = ChatAgent().answer(req.question, task_id=req.task_id, session_id=req.session_id)
    return res.to_dict()


@app.get("/chat/sessions")
def list_chat_sessions(limit: int = Query(10, ge=1, le=10)):
    """列出历史会话 (标题/轮数/累计 token), 供前端历史侧栏。"""
    from chat import session as session_mod
    session_mod.prune_old_sessions(keep=10)
    items = session_mod.list_sessions(limit=limit)
    return {
        "total": len(items),
        "total_tokens": sum(i["total_tokens"] for i in items),
        "items": items,
    }


@app.get("/chat/sessions/{session_id}")
def get_chat_session(session_id: str):
    """返回单个会话完整对话记录 (含每轮 token), 供回看历史。"""
    from chat import session as session_mod
    data = session_mod.get_session(session_id)
    if not data:
        raise HTTPException(status_code=404, detail="session not found")
    return data


@app.patch("/chat/sessions/{session_id}")
def rename_chat_session(session_id: str, req: RenameSessionRequest):
    """重命名历史会话。"""
    from chat import session as session_mod
    if not session_mod.rename_session(session_id, req.title):
        raise HTTPException(status_code=404, detail="session not found")
    return session_mod.get_session(session_id)


@app.delete("/chat/sessions/{session_id}")
def delete_chat_session(session_id: str):
    """删除历史会话及其 turns。"""
    from chat import session as session_mod
    if not session_mod.delete_session(session_id):
        raise HTTPException(status_code=404, detail="session not found")
    return {"ok": True, "session_id": session_id}


@app.post("/digest/run")
def digest_run(dry_run: bool = Query(False)):
    """每日论文速递: 画像 -> arXiv 拉新 -> 排序去重 -> 飞书推送 (M12)。

    供 cron 定时调用 (见 README/部署示例)。dry_run=true 时只返回结果不推送/落库。
    """
    from digest.runner import run_digest
    result = run_digest(dry_run=dry_run)
    log_event("digest.api.run", dry_run=dry_run, ok=result.get("ok"),
              groups=len(result.get("groups", [])))
    return result


@app.get("/digest/deepdive")
def digest_deepdive(topic: str = Query(...), bg: BackgroundTasks = None):
    """飞书速递卡片「一键深度综述」按钮入口: 基于该主题发起完整综述任务。

    返回任务详情页 URL (跳转前端), 让用户点完按钮即可看到任务进度。
    """
    from fastapi.responses import RedirectResponse

    task_id = uuid.uuid4().hex[:8]
    bg.add_task(_run_map, task_id, topic)
    log_event("digest.api.deepdive", task_id=task_id, topic=topic)
    return RedirectResponse(url=f"/?task={task_id}", status_code=303)


@app.get("/digest/history")
def digest_history(limit: int = Query(100, ge=1, le=500)):
    """历史推送记录 (供前端速递工作区「推送记录」页)。"""
    from digest import store
    items = store.list_pushed(limit=limit)
    return {"total": len(items), "items": items}


@app.get("/digest/profile")
def digest_profile():
    """我的兴趣画像 (主题 + 来源分布 + 权重最高信号), 供前端可视化。"""
    from digest.profile import profile_summary
    return profile_summary()


class DigestSearchRequest(BaseModel):
    query: str
    days_back: int | None = None


@app.post("/digest/search")
def digest_search(req: DigestSearchRequest):
    """主动检索 arXiv 最新论文 (供前端「主动检索」页)。"""
    from digest.arxiv_client import search_arxiv_papers
    from config import settings as _s
    cats = [c.strip() for c in _s.digest_arxiv_categories.split(",") if c.strip()]
    papers = search_arxiv_papers(
        req.query, max_results=_s.digest_fetch_per_topic,
        categories=cats or None, days_back=req.days_back,
    )
    return {"query": req.query, "total": len(papers),
            "items": [p.to_dict() for p in papers[:20]]}


class InterestRequest(BaseModel):
    text: str


@app.get("/digest/interests")
def digest_interests_list():
    """列出用户自定义兴趣 (会并入每日画像)。"""
    from digest import store
    return {"items": store.list_interests()}


@app.post("/digest/interests")
def digest_interests_add(req: InterestRequest):
    """自然语言新增一条自定义兴趣 (高权重并入画像)。"""
    from digest import store
    text = (req.text or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="interest text is empty")
    item = store.add_interest(text)
    log_event("digest.api.interest_add", text=text[:60])
    return item


@app.delete("/digest/interests/{interest_id}")
def digest_interests_delete(interest_id: str):
    """删除一条自定义兴趣。"""
    from digest import store
    if not store.delete_interest(interest_id):
        raise HTTPException(status_code=404, detail="interest not found")
    return {"ok": True, "id": interest_id}


@app.get("/healthz")
def healthz():
    """健康检查: 进程存活 + 向量库可达 (供探活/负载均衡)。"""
    try:
        from rag.store import get_store
        chunks = get_store().count()
        db_ok = True
    except Exception as exc:  # noqa: BLE001
        chunks, db_ok = -1, False
        log_event("healthz.degraded", level="WARNING", error=str(exc))
    return {
        "status": "ok" if db_ok else "degraded",
        "uptime_s": round(time.time() - _started_at, 1),
        "chunks": chunks,
    }


@app.get("/artifacts/{name}")
def get_artifact(name: str):
    """B1: 静态托管 storage 下的产物 (md/html), 供飞书卡片按钮点开。

    仅允许访问 storage_dir 直下文件, 解析真实路径后校验父目录, 防目录穿越。
    """
    from fastapi.responses import FileResponse, PlainTextResponse
    from config import settings

    base = settings.storage_dir.resolve()
    target = (base / name).resolve()
    if base not in target.parents or not target.is_file():
        raise HTTPException(status_code=404, detail="artifact not found")
    # .md 用纯文本内联展示 (浏览器直接读), .html 用文件响应 (浏览器渲染)
    if target.suffix.lower() == ".md":
        return PlainTextResponse(target.read_text(encoding="utf-8"))
    media = "text/html" if target.suffix.lower() == ".html" else None
    return FileResponse(str(target), media_type=media)
