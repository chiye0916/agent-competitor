# 单 Agent 竞品分析系统

这是一个不依赖 LangChain、LangGraph、CrewAI 或 AutoGen 的教学型单 Agent 竞品研究系统。Python Runtime 自行实现 Planning、Tool Calling、多轮循环、State 持久化、Context 裁剪、Replan、网页研究、知识检索、增量知识入库和最终报告。

## 项目结构

```text
main.py                 CLI、依赖装配、报告完成后的入库触发
agent/                  Agent Loop、State、Context
llm/                    LLM 协议、OpenAI 后端和 Prompts
tools/                  BaseTool、Registry、研究与知识检索工具
knowledge/              Document/Chunk、策略、评估和入库 Pipeline
embedding/              Embedding 抽象与 OpenAI 实现
retrieval/              ChromaStore、版本可见性和 Retriever
config/                 环境变量配置
storage/                JSONL 日志
tests/                  离线单元与集成测试
docs/                   当前实现的详细交接文档
```

详细架构见 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)，Knowledge Layer 见 [docs/KNOWLEDGE.md](docs/KNOWLEDGE.md)。

## 安装与配置

```powershell
D:\anaconda\Scripts\conda.exe activate competitor-agent
python -m pip install -r requirements.txt
Copy-Item .env.example .env
```

至少配置：

- `LLM_API_KEY`：OpenAI 或兼容对话服务的 Key；
- `LLM_MODEL`、可选 `LLM_BASE_URL`；
- `TAVILY_API_KEY`；
- `EMBEDDING_API_KEY`、`EMBEDDING_BASE_URL`、`EMBEDDING_MODEL`：可配置与对话模型不同的 OpenAI 兼容 Embedding 服务，Key 和 Base URL 留空时回退到 LLM 配置；
- `KNOWLEDGE_DB_PATH` 和 `KNOWLEDGE_COLLECTION`；
- `KNOWLEDGE_AUTO_INGEST=true`：真实报告成功后自动增量入库。

`.env` 和 `.knowledge/` 已被 Git 忽略，不要提交真实 Key 或本地向量库。

## 运行

离线 Demo 不使用真实 API，也不会读写真实知识库：

```powershell
python main.py --demo --output-dir artifacts\demo
```

真实调研：

```powershell
python main.py --task "调研并比较 Cursor、Claude Code、Codex 的定位、价格、核心能力和适用场景，优先引用官方资料"
```

从中断状态恢复：

```powershell
python main.py --resume artifacts\<run>\state.json --max-rounds 40
```

对已完成运行单独重试知识入库，不重新运行研究任务：

```powershell
python main.py --ingest-existing artifacts\<run>\state.json
```

真实成功运行的目录包含：

- `state.json`：Agent 最新可恢复状态；
- `run.jsonl`：完整事件日志；
- `report.md`：最终报告；
- `knowledge_ingestion.json`：独立的入库状态、评估、统计和错误。

## Tool 与 Knowledge 流程

真实 Registry 注册 `search_web`、`read_web`、`retrieve_knowledge`、`replan` 和 `finish`。LLM 每轮自主选择一个 Tool；`retrieve_knowledge` 不会自动执行，也没有自动 Context RAG。

报告成功后，独立入库 Pipeline 会评估本次 `read_web` 正文。符合规则的 Source 使用规范化 URL 生成稳定 Document ID，使用正文 SHA256 识别内容版本，再切分、Embedding 并写入 Chroma。相同版本不会重复 Embedding；更新版本完整写入后才通过原子版本清单切换为可检索状态。入库失败不会使已经生成的报告或 `completed` AgentState 失效。

## 测试

测试使用确定性 Embedding、临时 Chroma 和故障注入，不调用收费 API：

```powershell
python -m unittest tests.test_knowledge_ingestion -v
python -m unittest discover -s tests -v
python main.py --demo
```

覆盖 Agent Loop、Tool Registry、RAG Tool、入库过滤、幂等、内容更新、Embedding 失败、Chroma 部分写入恢复、跨任务检索和报告失败隔离。

## 已知边界

- 网页读取器不执行 JavaScript，也不解析 PDF；
- 知识库没有自动过期、定时刷新、来源冲突消解或事实真实性验证；
- 向量召回质量依赖 Embedding、Chunk 参数和查询表达；
- 版本清单没有多进程写锁，大规模 Collection 的版本过滤仍需优化；
- 当前没有 Long-term Memory、Multi-Agent 或自动 Context 检索。
