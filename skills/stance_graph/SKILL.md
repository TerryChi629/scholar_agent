---
name: stance_graph
description: 把一批论文卡片组织成「立场图谱」的标准作业流程 (SOP)，识别方法流派、观点对立与研究空白。
---

# 立场图谱构建 SOP

## 目标
不是逐篇总结，而是建模**论文之间的关系**：谁与谁对立、方法如何演进、空白在哪。

## 步骤
1. **聚类**：调用 `cluster_cards`，按 `method_family` 与 `stance_tags` 把卡片归簇，形成「方法流派 / 观点簇」节点。
2. **建边**：基于每张卡片的 `opposes` 与 `stance_tags` 推断节点间关系：
   - `opposes`（对立）、`extends`（扩展）、`supports`（支持）、`evolves_to`（演进）。
   - **每条边必须写 `rationale`（理由）与 `evidence`（来自 evidence_spans 的引用）**，无证据的边降级为「可能相关」。
3. **找空白**：调用 `detect_gaps`，识别「被讨论但无定论」「方法未覆盖的设定」。
4. **产出**：
   - 调用 `build_graph` 生成图谱 JSON。
   - 按图谱章节化撰写综述初稿，调用 `export_md` 落盘。
   - 调用 `export_graph_html` 生成可视化。

## 质量红线
- 引用必须真实、可回溯，禁止编造不存在的论文或结论。
- 对立关系必须有证据，否则不要下断言。
