# Single Agent Competitor Analysis System 规格说明

## 1. 文档依据

本文档描述当前仓库中的实际实现，主要依据如下：

- `main.py`：CLI 参数解析、依赖初始化和运行入口；
- `agent/`：State、Context 构建和 Agent Loop；
- `llm/`：模型后端协议、OpenAI API 实现和提示词；
- `tools/`：统一 Tool 接口、Registry、输入 Schema、内置研究工具和离线 Demo；
- `config/`：环境变量与运行配置；
- `storage/`：JSONL 运行日志；
- `tests/test_agent.py`：当前自动化测试覆盖范围；
- `README.md`、`.env.example`、`requirements.txt`：运行与配置方式；
- `artifacts/demo/`：离线演示产物；
- `artifacts/20261012-101236/`：真实 API 运行留下的状态、日志和报告。

本文档不把未来设想记为当前能力。

## 2. 项目背景

本项目的目标不是只完成一份普通竞品报告，而是验证如何通过一个由 Python 实现的 Agent Runtime，让本身不保存跨调用状态的 LLM 在外部程序控制下持续完成复杂研究任务。

LLM 每次只接收当前一轮构造出的 Context 并返回一次计划、动作、进度判断或报告。任务连续性由外部程序负责：`CompetitorAgent` 保存 `AgentState`、构造有限 Context、执行工具、记录 Observation、更新状态并发起下一轮调用。

## 3. System Goal

当前版本实际支持：

1. 通过 CLI 接收一段竞品分析任务文本；
2. 调用 LLM 将任务拆分成研究计划；
3. 根据持久化 State 构造当前轮 Context；
4. 由 Tool Registry 动态提供已注册工具的 Function Calling Schema，让 LLM 自主选择一个下一步动作；
5. 通过 Tavily Search API 搜索网页候选来源；
6. 通过内置 HTTP/HTML 读取器读取公开网页正文；
7. 将计划、来源、轮数、近期事件和循环保护信息保存到 `state.json`；
8. 多轮执行 `State → Context → Decision → Tool → Observation → State Update`；
9. 接受模型主动触发的 Replan，并在重复动作达到阈值时由 Runtime 请求强制 Replan；
10. 在模型选择 `finish` 后，基于独立的报告 Context 生成 Markdown 报告；
11. 将完整事件流写入 `run.jsonl`，并在终端输出压缩日志；
12. 使用已有 `state.json` 恢复执行。
13. 在工具执行前根据统一的 `input_schema` 校验模型参数，非法调用记录为 `tool_error` 后继续运行。
14. 提供 Document、Chunk、文本切分、Embedding 和 Chroma 向量存储基础设施；真实模式通过 `retrieve_knowledge` Tool 由 LLM 自主查询已有知识。
15. 真实报告成功后，以确定性策略评估已读取 Source，并通过可重试的增量 Pipeline 自动写入共享 Chroma；失败不改变研究完成状态。
16. 将本次任务实际召回的 Chunk 保存为可恢复的 `RetrievedEvidence`，保留到 Document、Source URL 和时间元数据的追踪链。
17. 由模型在正文选择真实 `[S#]` / `[E#]`，再由 Runtime 校验、重新编号并从系统数据生成最终来源，阻止伪造 ID 和 URL。
18. 支持默认 `existing` 和用户主动选择的 `latest` 任务级知识模式，工具调用仍由 LLM 自主判断。

当前动作集合固定为：

- `search_web`
- `read_web`
- `retrieve_knowledge`（真实模式）
- `replan`
- `finish`

真实 CLI 向 Registry 注册上述五个动作，离线 Demo 为保持无需 API Key 的固定六轮验收，只注册原有四项。动作不在 `CompetitorAgent._execute()` 中硬编码；代码可以仅通过实现 `BaseTool` 并调用 `registry.register()` 注册其他工具。

分析步骤顺序、搜索词、待读取 URL、何时 Replan 和何时 Finish 在真实模式下由 LLM 根据 Context 决定；Runtime 不硬编码 Cursor、Claude Code、Codex 等具体产品的研究流程。

## 4. 输入与输出

### 4.1 输入

- 新任务：`--task "..."`
- 恢复任务：`--resume <state.json>`
- 单独重试已完成运行的知识入库：`--ingest-existing <state.json>`
- 可选运行参数：`--output-dir`、`--max-rounds`、`--knowledge-mode existing|latest`
- 离线验收模式：`--demo`

### 4.2 输出

每个运行目录最多包含：

```text
artifacts/<run>/
├── state.json                 # 当前可恢复状态
├── run.jsonl                  # 追加写入的完整事件日志
├── report.md                  # 成功 Finish 后生成的最终报告
└── knowledge_ingestion.json   # 真实模式的独立入库状态与统计
```

如果运行在达到 `finish` 前失败，`report.md` 不一定存在；`state.json` 和 `run.jsonl` 仍会保留已执行过程。

## 5. Constraints

- 系统是单 Agent，核心类为 `CompetitorAgent`。
- 使用 Python 实现运行控制。
- 真实模式使用 OpenAI Python SDK 的 Chat Completions、JSON mode 和 function calling 接口。
- 搜索工具目前只接入 Tavily。
- Tool Framework 使用项目自身的 `BaseTool` 和 `ToolRegistry`，未引入第三方 Agent/Tool 框架。
- 不使用 LangGraph、CrewAI、AutoGen 等成熟 Agent 编排框架。
- 当前没有 Multi-Agent，也没有 Agent 间通信、角色分工或冲突协调。
- 当前没有长期 Memory。
- 当前 Chroma 与 Embedding 通过 `retrieve_knowledge` Tool 接入真实模式的 Tool Registry；ContextBuilder 不会主动检索，只读取 Tool 已写入 State 的 Evidence，是否检索完全由 LLM 决定。
- 真实模式默认在报告成功后自动入库；准入、版本与失败信息不写入 AgentState，而保存在独立入库状态和知识版本清单中。
- 当前没有任务队列、后台调度器或并发研究执行。
- `.env` 用于本地保存 API Key，`.gitignore` 忽略 `.env`。

## 6. Current Limitations

### 6.1 研究与证据

- `search_web` 只保存 Tavily 返回的标题、URL 和最长 1200 字符摘要；是否为官方来源没有权威校验。
- “疑似官方”仅由标题、子域名和 URL 路径启发式判断，可能误判。
- `read_web` 只支持公开的 HTML、XHTML 和纯文本，不执行 JavaScript，不解析 PDF，不处理登录、Cookie 或浏览器交互。
- 单页最多读取 2,000,000 字节，最终正文最多保存 50,000 字符。
- 搜索结果按 URL 去重，但没有规范化所有重定向或内容别名；报告 Context 只对相同正文做 SHA-256 去重。
- 系统没有独立的事实抽取结构、持久化证据矩阵或来源可信度评分。Runtime 只验证引用 ID/URL 来自实际 Evidence，不验证 Evidence 是否支持对应 Claim。

### 6.2 Agent 控制

- `finish` 的 Runtime 硬性条件只有“至少已有一个网页 Source 或知识 Evidence”；是否真正覆盖全部产品和维度主要由模型判断。
- `target_step_id` 会提供给模型的进度审查，但工具执行器不会校验它一定对应当前 pending 步骤。
- 搜索或读取成功后的步骤完成状态由另一次 LLM 调用判断，可能误判。
- Tool error 会被记录并继续下一轮，但没有指数退避、按错误类型制定的恢复策略或故障 URL 冷却机制。
- LLM 决策、Planning 或最终报告调用发生异常时，没有与 Tool error 等价的完整恢复机制。
- `--max-rounds` 在恢复时表示任务累计总轮数上限，不是“再运行 N 轮”。
- 状态为 `completed` 的任务仍可通过 `--resume` 再进入一轮并重新生成报告；当前代码没有禁止这种操作。

### 6.3 Context 与报告

- Context 预算按 JSON 字符数估算，不是精确 token 计数。
- 决策 Context 默认 14,000 字符，每个证据正文最多放入 1,800 字符，只保留最近 6 个压缩事件。
- 报告 Context 默认 70,000 字符，每个已读取来源最多 7,000 字符；仍可能因预算丢失部分来源内容。
- 最终的“产品 × 定位/价格/核心能力/适用场景”证据矩阵只存在于报告提示词要求中，没有作为结构化对象保存或校验。
- 报告只为正文实际使用且存在于 Report Context 的 `[S#]` / `[E#]` 生成来源列表；无效 ID 和非证据 URL 会被移除。当前仍不检查 Evidence 是否在语义上支持对应结论，也不是 Claim-Level Citation。

### 6.4 运行与评估

- 当前日志没有 token、延迟、费用或每次 LLM 请求 ID 统计。
- `progress_assessment` 事件当前不直接记录 `round`；累积 `run.jsonl` 也没有代码版本和单次 CLI invocation ID，跨恢复分析时需要依据事件顺序与时间戳还原。
- `state.json` 直接保存网页正文，来源增多后文件会明显变大；当前没有外部证据存储或增量快照机制。
- 当前没有面向真实任务质量的自动评测集。
- 自动化测试通过 mock 和固定夹具验证执行链路；真实 LLM、Tavily 和外部网站仍依赖有效 API Key 与网络环境。
- 知识版本清单采用原子文件替换和逻辑可见性过滤，但当前没有多进程写锁；Retriever 为过滤 staged/旧版本会查询 Collection 全部候选，规模化性能尚未优化。
- 当前没有知识过期检测、自动刷新、来源冲突消解、事实真实性验证或检索质量评测。`latest` 是用户主动任务策略，不是自动过期刷新机制。

## 7. 明确不属于当前系统的能力

以下能力当前版本未实现，不应被描述为已有能力：

- Multi-Agent 协作；
- 长期 Memory、用户画像或跨任务经验复用；
- 自动 Context RAG 或无 Tool 调用的自动语义检索；
- 定时知识刷新、自动整站抓取或 LLM 入库质量评审；
- 自动学习、自我训练或从历史运行中更新策略；
- 完整浏览器自动化；
- 异步/并行工具调用；
- 定时任务、任务队列或分布式执行。

这些项目可以作为未来扩展方向，但不属于当前系统能力。
