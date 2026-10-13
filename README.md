# 单 Agent 竞品分析系统

这是一个不用 LangGraph、CrewAI、AutoGen 等编排框架的教学型 MVP。Python harness 自己实现 Planning、函数调用、多轮 Agent Loop、State 持久化、Context 裁剪、Replan、来源保留、可观察日志和最终报告。

## 项目结构

```text
main.py                 核心代码：配置、状态、工具、LLM、循环与 CLI
tests/test_agent.py     无 Key 的可执行测试
ARCHITECTURE.md         Mermaid 架构图
SUMMARY.md              阶段一概念总结
.env.example            配置模板
artifacts/demo/         离线完整运行的状态、日志和报告
```

## 安装

项目使用现有 Conda 环境 `competitor-agent`（Python 3.11），没有新建环境。

```powershell
D:\anaconda\Scripts\conda.exe activate competitor-agent
python -m pip install -r requirements.txt
Copy-Item .env.example .env
```

编辑 `.env`：

- `LLM_API_KEY`：OpenAI 或兼容服务的 Key。
- `LLM_MODEL`：模型名。模型需要支持 Chat Completions、JSON mode 和 function calling。
- `LLM_BASE_URL`：使用兼容服务时填写，OpenAI 官方留空。
- `TAVILY_API_KEY`：Tavily Search API Key。

`.env` 已被 `.gitignore` 忽略，不要提交真实 Key。

## 运行

先运行完全离线的演示。它使用固定模型决策和网页夹具，验证 harness 全链路，不代表真实竞品调研：

```powershell
python main.py --demo --output-dir artifacts/demo
```

真实调研：

```powershell
python main.py --task "调研并比较 Cursor、Claude Code、Codex 的定位、价格、核心能力和适用场景，优先引用官方资料"
```

从中断状态恢复：

```powershell
python main.py --resume artifacts/<run>/state.json
```

每次运行输出：

- `state.json`：每轮持久化的任务状态；
- `run.jsonl`：完整逐轮日志；
- `report.md`：带 `[S1]` 引用和来源清单的最终报告。

## 测试

测试不需要 API Key 或网络：

```powershell
python -m unittest discover -s tests -v
```

覆盖多轮循环、搜索/读取工具路径、模型驱动的步骤完成、Replan、状态持久化、Context 事件窗口与字符预算、报告和来源生成。真实 LLM 与 Tavily 调用必须配置有效 Key 后单独验证。

## 为什么不是硬编码流程？

真实模式下，harness 只提供四种能力及安全边界，不规定固定竞品分析顺序。每轮模型根据当前 Context 通过 function calling 自主选择 `search_web`、`read_web`、`replan` 或 `finish`，并决定查询词、URL 和对应计划步骤。`--demo` 的固定决策仅是无需 Key 的确定性验收夹具。

每次搜索或读取成功后，系统会再调用一次模型做独立进度审查，而不是由工具调用直接宣告步骤完成。Context 包含全部来源的精简索引，正文则优先选择近期、已读取且疑似官方的来源。相同搜索或 URL 最多执行两次；重复动作达到阈值后，harness 会拒绝执行并强制 Replan。

最终报告使用独立的 Report Context：对已读取网页正文去重，按来源域名轮换以保证不同竞品都能获得证据空间，并优先发送较长正文；只有剩余预算才加入搜索摘要。默认报告预算为 `REPORT_CONTEXT_CHAR_BUDGET=70000`，不会增加每一轮决策的 Context 成本。

## 已知边界

- 网页读取器面向普通 HTML/文本，不执行 JavaScript，也不解析 PDF。
- 搜索仅实现 Tavily；可在 `ResearchTools` 中增加其他 API。
- Context 使用简单字符预算，不是精确 token 计数。
- Tool error 会写入状态并进入下一轮，但 MVP 未实现指数退避。
