"""Orchestrator: 规划 + 派发 + 终止判断。驱动整个 multi-agent 流程。"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import time

from core.blackboard import Blackboard, Status, SubTask
from core.harness import save_checkpoint
from core.run_context import set_active_blackboard
from core.state_graph import END, START, StateGraph
from agents.retriever import RetrieverAgent
from agents.reader import ReaderAgent
from agents.synthesizer import SynthesizerAgent
from agents.critic import CriticAgent
from config import settings


class Orchestrator:
    """编排器: 规划 -> 检索 -> 并行精读 -> 归纳建图 -> Critic 反馈重调度。"""

    def __init__(self, on_step=None) -> None:
        self.on_step = on_step
        self.retriever = RetrieverAgent()
        self.reader = ReaderAgent()
        self.synthesizer = SynthesizerAgent()
        self.critic = CriticAgent()
        self._critic_passed = False
        self._critic_attempts = 0
        self._rescheduled = False
        self._graph = self._build_graph()

    def run(self, bb: Blackboard) -> Blackboard:
        # 工具需要读黑板做确定性计算 (cluster/build_graph), 注册当前任务上下文
        set_active_blackboard(bb)
        _t0 = time.time()
        try:
            self._critic_passed = False
            self._critic_attempts = 0
            self._rescheduled = False
            self._graph.run(bb, max_steps=20 + settings.critic_max_retry * 3)
            self._notify_done(bb, self._critic_passed, round(time.time() - _t0, 1))
            return bb
        finally:
            set_active_blackboard(None)

    def _build_graph(self) -> StateGraph[Blackboard]:
        """构造显式状态图。

        这是 LangGraph-style 的轻量实现: 节点 = 业务阶段, 边 = 流转/条件分支,
        共享状态 = Blackboard。保留自研运行时以确保 checkpoint/证据链/调试可控。
        """
        graph: StateGraph[Blackboard] = StateGraph()
        graph.add_node("plan", self._node_plan)
        graph.add_node("retrieve", self._node_retrieve)
        graph.add_node("read", self._node_read)
        graph.add_node("synthesize", self._node_synthesize)
        graph.add_node("review", self._node_review)
        graph.add_node("reschedule", self._node_reschedule)
        graph.add_node("remember", self._node_remember)
        graph.add_node("done", self._node_done)

        graph.add_edge(START, "plan")
        graph.add_edge("plan", "retrieve")
        graph.add_edge("retrieve", "read")
        graph.add_edge("read", "synthesize")
        graph.add_edge("synthesize", "review")
        graph.add_conditional_edges("review", self._route_after_review, {
            "pass": "remember",
            "retry": "reschedule",
            "finish": "done",
        })
        graph.add_conditional_edges("reschedule", self._route_after_reschedule, {
            "retry": "synthesize",
            "finish": "done",
        })
        graph.add_edge("remember", "done")
        graph.add_edge("done", END)
        return graph

    def _node_plan(self, bb: Blackboard) -> None:
        self._event(bb, "plan", "start", "生成 StateGraph 执行计划")
        bb.plan = [
            SubTask("retrieve"), SubTask("read"),
            SubTask("synthesize"), SubTask("review"),
        ]
        bb.status = Status.RETRIEVING.value
        self._set_step(bb, "retrieve", "pending")
        self._set_step(bb, "read", "pending")
        self._set_step(bb, "synthesize", "pending")
        self._set_step(bb, "review", "pending")
        self._event(bb, "plan", "done", "计划: retrieve -> read -> synthesize -> review")
        save_checkpoint(bb)

    def _node_retrieve(self, bb: Blackboard) -> None:
        self._set_step(bb, "retrieve", "running")
        self._event(bb, "retrieve", "start", "RetrieverAgent 开始扩展查询并召回候选论文")
        save_checkpoint(bb)
        self.retriever.run(bb, self.on_step)
        self._set_step(bb, "retrieve", "done", f"候选论文 {len(bb.candidates)} 篇")
        self._event(bb, "retrieve", "done", f"候选论文 {len(bb.candidates)} 篇")
        bb.status = Status.READING.value
        save_checkpoint(bb)

    def _node_read(self, bb: Blackboard) -> None:
        self._set_step(bb, "read", "running")
        self._event(bb, "read", "start", f"ReaderAgent 并行精读 {len(bb.candidates)} 篇候选论文")
        save_checkpoint(bb)
        self._read_parallel(bb, bb.candidates)
        self._set_step(bb, "read", "done", f"论文卡片 {len(bb.cards)} 张")
        self._event(bb, "read", "done", f"论文卡片 {len(bb.cards)} 张")
        bb.status = Status.SYNTHESIZING.value
        save_checkpoint(bb)

    def _node_synthesize(self, bb: Blackboard) -> None:
        self._set_step(bb, "synthesize", "running")
        self._event(bb, "synthesize", "start", "SynthesizerAgent 开始建图并生成综述")
        save_checkpoint(bb)
        self.synthesizer.run(bb, self.on_step)
        nodes = len(bb.graph.nodes) if bb.graph else 0
        self._set_step(bb, "synthesize", "done", f"图谱节点 {nodes} 个")
        self._event(bb, "synthesize", "done", f"图谱节点 {nodes} 个, 产物 {len(bb.artifacts)} 个")
        bb.status = Status.REVIEWING.value
        save_checkpoint(bb)

    def _node_review(self, bb: Blackboard) -> None:
        self._set_step(bb, "review", "running")
        self._critic_attempts += 1
        self._event(bb, "review", "start", f"CriticAgent 第 {self._critic_attempts} 次质量检查")
        save_checkpoint(bb)
        self._critic_passed = self.critic.review(bb, self.on_step)
        note = "通过" if self._critic_passed else "未通过"
        self._set_step(bb, "review", "done" if self._critic_passed else "running", note)
        self._event(bb, "review", "done", f"Critic {note}")

    def _route_after_review(self, bb: Blackboard) -> str:
        if self._critic_passed:
            self._event(bb, "review", "route", "pass -> remember")
            return "pass"
        if self._critic_attempts <= settings.critic_max_retry:
            self._event(bb, "review", "route", "retry -> reschedule")
            return "retry"
        self._event(bb, "review", "route", "finish -> done (重试次数耗尽)")
        return "finish"

    def _node_reschedule(self, bb: Blackboard) -> None:
        # 不通过: 依据反馈定向重调度; 若无可补救动作则不再空转重试
        self._event(bb, "reschedule", "start", "根据 Critic 反馈定向补救")
        self._rescheduled = self._reschedule(bb)
        note = "已补救, 回到 synthesize" if self._rescheduled else "无可补救动作, 结束流程"
        self._event(bb, "reschedule", "done", note)
        save_checkpoint(bb)

    def _route_after_reschedule(self, bb: Blackboard) -> str:
        route = "retry" if self._rescheduled else "finish"
        self._event(bb, "reschedule", "route", f"{route} -> {'synthesize' if route == 'retry' else 'done'}")
        return route

    def _node_remember(self, bb: Blackboard) -> None:
        # 质量闸门: 仅当 Critic 通过时, 才把卡片沉淀进跨任务记忆 (杜绝低质固化)
        self._event(bb, "remember", "start", "Critic 通过, 写入 card memory")
        self._remember_cards(bb)
        self._event(bb, "remember", "done", "card memory 写入完成")

    def _node_done(self, bb: Blackboard) -> None:
        bb.status = Status.DONE.value
        self._event(bb, "done", "done", "任务完成")
        save_checkpoint(bb)

    @staticmethod
    def _set_step(bb: Blackboard, name: str, status: str, note: str = "") -> None:
        for st in bb.plan:
            if st.name == name:
                st.status = status
                st.note = note or st.note
                return

    @staticmethod
    def _event(bb: Blackboard, node: str, event: str, note: str = "") -> None:
        bb.graph_events.append({
            "ts": round(time.time(), 3),
            "node": node,
            "event": event,
            "status": bb.status,
            "note": note,
            "candidates": len(bb.candidates),
            "cards": len(bb.cards),
            "nodes": len(bb.graph.nodes) if bb.graph else 0,
            "artifacts": len(bb.artifacts),
            "tokens": bb.usage.get("total_tokens", 0),
        })

    @staticmethod
    def _remember_cards(bb: Blackboard) -> None:
        """把通过 Critic 的精读卡片写入跨任务记忆 (仅 topic 无关字段)。

        memory 模块内部只持久化 TOPIC_INVARIANT_FIELDS, stance 不入库 (topic 相关)。
        写入失败不影响主流程。
        """
        if not settings.memory_enabled:
            return
        from dataclasses import asdict
        from memory import remember_card
        from core.obs import log_event
        n = 0
        for pid, card in bb.cards.items():
            if not card.core_claim:
                continue
            try:
                remember_card(pid, asdict(card))
                n += 1
            except Exception as exc:  # noqa: BLE001  记忆写入失败不致命
                log_event("memory.remember_fail", level="WARNING",
                          paper_id=pid, error=str(exc))
        log_event("memory.remember", count=n)

    @staticmethod
    def _notify_done(bb: Blackboard, critic_passed: bool, elapsed_s: float) -> None:
        """任务完成后推送飞书富卡片 (未配置 webhook 时静默跳过, 不影响主流程)。"""
        from interfaces.feishu import notify_task_done
        graph = bb.graph
        stats = {
            "cards": len(bb.cards),
            "nodes": len(graph.nodes) if graph else 0,
            "edges": len(graph.edges) if graph else 0,
            "gaps": len(graph.gaps) if graph else 0,
            "tokens": bb.usage.get("total_tokens", 0),
            "elapsed_s": elapsed_s,
            "critic_passed": critic_passed,
        }
        try:
            notify_task_done(bb.topic, bb.artifacts, stats)
        except Exception:  # noqa: BLE001  通知失败不应中断任务
            pass

    def _read_parallel(self, bb: Blackboard, paper_ids: list[str], force: bool = False) -> None:
        """并行精读多篇论文。Reader 各自只调 rag_query, 互不共享状态, 线程安全。

        写黑板 cards 字典为各自独立 key, 无写冲突。并发度受 reader_concurrency 限制。
        """
        if not paper_ids:
            return
        workers = max(1, min(settings.reader_concurrency, len(paper_ids)))
        if workers == 1:
            for pid in paper_ids:
                self.reader.run_for(bb, pid, self.on_step, force=force)
            return
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(self.reader.run_for, bb, pid, self.on_step, force=force): pid
                       for pid in paper_ids}
            for fut in as_completed(futures):
                fut.result()  # 抛出子线程异常, 便于上层感知

    def _reschedule(self, bb: Blackboard) -> bool:
        """根据最近一次 critic 反馈定向重跑对应环节。返回是否实际采取了补救动作。

        只对"可补救"的问题重跑, 避免无效空转 (例如卡片本身就缺 evidence_spans,
        重跑 Synthesizer 也补不出证据 —— 这类问题应在 Reader 层解决, 不在此循环里硬刷)。
        """
        acted = False
        # 完整性: 缺卡片 -> 补精读缺失论文, 随后由图边流转回 synthesize。
        missing = [pid for pid in bb.candidates if pid not in bb.cards]
        if missing:
            self._read_parallel(bb, missing)
            acted = True
        # 引用真实性: 卡片存在但证据为空/不可回溯 -> 强制重读坏卡片。
        elif bad_evidence := self._bad_evidence_papers(bb):
            self._read_parallel(bb, bad_evidence, force=True)
            acted = True
        # 图谱缺失 -> 让图边流转回 synthesize 重建图谱。
        elif bb.graph is None or not bb.graph.nodes:
            acted = True
        return acted

    @staticmethod
    def _bad_evidence_papers(bb: Blackboard) -> list[str]:
        """从最近一次 Critic feedback 中提取需要重读的论文。"""
        if not bb.critic_feedback:
            return []
        fb = bb.critic_feedback[-1]
        pids: list[str] = []
        for pid in fb.get("empty_evidence_cards") or []:
            if pid in bb.cards and pid not in pids:
                pids.append(pid)
        for item in fb.get("unverifiable_evidence") or []:
            pid = item.get("paper_id") if isinstance(item, dict) else None
            if pid in bb.cards and pid not in pids:
                pids.append(pid)
        return pids
