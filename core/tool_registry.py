"""Tool 注册系统: @tool 装饰器自动登记 + 从签名/docstring 生成 JSON Schema。

借鉴 Claude Code: Agent 只能通过注册的 Tool 影响世界。
用法:
    @tool
    def rag_query(query: str, top_k: int = 8) -> list:
        '''检索本地库。'''
        ...

    schemas = registry.openai_schemas()      # 喂给 LLM 的 tools 参数
    result  = registry.call("rag_query", {"query": "..."})
"""
from __future__ import annotations

import inspect
from dataclasses import dataclass
from typing import Any, Callable, get_type_hints

_PY_TO_JSON = {
    str: "string",
    int: "integer",
    float: "number",
    bool: "boolean",
    list: "array",
    dict: "object",
}


@dataclass
class ToolSpec:
    name: str
    func: Callable
    description: str
    schema: dict


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}

    def register(self, func: Callable) -> Callable:
        name = func.__name__
        doc = (func.__doc__ or "").strip()
        schema = _build_schema(func, doc)
        self._tools[name] = ToolSpec(name=name, func=func, description=doc, schema=schema)
        return func

    def openai_schemas(self, names: list[str] | None = None) -> list[dict]:
        """返回 OpenAI tools 格式。names 为空则返回全部。"""
        specs = self._tools.values() if names is None else [self._tools[n] for n in names]
        return [{"type": "function", "function": s.schema} for s in specs]

    def call(self, name: str, arguments: dict[str, Any]) -> Any:
        if name not in self._tools:
            raise KeyError(f"未注册的 tool: {name}")
        return self._tools[name].func(**arguments)

    def list_names(self) -> list[str]:
        return list(self._tools)


def _build_schema(func: Callable, doc: str) -> dict:
    sig = inspect.signature(func)
    try:
        hints = get_type_hints(func)
    except Exception:
        hints = {}
    props: dict[str, Any] = {}
    required: list[str] = []
    for pname, param in sig.parameters.items():
        if pname in ("self", "cls"):
            continue
        ptype = hints.get(pname, str)
        json_type = _PY_TO_JSON.get(_strip_optional(ptype), "string")
        props[pname] = {"type": json_type}
        if param.default is inspect.Parameter.empty:
            required.append(pname)
    return {
        "name": func.__name__,
        "description": doc.split("\n")[0] if doc else func.__name__,
        "parameters": {"type": "object", "properties": props, "required": required},
    }


def _strip_optional(tp: Any) -> Any:
    """把 Optional[X] / X | None 还原为 X。"""
    import typing

    if typing.get_origin(tp) in (typing.Union, getattr(__import__("types"), "UnionType", None)):
        args = [a for a in typing.get_args(tp) if a is not type(None)]
        if args:
            return args[0]
    origin = typing.get_origin(tp)
    return origin or tp


# 全局单例
registry = ToolRegistry()


def tool(func: Callable) -> Callable:
    """装饰器: 注册到全局 registry。"""
    return registry.register(func)
