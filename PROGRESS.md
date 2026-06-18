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

### 阶段 12：M5 —— RAG 质量提质（召回 / 抽取 / 防幻觉）

> 背景：M1-M4 跑通闭环与工程化底座后，实跑发现弱模型（glm-4-flash）下抽取雷同空泛、查询扩展为占位、跨篇串味、空证据卡片免检等质量短板。本阶段做一轮**确定性增强**改造（先评审 A-J 十项方案，敲定做 A/B/C/D/E/F/I/J），不改五层分层、不引入重型框架。

- 👤 以「资深 RAG / 多 Agent 架构评审专家」视角评审 A-J 十项方案，敲定实施集合并定路径：C 用**代码层预检索注入**、A 仅做 **ingest 增量+幂等**（不在 map/ask 自动触发）
- 🤖 **A 增量入库 + 幂等写入**（`rag/ingest.py` + `rag/store.py`）：
  - `ingest_dir` 开头取 `store.list_papers()` 已有 `paper_id`，遍历 PDF 时**已存在的直接跳过**（不解析、不 embedding），返回统计扩为 `{papers, added, skipped, chunks, detail}`
  - `VectorStore.add` 由 `_col.add` 改 `_col.upsert`：同 `chunk_id` 重复写入覆盖而非冲突报错（修掉重复入库真 bug）
- 🤖 **B 查询扩展动态化**（`tools/__init__.py::expand_query`）：
  - 由 stub（`return [topic]`）改为 LLM 动态生成 5-8 个查询（中文表述 + 英文术语 + 核心方法词），temperature=0.2
  - 严格约束「紧扣原方向、严禁引入无关领域」（防主题漂移），**绝不硬编码任何示例主题词**
  - 解析 JSON 数组；失败/为空一律回退 `[topic]`；结果强制含原 topic 并去重、上限 8
- 🤖 **C 多维度精读 + E 跨维度去重**（`agents/reader.py`）：
  - 新增 `_pre_retrieve`：对 4 个核心维度（核心贡献/方法/实验/局限）各 `hybrid_search`，跨维度按 `chunk_id` 合并去重，把真实原文片段（带 section 标注）注入 prompt
  - `run_for` 改为「预检索注入 + 仍保留 loop 补充检索」，给弱模型喂真材实料而非盲目多轮试探，提升抽取深度与区分度
- 🤖 **D paper_id 自动注入**（`core/run_context.py` + `tools/rag_query` + `agents/reader.py`）：
  - `run_context` 新增 `threading.local()` 持有「当前线程精读的 paper_id」（与并行 Reader 隔离），提供 `set/get_reader_paper_id`
  - Reader `run_for` 进入时登记、`finally` 清除；`rag_query` 在模型**漏传 paper_id** 时自动注入当前精读 pid（记 `rag_query.inject_paper_id` 日志），**不报错打断**，彻底根治跨篇串味
- 🤖 **F 极轻量规则 rerank**（`rag/retrieve.py`）：
  - 新增 `_rule_rerank`：RRF 融合后叠加确定性弱加分——核心章节（method/experiment/abstract...）命中 +0.05、query 关键词命中按数量 +0.01/个（上限 0.05）
  - 加分系数远小于 RRF 量级，**仅在并列/接近时决胜，不颠覆语义主排序**；纯确定性、无模型、无额外 IO；`_hybrid_search_uncached` 先多取再 rerank 后裁剪到 top_k
- 🤖 **I evidence 为空即打回**（`agents/critic.py`）：
  - 补齐漏洞：此前 `evidence_spans` 整体为空的卡片反而免检通过。新增 `_empty_evidence_cards`，空证据卡片直接计入 `issues` 并 `passed=False`，suggestion 指向重读
- 🤖 **J 模板化/雷同检测（告警不击杀）**（`agents/critic.py`）：
  - 新增 `_detect_templated`：①套话词典（如「本文提出了一种新颖」「取得了显著」「state-of-the-art」等）命中 `core_claim`/`method`；②跨卡 `core_claim` 词集合 Jaccard ≥0.7 判雷同
  - 命中只 `log_event("critic.template_warn")` + 写入 `critic_feedback.warnings` 与 suggestion，**不改变 passed**（避免误杀真实但简短的结论）
- 🤖 验证：`python main.py selfcheck` → **仍通过**（11 tools 不变）；针对性单测确认 F rerank 正确上浮核心片段、I 识别空证据卡、J 命中套话告警、D thread-local 注入正常

> ✅ **M5 里程碑达成**：增量幂等入库、动态查询扩展（防漂移）、多维度预检索注入、paper_id 自动注入（防串味）、规则 rerank、空证据打回、模板化告警，全部确定性增强、`selfcheck` 通过。

### 阶段 13：M6 —— Agent Memory（卡片级语义记忆接通，P0）

> 背景：以 Agent 开发的四层记忆（工作/情景/语义/程序）审视项目，发现「横向单任务记忆完整、纵向跨任务沉淀几乎为零」。`memory/__init__.py` 的卡片库读写写好了却零调用——同一篇论文换 topic 就从头精读、重复烧 token。本阶段只做蓝图里的 **P0：卡片级语义记忆接通**（分字段复用 + Critic 质量闸门），不碰 L2 情景召回 / L4 用户画像（P1-P3 暂缓）。详见 CLAUDE.md 第 10 节。

- 👤 决策过程：先讨论「项目是否已有 memory 设计」（结论：有，但「设计了一半、跨任务沉淀留接口未接通」），再定「先做一点」，由助手拍板做 P0 最小闭环
- 🤖 **memory 模块语义升级**（`memory/__init__.py`）：
  - 定义 `TOPIC_INVARIANT_FIELDS`（title/authors/year/venue/core_claim/method/method_family/key_results/limitations/evidence_spans）——**topic 无关**字段才可跨任务复用
  - `remember_card(paper_id, fields)` / `recall_card(paper_id)` 改为收发 dict，写入时**过滤掉 stance_tags/opposes**（topic 相关，防「A topic 立场套到 B topic」）；删除未接通的 `user_profile` 死表
- 🤖 **Reader 三段式命中**（`agents/reader.py`）：
  - 黑板命中（同任务）→ 记忆命中（跨任务）→ 完整精读，从快到慢
  - 新增 `_try_memory`：记忆命中则载入 topic 无关字段建卡，**跳过最贵的 4 维完整精读**，记 `reader.memory_hit`
  - 新增 `_extract_stance`：命中后针对**新 topic** 单次 `chat_text` 轻量重抽 `stance_tags/opposes`（不开 loop，输入=已存要点+对比维度预检索片段），失败降级为空不阻断
- 🤖 **写入质量闸门**（`agents/orchestrator.py`）：
  - 新增 `_remember_cards`：**仅当 Critic 通过后**才把卡片沉淀进 memory（`critic_passed` 标志位控制），杜绝低质卡片被缓存固化——回应「换强模型前先别缓存低质结果」的顾虑
- 🤖 **配置开关**（`config.py`）：新增 `memory_enabled`（`MEMORY_ENABLED`，默认开），关掉后行为退回纯完整精读
- 🤖 验证：`python main.py selfcheck` → **仍通过**（11 tools 不变）；针对性单测确认 ①memory 只存 topic 无关字段、stance 不泄漏 ②Reader 记忆命中走复用分支 + 按新 topic 重抽 stance + title 元数据补全

> ✅ **M6 里程碑达成（P0）**：卡片级语义记忆接通，分字段复用 + Critic 质量闸门写入，跨任务命中可跳过主体精读省 token；L2 情景召回 / L4 用户画像按蓝图暂缓。

### 阶段 14：飞书接入增强（A 富卡片 + B1 产物可达）

> 背景：飞书「姿势 1」在 M3 已实现，但只是**纯文本 + 本地路径**——排版差、产物群里点不开、无规模/成本/质量信息。本阶段讨论了三档方案（A 富卡片 / B1 静态托管 / B2 云文档 / C 双向回调）。**C（飞书里发起任务）评估为可行但与本地定位冲突**（需公网可达 + App 鉴权或内网穿透，破坏「本地运行、不起公网服务」红线），故采纳 **A+B1**；C/B2 留作后续可选升级。

- 👤 决策过程：倾向 C → 助手评估 C 硬卡点（飞书事件需公网回调，内网穿透/部署/长连接三条路均破红线或引重依赖）→ 改定 A+B1
- 👤 需本人完成：飞书群「添加自定义机器人」拿 Webhook，填 `FEISHU_WEBHOOK_URL`；安全设置选自定义关键词 `ScholarStance`
- 🤖 **A 富文本交互卡片**（`interfaces/feishu.py` 重写）：
  - `notify_task_done(topic, artifacts, stats)` 推送 `msg_type=interactive` 卡片，展示规模（论文/节点/边/空白）、成本（token/耗时）、质量（Critic 是否通过）
  - 卡片标题固定含 `ScholarStance` 关键词，兼容自定义机器人关键词安全校验；stats 缺省时优雅降级为「方向 + 按钮」
  - 产物按钮按文件名映射「查看立场图谱 / 查看综述」，`url` 指向 B1 托管地址
- 🤖 **B1 产物静态托管**（`interfaces/api.py`）：新增 `GET /artifacts/{name}`，只服务 `storage_dir` 直下文件，`resolve()` + 父目录校验**防目录穿越**；`.md` 纯文本内联、`.html` 浏览器渲染
- 🤖 **数据接力**（`agents/orchestrator.py`）：`run` 记任务耗时，`_notify_done(bb, critic_passed, elapsed_s)` 从黑板组装 stats（cards/nodes/edges/gaps/usage.total_tokens）
- 🤖 **配置**（`config.py` + `.env.example`）：新增 `PUBLIC_BASE_URL`（按钮指向的可达地址，默认 localhost；同内网协作填本机 IP）；补全此前遗漏的 `MEMORY_ENABLED` 示例项
- 🤖 验证：`selfcheck` 通过；卡片构造单测确认 URL 映射、关键词存在、完整 stats 渲染、stats 缺省降级均正确

> ✅ **飞书接入增强达成（A+B1）**：任务完成推送富信息交互卡片，按钮可点开内网托管的图谱/综述产物；双向回调（C）与云文档（B2）按定位暂缓。

### 阶段 15：M7 —— 混合模型分发（按 Agent 难度分档，平衡成本与质量）

> 背景：实跑发现单模型两难——全用 deepseek-v4-pro 质量好但一次任务约 0.64 元偏贵；全用 glm-4-flash 便宜但综述质量差（把 paper_id 当正文、引用错乱、立场分歧识别不出）。本阶段按「任务难度」给不同 Agent 分配不同档位模型：最简单的用 GLM，中等的用 ds-flash，最难的综述用 ds-pro，预计成本砍到原 pro 全程的 1/3~1/2。

- 👤 决策：提出「最傻逼的任务用 GLM、次一点用 flash、最难用 pro」，评审分档表后「全都认可」
- 🤖 **配置中枢**（`config.py` + `.env` + `.env.example`）：新增三档模型规格 `MODEL_TIER_LOW/MID/HIGH`（值为 `provider:model`，默认 `glm:glm-4-flash` / `deepseek:deepseek-v4-flash` / `deepseek:deepseek-v4-pro`）与 Agent→档位映射 `AGENT_MODEL_RETRIEVER/READER/SYNTHESIZER/CRITIC`（默认 low/mid/high/low）；新增 `credentials_for(provider)` 跨厂取凭证、`_tier_spec(tier)` 档位解析、`resolve_agent_model(agent)` 返回 `(provider, model, key, base_url)`。**关键**：Agent 档位留空时 `model=None`，调用方回退默认单模型（保留 M4 主备降级，不开启分发时行为完全不变）
- 🤖 **LLM 网关支持多模型 client 池**（`core/llm.py`）：`LLM` 维护 `self._clients: dict[provider, OpenAI]`，`_client_for(provider)` 懒加载缓存（同厂复用、跨厂各持一个）；`_create(provider, model, params)` 增 provider 参数；`chat()` 新增 `provider/model` 形参——**指定 model 走分发路径**（直接调用，重试退避仍生效，但不跨模型降级以免跨厂 404），**不指定走默认路径**（保留主备降级）；`chat_text` 透传 kwargs
- 🤖 **Loop 按 Agent 解析并透传**（`core/agent_loop.py`）：`run_loop` 内 `provider, model, _, _ = settings.resolve_agent_model(agent)`，`llm.chat(..., provider=provider, model=model)`，`agent.think` 日志加 `model` 字段。业务 Agent 零改动（`BaseAgent.run` 已传 `agent=self.name`）
- 🤖 **Reader 轻量重抽同档**（`agents/reader.py`）：`_extract_stance` 的 `chat_text` 也按 `resolve_agent_model("reader")` 带上 mid 档位，与主体精读保持同模型（否则这条记忆命中重抽会漏到默认模型）
- 🤖 验证：`resolve_agent_model` 冒烟——retriever→glm-4-flash、reader→deepseek-v4-flash、synthesizer→deepseek-v4-pro、critic→glm-4-flash、未配置/orchestrator→model=None（走默认）；解析全部正确

> ✅ **M7 里程碑达成**：Agent 级混合模型分发落地，三档可配 + client 池跨厂复用，未配置时无损回退单模型（保留主备降级）。简单任务下沉 GLM、综述上浮 ds-pro，兼顾成本与质量。

---

### 阶段 16：产物美化（综述 HTML + 图谱节点可读性）

> 背景：飞书推送的产物体验不佳——①综述只有 markdown 底稿，飞书内点开是纯文本不直观；②立场图谱论文节点直接用完整英文长标题做 label，互相重叠盖住连线，画布「很乱」。

- 🤖 **综述 HTML**（`tools/__init__.py` `export_review_html` + `synthesizer.py`）：引入 `python-markdown`，把 LLM 已产出的 markdown 确定性渲染为暗色主题 HTML（与立场图谱同款），**零额外 token**（排版不交给 LLM）；Synthesizer 落盘时同源生成 md 底稿 + 综述 HTML；飞书「查看综述」按钮指向 HTML，md 底稿不再重复出按钮（`interfaces/feishu.py`）
- 🤖 **图谱节点取名**（`tools/__init__.py` + `core/label_cache.py`）：论文节点 label 改用 LLM（low 档 GLM，最便宜）起的简短可辨识短名（如 `GenRet`/`StreamVecRet`/`TrilSeqTrans`），完整标题进 hover tooltip；**批量一次性取名 + SQLite 持久化缓存**（同标题只烧一次 token，重渲染零成本），LLM 失败逐条回退确定性 `_short_label`（取冒号前简称/首词截断）；vis-network 物理引擎调散（springLength 220 / avoidOverlap 0.6 / 节点宽度限制 140）缓解重叠
- 👤 决策：先确定性截短发现「信息损失太多」（如 `Real`/`Actions`），改用 LLM 取名；多模态视觉自检方案暂不做
- 🤖 验证：`_short_labels` 实跑——长标题取出可辨识短名，第二次调用全缓存命中 0.0s；selfcheck 通过（13 tools）

> ✅ 产物可读性达标：综述变美化 HTML，图谱节点短名清爽且信息不丢（全名在 tooltip）。

---

### 阶段 17：前端控制台（原生单页，FastAPI 托管）

> 背景：此前只有 CLI + API，缺一个能可视化操作的入口。新增需求：做一个前端，覆盖库内问答（ask）、PDF 入库（ingest）、查看产物、发起+追踪任务，并为后续 arXiv 检索留占位。技术栈「助手拿主意」、视觉「明亮简洁」。

- 👤 决策：核心场景=库内问答 / PDF 入库 / 查看产物 / 发起+追踪任务 + arXiv 占位（「留个接口，之后做去 arXiv 检索的功能」）；技术栈交助手定；视觉明亮简洁
- 🤖 **原生单页前端**（`web/index.html`，新增）：纯 HTML+CSS+JS、零构建链，贴合「本地轻量」红线。5 个标签页（任务台 / 产物 / 库内问答 / PDF 入库 / arXiv 占位）；明亮简洁风（CSS 变量 `--bg:#f6f8fa` / `--accent:#0969da`）
  - 任务台：`loadTasks`（GET /tasks 分页）、`showDetail` 详情（运行中每 3s 轮询）、发起任务（POST /tasks）
  - 产物：收集 done 任务的 `.html` 产物，iframe 预览 + 新窗口打开
  - 库内问答：POST `/ask?q=`，渲染命中卡片
  - PDF 入库：POST /ingest 展示结果；`checkHealth` 轮询 /healthz 显示库内 chunks 数
- 🤖 **FastAPI 托管**（`interfaces/api.py`）：新增 `GET /`，直接读 `web/index.html` 返回 HTMLResponse（缺失时 404 提示），零构建、零静态服务器
- 🤖 arXiv 留占位：前端标签页 + 后端 `search_arxiv` 占位工具，等后续设计

> ✅ 前端控制台落地：一个页面覆盖问答 / 入库 / 产物 / 任务全流程，FastAPI 直接托管，arXiv 留接口。

---

### 阶段 18：检索质量修复（体感 + 真质量 + 分块）

> 背景：库内问答实跑「很搓」——①结果裸露 RRF 原始分（0.0x 量级，0.067 实为最高分却像「全低分」）；②中文 query 在 BM25 那路完全失效（分词只取英文数字），hybrid 退化为单腿向量；③片段切碎（出现 "3.1"、句中断开如 "igned for high cardinality"）。讨论后分两批修复。

- 👤 决策：第一批修「score 显示（体感）+ 中文分词（真质量）」，分块暂缓；第二批确定「中文 query 多做一路（方案 B）+ 分块改切分清库全量重入」
- 🤖 **score 显示（体感，`web/index.html`）**：库内问答结果不再裸露 RRF 原始分，改为**以本次最高分归一化的相对相关度**（排名徽章 `#1/#2…` + 进度条 + 「相关度 100%/99%」），消除「全是低分」错觉。纯前端改动
- 🤖 **中文分词（真质量，`rag/retrieve.py` `_tokenize`）**：`_TOKEN_RE` 由只取 `[A-Za-z0-9]+` 改为「英文/数字串 + 单个 CJK 字」，中文按**相邻二元组（bigram）+ 单字兜底**切分，无需 jieba 等重依赖即可让中文 query 在 BM25 生效
- 🤖 **中文 query 多做一路（方案 B，`rag/retrieve.py`）**：新增 `_bm25_query`——检测到中文 query 时用 low 档 LLM 译成英文术语，**只拼接给 BM25 那路**（向量仍用原中文保留跨语种语义），带 query 级缓存控 token；实测 `序列建模的技术` 原中文下 BM25 全 0 分（命中作者署名噪声），译为 `sequence modeling techniques` 后正确命中 sequence sparsity / SASRec 等相关内容，**BM25 腿真正复活**
- 🤖 **分块质量重入库（`rag/ingest.py`）**：
  - `_join_lines`：复原 PDF 跨行断词（`de-\nsigned` → `designed`），根治残句；保留 `(char_offset, page)` 标记以维持多页 section 的页码精度
  - `_split_sentences` + `_chunk_section`：改为**按句子边界切（绝不切句中）**，overlap 用整句而非字符
  - `_is_meaningful`：出库前过滤纯编号残片（"3.1"）与实质字符过少的孤片
  - 清空 collection 全量重入：885 → **755 chunks**（噪声清理 + 整句合并），9 篇全在
- 🤖 验证：浏览器对比前后截图——片段从 "3.1"/句中断开变为完整连贯句（完整列举基线方法、完整实验描述）；BM25 中文召回从空转变为命中真相关内容；相关度展示正常。遗留：含数学公式片段抽取仍不完美（PDF 公式抽取固有难题，非分块逻辑可解）

> ✅ 检索质量修复达成：score 展示符合直觉、中文 query 双腿生效、片段成句可读；分块清噪后全量重入库。

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
