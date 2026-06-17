"""Agent 基类: 统一契约 输入(黑板) -> ReAct loop -> 输出(写回黑板)。"""
from __future__ import annotations

from pathlib import Path

from core.agent_loop import LoopResult, run_loop
from core.blackboard import Blackboard


class BaseAgent:
    name: str = "base"
    system_prompt: str = "你是一个有用的 agent。"
    tools: list[str] | None = None  # 可用 tool 子集; None = 全部

    def __init__(self) -> None:
        # 子类可覆盖 system_prompt; 也支持从 skills/ 注入 (见 load_skill)
        pass

    def build_user_prompt(self, bb: Blackboard) -> str:
        """把黑板相关片段拼成本次任务输入。子类覆盖。"""
        return f"研究方向: {bb.topic}\n当前状态: {bb.status}"

    def run(self, bb: Blackboard, on_step=None) -> LoopResult:
        result = run_loop(
            system_prompt=self.system_prompt,
            user_prompt=self.build_user_prompt(bb),
            tool_names=self.tools,
            on_step=on_step,
            agent=self.name,
        )
        accumulate_usage(bb, result)
        self.apply_result(bb, result)
        return result

    def apply_result(self, bb: Blackboard, result: LoopResult) -> None:
        """把 loop 结果写回黑板。子类覆盖 (解析 JSON 等)。"""
        pass


def accumulate_usage(bb: Blackboard, result: LoopResult) -> None:
    """把单次 loop 的 token/耗时累计进黑板 (供任务级链路汇总)。线程安全场景下
    各 Reader 累加同一 dict, 受 GIL 保护的 += 对 int/float 足够。"""
    if not result or not result.usage:
        return
    for k, v in result.usage.items():
        bb.usage[k] = round(bb.usage.get(k, 0) + v, 1)


def load_skill(skill_name: str) -> str:
    """读取 skills/<name>/SKILL.md 正文, 供注入 prompt (渐进式披露)。"""
    p = Path(__file__).resolve().parent.parent / "skills" / skill_name / "SKILL.md"
    return p.read_text(encoding="utf-8") if p.exists() else ""
