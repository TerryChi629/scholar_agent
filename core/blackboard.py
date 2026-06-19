"""核心数据结构 + 黑板 (Blackboard)。

黑板是多 agent 协作的中枢: 所有中间产物写入黑板, 各 agent 读写黑板。
整体可序列化到 SQLite -> 实现断点恢复。
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field, asdict
from enum import Enum


class Status(str, Enum):
    PLANNING = "planning"
    RETRIEVING = "retrieving"
    READING = "reading"
    SYNTHESIZING = "synthesizing"
    REVIEWING = "reviewing"
    DONE = "done"
    FAILED = "failed"


@dataclass
class PaperCard:
    """Reader 的标准产出: 单篇论文精读卡片。"""
    paper_id: str
    title: str = ""
    authors: list[str] = field(default_factory=list)
    year: int = 0
    venue: str | None = None
    core_claim: str = ""
    method: str = ""
    method_family: str = ""
    key_results: list[str] = field(default_factory=list)
    stance_tags: list[str] = field(default_factory=list)
    opposes: list[str] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)
    evidence_spans: list[dict] = field(default_factory=list)  # {quote, page, char_range}


@dataclass
class StanceNode:
    id: str
    label: str
    type: str  # paper | method_family | viewpoint
    members: list[str] = field(default_factory=list)


@dataclass
class StanceEdge:
    source: str
    target: str
    relation: str  # opposes | extends | supports | evolves_to
    rationale: str = ""
    evidence: list[str] = field(default_factory=list)


@dataclass
class StanceGraph:
    topic: str
    nodes: list[StanceNode] = field(default_factory=list)
    edges: list[StanceEdge] = field(default_factory=list)
    gaps: list[str] = field(default_factory=list)


@dataclass
class SubTask:
    name: str
    status: str = "pending"  # pending | running | done | failed
    note: str = ""


@dataclass
class Blackboard:
    task_id: str
    topic: str
    plan: list[SubTask] = field(default_factory=list)
    candidates: list[str] = field(default_factory=list)
    cards: dict[str, PaperCard] = field(default_factory=dict)
    graph: StanceGraph | None = None
    critic_feedback: list[dict] = field(default_factory=list)
    status: str = Status.PLANNING.value
    artifacts: list[str] = field(default_factory=list)
    usage: dict = field(default_factory=dict)  # M4: token/耗时累计 {prompt_tokens, completion_tokens, total_tokens, llm_ms}
    graph_events: list[dict] = field(default_factory=list)  # M11: StateGraph 编排轨迹
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    # —— 序列化 ——
    def to_json(self) -> str:
        self.updated_at = time.time()
        return json.dumps(asdict(self), ensure_ascii=False, default=str)

    @classmethod
    def from_json(cls, raw: str) -> "Blackboard":
        data = json.loads(raw)
        bb = cls(task_id=data["task_id"], topic=data["topic"])
        bb.plan = [SubTask(**t) for t in data.get("plan", [])]
        bb.candidates = data.get("candidates", [])
        bb.cards = {k: PaperCard(**v) for k, v in data.get("cards", {}).items()}
        if data.get("graph"):
            g = data["graph"]
            bb.graph = StanceGraph(
                topic=g["topic"],
                nodes=[StanceNode(**n) for n in g.get("nodes", [])],
                edges=[StanceEdge(**e) for e in g.get("edges", [])],
                gaps=g.get("gaps", []),
            )
        bb.critic_feedback = data.get("critic_feedback", [])
        bb.status = data.get("status", Status.PLANNING.value)
        bb.artifacts = data.get("artifacts", [])
        bb.usage = data.get("usage", {})
        bb.graph_events = data.get("graph_events", [])
        bb.created_at = data.get("created_at", time.time())
        bb.updated_at = data.get("updated_at", time.time())
        return bb
