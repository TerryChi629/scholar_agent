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
| 检索 | Hybrid（向量+BM25+rerank） | 当前只实现向量，BM25/rerank 是高优 TODO |
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
- [ ] `core/harness.py`：上下文压缩 `compress_context`、token 计数。
- [ ] `mcp_clients/`：接官方 filesystem-mcp，包装成 `@tool`。
- [ ] `interfaces/feishu.py`：任务完成 Webhook 推送。
- [ ] `tools/export_graph_html`：vis-network / mermaid 可视化。

### M4（后端工程化：稳定性 / 高可用 / 可观测 / 成本）

> 背景：本项目定位为**后端主导型 AI Agent 服务**。AI/RAG 是业务能力，**后端工程（稳定性、高可用、可迭代、可观测）才是核心壁垒**。本里程碑专门补齐生产级工程能力，四个关键词贯穿始终：**稳定性、高可用、可观测、成本可控**。这些功能大多在现有脚手架已留接口，补齐即可，**不得打破第 2 节分层、不得引入重型框架**。

- [ ] **稳定性 — API 重试退避 + 限流**（`core/llm.py::LLM.chat` / `APIEmbedder.embed`）
  - chat 与 embed 包一层指数退避重试（如 1s/2s/4s，最多 3 次），专门捕获 `429`（限流）与 `5xx`（服务端错误），其余错误分类后直接抛出。
  - 客户端侧加轻量限流（令牌桶或最小请求间隔），避免突发打爆厂商配额。
- [ ] **高可用 — 主备模型降级**（`core/llm.py` + `config.py`）
  - 主模型连续失败/超时后，自动降级到兜底模型（如 GLM-flash 这类更便宜稳定的型号），降级动作记入 trace。
  - 降级为**可配置**：在 `config.py` 暴露 `fallback_chat_model`，禁止硬编码。
- [ ] **成本 — Embedding / 检索缓存**（`core/llm.py::APIEmbedder` + `rag/store.py`）
  - Embedding 缓存：对相同文本（按内容 hash）命中本地缓存（SQLite/磁盘），不重复调 API。
  - 检索结果缓存：相同 query + 参数在 TTL 内复用上次结果。
  - 已读论文卡片复用：相同 `paper_id` 已生成的 `PaperCard` 优先复用，避免重复 Reader 调用。
- [ ] **可观测 — 结构化日志 + 耗时/token 埋点**（`core/harness.py` + `core/agent_loop.py` trace）
  - 统一结构化日志（JSON 行），每条带 `task_id / agent / step / 耗时ms / token用量 / 是否重试或降级`。
  - 每个 think/act/observe 步骤记录耗时与 token，汇总到黑板 trace，便于链路追踪与问题排查。
- [ ] **接口 — 把 FastAPI 做实**（`interfaces/api.py`）
  - 任务异步化已有骨架（立即返回 `task_id`）；补：错误态/进度态返回、`/tasks` 列表分页、健康检查 `/healthz`。
  - 保持「不在沙箱起常驻服务」红线，仅作本地手动调试与接口契约。

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
