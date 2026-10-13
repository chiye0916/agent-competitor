# CompetitorAgent 设计说明

## 1. Agent Role

当前系统只有一个 Agent：`CompetitorAgent`。

它的角色是竞品研究 Agent Runtime：接收任务状态，组织 LLM 调用和外部工具调用，持续更新 State，并在研究结束后生成报告。

`CompetitorAgent` 本身不是另一个模型。它是 Python 控制器；推理由实现 `LLMBackend` 协议的后端完成。

## 2. Agent Responsibilities

当前实现中，`CompetitorAgent` 负责：

1. 初始化或加载 `AgentState`；
2. 在没有 Plan 时调用 LLM 创建研究计划；
3. 使用 `ContextBuilder` 构造每轮有限 Context；
4. 调用 LLM 判断唯一的下一步 Action；
5. 对搜索和读取 Action 做重复检测；
6. 调用 `ResearchTools` 执行搜索或网页读取；
7. 把工具结果转换成 Observation，并 Upsert 到 `sources`；
8. 调用 LLM 评估哪些 PlanStep 可以完成；
9. 执行模型主动 Replan 或重复保护触发的强制 Replan；
10. 每轮更新并保存 State；
11. 写入终端日志和 `run.jsonl`；
12. 控制最大轮数和结束条件；
13. 使用 `ReportContextBuilder` 构造最终报告 Context；
14. 调用 LLM 生成 Markdown 报告并附加来源清单。

## 3. Agent Loop

核心循环位于 `CompetitorAgent.run()`：

```text
Load/Create State
    ↓
Create Plan（仅在 plan 为空时）
    ↓
round += 1
    ↓
ContextBuilder.build(state)
    ↓
LLMBackend.decide(context, TOOL_SCHEMAS)
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

## 4. Agent Decision

真实模式下，`OpenAIBackend.decide()` 使用 function calling，并设置 `tool_choice="required"`。模型必须从以下四个 Action 中选择一个。

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

### 4.3 `replan`

参数：

- `reason`
- `new_steps`

Runtime 保留已完成步骤，用新步骤替换全部未完成步骤，并增加 `replan_count`。

### 4.4 `finish`

参数：

- `reason`
- `completed_step_ids`

如果至少已有一个来源，Runtime 接受 Finish，更新可匹配的计划步骤，然后进入最终报告生成。当前没有额外的结构化覆盖门禁。

## 5. Planning

当 `state.plan` 为空时，`OpenAIBackend.create_plan()` 要求 LLM 将任务拆成 3–6 个可验证、需要外部证据的步骤。Runtime 对返回结果做的处理只有：

- 去除空白项；
- 如果没有有效步骤则报错；
- 最多保留 8 个步骤；
- 编号为 `P1`、`P2`……。

Planning 是模型生成的研究导航，不是固定工作流，也不是任务调度系统。

## 6. Progress Assessment

每次 `search_web` 或 `read_web` 成功后，当前代码会调用 `LLMBackend.assess_progress()`。输入包括当前决策 Context 和刚完成的工具 Observation。

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
- 当 `repetition_strikes >= 2` 时，调用 `LLMBackend.revise_plan()`；
- 成功后记录 `forced_replan` 并应用新步骤。

因此“强制”指 Runtime 强制进入重新规划调用；新计划内容仍由 LLM 生成。当前没有针对一般错误、低质量证据或覆盖不足的规则式自动 Replan。

## 8. Context Management

### 8.1 决策 Context

`ContextBuilder` 负责将完整 State 压缩成每轮输入。默认预算 14,000 字符，包含全部来源索引、有限证据和最近 6 个事件。

### 8.2 报告 Context

`ReportContextBuilder` 只在 Finish 后使用，默认预算 70,000 字符。它优先加入已读取正文，按内容去重并按域名轮换，然后在剩余预算中加入搜索摘要。

两种 Context 都由 Runtime 每次临时构造，没有持久化上下文缓存，也没有向量检索。

## 9. Report Generation

Finish 后的流程为：

1. 构造报告 Context；
2. 记录 `report_context_built`；
3. 调用 `LLMBackend.write_report()`；
4. 检查返回内容非空；
5. 在报告末尾追加 State 中的全部来源及获取时间；
6. 写入 `report.md`；
7. 设置 `status="completed"` 和 `report_path`；
8. 记录 `run_completed` 并保存 State。

报告提示词要求使用 `[S1]` 形式引用，但 Runtime 当前不验证引用与结论是否匹配。

## 10. Error Handling

- 搜索/网页读取/Replan/Finish 执行异常会记录为 `tool_error`，保存 State 后继续下一轮；
- 进度审查异常会记录为 `progress_assessment_error`，本轮仍可保存工具结果；
- 强制 Replan 异常会记录为 `forced_replan_error`；
- 达到最大轮数后设置 `status="failed"`、记录 `run_failed` 并抛出异常；
- Planning、Decision 和报告 LLM 调用没有通用重试包装；这些异常会终止本次 `run()`，CLI 返回 1。

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
- 当前版本未实现长期 Memory 或 RAG。

这些可以作为未来扩展方向，但不属于当前 `CompetitorAgent` 的职责或能力。
