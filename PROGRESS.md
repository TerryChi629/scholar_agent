# ScholarStance 项目进展记录

> 本文档逐步记录项目的搭建与开发过程，供他人了解每一步做了什么。
> 图例：🤖 = AI 编码助手完成 ｜ 👤 = 项目负责人（你）本人配置 / 决策

---

## 项目简介

**ScholarStance** 是一个本地运行的多智能体科研助手。核心能力：把某个研究方向的论文
自动组织成「立场图谱」（谁与谁观点对立、方法如何演进、研究空白在哪），并产出带**真实引用**的综述初稿。

- 架构：五层（集成层 / Harness / 编排层 / 执行层 / 底座层）
- 通信：Agent 之间不直接对话，统一通过共享黑板 `core/blackboard.py` 读写中间产物
- 红线：核心 Agent Loop 自研（禁用 LangChain/AutoGen/CrewAI）、不把整篇 PDF 塞进上下文、图谱不下无证据断言

---

## 时间线

### 阶段 0：理解项目（对齐认知）

- 🤖 完整阅读项目宪法 `CLAUDE.md`，梳理五层架构、数据契约、设计原则、TODO 优先级与红线
- 🤖 浏览全部已有代码，确认：
  - 黑板通信机制（`core/blackboard.py` 的 `Blackboard` / `PaperCard` / `StanceGraph`）
  - 工具注册机制（`tools/__init__.py` 的 `@tool` 装饰器 + `core/tool_registry.py` 自动生成 schema）
  - 自研 ReAct 循环（`core/agent_loop.py`）
  - 定位全部 14 处 `TODO(Trae)` 占位

### 阶段 1：搭建并验证环境

- 🤖 发现系统默认 Python 为 3.9.6，不满足宪法要求的 3.11+；改用机器上已有的 **Python 3.11.15**
- 🤖 创建虚拟环境：`python3.11 -m venv .venv`
- 🤖 安装依赖：`pip install -r requirements.txt`（openai / chromadb / pymupdf / rank-bm25 等）
- 🤖 从模板复制配置文件：`cp .env.example .env`（**仅复制，密钥占位符未改动**）
- 🤖 运行自检：`python main.py selfcheck` → **输出「自检通过」**，9 个 tool 全部注册成功，黑板序列化往返正常

> ✅ 此时脚手架完整，环境就绪，等待填入 API key 即可联网运行。

### 阶段 2：编写本进展文档

- 🤖 创建本文件 `PROGRESS.md`，用于记录全过程并区分 AI / 人工分工

### 阶段 3：M1 任务 1 —— `rag/ingest.py` 语义分块 + 元数据抽取

- 👤 提供 GLM API key 并选择 GLM 路线（key 由你本人填入 `.env`，AI 未写入任何文件）
- 👤 提供测试 PDF 文件夹 `/Users/bytedance/Desktop/codes/scholar_agent/test_paper`（4 篇生成式推荐论文：HSTU / OneRec / TIGER / VQ）
- 🤖 探查真实 PDF 文本结构，确认章节标题可用「字号显著大于正文」稳健识别
- 🤖 重写 `rag/ingest.py`：
  - 按 PyMuPDF dict 模式抽取每行文本 + 字号 + 页码
  - 以字号 + 章节关键词识别标题，按 section 切分（Abstract/Introduction/Related Work/Method/Experiments/Conclusion 等），section 内再按字符预算二次切分，**切分不跨 section**
  - 每个 chunk 保留 `section` + `page` 元数据（为后续 evidence_spans 页码回溯铺路）
  - 元数据抽取：title（首页最大字号行，跳过 arXiv 水印）、year（PDF creationDate → arXiv 编号 → 正文 4 位年份）
  - 过滤作者署名 / 邮箱 / arXiv 水印等噪声行
- 🤖 用 4 篇真实 PDF 验证（不调用 API，直接验证解析）：标题、年份全部正确，核心章节识别准确，分块合理
- 🤖 运行 `python main.py selfcheck` → **仍通过**，脚手架未破坏

### 阶段 4：首次真实联网入库（ingest 跑通）

- 👤 在 `.env` 填入 GLM key 后，要求跑一次真实流程
- 🤖 验证 GLM embedding 连通性正常（embedding-3，2048 维）
- 🤖 首次 `ingest` 报错：GLM embedding 单次最多 64 条，而单篇论文有 90+ chunk
- 🤖 修复 `core/llm.py::APIEmbedder.embed`：按 64 条/批分批请求（兑现原 TODO）
- 🤖 重新运行 `python main.py ingest test_paper` → **成功**：4 篇论文共 444 个 chunk 全部写入 ChromaDB
- 🤖 真实检索验证：query「semantic ID for generative retrieval」召回正确，命中片段均带 title/section/page 元数据
- 🤖 运行 `python main.py selfcheck` → **仍通过**

> ⚠️ 提醒：`.env` 中 `LLM_PROVIDER` 当前为 `deepseek`，仅 embedding 走 GLM。
> 后续 `map` 命令会用 chat，需把 `LLM_PROVIDER` 改为 `glm`（否则 chat 会因无 DeepSeek key 失败）。

### 阶段 5：新增需求 —— 强化后端工程属性（写入项目宪法 M4）

- 👤 提出需求：项目要更贴合「后端主导型 AI Agent 岗」，重心从 agent/算法往后端工程（稳定性/高可用/可观测/缓存/接口）倾斜
- 🤖 对照现有脚手架分析缺口，确认相关 TODO 接口已预留（`core/llm.py` 重试、`core/harness.py` 可观测、`interfaces/api.py` 异步接口）
- 🤖 在 `CLAUDE.md` 新增里程碑 **M4（后端工程化）**，含 5 类任务并精确指向现有代码位置：
  - 稳定性：API 重试退避 + 限流（`core/llm.py`）
  - 高可用：主备模型降级，可配置兜底模型（`core/llm.py` + `config.py`）
  - 成本：Embedding / 检索 / 已读卡片缓存复用（`core/llm.py` + `rag/store.py`）
  - 可观测：结构化日志 + 每步耗时/token 埋点（`core/harness.py` + trace）
  - 接口：把 FastAPI 做实（异步任务、状态/进度、健康检查）（`interfaces/api.py`）
- 🤖 同步在第 9 节补 **M4 验收标准**，保持宪法自洽
- 🤖 运行 `python main.py selfcheck` → **仍通过**（纯文档改动，未触碰代码）

### 阶段 6：M1 任务 2 —— `rag/retrieve.py` BM25 + RRF 混合检索

- 🤖 在 `rag/store.py` 新增 `all_chunks()`：取全量 chunk（不含向量），供 BM25 建索引
- 🤖 重写 `rag/retrieve.py`：
  - 向量召回（语义） + BM25 召回（关键词字面匹配，用 `rank-bm25`）两路并行
  - 用 **RRF（Reciprocal Rank Fusion）** 融合两路排名（只看位次、规避分数量纲不可比）
  - 两路各多召回 3×top_k 再融合裁剪，提升召回覆盖
  - `year_min` 元数据过滤在两路同时生效；库为空时优雅退化为纯向量
  - 接口签名 `hybrid_search(query, top_k, year_min)` 保持不变，上层 `tools/rag_query` 无需改动
- 🤖 用已入库的 444 chunk 真实验证：
  - 语义 query「RQ-VAE semantic ID...」→ 正确命中 TIGER 的 Methods 章节
  - 关键词 query「HSTU pointwise aggregated attention」→ BM25 字面匹配把 HSTU 正确顶上来
  - `year_min=2025` 过滤 → 只返回 2025 年论文
- 🤖 运行 `python main.py selfcheck` → **仍通过**

### 阶段 7：M1 任务 3-5 —— 打通 map 完整闭环

- 🤖 实现 `agents/retriever.py::apply_result`：
  - 从模型输出/trace 抓 12 位 hex paper_id，**只接受向量库真实存在的 id（防幻觉）**
  - 检索按论文聚合（取每篇最佳 chunk 得分排序），避免单篇 chunk 挤占名额，保证候选覆盖
- 🤖 实现 `agents/reader.py::_parse_card`：健壮化 JSON 解析（容忍 ```json 围栏、前后废话、非法 JSON、list/year 类型规整），并用库内真实元数据补全 title/year
- 🤖 修复跨篇串味 bug：`rag_query` / `hybrid_search` 增加 `paper_id` 过滤；Reader 精读时锁定目标论文，避免检索到其它论文内容（守住「证据可回溯」红线）
- 🤖 真实跑通 `python main.py map "生成式推荐中的语义ID与生成式检索"`：
  - 4 篇候选全部召回 → 逐篇精读生成 PaperCard → 状态 done
  - 卡片质量：title/year 正确补全；method_family 区分清晰；evidence_spans 2-8 条可回溯；opposes 关系真实（如 HSTU↔传统 DLRM、OneRec↔多阶段排序）
- 🤖 运行 `python main.py selfcheck` → **仍通过**

> ✅ **M1 里程碑达成**：`ingest` + `map` 端到端跑通，产出带真实引用元数据的论文卡片。
> 说明：`cluster_cards/build_graph/detect_gaps`（聚类建图、综述落盘）属 M2 范围，当前为骨架，故 `产物` 暂空。

### 阶段 8：M2 —— 深度建图 + 防幻觉质量闸门

- 🤖 新增 `core/run_context.py`：进程内「当前任务黑板」持有器。解决工具无状态、读不到黑板的约束，使建图类工具能读卡片做**确定性计算**（而非把整堆卡片塞进 LLM 上下文 → 防幻觉、省 token）
- 🤖 实现 `tools/__init__.py` 三个核心工具（全部确定性、不靠 LLM 解析）：
  - `cluster_cards`：按 `method_family` 聚类卡片
  - `build_graph`：流派/论文/观点节点 + opposes/supports 边，每条边写 rationale，opposes 边尽量挂上来自 `evidence_spans` 的引用，结果写回 `bb.graph`
  - `detect_gaps`：启发式识别研究空白（孤立流派 / 高频未解决局限 / 有对立无演进）
  - `export_md`：落盘后把路径登记到 `bb.artifacts`
- 🤖 `agents/synthesizer.py::apply_result`：兜底确保图谱已建、综述有效落盘。LLM 写占位/过短内容时，自动用**确定性综述骨架**（由卡片+图谱拼装，真实可回溯）覆盖
- 🤖 `agents/critic.py`：改为**确定性回查**质量闸门——① 完整性（候选都有卡片）② 引用真实性（`evidence_spans` 的 quote 能在库内该论文检索回溯）③ 关系证据（opposes 边是否有 evidence），写结构化 `critic_feedback`
- 🤖 `agents/orchestrator.py`：Reader **并行精读**（`ThreadPoolExecutor`，受 `reader_concurrency` 限制）+ Critic 反馈定向重调度（只对可补救问题重跑，避免无效空转省 token）
- 🤖 修复数据健壮性 bug：`evidence_spans` 元素可能是 str 或 dict，统一用 `_spans_to_quotes` 兼容
- 🤖 真实跑通 `map`：4 篇并行精读 → 图谱 14 节点/10 边/5 空白 → 综述 7040 字落盘 → Critic 正确判定（4 条 opposes 边缺证据）且不空转重试
- 🤖 运行 `python main.py selfcheck` → **仍通过**

> ✅ **M2 里程碑达成**：立场图谱（节点+对立边+证据）+ 综述初稿落盘 + 防幻觉 Critic 闸门 + Reader 并行，全链路真实跑通。

### 阶段 9：接入 Git 版本管理

- 👤 决策：仓库根放在 `scholarstance/`；PDF（`test_paper/`）与 zip 包不纳入版本管理；**不动全局 git config**（本机有其他账户）
- 👤 本人设置**仓库级**身份（仅作用于本仓库，不影响全局）：`git config user.name TerryChi629` / `git config user.email 1125605344@qq.com`
- 🤖 校对 `.gitignore`：确认 `.env`（含密钥）、`.venv/`、`storage/`、`*.db`、`*.log`、`__pycache__/` 均已忽略；仅保留 `.env.example` 模板入库
- 🤖 `git init -b main` 初始化；`git add -A` 后核对暂存区，确认无任何密钥/虚拟环境/运行产物（37 个源码与文档文件）
- 🤖 首次提交 `feat: ScholarStance M1+M2 初始版本`（root-commit `a340383`，37 files）
- 👤 本机已配置 SSH key，遂将远程切为 SSH：`origin = git@github.com:TerryChi629/scholar_agent.git`
- 🤖 `git push -u origin main` 推送成功，`main` 已与远程建立追踪

> ✅ **代码已托管**：远程仓库 `https://github.com/TerryChi629/scholar_agent.git`，后续可正常增量提交。

### 阶段 10：M3 —— 工程化 + 集成

- 🤖 **上下文压缩 + token 计数**（`core/harness.py`）：实现 `estimate_tokens`（CJK 1 token/字、其余 1 token/4 字符的保守估算，不依赖 tiktoken）、`compress_context`（超 `context_token_budget` 时保留 system + 最近若干轮，旧轮次**确定性摘要**为一条历史消息）。关键：压缩时严守 **tool_call 配对**，绝不从悬空的 `tool` / 带 `tool_calls` 的 assistant 中途切入。接入 `core/agent_loop.py` 每轮 LLM 调用前，长对话防爆 token
- 🤖 **立场图谱可视化**（`tools/export_graph_html`）：重写为 **vis-network** 渲染——节点按类型着色（流派/论文/外部观点），边按关系着色（opposes 红 / supports 绿），悬停显示 rationale 与证据，附研究空白清单。**确定性可视化**：直接读黑板 `bb.graph`（`build_graph` 已写回的真实结果），不让 LLM 生成图结构。在 `synthesizer.apply_result` 兜底里始终导出 HTML
- 🤖 **飞书 Webhook 推送**（`interfaces/feishu.py` + `orchestrator`）：任务完成后在 `Orchestrator.run` 调用 `notify_task_done`，推送方向 + 产物路径；未配置 `FEISHU_WEBHOOK_URL` 时静默跳过，通知失败不中断主流程
- 🤖 **MCP filesystem 接入**（`mcp_clients/`）：
  - 👤 本机无 node → 选「先装 node 再接官方 server」；🤖 用 brew 装 node 26.3.0、pip 装官方 `mcp` SDK（1.28.0）
  - ⚠️ 真实联调发现：企业网络对 HTTPS 做 SSL 拦截（自签 CA），`npx` 无法验证证书拉取官方 `@modelcontextprotocol/server-filesystem`（`UNABLE_TO_GET_ISSUER_CERT_LOCALLY`）
  - 🤖 改用**自建 Python MCP server**（`mcp_clients/fs_server.py`，基于官方 `mcp` SDK 的 FastMCP）：实现标准 MCP 协议，client 以 stdio 子进程拉起，**不依赖 node/网络**，同样证明「协议化能力接入」。暴露 `list_directory` / `read_text_file`，并做**目录穿越防护**（路径收敛到授权根目录）
  - 🤖 包装成 `@tool`：`fs_list_dir` / `fs_read_file`，与本地工具同构，对 Agent 透明
  - 🤖 真实跑通：`list_tools` 握手成功、读文件正确、越权路径被安全拒绝
- 🤖 运行 `python main.py selfcheck` → **仍通过**（已注册 tools 由 9 增至 11）

> ✅ **M3 里程碑达成**：上下文压缩（防爆 token）+ 立场图谱可交互可视化 + 飞书完成推送 + MCP 协议化接入，全部真实验证。
> 说明：MCP 因企业网络 SSL 限制改为自建 Python server，规避了对 node/外网的依赖，落地更稳。

### 阶段 11：M4 —— 后端工程化（稳定性 / 高可用 / 可观测 / 成本）

> 本里程碑专门补齐**生产级后端工程能力**，对齐「后端主导型 AI Agent 服务」定位。全部基于现有脚手架补齐，不打破五层分层、不引入重型框架。

- 🤖 **配置出口先行**（`config.py` + `.env.example`）：新增 M4 配置项并给默认值，全部走 `settings`、零硬编码——`FALLBACK_CHAT_MODEL`（兜底模型）、`LLM_MAX_RETRIES` / `LLM_BACKOFF_BASE` / `LLM_MIN_INTERVAL`（重试退避+限流）、`CACHE_ENABLED` / `RETRIEVAL_CACHE_TTL`（缓存）、`LOG_LEVEL` / `LOG_TO_FILE`（日志）
- 🤖 **结构化可观测底座**（`core/obs.py`，新增）：标准库 `logging` + 自定义 `_JsonFormatter`，每条日志一行 JSON（`ts/level/event` + 任意业务字段平铺）；同时输出 stderr 与可选 `storage/scholarstance.log`（文件不可写不中断主流程）；提供 `log_event()` 与计时上下文 `timed()`
- 🤖 **稳定性 — 重试退避 + 限流**（`core/llm.py`）：`_retry_call` 对 `chat` / `embed` 做**指数退避**重试（`backoff_base * 2^n`，最多 `llm_max_retries` 次），精确分类**仅对 429 / 5xx / 超时 / 连接错误重试**，其余直接抛；`_RateLimiter`（线程安全）实现客户端**最小请求间隔**限流，间隔 ≤0 时直通
- 🤖 **高可用 — 主备模型降级**（`core/llm.py`）：主模型重试耗尽后，若配置了不同的 `fallback_chat_model` 则**自动降级**重试一次，降级动作经 `log_event("llm.fallback")` 落结构化日志
- 🤖 **成本 — 三级缓存**：
  - Embedding 内容缓存（`_EmbedCache`，SQLite 持久化）：按 `model + 文本 hash` 命中，只对**未命中**的文本发起 API 请求，相同文本零重复调用；真实验证：重复 embedding 同一批文本第二次 API 调用数为 0
  - 检索结果 TTL 缓存（`rag/retrieve.py`）：相同 `query + top_k + year_min + paper_id` 在 `retrieval_cache_ttl` 秒内复用上次结果（进程内、线程安全）
  - 已读卡片复用（`agents/reader.py`）：同 `paper_id` 已有非空 `PaperCard` 时直接复用，跳过整轮 Reader loop
- 🤖 **可观测 — 耗时/token 埋点**（`core/agent_loop.py` + `core/blackboard.py` + `agents/base.py`）：每轮 think 记 LLM 耗时与 `prompt/completion/total tokens`，每个 act 记工具耗时与成败；汇总 `usage` 随 `LoopResult` 返回，经 `accumulate_usage` 累计进黑板新增的 `Blackboard.usage` 字段（已同步 `from_json`），任务级链路可追溯
- 🤖 **接口做实**（`interfaces/api.py`）：`GET /tasks` 列表分页（limit/offset）、`GET /tasks/{id}` 返回 `done`/`error`/`nodes`/`usage` 进度态、后台任务异常落盘为 `FAILED` 供查询感知、新增 `GET /healthz` 健康检查（进程存活 + 向量库可达）
- 🤖 运行 `python main.py selfcheck` → **仍通过**；全模块 import 校验通过

> ✅ **M4 里程碑达成**：LLM 调用具备重试退避 + 限流 + 主备降级；embedding/检索/卡片三级缓存显著降调用；可导出含耗时/token 的结构化链路日志；FastAPI 能异步建任务、分页查状态、健康检查。

---

## 你（👤）需要本人完成的配置

### 1. 填入 LLM + Embedding API key（必需）

编辑 `.env` 文件，二选一路线：

- **DeepSeek 路线**（chat 用 DeepSeek，embedding 仍需 GLM，因为 DeepSeek 无 embedding 接口）
  ```
  LLM_PROVIDER=deepseek
  DEEPSEEK_API_KEY=sk-你的key
  EMBED_PROVIDER=glm
  EMBED_API_KEY=你的GLM-key
  ```
- **GLM 路线**（一个 key 同时覆盖 chat + embedding）
  ```
  LLM_PROVIDER=glm
  GLM_API_KEY=你的GLM-key
  EMBED_API_KEY=你的GLM-key
  ```

> `map`（生成图谱）走 chat；`ingest`（入库）走 embedding。完整闭环两者都需要。

### 2. 准备本地 PDF 文件夹（必需）

- 👤 自建一个目录，放入同一研究方向的论文 PDF（建议 3-5 篇有观点交锋的，立场图谱价值在跨篇对比）
- 运行入库：`python main.py ingest /你的/pdf/文件夹`

### 3. 无需手动准备的

- 向量库（ChromaDB）与 SQLite 会在首次运行时自动在 `storage/` 下创建

---

## 分工总览

| 事项 | 负责方 |
|---|---|
| 阅读宪法、理解架构、定位 TODO | 🤖 AI |
| 创建 venv（Python 3.11.15） | 🤖 AI |
| 安装依赖 requirements.txt | 🤖 AI |
| 复制 `.env`（不含密钥） | 🤖 AI |
| 运行 selfcheck 验证脚手架 | 🤖 AI |
| 本进展文档 PROGRESS.md | 🤖 AI |
| **填入真实 API key** | 👤 你 |
| **准备本地 PDF 文献** | 👤 你 |
| 后续按 M1→M2→M3 实现 TODO | 🤖 AI（你确认后逐个推进） |

---

## 后续计划（按 CLAUDE.md 里程碑，逐个推进）

- **M1（闭环跑通）**：语义分块入库、BM25+RRF 检索、Retriever/Reader 解析、跑通 `ingest` 与 `map`
- **M2（深度 + 防幻觉）**：聚类/建图/空白识别、图谱写回黑板、Critic 真正回查证据
- **M3（工程化 + 集成）**：上下文压缩、MCP 接入、飞书推送、图谱 HTML 可视化

> 每完成一个任务都会重新运行 `python main.py selfcheck` 确保脚手架未被破坏。
