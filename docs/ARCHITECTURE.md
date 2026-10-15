# 系统架构

## 1. 架构范围

当前系统是一个单进程、同步执行、单 Agent 的 Python CLI 应用。实现按配置、状态、Context、LLM、工具、日志和 Agent 编排职责拆分为普通 Python 模块，没有外部 Agent 编排框架。`main.py` 负责 CLI、依赖装配和报告完成后的入库触发。Knowledge Layer 通过 `retrieve_knowledge` Tool 提供自主检索，并在真实报告成功后通过独立 Pipeline 增量入库；`ContextBuilder` 不做自动检索。

## 2. 总体流程

```mermaid
flowchart TD
    U[User Task / --resume] --> CLI[CLI: main / parse_args]
    CFG[Config from .env] --> CLI
    CLI --> A[CompetitorAgent<br/>Agent Runtime]
    A <--> S[(AgentState<br/>state.json)]
    S --> C[ContextBuilder]
    C --> L[OpenAIBackend<br/>无状态 LLM 调用 + 决策解析重试]
    L --> D{Action Decision}
    L -->|非法 Tool JSON 且重试耗尽| DE[decision_error<br/>保存 State 后进入下一轮]
    DE --> S
    D --> TR[ToolRegistry<br/>校验 + 分发]
    TR -->|search_web| SW[SearchWebTool<br/>Tavily API]
    TR -->|read_web| RW[ReadWebTool<br/>HTTP + HTMLParser]
    TR -->|retrieve_knowledge| KT[KnowledgeRetrievalTool]
    TR -->|replan| RP[ReplanTool<br/>替换未完成 PlanStep]
    TR -->|finish| FN[FinishTool]
    SW --> O[Observation]
    RW --> O
    KT --> RET[Retriever]
    RET --> VS[(ChromaStore)]
    VS --> KT
    KT --> O
    RP --> O
    FN --> O
    O --> PA[LLM assess_progress<br/>仅 search/read 成功后]
    PA --> S
    O --> S
    S -->|下一轮| C
    O -->|finished| RC[ReportContextBuilder]
    RC --> WR[LLM write_report]
    WR --> R[report.md + 来源清单]
    R --> KIS[KnowledgeIngestionService<br/>仅真实成功运行]
    KIS --> KE[KnowledgeEvaluator]
    KE --> DOC[Document]
    DOC --> CH[Versioned Chunk]
    CH --> EM[EmbeddingModel]
    EM --> VS
    VS --> VM[原子版本清单<br/>切换可见版本]
    A --> J[run.jsonl + 终端日志]
```

实际循环是：

```text
User Task
  → Agent Runtime
  → State
  → Context Builder
  → LLM Backend
  → Action Decision
  → Tool Execution
  → Observation
  → State Update
  → Continue / Replan / Finish
```

## 3. 模块结构与职责

```text
competitor-agent/
├── main.py
├── agent/
│   ├── __init__.py
│   ├── core.py
│   ├── state.py
│   └── context.py
├── llm/
│   ├── __init__.py
│   ├── backend.py
│   ├── openai_backend.py
│   └── prompts.py
├── tools/
│   ├── __init__.py
│   ├── base.py
│   ├── registry.py
│   ├── schemas.py
│   ├── knowledge.py
│   ├── research.py
│   └── demo.py
├── config/
│   ├── __init__.py
│   └── settings.py
├── knowledge/
│   ├── __init__.py
│   ├── document.py
│   ├── chunk.py
│   ├── splitter.py
│   ├── policy.py
│   ├── evaluator.py
│   ├── ingestion.py
│   └── service.py
├── embedding/
│   ├── __init__.py
│   ├── base.py
│   └── openai_embedding.py
├── retrieval/
│   ├── __init__.py
│   ├── chroma_store.py
│   └── retriever.py
└── storage/
    ├── __init__.py
    └── logger.py
```

| 文件 | 类/对象 | 当前职责 |
|---|---|---|
| `main.py` | `parse_args`、`main` | 解析 CLI、装配 Agent 与共享知识服务；报告成功后触发自动入库，并支持从已完成 State 单独重试 |
| `config/settings.py` | `Config` | 从 `.env` 读取 LLM、Embedding、知识库、入库策略、轮数、Context 预算和 HTTP 配置 |
| `agent/state.py` | `PlanStep`、`Source`、`AgentState` | 保存计划步骤、来源和最新可恢复状态，并负责 JSON 序列化 |
| `agent/context.py` | `ContextBuilder`、`ReportContextBuilder` | 将 State 转换为有限决策 Context 或较丰富的报告 Context，不调用 LLM |
| `agent/core.py` | `CompetitorAgent` | 编排组件，通过 Registry 执行 Action，保持原有 Agent Loop、状态持久化和报告落盘 |
| `llm/backend.py` | `LLMBackend`、`LLMResponseError` | 规定五类 LLM 能力接口，并提供可恢复响应格式错误类型 |
| `llm/openai_backend.py` | `OpenAIBackend` | 使用 OpenAI SDK Chat Completions 实现 `LLMBackend`；对缺失 Tool Call 或非法参数 JSON 做有限重试 |
| `llm/prompts.py` | 提示词常量 | 集中保存 OpenAI 后端使用的五类提示词 |
| `tools/base.py` | `BaseTool`、`ToolValidationError` | 定义统一 Tool 接口、生成单个 Function Schema 并执行轻量输入校验 |
| `tools/registry.py` | `ToolRegistry`、`build_default_registry` | 注册 Tool、动态汇总 Schema、校验并分发执行；真实模式传入 Retriever 时组装五个工具 |
| `tools/schemas.py` | 描述与输入 Schema 常量 | 保存五个内置 Tool 的参数契约，不再维护静态工具列表 |
| `tools/knowledge.py` | `KnowledgeRetrievalTool` | 把 `retrieve_knowledge` 暴露给 LLM，并把 Retriever 结果转换为统一 Observation |
| `tools/research.py` | `SearchWebTool`、`ReadWebTool`、`ReplanTool`、`FinishTool`、`ResearchTools` | 实现四个可执行 Tool；底层研究客户端负责 Tavily、URL 安全检查和网页读取 |
| `tools/demo.py` | `DemoLLMBackend`、`DemoResearchTools` | 提供固定动作脚本和 Alpha/Beta 离线网页夹具 |
| `storage/logger.py` | `RunLogger`、`utc_now`、`compact` | 将事件追加到 `run.jsonl`、输出压缩终端日志，并提供时间/压缩辅助函数 |
| `knowledge/document.py` | `Document` | 表示可序列化的原始知识文档，并提供从 `Source` 转换的兼容入口 |
| `knowledge/chunk.py` | `Chunk` | 表示携带文档 ID 和元数据的可序列化文本片段 |
| `knowledge/splitter.py` | `TextSplitter` | 使用可配置字符窗口和重叠量把 Document 切分为 Chunk |
| `knowledge/policy.py` | `KnowledgePolicy` | 保存最小正文长度等确定性准入阈值 |
| `knowledge/evaluator.py` | `KnowledgeEvaluator`、`EvaluationResult` | 评估 Source，返回可解释的接受、拒绝原因和质量标记，不访问 Chroma |
| `knowledge/ingestion.py` | `KnowledgeIngestionPipeline` | 规范化 URL/正文、识别版本、切分、Embedding、stage、版本切换并汇总结果 |
| `knowledge/service.py` | `KnowledgeIngestionService` | 隔离报告完成边界和入库失败，写入完成或错误日志 |
| `embedding/base.py` | `EmbeddingModel` | 定义单文本和批量文本向量化的提供商无关接口 |
| `embedding/openai_embedding.py` | `OpenAIEmbedding` | 复用 Config 的 API Key/Base URL，通过 OpenAI SDK 批量生成向量 |
| `retrieval/chroma_store.py` | `ChromaStore` | 管理 Chroma、分阶段版本写入、原子版本清单、旧版本过滤与相似度查询 |
| `retrieval/retriever.py` | `Retriever` | 为 Tool 提供稳定查询边界，隔离 Chroma 和 Embedding 细节 |
| 各目录 `__init__.py` | 包导出 | 声明 Python 包并导出主要公共类型 |

当前不存在 Scheduler、Planner 服务、自动 Context RAG 服务或 Multi-Agent Coordinator。真实模式在 CLI 装配阶段让 Retriever 和 Ingestion Pipeline 共享同一 ChromaStore；Agent Core 仍只面向 Tool Registry，不包含入库分支。Demo 不装配知识服务，保持原有固定六轮行为且不污染真实知识库。

## 4. LLM 与 Agent Runtime 的职责区别

### 4.1 LLM 负责

- 把任务拆成研究步骤；
- 根据当前 Context 选择一个下一步 Action；
- 在工具成功后判断哪些步骤已有足够证据；
- 在重复保护触发时生成替代计划；
- 根据报告 Context 撰写最终报告。

### 4.2 Agent Runtime 负责

- 保管完整 State；
- 决定何时调用哪类 LLM 接口；
- 向模型暴露 Registry 根据已注册 Tool 动态生成的 Schema；
- 通过 Registry 校验和执行 Tool；
- 接收 Tool 返回的 Observation；
- 实施 URL 安全检查、重复保护和最大轮数；
- 持久化状态、日志和报告。

LLM 不直接写文件，也不直接访问 Tavily 或网页。所有外部副作用都由 Runtime 的 Python 代码执行。

## 5. 为什么说 LLM 是无状态的

每次 `client.chat.completions.create()` 都只收到本次构造的 `messages`。代码没有复用服务端 conversation ID，也没有把完整对话历史原样发送给模型。

模型能“知道”前面发生了什么，是因为 Runtime 在下一轮从 `AgentState` 重新构造 Context，其中包含任务、计划、来源索引、有限证据和最近事件。如果删除 State 又不提供恢复文件，模型无法从之前的调用中自行恢复执行过程。

## 6. State 如何让 Agent 持续执行

`AgentState` 在内存中作为权威状态，并在关键节点写入 `state.json`：

```mermaid
stateDiagram-v2
    [*] --> created
    created --> running: 创建 Plan
    failed --> running: --resume 且累计轮数上限允许继续
    running --> running: Tool + Observation + Save
    running --> failed: 达到 max_rounds
    running --> completed: finish + 报告生成成功
    completed --> running: 当前代码仍允许 --resume
```

上图最后一条是当前代码行为，不表示推荐工作流：代码没有阻止已完成状态再次恢复，所以真实日志中出现了重复生成报告的运行。

## 7. Context Management

### 7.1 `ContextBuilder`

决策 Context 默认限制为 14,000 字符：

- 保留全部来源的精简 `source_index`；
- 只放入预算允许的 `evidence`；
- 单个 Evidence 最多 1,800 字符；
- 优先“疑似官方”、已读取、更新时间较新的来源；
- 只包含最近 6 个压缩事件；
- 列出读取次数达到 2 次的 `blocked_urls`。

“疑似官方”是启发式标记，不是域名所有权校验。

### 7.2 `ReportContextBuilder`

报告 Context 默认限制为 70,000 字符：

- `source_catalog` 列出全部来源；
- `primary_evidence` 放已读取的正文；
- 相同正文按 SHA-256 去重；
- 来源按基础域名分组并轮换加入；
- 单个正文最多 7,000 字符；
- 剩余预算用于 `supplemental_evidence` 搜索摘要，每项最多 900 字符。

这两个 Builder 都直接操作普通 Python 字典，没有向量检索、语义检索或 Context 数据库。

## 8. Tool Calling 如何工作

每个 `BaseTool` 同时声明 `name`、`description`、`input_schema` 和 `execute()`。`ToolRegistry.schemas()` 将全部已注册 Tool 动态转换为 OpenAI function schema。`OpenAIBackend.decide()` 设置 `tool_choice="required"`，读取第一条 Tool Call，并转换为：

```json
{
  "name": "search_web",
  "arguments": {
    "query": "...",
    "purpose": "...",
    "target_step_id": "P1"
  }
}
```

`CompetitorAgent._execute()` 只把 `name` 和 `arguments` 交给 `ToolRegistry.execute()`。Registry 查找工具、根据同一 Tool 的 `input_schema` 校验参数，再调用 `tool.execute(**arguments)`。Tool 只返回自身 Observation；Registry 根据 Tool 的 `ends_run` 元数据统一返回 `(ends_run, observation)`，默认只有 `FinishTool` 将其设为 `True`。现有四个 Tool 保持严格 Schema；`retrieve_knowledge` 因 `top_k` 是带默认值的可选参数而使用非严格 Schema，同时仍保留 Runtime 校验。非法参数或未知名称会由 Agent 原有错误路径记录为 `tool_error`，不会终止整个循环。

如果模型返回的 `function.arguments` 不是合法 JSON，`OpenAIBackend` 会在请求中加入格式纠正提示后重试。默认允许 2 次额外重试；全部失败后抛出 `LLMResponseError`。Runtime 将其记录为 `decision_error`，日志只保存参数长度及最多 600 字符的首尾摘要，随后保存 State 并进入下一轮。该异常发生在 Registry 之前，不属于 `tool_error`。

默认 Registry 注册 `SearchWebTool`、`ReadWebTool`、`ReplanTool` 和 `FinishTool`。新增普通工具只需继承 `BaseTool` 并注册，不需要修改 Agent 的执行分发代码。当前没有自动插件发现或 MCP 连接生命周期管理。

### 8.1 搜索

`ResearchTools.search_web()` 向 `https://api.tavily.com/search` 发 POST 请求，固定使用 `search_depth="advanced"`，默认取 5 条，内部上限 8 条，不请求 Tavily answer 或 raw content。

### 8.2 网页读取

`ResearchTools.read_web()`：

1. 解析 URL 并限制为 HTTP(S)；
2. DNS 解析主机，拒绝非公网 IP；
3. 使用 `urllib.request` 请求最多 2 MB；
4. 对 HTML 使用 `VisibleTextParser` 提取文本；
5. 规范空白并截断到 50,000 字符。

当前没有浏览器引擎或 JavaScript 执行。

## 9. Planning 如何生成

新任务且 `state.plan` 为空时：

```mermaid
sequenceDiagram
    participant A as CompetitorAgent
    participant L as OpenAIBackend
    participant S as AgentState
    A->>L: create_plan(task)
    L-->>A: JSON {steps: [...]}
    A->>A: 去空、最多保留 8 步
    A->>S: 创建 P1...Pn，status=pending
    A->>S: 保存 state.json
```

提示词要求 3–6 步，但 Python 接收上限是 8；代码没有强制最少 3 步。

## 10. Replan 如何触发

### 10.1 模型主动触发

模型在每轮可选择 `replan`，提供原因和至少一个新步骤。Runtime 不自行判断原因是否成立。

### 10.2 重复动作保护触发

搜索查询经小写和空白规范化，读取 URL 去掉 fragment、尾部斜杠并小写。相同签名最多实际执行 2 次；第三次会被阻止。不同签名的重复也会累计 `repetition_strikes`：当尚未取得新步骤进展且累计达到 2 次时，当前重复动作即被阻止，Runtime 调用 `revise_plan()` 生成替代未完成步骤。步骤完成或 Replan 成功后计数清零。

Replan 只保留已完成步骤，不保留被替换的 pending 步骤到当前 Plan；变更历史保存在日志中。

## 11. Finish 与报告生成

模型选择 `finish` 后：

1. Runtime 检查至少有一个 Source；
2. 更新模型声明完成的步骤；
3. 退出 Agent Loop；
4. 构造 Report Context；
5. 调用 LLM 写报告；
6. 追加全部来源 Markdown 列表；
7. 写 `report.md`，更新 State 并记录 `run_completed`；
8. 真实模式且 `KNOWLEDGE_AUTO_INGEST=true` 时，在 Agent 返回后执行独立入库。

入库不位于 Agent Loop 内。失败时记录 `ingestion_error` 和独立状态，不回滚报告、不把 AgentState 从 `completed` 改为 `failed`。报告生成不是另一个 Agent，也没有独立的审稿循环。

## 12. 配置与依赖

`.env.example` 对应 `Config.from_env()`：

| 环境变量 | 默认值/作用 |
|---|---|
| `LLM_API_KEY` 或 `OPENAI_API_KEY` | 真实模式必需 |
| `LLM_DECISION_RETRIES` | Tool Call 缺失或参数 JSON 非法时的额外重试次数，默认 2 |
| `LLM_MODEL` | `gpt-4.1-mini` |
| `LLM_BASE_URL` | 可选 OpenAI 兼容服务地址 |
| `EMBEDDING_MODEL` | `text-embedding-3-small`，供 `retrieve_knowledge` 查询向量化使用 |
| `KNOWLEDGE_DB_PATH` | `.knowledge/chroma`，真实模式的持久化 Chroma 路径 |
| `KNOWLEDGE_COLLECTION` | `competitor_knowledge` |
| `KNOWLEDGE_AUTO_INGEST` | `true`，报告成功后自动执行增量入库 |
| `KNOWLEDGE_MIN_CONTENT_CHARS` | `200`，已读取正文的最低有效字符数 |
| `KNOWLEDGE_CHUNK_SIZE` | `500` |
| `KNOWLEDGE_CHUNK_OVERLAP` | `50` |
| `TAVILY_API_KEY` | 真实搜索必需 |
| `MAX_ROUNDS` | `12` |
| `CONTEXT_CHAR_BUDGET` | `14000` |
| `REPORT_CONTEXT_CHAR_BUDGET` | `70000` |
| `HTTP_TIMEOUT` | `20` 秒 |

第三方 Python 依赖包括 `openai`、`python-dotenv` 和 `chromadb`；搜索和网页读取使用标准库 `urllib`。

## 13. 当前部署形态与边界

- 单机 CLI、同步执行；
- State 和知识版本清单使用本地 JSON 文件，没有多进程并发写保护；
- Log 使用本地 JSONL 追加，没有集中式日志系统；
- JSONL 没有代码版本和单次 CLI invocation ID，且 `progress_assessment` 事件本身不含轮次字段；
- 没有服务端 API、Web UI、鉴权或多租户隔离；
- 没有异步 Worker 或分布式执行；
- 没有自动 Context RAG、知识刷新、事实可信度判断、长期 Memory 或 Multi-Agent；当前自动入库只发生在真实报告成功之后。

上述均可作为未来扩展方向，但不属于当前架构。
