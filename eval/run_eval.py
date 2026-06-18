"""M8 评估脚本: 检索 recall@k / MRR + ChatAgent 引用可回溯率。

用法:
    python -m eval.run_eval                 # 跑检索指标 + 引用可回溯率
    python -m eval.run_eval --no-chat       # 只跑检索 (不调 LLM, 省 token)

评估集 eval/qa_set.jsonl 每行: {query, expected_keywords:[...]}。
- recall@k: 期望关键词中, 在前 k 个召回片段文本里出现的比例 (按 query 平均)。
- MRR: 第一个"命中任一期望关键词"的片段排名的倒数 (按 query 平均)。
- 引用可回溯率: ChatAgent 答案引用的 quote 能在库内检索回溯的比例。
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

_EVAL_DIR = Path(__file__).resolve().parent
_QA_SET = _EVAL_DIR / "qa_set.jsonl"


def _load_set() -> list[dict]:
    rows = []
    for line in _QA_SET.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def _hit(text: str, keywords: list[str]) -> bool:
    low = (text or "").lower()
    return any(kw.lower() in low for kw in keywords)


def eval_retrieval(top_k: int = 8) -> dict:
    from rag.retrieve import hybrid_search

    rows = _load_set()
    recalls, rrs = [], []
    for row in rows:
        kws = row.get("expected_keywords", [])
        hits = hybrid_search(row["query"], top_k=top_k)
        texts = [h.text for h in hits]
        # recall@k: 期望关键词在召回片段中出现的覆盖比例
        covered = sum(1 for kw in kws if any(kw.lower() in (t or "").lower() for t in texts))
        recalls.append(covered / len(kws) if kws else 0.0)
        # MRR: 首个命中片段的排名倒数
        rr = 0.0
        for rank, t in enumerate(texts, start=1):
            if _hit(t, kws):
                rr = 1.0 / rank
                break
        rrs.append(rr)
    n = len(rows) or 1
    return {
        "queries": len(rows),
        "recall@k": round(sum(recalls) / n, 4),
        "MRR": round(sum(rrs) / n, 4),
        "top_k": top_k,
    }


def eval_citation_traceability(top_k: int = 8) -> dict:
    """对每条 query 跑 ChatAgent, 统计答案引用的 quote 能否在库内回溯。"""
    from chat import ChatAgent
    from rag.retrieve import hybrid_search

    rows = _load_set()
    agent = ChatAgent()
    total_refs, traceable = 0, 0
    for row in rows:
        res = agent.answer(row["query"])
        for ref in res.evidence:
            total_refs += 1
            quote = (ref.get("quote") or "")[:60]
            if not quote:
                continue
            back = hybrid_search(quote, top_k=top_k, paper_id=None)
            if any(quote[:30].lower() in (h.text or "").lower() for h in back):
                traceable += 1
    return {
        "total_refs": total_refs,
        "traceable": traceable,
        "traceable_rate": round(traceable / total_refs, 4) if total_refs else 0.0,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-chat", action="store_true", help="只跑检索指标, 不调 ChatAgent")
    ap.add_argument("--top-k", type=int, default=8)
    args = ap.parse_args()

    print("== M8 检索评估 ==")
    retr = eval_retrieval(top_k=args.top_k)
    print(json.dumps(retr, ensure_ascii=False, indent=2))

    if not args.no_chat:
        print("\n== ChatAgent 引用可回溯率 ==")
        cite = eval_citation_traceability(top_k=args.top_k)
        print(json.dumps(cite, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
