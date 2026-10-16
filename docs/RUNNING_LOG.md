# 运行日志说明与真实执行记录

## 1. 文档依据

本文件只整理仓库中已经存在的日志，不补写未记录的决策或结果：

- `artifacts/demo/run.jsonl` 与 `artifacts/demo/state.json`
- `artifacts/20261012-101236/run.jsonl` 与 `artifacts/20261012-101236/state.json`
- `artifacts/knowledge-task-a/run.jsonl`、`state.json` 与 `knowledge_ingestion.json`
- `artifacts/knowledge-task-b-2/run.jsonl`、`state.json` 与 `knowledge_ingestion.json`

日志时间使用 UTC。本文档中的“当前状态”是根据同一日志内上一轮 `state_change` 和最终 `state.json` 还原的摘要。

## 2. 当前代码的完整离线执行

`artifacts/demo/run.jsonl` 是一条 6 轮完成日志，包含 Planning、Search、Read、Replan、Progress Assessment、Finish 和最终报告生成。它使用 `DemoLLMBackend` 与 `DemoResearchTools` 固定夹具，因此只证明 Agent Runtime 链路可执行，不代表实时市场信息，也不证明真实 LLM 会做出相同决策。

### 2.1 Task

> 比较 Alpha 与 Beta 的定位、定价和适用场景

### 2.2 Initial Plan

| ID | 描述 | 初始状态 |
|---|---|---|
| P1 | 收集 Alpha 的定位与价格证据 | pending |
| P2 | 收集 Beta 的定位与价格证据 | pending |
| P3 | 交叉比较并形成结论 | pending |

### 2.3 Round 1

- 当前状态：0 个来源，P1/P2/P3 均 pending。
- Agent Decision：选择 `search_web`。
- Tool Call：查询 `Alpha official pricing`，目标步骤 P1。
- Tool Result：返回 1 条固定搜索结果，新增 S1。
- Progress Assessment：没有步骤完成。
- State 变化：来源数 `0 → 1`；Plan 不变；Replan 次数 0。

### 2.4 Round 2

- 当前状态：1 个来源，P1/P2/P3 均 pending。
- Agent Decision：选择 `read_web`。
- Tool Call：读取 `https://example.test/alpha`，目标步骤 P1。
- Tool Result：S1 获得 109 字符正文。
- Progress Assessment：P1 完成。
- State 变化：P1 `pending → completed`；来源数仍为 1。

### 2.5 Round 3

- 当前状态：P1 completed，P2/P3 pending，1 个来源。
- Agent Decision：选择 `replan`。
- Tool Call：原因是 Alpha 资料显示协作能力是重要比较维度；新步骤为“收集 Beta 的定位、价格与协作/管控证据”和“按价格、定位和协作/管控维度比较”。
- Tool Result：Replan 成功，新步骤 ID 为 R1-1、R1-2。
- State 变化：保留已完成的 P1，替换 pending 的 P2/P3；`replan_count: 0 → 1`。

### 2.6 Round 4

- 当前状态：P1 completed，R1-1/R1-2 pending，1 个来源。
- Agent Decision：选择 `search_web`。
- Tool Call：查询 `Beta official pricing enterprise controls`，目标步骤 R1-1。
- Tool Result：返回 1 条固定搜索结果，新增 S2。
- Progress Assessment：没有步骤完成。
- State 变化：来源数 `1 → 2`；Plan 状态不变。

### 2.7 Round 5

- 当前状态：P1 completed，R1-1/R1-2 pending，2 个来源。
- Agent Decision：选择 `read_web`。
- Tool Call：读取 `https://example.test/beta`，目标步骤 R1-1。
- Tool Result：S2 获得 103 字符正文。
- Progress Assessment：R1-1 完成。
- State 变化：R1-1 `pending → completed`；来源数仍为 2。

### 2.8 Round 6

- 当前状态：P1、R1-1 completed，R1-2 pending，2 个来源。
- Agent Decision：选择 `finish`。
- Tool Call：理由为两产品的一手定位信息已收集，且公开价格信息缺口已明确；请求完成 R1-2。
- Tool Result：R1-2 完成，循环结束。
- State 变化：全部当前计划步骤 completed。

### 2.9 报告生成

- `ReportContextBuilder` 构造 1,459 字符的报告 Context；
- `primary_evidence` 包含 S1、S2；
- `supplemental_evidence` 为空；
- `DemoLLMBackend.write_report()` 返回固定 Markdown 报告；
- 报告写入 `artifacts/demo/report.md`；
- 最终状态：`completed`，6 轮，2 个来源，1 次 Replan。

## 3. 真实 API 累积运行记录

### 3.1 日志性质

`artifacts/20261012-101236/run.jsonl` 是同一个 State 跨 5 次 CLI 调用追加形成的累积日志，而不是一次无中断进程：

1. Round 1–12 后达到累计上限 12，记录 `run_failed`；
2. 从 Round 12 恢复，Round 13–30 后达到累计上限 30，再次记录 `run_failed`；
3. 从 Round 30 恢复，Round 31–34 完成并生成报告；
4. 从已完成的 Round 34 再次恢复，Round 35 再次 Finish 并覆盖报告；
5. 从已完成的 Round 35 再次恢复，Round 36 再次 Finish，使用当前丰富报告 Context 覆盖报告。

日志前 30 轮没有当前代码在每次成功搜索/读取后应写入的 `progress_assessment` 事件；Round 31–33 才有 3 条该事件。日志中也只有最后一次报告生成包含 `report_context_built`。因此，这个目录反映了开发过程中多次恢复和代码演进后的累积结果，不能作为“同一版代码一次性运行 36 轮”的证据。

另外，`progress_assessment` 记录本身没有 `round` 字段，日志也没有代码版本或单次 CLI invocation ID。本文将 Round 31–33 的进度审查按相邻事件顺序关联；这是日志可支持的还原，不是原记录中的显式轮次字段。

### 3.2 Task

> 调研并比较 Cursor、Claude Code、Codex 的定位、价格、核心能力和适用场景，优先引用官方资料

### 3.3 Initial Plan

| ID | 描述 |
|---|---|
| P1 | 检索三者官方产品页面、官方文档或官方发布说明，确认官方定位与目标用户 |
| P2 | 从官方定价资料提取免费额度、订阅档位、按量计费和企业方案，并记录价格生效日期 |
| P3 | 从官方文档或功能列表提取代码生成、补全、多文件编辑、CLI、Agent、上下文、模型和扩展机制 |
| P4 | 查找官方适用场景、案例研究或推荐方式，记录典型工作流与限制 |
| P5 | 对缺失或不一致维度做交叉验证，并标注来源与获取时间 |
| P6 | 形成定位、价格、核心能力和适用场景的结构化对比表及引用 |

### 3.4 Round-by-Round Trace

下表的“进入轮次时状态”列列出已完成步骤和来源数；整个真实日志的 `replan_count` 始终为 0。

| Round | 进入轮次时状态 | Agent Decision / Tool Call | Tool Result | State 变化 |
|---:|---|---|---|---|
| 1 | 无完成步骤；0 来源 | `search_web`：`Cursor official pricing plans` | 5 条结果，新增 S1–S5 | 来源 0→5 |
| 2 | 无完成步骤；5 来源 | `search_web`：`Cursor official pricing page cursor.com/pricing` | 5 条结果，新增 4 条 | 来源 5→9 |
| 3 | 无完成步骤；9 来源 | `read_web`：`https://cursor.com/pricing` | 更新 S6，正文 8,084 字符 | 来源仍 9 |
| 4 | 无完成步骤；9 来源 | `search_web`：`Claude Code official product page Anthropic pricing plans` | 5 条结果，新增 5 条 | 来源 9→14 |
| 5 | 无完成步骤；14 来源 | `read_web`：`https://claude.com/product/claude-code` | 更新 S13，正文 16,557 字符 | 来源仍 14 |
| 6 | 无完成步骤；14 来源 | `read_web`：`https://claude.com/pricing` | 更新 S14，正文 21,975 字符 | P1 完成 |
| 7 | P1；14 来源 | `read_web`：Cursor pricing | 更新 S6，正文 8,084 字符 | 无新增完成步骤 |
| 8 | P1；14 来源 | `read_web`：`https://openai.com/codex/pricing` | SSL `UNEXPECTED_EOF_WHILE_READING` | 记录 `tool_error`，无来源变化 |
| 9 | P1；14 来源 | `read_web`：`https://www.anthropic.com/pricing` | SSL `UNEXPECTED_EOF_WHILE_READING` | 记录 `tool_error`，无来源变化 |
| 10 | P1；14 来源 | `read_web`：Cursor pricing | 更新 S6，正文 8,084 字符 | 无新增完成步骤 |
| 11 | P1；14 来源 | `read_web`：Anthropic pricing | SSL `UNEXPECTED_EOF_WHILE_READING` | 记录 `tool_error`，无来源变化 |
| 12 | P1；14 来源 | `read_web`：Anthropic pricing | SSL `UNEXPECTED_EOF_WHILE_READING` | 记录 `tool_error`；随后达到最大轮数 12，状态 failed |
| 13 | 恢复；P1；14 来源 | `read_web`：Cursor pricing | 更新 S6，正文 8,084 字符 | 无新增完成步骤 |
| 14 | P1；14 来源 | `read_web`：`https://docs.anthropic.com/en/docs/claude-code/overview` | 新增 S15，正文 13,344 字符 | 来源 14→15；P2 完成 |
| 15 | P1/P2；15 来源 | `read_web`：`https://openai.com/codex/` | 新增 S16，正文 5,158 字符 | 来源 15→16 |
| 16 | P1/P2；16 来源 | `read_web`：`https://docs.cursor.com/en/get-started/concepts` | 新增 S17，正文 5,787 字符 | 来源 16→17 |
| 17 | P1/P2；17 来源 | `read_web`：`https://docs.claude.com/en/docs/claude-code/overview` | 新增 S18，正文 13,344 字符 | 来源 17→18 |
| 18 | P1/P2；18 来源 | `read_web`：OpenAI Codex 产品页 | 更新 S16，正文 5,158 字符 | 无新增完成步骤 |
| 19 | P1/P2；18 来源 | `read_web`：`https://cursor.com/docs` | 新增 S19，正文 5,787 字符 | 来源 18→19 |
| 20 | P1/P2；19 来源 | `read_web`：Claude Code overview | 更新 S18，正文 13,344 字符 | 无新增完成步骤 |
| 21 | P1/P2；19 来源 | `read_web`：`https://developers.openai.com/codex/` | 新增 S20，正文 13,850 字符 | 来源 19→20 |
| 22 | P1/P2；20 来源 | `read_web`：Cursor docs | 更新 S19，正文 5,787 字符 | 无新增完成步骤 |
| 23 | P1/P2；20 来源 | `read_web`：Claude Code overview | 更新 S18，正文 13,344 字符 | 无新增完成步骤 |
| 24 | P1/P2；20 来源 | `read_web`：OpenAI developer Codex | 更新 S20，正文 13,850 字符 | 无新增完成步骤 |
| 25 | P1/P2；20 来源 | `read_web`：`https://cursor.com/docs/agent/overview` | 新增 S21，正文 15,904 字符 | 来源 20→21 |
| 26 | P1/P2；21 来源 | `read_web`：Claude Code overview | 更新 S18，正文 13,344 字符 | 无新增完成步骤 |
| 27 | P1/P2；21 来源 | `read_web`：OpenAI Codex 产品页 | 更新 S16，正文 5,158 字符 | 无新增完成步骤 |
| 28 | P1/P2；21 来源 | `read_web`：Cursor docs | 更新 S19，正文 5,787 字符 | 无新增完成步骤 |
| 29 | P1/P2；21 来源 | `read_web`：Claude Code overview | 更新 S18，正文 13,344 字符 | 无新增完成步骤 |
| 30 | P1/P2；21 来源 | `read_web`：OpenAI developer Codex | 更新 S20，正文 13,850 字符 | 无新增步骤；随后达到累计上限 30，状态 failed |
| 31 | 恢复；P1/P2；21 来源 | `read_web`：OpenAI Codex 产品页 | 记录一次 `repeated_action_detected`，动作未被阻止；更新 S16 | Progress Assessment 完成 P3 |
| 32 | P1/P2/P3；21 来源 | `search_web`：`OpenAI Codex official docs use cases CLI IDE cloud` | 5 条结果，新增 S22–S26 | 来源 21→26；Progress Assessment 完成 P4 |
| 33 | P1–P4；26 来源 | `search_web`：`OpenAI Codex pricing plans ChatGPT Plus Pro Business official` | 5 条结果，新增 S27–S31 | 来源 26→31；Progress Assessment 完成 P5 |
| 34 | P1–P5；31 来源 | `finish`：认为证据已覆盖，P6 可在报告阶段完成 | Finish 成功 | P6 完成；首次生成报告；状态 completed |
| 35 | 从 completed 状态恢复；P1–P6；31 来源 | `finish`：再次确认全部步骤完成 | Finish 成功，无新完成步骤 | 再次覆盖 `report.md`；状态 completed |
| 36 | 从 completed 状态恢复；P1–P6；31 来源 | `finish`：再次确认全部步骤完成 | Finish 成功，无新完成步骤 | 使用丰富 Report Context 再次覆盖报告；状态 completed |

### 3.5 Replan 结果

真实 API 累积日志没有 `replan` Action、`forced_replan` 或 `action_blocked` 事件，最终 `replan_count` 为 0。

Round 31 出现一次 `repeated_action_detected`，当时该签名此前计数为 1，未达到“此前已执行至少 2 次”的阻止条件，因此该读取仍被执行，也没有触发强制 Replan。

离线 Demo 则真实记录了一次模型主动 Replan，见第 2 节。

### 3.6 最终报告生成

最后一次运行记录：

- `report_context_built.context_chars = 69980`
- Primary sources：S20、S18、S19、S16、S14、S21、S13、S6
- Supplemental sources：S1、S2、S3、S4、S5、S7、S8
- 最终报告：`artifacts/20261012-101236/report.md`
- 最终 State：`status = completed`
- 最终轮数：36
- 来源数：31
- Replan 次数：0

`report.md` 包含证据矩阵、执行摘要、结构化对比、逐产品分析、优劣势、选择建议、结论、证据边界和来源列表。这是日志中最后一次 `run_completed` 对应的覆盖结果。

### 3.7 事件统计

| Event | 数量 |
|---|---:|
| `planning` | 1 |
| `resume` | 4 |
| `round_start` | 36 |
| `agent_decision` | 36 |
| `tool_result` | 32 |
| `tool_error` | 4 |
| `progress_assessment` | 3 |
| `repeated_action_detected` | 1 |
| `run_failed` | 2 |
| `report_context_built` | 1 |
| `run_completed` | 3 |

## 4. 从日志能确认与不能确认的内容

可以确认：

- State 能跨 CLI 调用恢复；
- Search、Read、Finish 和报告生成在真实 API 运行中实际发生；
- Tool error 会被记录并允许后续轮次继续；
- 当前丰富报告 Context 在最后一次运行中实际构造到 69,980 字符；
- 最终报告和 State 已落盘。

不能仅凭这份真实累积日志确认：

- 当前代码从 Round 1 开始完整无改动地执行过一次；
- 强制 Replan 在真实 API 运行中发生过；
- 所有 31 个来源都被完整正文读取；
- 报告中的每个引用都经过程序化事实校验；
- 报告内容没有 LLM 判断错误。

其中强制 Replan 路径只在自动化测试中通过 `RepeatingLLM` 验证，主动 Replan 路径在离线 Demo 中验证。

## 5. 2026-10-13 真实运行故障与修复

`artifacts/20261013-191144/run.jsonl` 记录了一次真实模式运行。Round 27 完成后已保存 54 个来源；Round 28 只记录了 `round_start`，随后 CLI 输出：

```text
Expecting ',' delimiter: line 1 column 1070 (char 1069)
```

该错误发生在 `OpenAIBackend.decide()` 解析 `function.arguments` 时。日志中没有 Round 28 的 `agent_decision` 或 `tool_error`，说明异常发生在 Tool Registry 接管之前。原实现直接调用 `json.loads()`，而 Decision 又位于工具异常处理之外，因此非法 JSON 终止了整次运行。持久化 State 仍停留在 Round 27、`status=running`，证据没有丢失。

本次修复包括：

- Decision 对缺失 Tool Call 或非法参数 JSON 默认额外重试 2 次；
- 重试耗尽后记录 `decision_error`、保存 State 并进入下一轮；
- 日志增加工具名、错误位置、重试次数、原始参数长度和截断摘要；
- Function Schema 开启严格参数模式，Registry 的 Runtime 校验继续保留；
- 累计两次重复动作即可阻止当前动作并强制 Replan，不再要求重复集中在同一个 URL；
- Progress Prompt 明确以证据覆盖为完成条件，不要求先产生中间整合文本；
- 本地真实运行的 `CONTEXT_CHAR_BUDGET` 从 80,000 恢复为默认 14,000，减少决策漂移和格式异常风险。

自动化测试新增了非法 JSON 重试、重试耗尽诊断、Decision 跨轮恢复和不同 URL 累计重复触发 Replan 四类用例。

## 6. 2026-10-15/16 Knowledge Layer 真实端到端验证

本节使用 DeepSeek 对话模型、Voyage AI `voyage-4` Embedding 和本地 Chroma，验证“报告后入库 → 新任务 Tool 检索 → 新资料增量入库”。日志时间为 UTC。

### 6.1 任务 A：首次知识入库

`artifacts/knowledge-task-a/knowledge_ingestion.json` 的最终结果：

| 字段 | 值 |
|---|---:|
| `status` | `completed` |
| `total_sources` | 14 |
| `accepted` / `rejected` | 3 / 11 |
| `new` / `failed` | 3 / 0 |
| `chunks_written` | 31 |

被接受的 S1、S11 和 S12 都是 `read_web` 正文，分别写入 13、2 和 16 个 Chunk。11 个 `search_web` 摘要以 `missing_verified_full_text` 被拒绝，这是准入策略的预期结果。

### 6.2 任务 B：跨任务检索与故障恢复

`artifacts/knowledge-task-b-2/run.jsonl` 使用明确任务“优先查询已有知识，总结并分析 2Cshop 的价格方案与产品定位”。运行结果：

- Round 1–3 连续调用 `retrieve_knowledge`，每次 `result_count=8`，证明任务 A 的 Chunk 可被新任务召回；
- Round 4–6 继续调用检索，触发 Voyage 未绑定付款账户的 3 RPM 限制，三次记录为 429 `tool_error`；
- Round 7 改用 `search_web`，Round 8 用 `read_web` 读取官方价格页，Round 9 `finish`；
- AgentState 最终为 `completed`，生成 13,341 字节的 `report.md`，证明 Tool 限流没有使整个 Agent 失败。

最终 `report_context_built` 只包含网页 Source：Primary 为 S3，Supplemental 为 S1、S2、S4、S5。虽然知识 Tool 已成功召回 Chunk，但检索结果不会转为 Source，因此未直接进入最终 `ReportContextBuilder`。

### 6.3 限流后入库重试与幂等验证

报告完成后的首次自动入库因同一 429 限流失败，但没有撤销报告或 `completed` AgentState。等待限流窗口后执行：

```powershell
python main.py --ingest-existing artifacts\knowledge-task-b-2\state.json
```

2026-10-15T12:20:59Z 的重试成功：`new=1`、`chunks_written=4`、`failed=0`。2026-10-16T01:21:17Z 再次执行同一命令，结果为 `new=0`、`unchanged=1`、`chunks_written=0`、`failed=0`，证明内容未变时不会重复 Embedding 或写入。

当前 `.knowledge/chroma/competitor_knowledge.versions.json` 有 4 篇活跃文档，Chunk 数为 `13 + 2 + 16 + 4 = 35`。

### 6.4 本次验证确认的 Step 4 输入

- `retrieve_knowledge` 不在当前重复动作签名中，相同/相近查询在短时间内重复请求 Embedding；
- Retriever 没有查询缓存、`Retry-After`/429 退避或提供商级限速；
- 成功召回的 Chunk 只是 Tool Observation，不会进入 `state.sources` 和最终报告 Context；
- 通用 Embedding 适配器没有传递 Voyage 建议的 `input_type=query/document`。

这些是实测暴露的后续优化输入，本次只记录现状，没有修改 Agent Loop、Retriever 或 Report Context。
