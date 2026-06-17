---
name: citation_check
description: Critic 用于核对引用真实性、检查逻辑矛盾的流程。
---

# 引用核对流程

## 检查项
1. **引用真实性**：综述/图谱中每条引用，必须能在对应论文卡片的 `evidence_spans` 中找到来源片段。找不到 → 判为幻觉，打回。
2. **关系证据**：图谱里每条 `opposes`/`evolves_to` 边，检查 `evidence` 是否非空且与 `rationale` 一致。
3. **完整性**：对照候选 `paper_id`，确认没有重要论文被遗漏。

## 输出
返回 JSON：
```json
{"passed": true, "issues": [], "suggestions": []}
```
- `passed=false` 时，`issues` 指明问题、`suggestions` 给修复建议，供 Orchestrator 带反馈重调度。
