"""Reader Agent: 单篇论文精读 -> 结构化 PaperCard。可并行。"""
from __future__ import annotations

import json
import re

from agents.base import BaseAgent
from core.blackboard import Blackboard, PaperCard
from config import settings

# 列表型字段: 解析出来若是字符串要包成单元素 list。
_LIST_FIELDS = {"authors", "key_results", "stance_tags", "opposes",
                "limitations", "evidence_spans"}

# 精读的 4 个核心维度 (砍维度: 围绕这 4 个关键面做多 query 预检索, 覆盖一篇论文
# 的主干信息, 避免过多维度稀释召回名额、也省 token)。
_READ_DIMENSIONS = ["核心贡献 contribution", "方法 method approach",
                    "实验结果 experiment result", "局限 limitation future work"]


class ReaderAgent(BaseAgent):
    name = "reader"
    system_prompt = (
        "你是论文精读专家。针对给定论文, 通过 rag_query 取其内容, "
        "抽取结构化信息并以 JSON 输出 PaperCard:\n"
        "{core_claim, method, method_family, key_results[], stance_tags[], "
        "opposes[], limitations[], evidence_spans[]}\n"
        "语言要求: core_claim/method/method_family/key_results/limitations 一律用"
        "简体中文撰写 (即使原文是英文, 也要翻译并提炼成通顺中文), 论文专有名词/缩写"
        "(如 TIGER、HSTU、Semantic ID) 可保留英文。\n"
        "字段规范:\n"
        "- method_family: 用简短中文方法范式名 (如「生成式检索」「向量量化检索」), "
        "便于跨论文聚类, 不要写整句描述。\n"
        "- opposes: 填该论文明确反对/对比的【方法或范式的简短名称】(如「传统级联排序」"
        "「双塔召回」), 每项不超过 12 字; 若原文未明确反对任何方法, 直接留空数组 []。"
        "禁止把整句英文描述塞进 opposes。\n"
        "重要: 已为你预取了该论文若干关键片段 (见 user 消息); 如需补充细节, "
        "调用 rag_query 时无需再传 paper_id (系统已自动锁定本篇)。\n"
        "evidence_spans 必须包含可回溯的原文片段 (quote), 供后续引用核对。"
    )
    tools = ["rag_query"]

    def run_for(self, bb: Blackboard, paper_id: str, on_step=None):
        """对单篇论文运行精读。

        三段式命中策略 (从快到慢):
        1) 黑板已有同 paper_id 非空卡片 -> 直接复用 (同任务内, 省一次 loop)。
        2) 跨任务记忆命中 -> 载入 topic 无关字段, 仅按新 topic 轻量重抽 stance (省主体精读)。
        3) 均未命中 -> 4 维预检索注入 + 完整 loop 精读。
        """
        existing = bb.cards.get(paper_id)
        if existing and existing.core_claim:
            from core.obs import log_event
            log_event("reader.card_reuse", agent="reader", paper_id=paper_id)
            return None

        from core.run_context import set_reader_paper_id
        set_reader_paper_id(paper_id)  # 锁定本线程精读论文, rag_query 自动注入 paper_id
        try:
            if self._try_memory(bb, paper_id, on_step):  # 2) 跨任务记忆命中
                return None
            # 3) 完整精读
            excerpts = self._pre_retrieve(paper_id)
            prompt = (
                f"研究方向: {bb.topic}\n目标论文 paper_id: {paper_id}\n"
                f"以下是从本篇论文预取的关键片段 (按维度组织), 请据此抽取并输出 "
                f"PaperCard JSON; 证据不足时可再调用 rag_query 补充:\n\n{excerpts}"
            )
            from core.agent_loop import run_loop
            result = run_loop(self.system_prompt, prompt, self.tools,
                              on_step=on_step, agent=self.name)
            from agents.base import accumulate_usage
            accumulate_usage(bb, result)
            self._parse_card(bb, paper_id, result.final_text)
            return result
        finally:
            set_reader_paper_id(None)

    def _try_memory(self, bb: Blackboard, paper_id: str, on_step=None) -> bool:
        """尝试用跨任务记忆命中: 载入 topic 无关字段 + 按新 topic 重抽 stance。

        命中并成功构建卡片返回 True (跳过完整精读); 未启用/未命中返回 False。
        """
        if not settings.memory_enabled:
            return False
        from memory import recall_card
        invariant = recall_card(paper_id)
        if not invariant or not invariant.get("core_claim"):
            return False

        from core.obs import log_event
        log_event("reader.memory_hit", agent="reader", paper_id=paper_id)

        clean: dict = {}
        for k, v in invariant.items():
            if k not in PaperCard.__dataclass_fields__ or k == "paper_id":
                continue
            if k in _LIST_FIELDS and not isinstance(v, list):
                v = [v] if v else []
            clean[k] = v
        card = PaperCard(paper_id=paper_id, **clean)

        # stance_tags/opposes 不从记忆复用 (topic 相关), 针对当前 topic 轻量重抽。
        stance = self._extract_stance(bb.topic, card, paper_id)
        card.stance_tags = stance.get("stance_tags", [])
        card.opposes = stance.get("opposes", [])

        # 元数据补全 (记忆里若缺 title/year, 用库内真实值)
        meta = bb_store_meta(paper_id)
        if not card.title and meta:
            card.title = meta.get("title", "")
        if not card.year and meta:
            card.year = meta.get("year", 0) or 0

        bb.cards[paper_id] = card
        if on_step:
            on_step({"round": 0, "type": "final",
                     "text": f"记忆命中, 复用精读卡 + 重抽立场: {paper_id}"})
        return True

    def _extract_stance(self, topic: str, card: PaperCard, paper_id: str) -> dict:
        """针对当前 topic 轻量重抽 stance_tags/opposes (单次 chat, 不开 loop)。

        输入 = 已存的 core_claim/method/method_family + 针对"对比/反对"维度预检索的
        少量本篇片段 (thread-local 已锁 paper_id)。失败降级为空, 不阻断主流程。
        """
        from rag.retrieve import hybrid_search

        hits = hybrid_search("对比 反对 baseline compare against", top_k=3, paper_id=paper_id)
        snippets = "\n".join(f"- {h.text[:300]}" for h in hits) or "(无)"
        sys = (
            "你是论文立场分析助手。给定一篇论文的已知要点与若干原文片段, 针对指定研究方向"
            "抽取该论文的立场。只输出 JSON: {stance_tags: [...], opposes: [...]}。\n"
            "- stance_tags: 该论文相对本研究方向的立场标签 (简短中文短语)。\n"
            "- opposes: 该论文明确反对/对比的方法或范式简短名称 (每项≤12字); 无则空数组 []。"
        )
        user = (
            f"研究方向: {topic}\n论文核心主张: {card.core_claim}\n方法: {card.method}\n"
            f"方法范式: {card.method_family}\n原文片段:\n{snippets}"
        )
        try:
            from core.llm import get_llm
            text = get_llm().chat_text(
                [{"role": "system", "content": sys}, {"role": "user", "content": user}],
                temperature=0.2,
            )
            data = self._extract_json(text)
            return {
                "stance_tags": data.get("stance_tags") if isinstance(data.get("stance_tags"), list) else [],
                "opposes": data.get("opposes") if isinstance(data.get("opposes"), list) else [],
            }
        except Exception as exc:  # noqa: BLE001  重抽失败不阻断: 图谱少几条边, 不致命
            from core.obs import log_event
            log_event("reader.stance_reextract_fail", level="WARNING",
                      paper_id=paper_id, error=str(exc))
            return {"stance_tags": [], "opposes": []}

    @staticmethod
    def _pre_retrieve(paper_id: str, per_dim: int = 3) -> str:
        """对 4 个核心维度各检索若干片段, 跨维度按 chunk_id 合并去重后拼成文本。

        确定性预检索: 给模型喂真实原文片段而非让它盲目多轮试探, 提升弱模型抽取质量。
        """
        from rag.retrieve import hybrid_search

        seen: set[str] = set()
        blocks: list[str] = []
        for dim in _READ_DIMENSIONS:
            hits = hybrid_search(dim, top_k=per_dim, paper_id=paper_id)
            lines: list[str] = []
            for h in hits:
                if h.chunk_id in seen:  # E: 跨维度去重, 同一片段不重复喂
                    continue
                seen.add(h.chunk_id)
                sec = (h.metadata or {}).get("section", "")
                lines.append(f"  - [{sec}] {h.text[:400]}")
            if lines:
                blocks.append(f"【{dim}】\n" + "\n".join(lines))
        return "\n\n".join(blocks) if blocks else "(未检索到片段, 请调用 rag_query 获取)"

    @staticmethod
    def _extract_json(text: str) -> dict:
        """从模型输出里抽出 JSON 对象, 容忍 ```json 围栏与前后多余文本。"""
        if not text:
            return {}
        # 优先取代码围栏内的内容
        fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
        candidate = fence.group(1) if fence else None
        if candidate is None:
            start, end = text.find("{"), text.rfind("}")
            candidate = text[start:end + 1] if start >= 0 and end > start else ""
        try:
            data = json.loads(candidate)
            return data if isinstance(data, dict) else {}
        except json.JSONDecodeError:
            return {}

    def _parse_card(self, bb: Blackboard, paper_id: str, text: str) -> None:
        """解析 JSON 为 PaperCard 写入黑板 (容错 + 类型校验 + 元数据补全)。"""
        data = self._extract_json(text)

        # 只保留合法字段, 并做基础类型规整 (list 字段容错)
        clean: dict = {}
        for k, v in data.items():
            if k not in PaperCard.__dataclass_fields__ or k == "paper_id":
                continue
            if k in _LIST_FIELDS and not isinstance(v, list):
                v = [v] if v else []
            if k == "year":
                try:
                    v = int(v)
                except (TypeError, ValueError):
                    continue
            clean[k] = v

        card = PaperCard(paper_id=paper_id, **clean)

        # 用库内真实元数据补全 title/year (模型常缺失或编造)
        meta = bb_store_meta(paper_id)
        if not card.title and meta:
            card.title = meta.get("title", "")
        if not card.year and meta:
            card.year = meta.get("year", 0) or 0

        bb.cards[paper_id] = card


def bb_store_meta(paper_id: str) -> dict:
    """查库内该论文的真实元数据 {title, year}; 查不到返回空。"""
    from rag.store import get_store
    return get_store().list_papers().get(paper_id, {})
