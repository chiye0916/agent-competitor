# Agent State 设计

## 1. State 的作用

LLM 的每次 API 调用彼此独立，不会自动记住上一轮做过什么。当前系统使用 `agent/state.py` 中的 Python 数据结构 `AgentState` 保存任务执行事实，并在每轮后序列化到 `state.json`。

State 解决的是同一个运行中的连续执行与恢复问题：

- 当前任务是什么；
- 计划有哪些步骤、哪些已经完成；
- 已经获得哪些网页证据；
- 已执行到第几轮；
- 是否发生过 Replan；
- 最近发生了什么；
- 哪些动作发生重复；
- 最终报告是否已经生成。

它不是长期 Memory、对话记忆库、向量数据库或 RAG 索引。

## 2. `AgentState` 完整字段

当前代码中的实际字段如下。除需求中列出的核心字段外，代码还包含循环保护和审计时间字段。

| 字段 | 类型/默认值 | 作用 | 为什么需要保存 |
|---|---|---|---|
| `task` | `str`，必填 | 当前竞品研究目标 | Planning、每轮决策和最终报告必须始终围绕同一目标；恢复时也需要原始目标 |
| `plan` | `list[PlanStep]`，空列表 | 当前计划及每步状态 | 让模型知道剩余工作，并让 Runtime 在多轮之间保存完成进度 |
| `sources` | `list[Source]`，空列表 | 已搜索或读取的外部证据 | 避免每轮重新搜索；为后续网页读取、决策和报告提供引用来源 |
| `recent_events` | `list[dict]`，空列表 | 最近的状态事件窗口，最多 20 条 | 为恢复和下一轮 Context 提供近期过程信息；完整历史另存 `run.jsonl` |
| `action_history` | `list[str]`，空列表 | 规范化后的搜索/读取动作签名，最多 40 条 | 检测相同查询或 URL 是否重复执行 |
| `read_attempts` | `dict[str, int]`，空字典 | 各规范化 URL 的读取尝试次数 | 对重复读取实施上限保护，并把达到上限的 URL放入 Context 的 `blocked_urls` |
| `repetition_strikes` | `int`，`0` | 当前连续研究过程中的重复动作计数 | 达到阈值时触发 Runtime 请求强制 Replan；完成新步骤或 Replan 后清零 |
| `round` | `int`，`0` | 已进入的决策轮数 | 控制最大轮数、编号日志，并支持从中断点继续 |
| `replan_count` | `int`，`0` | 已应用的 Replan 次数 | 审计计划变更，并参与新步骤 ID 的生成 |
| `status` | `str`，`created` | 运行状态 | 区分 `created`、`running`、`failed`、`completed` |
| `report_path` | `str | None` | 最终报告文件路径 | 成功结束后关联运行状态与报告产物 |
| `created_at` | UTC ISO 时间 | State 创建时间 | 审计运行开始时间 |
| `updated_at` | UTC ISO 时间 | 最近保存时间 | 判断持久化状态的新旧；每次 `save()` 时更新 |

## 3. 嵌套结构

### 3.1 `PlanStep`

```python
@dataclass
class PlanStep:
    id: str
    description: str
    status: str = "pending"
```

- 初始计划使用 `P1`、`P2` 等 ID。
- Replan 产生的步骤使用 `R<replan_count>-<index>`，例如 `R1-1`。
- 当前代码实际使用的状态值是 `pending` 和 `completed`；没有 `in_progress`、`blocked` 或步骤级失败状态。

### 3.2 `Source`

```python
@dataclass
class Source:
    id: str
    title: str
    url: str
    snippet: str = ""
    content: str = ""
    retrieved_at: str
    via: str = "search_web"
```

- `id`：按加入顺序生成 `S1`、`S2`……，用于报告引用；
- `title`、`url`：来源标识；
- `snippet`：搜索 API 返回的摘要；
- `content`：`read_web` 获取的网页正文；
- `retrieved_at`：搜索创建或正文更新的 UTC 时间；
- `via`：默认 `search_web`，读取正文后更新为 `read_web`。

搜索结果按 URL Upsert：已存在的 URL 只更新摘要，不新增 Source。读取结果同样按精确 URL 更新已有 Source；如果 URL 尚不存在则新增 Source。

## 4. State 的写入时机

`CompetitorAgent.run()` 在以下位置调用 `state.save()`：

1. 初始 Planning 完成后；
2. 每轮 Action 执行、被阻止或发生 Tool error 后；
3. 达到最大轮数并标记 `failed` 后；
4. 报告生成完成并标记 `completed` 后。

`save()` 会覆盖同一运行目录中的 `state.json`，因此它表示最新快照，而不是历史版本集合。

## 5. State、Context 与 Log 的区别

| 对象 | 内容 | 生命周期 | 用途 |
|---|---|---|---|
| `AgentState` / `state.json` | 当前任务、计划、来源、进度、保护字段和近期事件 | 每轮覆盖保存，可恢复 | Runtime 的事实状态 |
| Decision Context | 从 State 选择出的有限模型输入 | 每轮重新构造，不单独落盘 | 决定下一步 Action、评估进度 |
| Report Context | 面向最终综合的较大证据输入 | Finish 后构造，不单独落盘 | 生成最终报告 |
| `run.jsonl` | 全部事件逐行追加 | 整个运行历史 | 观察与审计，不用于完整恢复 |

`recent_events` 最多保存 20 条；`ContextBuilder` 实际只取其中最后 6 条，并将每条详情压缩到约 350 字符。完整过程应查看 `run.jsonl`，而不是只看 `state.json`。

## 6. Context 如何使用 State

### 6.1 每轮决策 Context

`ContextBuilder` 从 State 构造：

- `task`
- 完整 `plan`
- `progress`：轮数、Replan 次数、来源数、被阻止 URL
- `source_index`：全部来源的精简索引
- `evidence`：预算内选取的正文或摘要
- 最近 6 个压缩事件

默认总预算为 14,000 字符，单个证据最多 1,800 字符。来源按“疑似官方、已读取、更新时间”排序。

### 6.2 最终报告 Context

`ReportContextBuilder` 从同一个 State 构造更大的报告输入：

- 任务和完整计划；
- 全部来源目录；
- 已读取正文组成的 `primary_evidence`；
- 搜索摘要组成的 `supplemental_evidence`；
- 固定的四维覆盖要求和报告策略。

默认预算为 70,000 字符，单个正文最多 7,000 字符。全文按内容哈希去重，并按域名组轮换，以减少单一域名占满预算的概率。

报告 Context 只是临时字典，不是持久化的 Memory 或证据数据库。

## 7. Replan 对 State 的影响

执行 Replan 时：

1. 保留所有 `status == "completed"` 的原计划步骤；
2. 删除所有尚未完成的原计划步骤；
3. 将新步骤写成 `R<n>-<index>`；
4. `replan_count += 1`；
5. `repetition_strikes = 0`。

系统没有保存完整的计划版本树。计划变更历史只能从 `run.jsonl` 中的 `tool_result` 或 `forced_replan` 事件还原。

## 8. 当前明确未实现的状态能力

- 当前版本未实现跨任务长期 Memory；
- 当前版本未实现 conversation memory；
- 当前版本未实现 memory database；
- 当前版本未实现 vector memory、Embedding 或 RAG；
- 当前版本未实现结构化事实表或持久化证据矩阵；
- 当前版本未实现 State 版本迁移框架；旧状态仅依靠 dataclass 默认字段兼容缺少的新字段。

这些可以作为未来扩展方向，但不属于当前系统能力。
