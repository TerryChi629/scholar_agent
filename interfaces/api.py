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
    }


@app.post("/ingest")
def ingest(req: IngestRequest):
    from rag.ingest import ingest_dir
    return ingest_dir(req.directory)


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
