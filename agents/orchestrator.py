"""Orchestrator: 规划 + 派发 + 终止判断。驱动整个 multi-agent 流程。"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed

from core.blackboard import Blackboard, Status, SubTask
from core.harness import save_checkpoint
from core.run_context import set_active_blackboard
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

    def run(self, bb: Blackboard) -> Blackboard:
        # 工具需要读黑板做确定性计算 (cluster/build_graph), 注册当前任务上下文
        set_active_blackboard(bb)
        import time
        _t0 = time.time()
        try:
            # 1) 规划
            bb.plan = [
                SubTask("retrieve"), SubTask("read"),
                SubTask("synthesize"), SubTask("review"),
            ]
            bb.status = Status.RETRIEVING.value
            save_checkpoint(bb)

            # 2) 检索
            self.retriever.run(bb, self.on_step)
            bb.status = Status.READING.value
            save_checkpoint(bb)

            # 3) 精读: 并行 fork N 个 Reader (受 reader_concurrency 限制)
            self._read_parallel(bb, bb.candidates)
            bb.status = Status.SYNTHESIZING.value
            save_checkpoint(bb)

            # 4) 归纳建图 + 5) Critic 反馈重调度
            self.synthesizer.run(bb, self.on_step)
            bb.status = Status.REVIEWING.value
            save_checkpoint(bb)

            critic_passed = False
            for _ in range(settings.critic_max_retry + 1):
                if self.critic.review(bb, self.on_step):
                    critic_passed = True
                    break
                # 不通过: 依据反馈定向重调度; 若无可补救动作则不再空转重试
                if not self._reschedule(bb):
                    break
                save_checkpoint(bb)

            # 质量闸门: 仅当 Critic 通过时, 才把卡片沉淀进跨任务记忆 (杜绝低质固化)
            if critic_passed:
                self._remember_cards(bb)

            bb.status = Status.DONE.value
            save_checkpoint(bb)
            self._notify_done(bb, critic_passed, round(time.time() - _t0, 1))
            return bb
        finally:
            set_active_blackboard(None)

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

    def _read_parallel(self, bb: Blackboard, paper_ids: list[str]) -> None:
        """并行精读多篇论文。Reader 各自只调 rag_query, 互不共享状态, 线程安全。

        写黑板 cards 字典为各自独立 key, 无写冲突。并发度受 reader_concurrency 限制。
        """
        if not paper_ids:
            return
        workers = max(1, min(settings.reader_concurrency, len(paper_ids)))
        if workers == 1:
            for pid in paper_ids:
                self.reader.run_for(bb, pid, self.on_step)
            return
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(self.reader.run_for, bb, pid, self.on_step): pid
                       for pid in paper_ids}
            for fut in as_completed(futures):
                fut.result()  # 抛出子线程异常, 便于上层感知

    def _reschedule(self, bb: Blackboard) -> bool:
        """根据最近一次 critic 反馈定向重跑对应环节。返回是否实际采取了补救动作。

        只对"可补救"的问题重跑, 避免无效空转 (例如卡片本身就缺 evidence_spans,
        重跑 Synthesizer 也补不出证据 —— 这类问题应在 Reader 层解决, 不在此循环里硬刷)。
        """
        acted = False
        # 完整性: 缺卡片 -> 补精读缺失论文 (可补救)
        missing = [pid for pid in bb.candidates if pid not in bb.cards]
        if missing:
            self._read_parallel(bb, missing)
            self.synthesizer.run(bb, self.on_step)  # 卡片变化, 重新建图+综述
            acted = True
        # 图谱缺失 -> 重跑 Synthesizer (可补救)
        elif bb.graph is None or not bb.graph.nodes:
            self.synthesizer.run(bb, self.on_step)
            acted = True
        return acted
