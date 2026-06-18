"""轻量 StateGraph 编排器。

借鉴 LangGraph 的核心思想: 节点处理共享状态, 边决定下一步流转。
不引入外部运行时, 保持本项目黑板/checkpoint/可观测链路完全可控。
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Generic, TypeVar


StateT = TypeVar("StateT")
NodeFn = Callable[[StateT], None]
RouteFn = Callable[[StateT], str]

START = "__start__"
END = "__end__"


@dataclass(frozen=True)
class _ConditionalEdge(Generic[StateT]):
    route: RouteFn[StateT]
    branches: dict[str, str]


class StateGraph(Generic[StateT]):
    """最小状态图执行器: 显式节点、普通边、条件边、最大步数保护。"""

    def __init__(self) -> None:
        self._nodes: dict[str, NodeFn[StateT]] = {}
        self._edges: dict[str, str] = {}
        self._conditional: dict[str, _ConditionalEdge[StateT]] = {}
        self._entry: str | None = None

    def add_node(self, name: str, fn: NodeFn[StateT]) -> "StateGraph[StateT]":
        if name in (START, END):
            raise ValueError(f"{name} is reserved")
        self._nodes[name] = fn
        return self

    def set_entry_point(self, name: str) -> "StateGraph[StateT]":
        self._assert_node(name)
        self._entry = name
        return self

    def add_edge(self, source: str, target: str) -> "StateGraph[StateT]":
        if source != START:
            self._assert_node(source)
        if target != END:
            self._assert_node(target)
        if source == START:
            self.set_entry_point(target)
        else:
            self._edges[source] = target
        return self

    def add_conditional_edges(
        self,
        source: str,
        route: RouteFn[StateT],
        branches: dict[str, str],
    ) -> "StateGraph[StateT]":
        self._assert_node(source)
        for target in branches.values():
            if target != END:
                self._assert_node(target)
        self._conditional[source] = _ConditionalEdge(route=route, branches=branches)
        return self

    def run(self, state: StateT, *, max_steps: int = 50) -> StateT:
        if not self._entry:
            raise ValueError("StateGraph entry point is not set")
        current = self._entry
        steps = 0
        while current != END:
            if steps >= max_steps:
                raise RuntimeError(f"StateGraph exceeded max_steps={max_steps}")
            self._assert_node(current)
            self._nodes[current](state)
            current = self._next(current, state)
            steps += 1
        return state

    def _next(self, current: str, state: StateT) -> str:
        if current in self._conditional:
            cond = self._conditional[current]
            key = cond.route(state)
            if key not in cond.branches:
                raise RuntimeError(f"StateGraph route '{key}' missing for node '{current}'")
            return cond.branches[key]
        return self._edges.get(current, END)

    def _assert_node(self, name: str) -> None:
        if name not in self._nodes:
            raise ValueError(f"StateGraph node not found: {name}")
