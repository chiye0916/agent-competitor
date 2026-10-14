# Agent 任务模型

## 1. 任务在系统中的表示

当前系统把任务表示为 `AgentState.task` 中的一段字符串：

```python
@dataclass
class AgentState:
    task: str
```

系统没有额外的任务对象、任务队列、任务优先级、依赖图或调度系统。`task` 是整个研究运行的顶层目标，也是 Planning、每轮决策和最终报告生成的共同输入。

## 2. 输入任务是什么

真实模式下，用户通过 CLI 的 `--task` 传入目标，例如：

```powershell
python main.py --task "分析 Cursor、Claude Code、Codex 的产品定位、功能和价格"
```

如果既没有提供 `--task`，也没有使用 `--resume` 或 `--demo`，CLI 会返回错误码 2。系统只检查任务是否存在，不对产品数量、研究维度、语言或输出格式做结构化校验。

`--demo` 是例外：它会使用固定任务“比较 Alpha 与 Beta 的定位、定价和适用场景”，并配合固定的模型决策和网页夹具验证执行链路。Demo 结果不是实时竞品研究。

## 3. Agent 如何理解任务

任务理解由 LLM 调用完成，而不是由 Python 代码解析具体竞品字段：

1. `CompetitorAgent.run()` 检查 `state.plan` 是否为空；
2. 若为空，调用 `LLMBackend.create_plan(task)`；
3. `OpenAIBackend.create_plan()` 要求模型把目标拆成 3–6 个可验证、需要外部证据的步骤；
4. 后端最多接受前 8 个非空步骤；
5. Runtime 将步骤编号为 `P1`、`P2`……并保存为 `PlanStep`。

因此，当前系统对任务的“理解”表现为模型生成的研究计划。Python Runtime 不包含预设的“先价格、再功能、再场景”工作流。

## 4. 任务如何驱动 Plan 和后续执行

`task` 会进入两种 Context：

- `ContextBuilder` 生成的决策 Context：用于每轮 Action 选择和进度审查；
- `ReportContextBuilder` 生成的报告 Context：用于最终报告综合。

Plan 生成后，每轮 Context 同时包含：

- 原始 `task`；
- 全部计划步骤及其状态；
- 当前轮数、Replan 次数、来源数量和被重复保护阻止的 URL；
- 全部来源的精简索引；
- 字符预算内选取的证据；
- 最近 6 个压缩事件。

真实模式下，LLM 根据这些信息从 `search_web`、`read_web`、`retrieve_knowledge`、`replan`、`finish` 中选择一个 Action。查询词、URL、用途和目标步骤由 LLM 在 Action 参数中给出。`retrieve_knowledge` 用于复用已有知识库，`search_web` 用于知识库缺失或需要最新信息的情况；Runtime 不强制固定调用顺序。

成功执行搜索或读取后，系统调用 `assess_progress()`，让模型根据当前计划、已有证据和最新 Observation 提议可完成的步骤。Runtime 只会更新实际存在且尚未完成的步骤 ID。

## 5. 真实任务示例

`artifacts/20261012-101236/state.json` 中保存的真实任务为：

> 调研并比较 Cursor、Claude Code、Codex 的定位、价格、核心能力和适用场景，优先引用官方资料

该任务生成了 6 个初始步骤：

1. `P1`：查找三者官方产品页、官方文档或发布说明，确认定位与目标用户；
2. `P2`：从官方定价资料提取免费额度、订阅档位、按量计费和企业方案；
3. `P3`：从官方资料提取代码生成、补全、多文件编辑、CLI、Agent 等核心能力；
4. `P4`：查找官方适用场景、案例或推荐用法；
5. `P5`：对缺失或不一致的维度做交叉验证；
6. `P6`：整理结构化对比表并标注引用。

这组步骤来自该次 LLM Planning 结果，不是写死在代码中的通用模板。

## 6. 任务的结束条件

模型通过 `finish` Action 声明研究可以结束，并可提交 `completed_step_ids`。Runtime 当前只做两项直接处理：

1. 至少已有一个 `Source`，否则拒绝 Finish；
2. 将 `completed_step_ids` 中匹配现有计划的步骤设为 `completed`。

之后系统构造报告 Context、调用 `write_report()`、写入 `report.md`，并将状态改为 `completed`。

当前没有独立的任务完成度规则引擎，也没有强制校验“每个竞品 × 每个维度”都已有证据。最终是否足够完成主要依赖 LLM 决策。

## 7. 恢复任务

使用：

```powershell
python main.py --resume artifacts/<run>/state.json --max-rounds 40
```

系统会加载原 `task`、Plan、Sources 和进度，并把新事件追加到同目录的 `run.jsonl`。`--max-rounds` 是累计上限；若状态已执行 30 轮并传入 `--max-rounds 40`，最多继续到第 40 轮。

恢复机制不是任务调度系统，也不提供多个任务之间的排队或切换。
