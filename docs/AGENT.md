# CompetitorAgent 设计说明

## 1. Agent Role

当前系统只有一个 Agent：`CompetitorAgent`，实现位于 `agent/core.py`。

它的角色是竞品研究 Agent Runtime：接收任务状态，组织 LLM 调用和外部工具调用，持续更新 State，并在研究结束后生成报告。

`CompetitorAgent` 本身不是另一个模型。它是 Python 控制器；推理由实现 `LLMBackend` 协议的后端完成。

## 2. Agent Responsibilities

当前实现中，`CompetitorAgent` 负责：

1. 初始化或加载 `AgentState`；
2. 在没有 Plan 时调用 LLM 创建研究计划；
3. 使用 `ContextBuilder` 构造每轮有限 Context；
4. 调用 LLM 判断唯一的下一步 Action；
5. 对搜索和读取 Action 做重复检测；
6. 把 Registry 动态生成的 Tool Schema 交给 LLM；
7. 调用 `ToolRegistry` 校验并执行模型选择的工具，接收其 Observation；
8. 调用 LLM 评估哪些 PlanStep 可以完成；
9. 执行模型主动 Replan 或重复保护触发的强制 Replan；
10. 每轮更新并保存 State；
11. 写入终端日志和 `run.jsonl`；
12. 控制最大轮数和结束条件；
13. 使用 `ReportContextBuilder` 构造最终报告 Context；
14. 调用 LLM 生成 Markdown 报告，校验模型选择的证据 ID，并由程序追加来源清单。

## 3. Agent Loop

核心循环位于 `agent/core.py` 的 `CompetitorAgent.run()`：

```text
Load/Create State
    ↓
Create Plan（仅在 plan 为空时）
    ↓
round += 1
    ↓
ContextBuilder.build(state)
    ↓
LLMBackend.decide(context, tool_registry.schemas())
    ↓
重复动作检查
    ↓
执行一个 Action
    ↓
Observation + 进度审查
    ↓
更新并保存 State
    ↓
Continue / Finish / 达到 max_rounds
```

每轮只允许模型选择一个工具调用。当前没有同轮并行工具调用。

`CompetitorAgent._execute()` 不再按工具名包含执行分支，只调用：

```python
self.tool_registry.execute(action["name"], action["arguments"])
```

具体工具负责自己的执行和 State 更新逻辑。新增普通 Tool 不需要修改 `_execute()`。

## 4. Agent Decision

真实模式下，`OpenAIBackend.decide()` 使用 function calling，并设置 `tool_choice="required"`。可用 Schema 由 `ToolRegistry` 根据已注册 Tool 动态生成。真实 CLI 注册五个 Action；离线 Demo 不初始化 Embedding/Chroma，因此仍使用原有四个 Action。模型必须每轮选择一个可用 Tool，但没有任何规则强制选择知识检索或 Web 搜索。

模型返回 Tool Call 后，后端先把 `function.arguments` 解析为 JSON。缺失 Tool Call 或 JSON 非法时，后端会附加纠错提示并按 `LLM_DECISION_RETRIES` 重试（默认额外重试 2 次）。重试耗尽后抛出携带工具名、错误位置、参数长度和截断摘要的 `LLMResponseError`。

### 4.1 `search_web`

参数：

- `query`
- `purpose`
- `target_step_id`

Runtime 将 `query` 交给 Tavily。搜索结果保存为 `Source`，包含标题、URL 和摘要。`purpose` 与 `target_step_id` 主要提供决策和进度审查语义；搜索执行器本身只使用 `query`。

### 4.2 `read_web`

参数：

- `url`
- `purpose`
- `target_step_id`

Runtime 读取公开网页并保存可见文本。读取器会拒绝非 HTTP(S)、localhost、解析为非公网 IP 的目标，并只接受 HTML、XHTML 或纯文本。

### 4.3 `retrieve_knowledge`

参数：

- `query`
- `top_k`，可选，默认 5

该 Tool 用于复用 `.knowledge/chroma` 中已有的历史知识。调用链为 `KnowledgeRetrievalTool → Retriever → ChromaStore`，成功时返回统一 Observation：

```json
{
  "success": true,
  "tool": "retrieve_knowledge",
  "query": "AI model pricing",
  "result_count": 2,
  "results": []
}
```

知识库缺少资料或任务要求最新信息时，可由 LLM 自主选择 `search_web`。检索异常沿用现有 Tool 错误路径记录为 `tool_error`，Agent 可以在下一轮恢复。检索结果不会转成 `Source`，也不会触发自动 Context 检索；成功返回的每个 Chunk 会转成当前任务的 `RetrievedEvidence`，按 `chunk_id` 去重后持久化到 State，并获得 `E1`、`E2`……内部 ID。

### 4.4 `replan`

参数：

- `reason`
- `new_steps`

Runtime 保留已完成步骤，用新步骤替换全部未完成步骤，并增加 `replan_count`。

### 4.5 `finish`

参数：

- `reason`
- `completed_step_ids`

如果至少已有一个网页 `Source` 或知识 `RetrievedEvidence`，Runtime 接受 Finish，更新可匹配的计划步骤，然后进入最终报告生成。当前没有额外的结构化覆盖门禁。

## 5. Planning

当 `state.plan` 为空时，`OpenAIBackend.create_plan()` 要求 LLM 将任务拆成 3–6 个可验证、需要外部证据的步骤。Runtime 对返回结果做的处理只有：

- 去除空白项；
- 如果没有有效步骤则报错；
- 最多保留 8 个步骤；
- 编号为 `P1`、`P2`……。

Planning 是模型生成的研究导航，不是固定工作流，也不是任务调度系统。

## 6. Progress Assessment

每次 `search_web` 或 `read_web` 成功后，当前代码会调用 `LLMBackend.assess_progress()`。输入包括当前决策 Context 和刚完成的工具 Observation。步骤完成标准是已有证据足以支撑最终报告，不要求先生成中间总结或对比表；最终综合由 `write_report()` 完成。`retrieve_knowledge` 会产生 Observation 并保存 Evidence，但当前不会自动触发步骤进度评估。

模型返回：

```json
{
  "completed_step_ids": ["P1"],
  "reason": "..."
}
```

Runtime 使用 `AgentState.complete_steps()` 更新步骤。不存在的 ID 或已完成步骤不会造成新的状态变化。

这是单独的 LLM 进度判断，不是自动反思模块，也没有独立验证器保证判断一定正确。

## 7. Replan

当前存在两种 Replan 路径。

### 7.1 模型主动 Replan

模型在证据不足、出现新维度或原计划不适用时选择 `replan` Action，直接提供原因和新步骤。

### 7.2 Runtime 强制请求 Replan

Runtime 对 `search_web` 查询和 `read_web` URL 生成规范化签名：

- 相同动作第一次重复时记录 `repeated_action_detected`；
- 同一签名此前已执行至少 2 次时，第三次会被阻止；
- 不同签名的重复会共同累计 `repetition_strikes`；累计达到 2 时，当前重复动作也会被阻止；
- 动作被重复策略阻止后，调用 `LLMBackend.revise_plan()`；
- 成功后记录 `forced_replan` 并应用新步骤。

因此“强制”指 Runtime 强制进入重新规划调用；新计划内容仍由 LLM 生成。当前没有针对一般错误、低质量证据或覆盖不足的规则式自动 Replan。

## 8. Context Management

### 8.1 决策 Context

`ContextBuilder` 负责将完整 State 压缩成每轮输入。默认预算 14,000 字符，包含任务级 `knowledge_mode`、全部网页来源索引、预算内网页证据、预算内知识 Evidence 和最近 6 个事件。`existing` 模式优先放入知识 Evidence；`latest` 模式优先放入本次网页证据。

### 8.2 报告 Context

`ReportContextBuilder` 只在 Finish 后使用，默认预算 70,000 字符。它加入已读取正文、搜索摘要和本次任务实际召回的知识 Evidence；网页正文按内容去重并按域名轮换。`existing` 和 `latest` 使用相反的知识/网页加入优先级，但都受同一个字符预算约束。

两种 Context 都由 Runtime 每次临时构造，没有持久化上下文缓存或自动向量检索；知识检索只能由 LLM 显式调用 `retrieve_knowledge`。

## 9. Report Generation

Finish 后的流程为：

1. 构造报告 Context；
2. 记录 `report_context_built`；
3. 调用 `LLMBackend.write_report()`；
4. 检查返回内容非空；
5. `ReportCitationRenderer` 删除不存在的内部引用和非证据 URL；
6. 按正文首次出现顺序把有效 `[S#]` / `[E#]` 转换为 `[1]` / `[2]`；
7. 从 Context 中的可信 URL 与时间元数据生成 `参考来源`，记录 `report_citations_built`；
8. 写入 `report.md`；
9. 设置 `status="completed"` 和 `report_path`；
10. 记录 `run_completed` 并保存 State；
11. Agent 返回后，真实 CLI 按配置执行独立 Knowledge Ingestion。

报告提示词要求网页事实使用真实 `[S#]`、知识事实使用真实 `[E#]`。最终编号、标题、URL、获取时间以及知识证据的 Document/Chunk/Content Hash 映射由程序生成，因此模型不能凭空增加引用 URL。当前只实现证据级引用：Runtime 校验证据 ID 是否存在，但不判断某个 Evidence 是否真的支持某一句 Claim。

任务级 `knowledge_mode` 默认为 `existing`；CLI 可用 `--knowledge-mode latest` 显式选择，创建新任务时也会识别任务中的明确新鲜度表达。两种模式只改变提供给 LLM 的策略和 Context 优先级，不在 Agent Loop 中硬编码 Tool 顺序，也不根据 `retrieved_at` 或 `ingested_at` 自动联网。

入库不属于 `CompetitorAgent.run()` 或任何 Tool，不改变 Agent 的 Finish 语义。它只在报告成功后读取 Sources；异常记录为 `ingestion_error`，不会撤销报告或研究完成状态。

## 10. Error Handling

- `ToolRegistry` 会在执行前依据 Tool 的 `input_schema` 校验必填字段、字段类型、数组项和额外参数；
- `OpenAIBackend` 会对缺失 Tool Call 或非法 Tool 参数 JSON 做有限重试；
- 重试耗尽后的 `LLMResponseError` 会记录为 `decision_error`，保存 State 后继续下一轮；
- 非法参数或未知工具会进入 Agent 原有异常处理，记录为 `tool_error`，保存 State 后继续下一轮；
- 搜索/网页读取/Replan/Finish 执行异常会记录为 `tool_error`，保存 State 后继续下一轮；
- 进度审查异常会记录为 `progress_assessment_error`，本轮仍可保存工具结果；
- 强制 Replan 异常会记录为 `forced_replan_error`；
- 达到最大轮数后设置 `status="failed"`、记录 `run_failed` 并抛出异常；
- Planning、Progress、Replan 和报告调用仍没有通用重试包装；非 `LLMResponseError` 的 Decision 异常也会终止本次 `run()`，以免掩盖程序缺陷或不可恢复的 API 错误。

## 11. 自主性边界

真实模式下，模型自主决定下一步动作、查询词、目标 URL、Replan 内容和 Finish 时机。但 Runtime 固定了：

- 可用 Action 集合；
- 工具实现和安全检查；
- Context 字符预算与来源排序规则；
- 重复动作阈值；
- 最大轮数；
- State 与日志写入方式；
- 报告文件格式和来源附加方式。

`DemoLLMBackend` 是固定六轮脚本，只用于无需 API Key 的执行链路测试，不能代表真实模式的自主决策能力。

## 12. 当前未实现

- 当前版本未实现自动学习或策略更新；
- 当前版本未实现通用“反思”循环；
- 当前版本未实现 Multi-Agent 协作；
- 当前版本未实现人类审批节点；
- 当前版本未实现并行工具调用；
- 当前版本没有自动 Context RAG 或长期 Memory；`retrieve_knowledge` 仍由 LLM 自主选择，自动知识入库只在真实报告成功后作为独立后处理执行。

这些可以作为未来扩展方向，但不属于当前 `CompetitorAgent` 的职责或能力。
