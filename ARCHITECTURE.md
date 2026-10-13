# 单 Agent 架构图

```mermaid
flowchart TD
    U[用户 Task] --> H[Python Agent Harness]
    H --> S[(AgentState / state.json)]
    S --> C[Context Builder<br/>计划 + 进度 + 证据摘要 + 最近事件]
    C --> L[无状态 LLM]
    L --> A{下一步 Tool Call}
    A -->|search_web| T1[Tavily Search API]
    A -->|read_web| T2[HTTP 网页读取]
    A -->|replan| P[替换未完成计划]
    T1 --> O[Observation]
    T2 --> O
    P --> O
    O --> S
    S -->|Continue| C
    A -->|finish| R[LLM 生成最终报告]
    S --> R
    R --> F[report.md + 来源]
    H --> J[run.jsonl 完整可观察日志]
```

关键循环是 `State → Context → LLM → Action → Tool → Observation → State Update`。LLM 每次调用都是无状态的；持续执行、持久化、错误恢复、最大轮数和报告落盘都由 Python harness 负责。
