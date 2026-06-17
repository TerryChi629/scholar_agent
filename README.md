# ScholarStance

本地私有库驱动的**多智能体科研助手**。把一个研究方向的论文自动组织成「**立场图谱**」——谁与谁观点对立、方法如何演进、研究空白在哪——并产出带真实引用的综述初稿。

> 与 Elicit/Consensus 的区别：它们做单篇问答 + 云端 SaaS；本项目做**跨篇关系建模 + 本地私有数据**。

## 快速开始

```bash
# 1. 依赖
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 2. 配置 (填入 DeepSeek 或 GLM 的 key)
cp .env.example .env

# 3. 自检 (不联网, 验证脚手架)
python main.py selfcheck

# 4. 使用
python main.py ingest ~/papers/icl     # 入库本地 PDF 目录
python main.py ask "什么是 in-context learning"   # 库内问答
python main.py map "In-context Learning"          # 生成立场图谱 + 综述
python main.py tasks                    # 历史任务
python main.py resume <task_id>         # 断点续跑
```

## 架构

五层：集成层 (CLI/API/飞书) → Harness 工程外壳 → Orchestrator 编排 → 四类执行 Agent (Retriever/Reader/Synthesizer/Critic) → 底座 (Tools/Skills/MCP/RAG/Memory)。

Agent 间通过**共享黑板** (`core/blackboard.py`) 协作，黑板可序列化到 SQLite 实现断点恢复。

技术栈：Python · DeepSeek/GLM (热切换) · ChromaDB · Hybrid 检索 · MCP · 自研 Agent Loop。

## 给 AI 编码助手

**动手前先读 `CLAUDE.md`**（项目宪法）——架构约束、数据契约、TODO 优先级、红线都在里面。所有占位逻辑标了 `TODO(Trae)`。

## 状态

脚手架可跑通 (`selfcheck` 通过)，业务逻辑按 `CLAUDE.md` 的 M1→M2→M3 顺序填充。
