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

            for _ in range(settings.critic_max_retry + 1):
                if self.critic.review(bb, self.on_step):
                    break
                # 不通过: 依据反馈定向重调度; 若无可补救动作则不再空转重试
                if not self._reschedule(bb):
                    break
                save_checkpoint(bb)

            bb.status = Status.DONE.value
            save_checkpoint(bb)
            return bb
        finally:
            set_active_blackboard(None)

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
