"""FastAPI 接口 (预留)。任务异步执行, 接口立即返回 task_id。

启动 (本地调试):  uvicorn interfaces.api:app --reload
注意: 项目约定不在沙箱环境起服务; 本文件仅为接口契约骨架。
"""
from __future__ import annotations

import uuid

from fastapi import BackgroundTasks, FastAPI
from pydantic import BaseModel

import tools  # noqa: F401  注册 tool
from core.blackboard import Blackboard
from core.harness import load_checkpoint
from agents.orchestrator import Orchestrator

app = FastAPI(title="ScholarStance API")


class MapRequest(BaseModel):
    topic: str


class IngestRequest(BaseModel):
    directory: str


def _run_map(task_id: str, topic: str) -> None:
    bb = Blackboard(task_id=task_id, topic=topic)
    Orchestrator().run(bb)
    # TODO(Trae): 完成后可触发飞书 Webhook 推送 (见 interfaces/feishu.py)


@app.post("/tasks")
def create_task(req: MapRequest, bg: BackgroundTasks):
    task_id = uuid.uuid4().hex[:8]
    bg.add_task(_run_map, task_id, req.topic)
    return {"task_id": task_id, "status": "accepted"}


@app.get("/tasks/{task_id}")
def get_task(task_id: str):
    bb = load_checkpoint(task_id)
    if not bb:
        return {"error": "not found"}
    return {
        "task_id": bb.task_id, "topic": bb.topic, "status": bb.status,
        "cards": len(bb.cards), "artifacts": bb.artifacts,
    }


@app.post("/ingest")
def ingest(req: IngestRequest):
    from rag.ingest import ingest_dir
    return ingest_dir(req.directory)


@app.post("/ask")
def ask(q: str):
    from tools import rag_query
    return {"hits": rag_query(q, top_k=5)}
