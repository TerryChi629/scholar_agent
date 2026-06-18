# CLAUDE.md — ScholarStance 项目宪法

> 本文件是给 AI 编码助手 (Trae Solo / Claude) 的最高指令。动手写任何代码前，先读完本文件。

---

## 0. 一句话项目定位

**ScholarStance**：一个跑在本地、能读私有文献库的**多智能体科研助手**。核心能力是把某个研究方向的论文自动组织成「**立场图谱**」——谁与谁观点对立、方法如何演进、研究空白在哪——并产出带**真实引用**的综述初稿。

与 Elicit/Consensus 的根本区别：它们做**单篇问答 + 云端 SaaS**；我们做**跨篇关系建模 + 本地私有数据**。

---

## 1. 开发环境

- 设备：M5 Pro 顶配 Mac（算力充足，模型走 API 不吃本地资源）。
- 语言：Python 3.11+。
- 包管理：`python -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt`。
- 配置：`cp .env.example .env` 后填入 DeepSeek 或 GLM 的 key。
- **第一步永远先跑**：`python main.py selfcheck`（不联网，验证脚手架完整）。

---

## 2. 架构总览（五层，不要打破分层）

```
L5 集成层   interfaces/  (cli.py / api.py / feishu.py)
L4 Harness  core/harness.py  (上下文/落盘/确认/会话恢复)
L3 编排层   agents/orchestrator.py
L2 执行层   agents/ (retriever, reader, synthesizer, critic)
L1 底座层   tools/ + skills/ + rag/ + mcp_clients/ + memory/
```

**通信约定**：Agent 之间**不直接对话**，一律通过**共享黑板 `core/blackboard.py::Blackboard`** 读写中间产物。黑板可序列化到 SQLite，实现断点恢复。

---

## 3. 核心设计原则（借鉴 Claude Code，必须遵守）

1. **轻框架**：核心 Agent Loop (`core/agent_loop.py`) 自研，**禁止**引入 LangChain / AutoGen / CrewAI 等重型编排框架。可以用它们的思想，不能用它们的运行时。
2. **工具即能力边界**：Agent 只能通过 `@tool` 注册的函数影响世界。新增能力 = 在 `tools/__init__.py` 写一个 `@tool` 函数，不要在 agent 里直接写副作用代码。
3. **上下文是稀缺资源**：工具大输出必须经 `core/harness.py::persist_if_large` 落盘，上下文里只留路径+摘要。
4. **人在关键环**：写库 (`rag_ingest`)、写文件 (`export_*`)、外部网络调用前，走 `harness.confirm`。
5. **每步可观测**：Agent Loop 的每个 think/act/observe 都进 `trace`，CLI 通过 `on_step` 回调实时展示。

---

## 4. 数据契约（改这些结构要全局同步）

定义在 `core/blackboard.py`：

- **`PaperCard`**：Reader 的标准产出。`evidence_spans` 必须带可回溯原文 (quote/page)，供 Critic 核对——**这是防幻觉的命脉，不能省**。
- **`StanceGraph`**：Synthesizer 产出。每条 `StanceEdge` 必须有 `rationale` + `evidence`，无证据的关系降级，不准下断言。
- **`Blackboard`**：任务中枢，含 `to_json/from_json`，每个关键步骤后 `harness.save_checkpoint`。

---

## 5. 关键技术选型（不要随意替换）

| 用途 | 选型 | 备注 |
|---|---|---|
| LLM | DeepSeek / GLM | OpenAI 兼容，`config.py` 热切换，统一走 `core/llm.py` |
| Embedding | API（初版） | `rag/embedder.py` 留了 `LocalEmbedder` 接口，后期换自训模型**不动上层** |
| 向量库 | ChromaDB | 本地持久化，零服务 |
| 检索 | Hybrid（向量+BM25+RRF+规则 rerank） | 向量+BM25+RRF 已实现；rerank 为极轻量确定性规则（章节/关键词弱加分），非 cross-encoder |
| PDF | PyMuPDF (`import fitz`) | |
| 存储 | SQLite | 会话/记忆 |

---

## 6. 实现优先级（按此顺序填 TODO）

代码里所有占位都标了 `TODO(Trae)`。按里程碑推进：

### M1（先让闭环跑通）
- [x] `rag/ingest.py`：语义分块（按章节，而非定长）+ 元数据抽取（标题/年份）。
- [x] `rag/retrieve.py`：补 BM25 + 融合（RRF）。
- [x] `agents/retriever.py::apply_result`：解析 LLM 输出，把 paper_id 写入 `bb.candidates`。
- [x] `agents/reader.py::_parse_card`：健壮化 JSON 解析。
- [x] 跑通 `python main.py ingest <dir>` 和 `python main.py map "<topic>"`。

### M2（深度 + 防幻觉）
- [x] `tools/__init__.py`：实现 `cluster_cards` / `build_graph` / `detect_gaps`。
- [x] `agents/synthesizer.py::apply_result`：图谱写入 `bb.graph`，综述落盘。
- [x] `agents/critic.py`：真正回查 `evidence_spans`，不通过则带反馈重调度。
- [x] `agents/orchestrator.py`：Reader 并行（`settings.reader_concurrency`）+ Critic 反馈重调度。

### M3（工程化 + 集成）
- [x] `core/harness.py`：上下文压缩 `compress_context`、token 计数。
- [x] `mcp_clients/`：自建 Python filesystem MCP server（受限网络绕开 npx），包装成 `@tool`。
- [x] `interfaces/feishu.py`：任务完成 Webhook 推送。
- [x] `tools/export_graph_html`：vis-network 可视化。

### M4（后端工程化：稳定性 / 高可用 / 可观测 / 成本）

> 背景：本项目定位为**后端主导型 AI Agent 服务**。AI/RAG 是业务能力，**后端工程（稳定性、高可用、可迭代、可观测）才是核心壁垒**。本里程碑专门补齐生产级工程能力，四个关键词贯穿始终：**稳定性、高可用、可观测、成本可控**。这些功能大多在现有脚手架已留接口，补齐即可，**不得打破第 2 节分层、不得引入重型框架**。

- [x] **稳定性 — API 重试退避 + 限流**（`core/llm.py::LLM.chat` / `APIEmbedder.embed`）
  - chat 与 embed 包一层指数退避重试（如 1s/2s/4s，最多 3 次），专门捕获 `429`（限流）与 `5xx`（服务端错误），其余错误分类后直接抛出。
  - 客户端侧加轻量限流（令牌桶或最小请求间隔），避免突发打爆厂商配额。
- [x] **高可用 — 主备模型降级**（`core/llm.py` + `config.py`）
  - 主模型连续失败/超时后，自动降级到兜底模型（如 GLM-flash 这类更便宜稳定的型号），降级动作记入 trace。
  - 降级为**可配置**：在 `config.py` 暴露 `fallback_chat_model`，禁止硬编码。
- [x] **成本 — Embedding / 检索缓存**（`core/llm.py::APIEmbedder` + `rag/store.py`）
  - Embedding 缓存：对相同文本（按内容 hash）命中本地缓存（SQLite/磁盘），不重复调 API。
  - 检索结果缓存：相同 query + 参数在 TTL 内复用上次结果。
  - 已读论文卡片复用：相同 `paper_id` 已生成的 `PaperCard` 优先复用，避免重复 Reader 调用。
- [x] **可观测 — 结构化日志 + 耗时/token 埋点**（`core/harness.py` + `core/agent_loop.py` trace）
  - 统一结构化日志（JSON 行），每条带 `task_id / agent / step / 耗时ms / token用量 / 是否重试或降级`。
  - 每个 think/act/observe 步骤记录耗时与 token，汇总到黑板 trace，便于链路追踪与问题排查。
- [x] **接口 — 把 FastAPI 做实**（`interfaces/api.py`）
  - 任务异步化已有骨架（立即返回 `task_id`）；补：错误态/进度态返回、`/tasks` 列表分页、健康检查 `/healthz`。
  - 保持「不在沙箱起常驻服务」红线，仅作本地手动调试与接口契约。

### M5（RAG 质量提质：召回 / 抽取 / 防幻觉）

> 背景：M1-M4 跑通闭环与工程化底座后，针对弱模型（glm-4-flash）下抽取雷同空泛、召回主题漂移、跨篇串味等质量问题，做一轮**确定性增强**改造。原则不变：能用确定性算法的环节不交给 LLM，所有断言可回溯。

- [x] **A 增量入库 + 幂等写入**（`rag/ingest.py` + `rag/store.py`）：`ingest_dir` 以 `_paper_id` 为身份跳过库内已有论文，只处理新增；`VectorStore.add` 改 `upsert`，重复入库不报错不浪费。
- [x] **B 查询扩展动态化**（`tools/expand_query`）：用 LLM 围绕 topic 动态生成 5-8 个查询（中文+英文术语+方法词），**严禁硬编码示例主题词**（防主题漂移），失败/为空回退 `[topic]`，强制含原 topic 并去重。
- [x] **C 多维度精读 + D paper_id 自动注入 + E 去重**（`agents/reader.py` + `core/run_context.py` + `tools/rag_query`）：Reader 对 4 个核心维度（核心贡献/方法/实验/局限）预检索、跨维度按 `chunk_id` 合并去重后把真实片段注入 prompt（喂弱模型真材实料）；用 thread-local 登记当前精读 `paper_id`，`rag_query` 在模型漏传时**自动注入**（不报错打断），根治跨篇串味。
- [x] **F 极轻量规则 rerank**（`rag/retrieve.py`）：RRF 融合后叠加确定性弱加分（核心章节命中 + query 关键词命中，系数远小于 RRF 量级，仅并列时决胜），不引入 cross-encoder、零额外 IO。
- [x] **I evidence 为空即打回**（`agents/critic.py`）：补齐漏洞——`evidence_spans` 整体为空的卡片直接判不通过（此前空数组反而免检），守住防幻觉命脉。
- [x] **J 模板化/雷同检测**（`agents/critic.py`）：套话词典命中 + 跨卡 `core_claim` Jaccard 相似度过高 → **仅告警 + 建议重读，不改 passed**（不击杀，避免误杀真实但简短的结论）。

### M7（混合模型分发：按 Agent 难度分档，平衡成本与质量）

> 背景：单模型两难——全用 deepseek-v4-pro 质量好但偏贵（一次任务约 0.64 元），全用 glm-4-flash 便宜但综述质量崩（paper_id 当正文、引用错乱）。按「任务难度」给不同 Agent 配不同档位模型，简单下沉、综述上浮。原则不变：配置走 `settings`、零硬编码，不开启分发时行为完全不变。

- [x] **三档可配 + Agent 映射**（`config.py` + `.env.example`）：`MODEL_TIER_LOW/MID/HIGH`（值 `provider:model`）+ `AGENT_MODEL_RETRIEVER/READER/SYNTHESIZER/CRITIC`（值 low/mid/high）；`resolve_agent_model(agent)` 解析为 `(provider, model, key, base_url)`，**未配置时 model=None 回退默认单模型**（保留 M4 主备降级）。
- [x] **网关多模型 client 池**（`core/llm.py`）：`_clients: dict[provider, OpenAI]` 懒加载缓存；`chat(provider, model)` 指定时走分发路径（不跨模型降级以免跨厂 404），不指定走默认路径（保留主备降级）。
- [x] **Loop 按 Agent 透传**（`core/agent_loop.py` + `agents/reader.py`）：`run_loop` 按 `agent` 解析模型并传入，`agent.think` 日志加 `model` 字段；Reader `_extract_stance` 的轻量重抽也吃 Reader 档位（mid）。
- 默认分档：Retriever=low(GLM) / Reader=mid(ds-flash) / Synthesizer=high(ds-pro) / Critic=low(GLM)。

### M8（对话式 RAG / ChatAgent：本地知识库问答 + 轻量记忆）—— 设计文档

> 背景：M1-M7 把「跨篇关系建模 + 综述」这条重链路跑通了，但缺一条**轻量、即问即答**的入口——用户想直接问「我库里哪些论文用了 RQ-VAE？它们的差异是什么？」，不必每次都触发完整 map 流程（耗 token）。M8 新增 **ChatAgent**：以**本地知识库为事实来源**的对话式问答，配一套**轻量记忆**记住用户偏好/规则。这是后续「完整、有深度的 RAG」的承载主体。
>
> **暂不做**：知识图谱、综述流程的对话化触发（token 成本高，留待后续）。

#### M8.0 核心理念（必须遵守，与项目红线一致）

1. **RAG 主导事实，memory 仅辅助**：论文事实只能来自 `hybrid_search` 召回的真实片段；记忆只承载「用户偏好 / 长期规则」，**绝不能覆盖或改写知识库事实**。证据优先级：`RAG evidence > procedure/preference 记忆`。
2. **确定性主链路**：第一版主流程为 **检索 → 确定性组织证据 → 一次 LLM 合成**，**不复用 `BaseAgent` 的 ReAct loop**（避免多轮试探烧 token / 不可控）。能确定性的环节（意图路由、证据排版、落答）不交给 LLM。
3. **不动现有 card memory**：`memory/__init__.py` 的 `memory_cards`（服务 ReaderAgent 跨任务精读复用）保持原样。聊天记忆走**独立的 memory2 子系统**，两者并存、互不干扰。
4. **复用而非另起**：检索复用 `rag.retrieve.hybrid_search`，embedding 复用 `core.llm`，配置走 `settings`，SQLite 复用 `settings.sqlite_path`。
5. **复用底座函数，不继承 `BaseAgent`**：ChatAgent 是独立类，**不继承 `agents/base.py::BaseAgent`**——其三大契约（输入 `Blackboard`、`run()` 写死多轮 ReAct、`apply_result` 写回黑板）与 ChatAgent（输入裸 question、确定性一次合成、直接返回结果）有两项半不匹配，继承会逼出「塞假黑板 + override 内核 + 空 apply_result」的畸形子类。改为**函数级组合复用**：`core.run_loop`（仅慢路径）、`agents.base.accumulate_usage`、`core.llm`、`core.obs`、`config.settings`。符合第 2 节「轻框架、组合优先」。

#### M8.1 目录结构（新增）

```
scholarstance/
├── chat/                      # 对话式 RAG（新增，L2 执行层旁挂）
│   ├── agent.py               # ChatAgent.answer(question, task_id) 主入口
│   ├── intent.py              # IntentRouter：规则版意图路由（无 LLM）
│   ├── retriever.py           # 复用 hybrid_search，做 paper 聚合 / 技术聚类
│   ├── synthesizer.py         # AnswerSynthesizer：确定性组织证据 + 一次 LLM 合成
│   └── memory_writer.py       # 从对话中抽取 preference/procedure 写入 memory2
└── memory2/                   # 轻量长期记忆子系统（新增，与 memory/ 并存）
    ├── models.py              # MemoryItem 数据模型 + 两类记忆枚举
    ├── store.py               # SQLite：memory_items + memory_replacements 双表
    ├── memorizer.py           # remember()：去重 / reinforcement / 语义 supersede
    ├── retriever.py           # 向量全扫 + 关键词 RRF + hotness 融合检索
    └── injection.py           # 按优先级组织注入文本 + 低置信标注
```

#### M8.2 ChatAgent 主链路（`chat/agent.py`）

`ChatAgent.answer(question, task_id=None) -> ChatResult`（`ChatResult` 为 dataclass：`answer: str / evidence: list / used_memory: list / usage: dict`，与黑板风格一致，不用裸 dict）。

**快慢双路径**（由 `IntentRouter` 决定），v1 只实现快路径：

```
ChatAgent.answer(question)
  ├─ intent = IntentRouter.route(question)
  ├─ [快路径·v1 默认]  knowledge_qa / memory_qa / *_write
  │     → _answer_fast(): 检索 → 确定性组织证据 → 一次 chat_text 合成 → ChatResult
  │       (不进 loop，省 token、可控)
  └─ [慢路径·后续阶段]  complex_qa（多跳 / 需追问库再查）
        → _answer_loop(): 复用 core.run_loop，chat 专属精简工具集（仅 rag_query），
          agent="chat" → accumulate_usage → 解析末轮答案 → ChatResult
```

- **v1 预留慢路径接口**：`answer()` 内部按 intent 分发到 `_answer_fast` / `_answer_loop` 两个私有方法；`_answer_loop` 第一版可先 `raise NotImplementedError` 或回退快路径，待后续阶段实现。这样升级到多跳推理时**无需回头继承 `BaseAgent`**。
- **不包黑板**：ChatAgent 单轮单 Agent，不复用 `Blackboard`（避免背一身用不上的 candidates/cards/graph 字段）；token 口径仍走 `accumulate_usage` 累加到 `ChatResult.usage`。

快路径 `_answer_fast` 步骤：

1. `IntentRouter.route(question)` → 意图（见 M8.3）。
2. 若意图含写记忆（preference/procedure）：`memory_writer` 抽取并写入 memory2，回执确认即可（不必检索）。
3. 知识问答类：`chat.retriever` 调 `hybrid_search` 召回 → **按 paper 聚合 / 技术聚类**（确定性，复用现有聚合逻辑）。
4. `memory2.retriever.recall(question)` 召回相关 preference/procedure，`injection` 按优先级组织成注入块（带低置信标注）。
5. `AnswerSynthesizer.synthesize(question, evidence, memory_block)` → **一次 LLM 合成**带引用的答案；证据排版确定性完成，不让 LLM 编引用。

#### M8.3 IntentRouter（规则版 MVP，`chat/intent.py`）

不引入 LLM 不确定性，用关键词/句式规则分五类，默认落 `knowledge_qa`：

| 意图 | 触发示例 | 处理 |
|---|---|---|
| `knowledge_qa` | 「哪些论文…」「X 和 Y 的区别」 | 检索 + 合成（默认） |
| `memory_qa` | 「我之前说过的偏好是什么」 | 只查 memory2 |
| `preference_write` | 「以后回答都用中文」「我偏好…」 | 写 preference |
| `procedure_write` | 「记住：比较论文时先列方法再列指标」 | 写 procedure |
| `hybrid` | 既问知识又含偏好 | 检索 + 合成 + 顺带写记忆 |

#### M8.4 memory2 子系统（**薄版：只做 preference + procedure 两类**）

> 决策：四类记忆（procedure/preference/event/profile）中，v1 **只做 procedure + preference**，足以覆盖「记住用户偏好/长期规则」的核心诉求。`event`（对话流水）/ `profile`（用户画像）列为 M8 后续阶段，避免记忆子系统吃掉 v1 主要精力。

- **数据模型**（`models.py`）：`MemoryItem{ id, category(procedure|preference), content, content_hash, freq, created_at, last_used_at, embedding_ref }`。
- **存储**（`store.py`）：
  - `memory_items` 表：`content_hash` 唯一索引（精确去重）。
  - `memory_replacements` 表：记录 supersede 关系（新条目取代旧条目），保留审计链不物理删除。
- **写入**（`memorizer.remember`）：
  1. `content_hash` 命中 → **reinforcement**：`freq += 1`、刷新 `last_used_at`，不新增。
  2. 语义相似度 ≥ 0.90 → **supersede**：新条目入库 + 写 `memory_replacements` 指向旧条目。
  3. 0.70–0.90 → v1 **暂不合并**（避免误并），直接新增。
- **检索**（`retriever.recall`）：向量全扫（条目少，无需 ANN）+ 关键词（ASCII token + 中文 bigram，复用 retrieve 的分词思路）→ **RRF 融合**（K=60，KEYWORD_WEIGHT=0.5）→ 叠加 **hotness**（`freq × recency`，half_life=30 天）。`final = 0.85 * retrieval + 0.15 * hotness`。
- **注入**（`injection`）：优先级 **procedure > preference**；每条标注来源，低置信（相似度低/freq=1）条目显式标「（低置信，仅供参考）」，让合成端可酌情忽略。

#### M8.5 评估闭环（**纳入 v1，最高价值项**）

> 没有度量，「深度 RAG」无法验证是否真的变好。v1 建一个**最小评估集 + 评估脚本**，让每次检索/合成改动可量化。

- `eval/qa_set.jsonl`（新增，人工标注 10–20 条）：`{query, expected_paper_ids:[...], expected_chunk_keywords:[...]}`。
- `eval/run_eval.py`（新增）：对评估集跑 `hybrid_search`，输出 **recall@k / MRR**；对 ChatAgent 答案统计**引用可回溯率**（答案引用的 quote 能在库内检索回溯的比例，复用 Critic 的回查机制）。
- 验收基线：首次跑出数值后记入 PROGRESS，作为后续优化的对照基准（不设硬指标，重在「可观测、可对比」）。

#### M8.6 接口（`chat/` 接入 L5）

- CLI：`python -m scholarstance chat "<question>"` → `interfaces/cli.py::cmd_chat` 调 `ChatAgent().answer()`。
- API：`POST /chat {question, task_id?}` → 返回 `ChatResult` 序列化（`{answer, evidence, used_memory, usage}`）（`interfaces/api.py`）。
- 前端：`web/index.html` 可新增「对话」标签页（可选，非 v1 必须）。

#### M8.7 已知检索债务（继承自 RAG 现状，分阶段还）

> ChatAgent 复用 `hybrid_search`，继承以下债务。**v1 只补 `store.delete_paper`**（成本最低、支撑单篇更新），其余列为后续阶段，逐步做：

| 债务 | 影响 | 排期 |
|---|---|---|
| `store` 无 `delete_paper` | 单篇论文无法更新/删除，只能整库重建 | **v1 补** |
| BM25 每查全量 `all_chunks()` 重建 | 库变大后检索变慢 | 后续：持久化/增量索引 |
| 只有规则 rerank，无 cross-encoder | 精排深度不足 | 后续阶段 |
| 无父文档扩展（chunk 召回上下文窄） | 答案上下文可能割裂 | 后续阶段 |
| query/doc 对称编码，无非对称优化 | 召回精度有上限 | 后续阶段 |

#### M8.8 边界红线（v1 不做）

- ❌ 不引入 Akasha / 完整 markdown consolidation / 双 HyDE（过度工程，留待验证有需要再说）。
- ❌ 不做 event / profile 记忆（薄版只 procedure + preference）。
- ❌ ChatAgent 不复用 ReAct loop（确定性主链路）。
- ❌ memory 不替代 RAG 证据；preference 不改写论文事实。
- ❌ 不动 `memory/memory_cards`（ReaderAgent 专用）。

#### M8.9 实现顺序（MVP）

1. `store.delete_paper`（补检索债务）→ 2. `memory2/models.py` + `store.py`（双表）→ 3. `memorizer.remember`（去重/reinforcement/supersede）→ 4. `memory2/retriever.py`（RRF + hotness）→ 5. `injection.py` → 6. `chat/intent.py`（规则路由）→ 7. `chat/retriever.py`（复用 hybrid_search + 聚合）→ 8. `chat/synthesizer.py`（确定性证据 + 一次合成）→ 9. `chat/memory_writer.py` → 10. `chat/agent.py`（串主链路：`ChatResult` dataclass + `_answer_fast` 实现 + `_answer_loop` 慢路径预留接口）→ 11. CLI `chat` + `POST /chat` → 12. `eval/` 评估集 + 脚本，跑出基线。

#### M8.10 测试期模型配置

> **测试阶段所有 LLM 调用统一走 GLM**（便宜），ChatAgent 的合成、memory_writer 抽取、意图兜底等均用 low 档（GLM）。需要看效果质量时再按需切换到高档（走 M7 的 `resolve_agent_model` 分发，零代码改动）。

---

### M9（检索纵深：多阶段 rerank 精排 + 索引工程 + 多样性 + 父文档扩展）—— 设计文档

#### M9.0 设计哲学

把检索从「召回 + 轻量重排」升级为**多阶段管线**，纵深提升相关性与上下文完整性，同时坚持两条红线：

1. **开关化 + 向后兼容**：每个新阶段都有 config 开关，关闭即退化为上游结果，不开启任何 M9 能力时检索行为与 M8 完全一致。
2. **降级化、绝不阻断**：所有重型/外部依赖（cross-encoder、LLM、embedding API、磁盘索引）失败均有回退路径，最坏情况退化为上游排序，检索永不因精排环节崩溃。

#### M9.1 多阶段检索管线（`rag/retrieve.py`，唯一检索出口）

```
召回(向量 + BM25) → RRF 融合 → 规则 rerank → cross-encoder/LLM 精排 → MMR 去冗余 → 父文档扩展 → top_k
```

- 召回阶段多取候选 `fetch_n = max(top_k×3, rerank_top_n)`，给精排足够池子，最后裁到 top_k。
- 无 BM25 命中时退化为纯向量；精排/MMR/扩展任一关闭则跳过该阶段。
- `chat` / `eval` / `tools` 全部复用此出口，改一处全链路生效。

#### M9.2 精排（`rag/rerank.py`）三级回退

1. **本地 cross-encoder** 优先（`BAAI/bge-reranker-v2-m3`，query×doc 交叉编码，比向量召回精准）。懒加载单例 + `_ce_unavailable` 失败标志（失败一次不再反复尝试）。
2. cross-encoder 不可用（未装 sentence-transformers / 下载失败）→ 回退 **LLM listwise 打分**（low 档 GLM，0-10 相关性评分，解析 JSON）。
3. 两者都不可用 → 保持上游 RRF 排序。

> 注意：cross-encoder 首次加载会尝试联网验证缓存，离线环境用 `HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1` 强制走本地缓存。

#### M9.3 索引工程：持久化 + 增量 BM25 索引（`rag/bm25_index.py`）

- 消灭「每查 `all_chunks()` 全量取 + 重建 `BM25Okapi`」的 O(N) 重复劳动。
- pickle 落盘**只存 tokenized 语料 + chunk 元信息**（不存 BM25Okapi 对象，加载后懒重建，避免对象序列化兼容问题）。
- 进程内单例：磁盘优先 → 缺失则从 store 全量重建并落盘。
- ingest 时 `add_chunks` 按 chunk_id 去重增量追加并落盘（与 `store.add` 配套）；`reset()` 清库重入用。

#### M9.4 MMR 多样性去冗余（`rag/rerank.py::mmr`）

- `val = λ·rel[i] − (1−λ)·max_sim`，λ=0.7（偏相关性）；消除 top-k 里近乎重复的片段。
- 复用 embedding 算片段间相似度，query 相关性用精排后 score 归一近似；embedding 失败退化为按 score 截断。

#### M9.5 父文档/邻居窗口扩展（`rag/store.py::neighbors` + `rag/retrieve.py::_expand_context`）

- 命中 chunk 后按 `paper_id + chunk_index` 取邻域 `[idx−window, idx+window]`（window=1）拼回合并文本，给合成更完整上下文。
- 原命中片段存 `metadata['hit_text']`，**引用回溯用 `hit_text`（精确命中片段）而非扩展合并文本**（`chat/retriever.py` 透传、`chat/synthesizer.py` 的 `quote` 取之）。
- 同篇内被某次扩展吸收的邻居去重，不再单独出现。

#### M9.6 配置（`config.py`）

`rerank_enabled` / `rerank_model` / `rerank_top_n`(20) / `rerank_llm_fallback` / `mmr_enabled` / `mmr_lambda`(0.7) / `context_expand_enabled` / `context_expand_window`(1) / `bm25_persist_enabled`；路径 `bm25_index_path`。依赖新增 `sentence-transformers>=3.0.0`（不装则自动回退 LLM listwise）。

---

### M10（智能问答升级：合成式回答 + 对话流前端 + 长短期记忆）—— 设计文档

#### M10.0 设计哲学

把 ChatAgent 从「单轮检索 + 罗列片段」升级为「多轮对话式研究助手」，三条主线：合成式结构化回答、零构建对话流前端、长短期记忆分工。坚持红线：**两类记忆均不替代 RAG 证据，论文事实只来自 `hybrid_search`**。

#### M10.1 合成式回答（`chat/synthesizer.py`）

- synthesizer 产出结构化 Markdown：总述 + 分点 + LaTeX 公式 + Mermaid 图，结论锚定 `[n]` 引用，强约束防幻觉（结论必须挂证据编号）。
- **Mermaid 语法约束**（mid 档易踩坑）：节点标签一律双引号包裹，禁止裸括号 / 逗号 / 分号 / `$` / `<br>`（改用中文顿号或空格），节点 id 只用英文字母数字。否则前端渲染失败。

#### M10.2 长短期记忆分工

- **短期（会话）记忆** = `chat/session.py`，SQLite 双表（`chat_sessions` 滚动摘要 + `chat_turns` 每轮），滑动窗口 + 摘要压缩，生命周期 = 一次会话，承载多轮对话以支持追问指代消解。
  - `append_turn` / `_load` / `build_context_block`（最近 `window_turns×2` 条原文 + 早前摘要，组织成「当前会话上下文」注入块，**显式标注不得作为论文事实来源**）。
  - `maybe_summarize`：超窗旧轮用 low 档 LLM 压成滚动摘要，记 `summarized_upto`，失败静默跳过不阻断。
  - 全部受 `chat_session_enabled` 门控。
- **长期记忆** = 既有 `memory2/`（preference / procedure 两类，跨会话），调答案风格 / 组织，不新增 episodic。

#### M10.3 追问指代消解（`chat/agent.py`）

- `answer(question, task_id, session_id)`：route → `_dispatch` → 有 session_id 则 `append_turn(user/assistant)` + `maybe_summarize`。
- `_answer_fast`：有会话历史时用 `_contextualize_query`（low 档 LLM）把含指代追问（如「它的量化方法」）改写成自包含检索式（否则召不回），失败回退原问。

#### M10.4 零构建对话流前端（`web/index.html`）

- marked + KaTeX + Mermaid 全走 CDN；多轮气泡（user 右对齐 bubble + bot ChatAgent 标注）。
- `session_id` 前端生成随 `/chat` 透传；引用 id 按 turn 隔离（`ref-{tid}-{n}`）避免多轮冲突；「新会话」按钮重置 session。
- `linkCitations(html, maxRef)` 只把 `1..证据数` 范围的 `[n]` 转可点击徽章（论文原文自带文献编号保持纯文本）；`renderMermaid` 渲染失败退化为可读源码块（不丢结构信息）。

#### M10.5 配置接口

- `config.py`：`chat_session_enabled`（`CHAT_SESSION_ENABLED`，默认开）/ `chat_session_window_turns`（`CHAT_SESSION_WINDOW_TURNS`，默认 4）。
- `interfaces/api.py`：`ChatRequest` 新增 `session_id`，`/chat` 透传。
- chat 档位经 `AGENT_MODEL_CHAT` env 切 low / mid / high（M7 `resolve_agent_model`），零代码改动；选定生产档位 DeepSeek(mid)，测试期切回 GLM low。

---

## 7. 编码规范

- 所有模块通过 `from config import settings` 拿配置，**禁止**散落 `os.environ`。
- 新增 tool：写 `@tool` 函数 + 清晰 docstring 第一行（会作为 schema description 给模型）。
- 新增 agent：继承 `agents/base.py::BaseAgent`，覆盖 `system_prompt` / `build_user_prompt` / `apply_result`。
- Skill 是给模型读的说明书（`skills/<name>/SKILL.md`），改流程优先改 SKILL.md，而非硬编码到 prompt。
- 类型标注尽量写全（tool schema 依赖类型推断）。
- 中文注释 OK，但对外字符串/日志保持清晰。

---

## 8. 红线（绝对不要做）

- ❌ 不要把整篇 PDF 塞进 LLM 上下文——必须经 RAG 分块召回。
- ❌ 不要让图谱产生无证据的「对立」断言。
- ❌ 不要在沙箱/本机随意起常驻网络服务（FastAPI 仅本地手动调试）。
- ❌ 不要把 `.env` 提交到 git。
- ❌ 不要为了用框架而用框架——核心 loop 保持自研可控。

---

## 9. 验收标准

- **M1 达标**：`ingest` 一个 PDF 目录后，`map "<topic>"` 能产出基础图谱 + 综述初稿。
- **M2 达标**：综述里每条引用都能回溯到库内真实片段（零幻觉），图谱每条边有 rationale。
- **M3 达标**：支持 `resume <task_id>` 断点续跑；飞书能收到完成推送。
- **M4 达标**：LLM 调用具备重试退避 + 限流 + 主备降级（断网/限流不致整体失败）；embedding/检索命中缓存可显著降调用次数；每个任务可导出含耗时/token 的结构化链路日志；FastAPI 能异步建任务、查状态、健康检查。
- **M7 达标**：不同 Agent 按配置走不同档位模型（简单任务下沉低档省钱、综述上浮高档保质量）；未配置分发时无损回退默认单模型（保留主备降级）；日志可见每步实际所用 `model`。

---

## 10. Memory 架构设计（Agent 记忆系统）

> 用 Agent 开发的标准记忆分层（工作 / 情景 / 语义 / 程序）来审视本项目。**核心判断：横向（单任务内）记忆完整，纵向（跨任务沉淀）记忆几乎为零**——项目擅长「把一件事做完且能中途恢复」，但不擅长「做完后变得更聪明」。

### 10.1 四层记忆现状

| 层 | 落点 | 状态 | 说明 |
|---|---|---|---|
| **L1 工作记忆**（单次 loop 内） | `core/agent_loop.py` 的 `messages` + `harness.compress_context` | ✅ 扎实 | think/act/observe 沉在消息链；超预算时**确定性摘要**旧轮次（不调 LLM），并用 `_is_safe_boundary` 保护 tool_call 配对不被裁断。 |
| **L2 情景记忆**（跨 step / 会话） | `Blackboard` + `tasks` 表 checkpoint | ⚠️ 只用了一半 | 黑板是「本次任务发生了什么」的完整情景，能存盘 → `resume` 续跑。**但仅服务于断点恢复，从未当作「过往经验」复用**。 |
| **L3 语义记忆**（长期事实） | 片段级：ChromaDB + embedding 缓存；卡片级：`memory/__init__.py::memory_cards` | 🔶 片段成熟 / 卡片空转 | 片段级是 RAG 地基，跨任务复用良好。**卡片级 `remember_card/recall_card` 已写好但零调用**——同一篇论文换 topic 就从头精读（烧 token）。 |
| **L4 程序记忆**（怎么做事 / 偏好） | 规则：各 Agent `system_prompt`（硬编码）；偏好：`memory/__init__.py::user_profile` | ❌ 静态 / 空表 | 规则写死在 prompt，不可学习；`user_profile` 仅建表，读写是 TODO，关注方向/常用检索词/综述风格均未沉淀。 |

读写时机现状：工作记忆每轮自动管理；片段级语义记忆由 Retriever/Reader 主动召回；checkpoint 每个关键步骤后写盘；**卡片库与用户画像从不写**。

### 10.2 目标架构蓝图

设计原则沿用项目红线：**确定性优先、可回溯、不引框架、分层不混淆**。

```
┌─────────────────────────────────────────────────────────────────┐
│ L1 工作记忆   [保持] messages + compress_context                   │
├─────────────────────────────────────────────────────────────────┤
│ L2 情景记忆   [升级] tasks 表 + topic 向量索引                      │
│              新任务启动按 topic 相似度召回历史图谱/结论, 作为"先验   │
│              提示"(人/Critic 可见、可拒绝), 不直接信任进产物         │
├─────────────────────────────────────────────────────────────────┤
│ L3 语义记忆   片段级[保持] ChromaDB                                │
│              卡片级[接通] memory_cards ←→ Reader                   │
│                写: 精读成功后存 topic 无关字段                      │
│                读: 命中则跳过主体精读, 仅按新 topic 重抽 stance      │
├─────────────────────────────────────────────────────────────────┤
│ L4 程序记忆   规则[保持] system_prompt                             │
│              偏好[接通] user_profile ──反哺──> expand_query        │
└─────────────────────────────────────────────────────────────────┘
```

### 10.3 三个关键设计决策（每层一个核心 tradeoff）

- **L3 卡片复用 → 分字段复用**（非整卡复用）：`PaperCard` 拆两类——
  - *topic 无关*（论文固有，可跨任务复用）：`title/authors/year/venue/core_claim/method/method_family/key_results/limitations/evidence_spans`
  - *topic 相关*（随研究方向变化）：`stance_tags/opposes`
  - 命中记忆时复用 topic 无关字段、按新 topic 轻量重抽 stance，既省掉最贵的主体精读，又避免「A topic 的立场套到 B topic」污染立场图谱。
- **L2 情景召回 → 提示参考而非自动信任**：历史图谱可能基于旧库 / 旧 topic，直接注入会污染新结论。仅作先验展示，由 Critic / 人决定是否采纳。
- **横切·防记忆污染**：记忆 ≠ 真相。所有跨任务召回的内容必须能在**当前向量库二次验证**（复用 Critic 的 quote 回查机制），验证不过的记忆降级或丢弃，绝不直接进最终产物。反思式写入也走确定性规则（如「仅通过 Critic 的卡片才 `remember`」），不引入 LLM 自反思黑盒。

### 10.4 落地优先级

| 优先级 | 项 | 价值 / 风险 |
|---|---|---|
| **P0 ✅ 已落地（M6）** | L3 卡片级接通（分字段复用 + Critic 写入闸门） | 价值最高（直接省 token），风险低，与 M5 增量入库同源 |
| **P1** | L2 情景召回（topic 相似先验，仅展示不信任） | 提升新任务起点质量，需为 task 加 topic 向量索引 |
| **P2（暂不做）** | L4 `user_profile`（确定性统计沉淀 → 反哺 `expand_query`） | 个性化，价值中等，**易过度设计，优先级低** |
| **P3（暂不做）** | L1 摘要分级、L2/L3 反思式写入 | 锦上添花，**优先级最低** |

> 当前结论：**P0 已落地（见 M6）**——卡片级语义记忆接通，只存 topic 无关字段、仅 Critic 通过后写入、命中后按新 topic 重抽 stance。P1 视重复跑库频率再启动；P2 / P3 暂不投入，避免过度设计。

### 10.5 两套记忆的边界（card memory vs memory2）

本项目存在**两套互不干扰**的长期记忆，服务对象不同，不要混用：

| | `memory/`（card memory，M6） | `memory2/`（聊天记忆，M8） |
|---|---|---|
| 服务对象 | ReaderAgent（map 流程） | ChatAgent（对话问答） |
| 存什么 | 论文 topic 无关字段（精读结果） | 用户 preference / procedure |
| 表 | `memory_cards`（单表，paper_id 主键） | `memory_items` + `memory_replacements`（双表） |
| 写入闸门 | Critic 通过后 | 对话中显式偏好/规则触发 |

**红线**：M8 新增 memory2 **绝不改动** `memory/memory_cards`；ChatAgent 的记忆只承载偏好/规则，**不替代 RAG 证据**（论文事实只来自 `hybrid_search`）。memory2 完整设计见 M8.4。
