"""Reader Agent: 单篇论文精读 -> 结构化 PaperCard。可并行。"""
from __future__ import annotations

import json
import re

from agents.base import BaseAgent
from core.blackboard import Blackboard, PaperCard

# 列表型字段: 解析出来若是字符串要包成单元素 list。
_LIST_FIELDS = {"authors", "key_results", "stance_tags", "opposes",
                "limitations", "evidence_spans"}


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
        "重要: 调用 rag_query 时必须传入 paper_id 参数 (锁定目标论文), "
        "否则会检索到其它论文的内容导致串味。\n"
        "evidence_spans 必须包含可回溯的原文片段 (quote), 供后续引用核对。"
    )
    tools = ["rag_query"]

    def run_for(self, bb: Blackboard, paper_id: str, on_step=None):
        """对单篇论文运行精读。

        已有同 paper_id 的非空卡片时直接复用 (省一次完整 agent loop), 防重复精读。
        """
        existing = bb.cards.get(paper_id)
        if existing and existing.core_claim:
            from core.obs import log_event
            log_event("reader.card_reuse", agent="reader", paper_id=paper_id)
            return None
        prompt = (
            f"研究方向: {bb.topic}\n目标论文 paper_id: {paper_id}\n"
            f"请精读并输出 PaperCard JSON。调用 rag_query 时务必带上 "
            f"paper_id=\"{paper_id}\" 以锁定本篇。"
        )
        from core.agent_loop import run_loop
        result = run_loop(self.system_prompt, prompt, self.tools, on_step=on_step, agent=self.name)
        from agents.base import accumulate_usage
        accumulate_usage(bb, result)
        self._parse_card(bb, paper_id, result.final_text)
        return result

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
