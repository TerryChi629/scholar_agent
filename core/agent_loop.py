"""Agent Loop 引擎 (自研, 不依赖 LangChain/AutoGen)。

标准 ReAct 循环: think -> (tool) act -> observe -> 直到模型给出 final 或达上限。
这是项目的核心, 保持简单可控、每步可观测。
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Callable

from rich.console import Console

from config import settings
from core.harness import compress_context
from core.llm import get_llm
from core.obs import log_event
from core.tool_registry import registry

console = Console()


@dataclass
class LoopResult:
    final_text: str
    rounds: int
    trace: list[dict]  # 每轮的 think/act/observe, 供复盘
    usage: dict | None = None  # token/耗时汇总: {prompt_tokens, completion_tokens, total_tokens, llm_ms}


def _usage_tokens(resp) -> tuple[int, int, int]:
    """从 LLM response 安全取 (prompt, completion, total) token (无则 0)。"""
    u = getattr(resp, "usage", None)
    if not u:
        return 0, 0, 0
    return (getattr(u, "prompt_tokens", 0) or 0,
            getattr(u, "completion_tokens", 0) or 0,
            getattr(u, "total_tokens", 0) or 0)


def run_loop(
    system_prompt: str,
    user_prompt: str,
    tool_names: list[str] | None = None,
    max_rounds: int | None = None,
    on_step: Callable[[dict], None] | None = None,
    agent: str | None = None,
) -> LoopResult:
    """运行一个 agent 的 ReAct 循环。

    Args:
        system_prompt: 该 agent 的角色与职责。
        user_prompt:   本次任务输入 (通常含黑板片段)。
        tool_names:    该 agent 可用的 tool 子集; None = 全部。
        max_rounds:    最大轮次, 防死循环。
        on_step:       每步回调 (用于 CLI 实时展示 / 日志)。
        agent:         agent 名 (仅用于结构化日志标注)。
    """
    llm = get_llm()
    max_rounds = max_rounds or settings.max_loop_rounds
    tools = registry.openai_schemas(tool_names) if registry.list_names() else None
    # M7: 按 agent 解析应使用的模型档位 (未配置则回退默认单模型)
    provider, model, _, _ = settings.resolve_agent_model(agent)

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]
    trace: list[dict] = []
    agg = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "llm_ms": 0.0}

    for rnd in range(1, max_rounds + 1):
        messages = compress_context(messages)  # 长对话防爆 token: 超预算则摘要化旧轮次
        t0 = time.time()
        resp = llm.chat(messages, tools=tools, provider=provider, model=model)
        llm_ms = round((time.time() - t0) * 1000, 1)
        pt, ct, tt = _usage_tokens(resp)
        agg["prompt_tokens"] += pt
        agg["completion_tokens"] += ct
        agg["total_tokens"] += tt
        agg["llm_ms"] += llm_ms
        log_event("agent.think", agent=agent, model=model, round=rnd, latency_ms=llm_ms,
                  prompt_tokens=pt, completion_tokens=ct, total_tokens=tt)
        msg = resp.choices[0].message

        # 模型要求调用工具
        tool_calls = getattr(msg, "tool_calls", None)
        if tool_calls:
            messages.append({
                "role": "assistant",
                "content": msg.content or "",
                "tool_calls": [tc.model_dump() for tc in tool_calls],
            })
            for tc in tool_calls:
                fname = tc.function.name
                try:
                    fargs = json.loads(tc.function.arguments or "{}")
                except json.JSONDecodeError:
                    fargs = {}
                step = {"round": rnd, "type": "act", "tool": fname, "args": fargs}
                if on_step:
                    on_step(step)
                t_tool = time.time()
                try:
                    result = registry.call(fname, fargs)
                    observation = _stringify(result)
                    tool_ok = True
                except Exception as exc:  # noqa: BLE001
                    observation = f"[tool error] {exc}"
                    tool_ok = False
                tool_ms = round((time.time() - t_tool) * 1000, 1)
                step_obs = {"round": rnd, "type": "observe", "tool": fname,
                            "latency_ms": tool_ms, "ok": tool_ok,
                            "result_preview": observation[:200]}
                log_event("agent.act", agent=agent, round=rnd, tool=fname,
                          latency_ms=tool_ms, ok=tool_ok)
                trace.append(step)
                trace.append(step_obs)
                if on_step:
                    on_step(step_obs)
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": observation,
                })
            continue  # 带着观测进入下一轮

        # 没有工具调用 = 最终答复
        final = msg.content or ""
        trace.append({"round": rnd, "type": "final", "text_preview": final[:200]})
        if on_step:
            on_step({"round": rnd, "type": "final", "text": final})
        agg["llm_ms"] = round(agg["llm_ms"], 1)
        log_event("agent.done", agent=agent, rounds=rnd, **agg)
        return LoopResult(final_text=final, rounds=rnd, trace=trace, usage=agg)

    agg["llm_ms"] = round(agg["llm_ms"], 1)
    log_event("agent.exhausted", level="WARNING", agent=agent, rounds=max_rounds, **agg)
    return LoopResult(final_text="[达到最大轮次, 强制终止]", rounds=max_rounds, trace=trace, usage=agg)


def _stringify(result) -> str:
    """工具结果转字符串。大输出应由 harness 落盘, 这里只做基础转换。

    TODO(Trae): 接入 harness.persist_if_large(), 超长结果落盘只回路径+摘要。
    """
    if isinstance(result, str):
        return result
    try:
        return json.dumps(result, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(result)
