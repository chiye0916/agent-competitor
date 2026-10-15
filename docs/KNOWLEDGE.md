# Knowledge Layer、检索与增量入库

## 1. 当前能力与边界

Knowledge Layer 目前包含三条相互独立的职责链：

1. Document、Chunk、TextSplitter、Embedding 与 Chroma 基础设施；
2. 由 LLM 自主选择的 `retrieve_knowledge` Tool 检索链；
3. 真实研究报告成功落盘后的自动知识入库链。

自动入库发生在 Agent Loop 和报告生成之后，不改变 Planning、Replan、Finish、State 或 Tool Calling。它不是每轮自动 RAG，也不是 Long-term Memory。Demo 不初始化真实知识库，不执行自动入库。

## 2. 模块职责

```text
knowledge/
├── document.py       # 原始知识文档和 Source 转换
├── chunk.py          # 向量索引片段
├── splitter.py       # 重叠字符窗口切分
├── policy.py         # 可配置的确定性入库规则
├── evaluator.py      # 可解释的 Source 接受/拒绝结果
├── ingestion.py      # 去重、版本判断、切分、Embedding、分阶段写入
└── service.py        # 报告完成后的隔离边界与日志

embedding/
├── base.py
└── openai_embedding.py

retrieval/
├── chroma_store.py   # Chroma、版本清单、可见性切换与查询过滤
└── retriever.py      # Tool 面向的查询服务

tools/
└── knowledge.py      # retrieve_knowledge Tool
```

`KnowledgeEvaluator` 不调用 LLM、Embedding 或 Chroma，也不修改 AgentState。`KnowledgeIngestionPipeline` 复用 `Document.from_source()`、TextSplitter、EmbeddingModel 和 ChromaStore，不复制已有能力。

## 3. 知识读取链

```text
LLM decide
    → retrieve_knowledge(query, top_k)
ToolRegistry
    → KnowledgeRetrievalTool
Retriever
    → ChromaStore.query
    → 只保留当前完整版本
Chunk Observation
    → Agent 下一轮决策
```

`retrieve_knowledge` 用于已有资料，`search_web` 用于知识库缺失或需要最新信息的场景。Prompt 只说明边界，不强制任一工具。检索结果不会自动转成 Source、不会写入 AgentState，也不会自动注入 ContextBuilder。

## 4. 自动入库触发时机

真实模式的顺序是：

```text
Agent finish
    → write_report
    → report.md 成功写入
    → State 标记 completed
    → run_completed
    → KnowledgeIngestionService
    → KnowledgeIngestionPipeline
```

只有 `agent.run()` 成功返回后才触发入库。研究失败、报告为空、达到最大轮数或 Demo 模式不会触发。设置 `KNOWLEDGE_AUTO_INGEST=false` 可以关闭自动入库。

入库状态单独写入运行目录的 `knowledge_ingestion.json`，状态包括 `pending`、`running`、`completed`、`partial_failed` 和 `failed`。它不修改 AgentState 的 `completed` 状态。入库异常记录为 `ingestion_error`，已生成的 `report.md` 保持有效，CLI 的研究成功结果也不会因为自动入库失败而改为失败。

## 5. 入库规则

第一版使用确定性规则，不使用 LLM 质量评审。Source 只有同时满足以下条件才会被接受：

- URL 是不带用户凭据的有效 HTTP/HTTPS URL；
- `via == "read_web"`，证明正文来自网页读取而不是搜索摘要；
- `content` 非空，不包含 NUL 等明显非文本内容；
- 规范化正文达到 `KNOWLEDGE_MIN_CONTENT_CHARS`；
- 正文包含足够的字母、数字或中文等有效字符。

只有 snippet、正文为空、过短、明显非文本或 URL 无效的 Source 会被拒绝。标题中是否出现 `official`、`docs`、`pricing` 不参与准入判断；当前规则不会把第三方资料一律排除，也不会把向量相似度视为可信度。

每个评估结果都记录：

```json
{
  "accepted": true,
  "reason": "valid_full_text",
  "source_id": "S1",
  "quality_flags": []
}
```

拒绝原因和后续处理结果同样保存在 `knowledge_ingestion.json`。

## 6. Document ID、Content Hash 与 Chunk ID

### 6.1 Document ID

Document ID 表示跨任务稳定的文档身份。URL 规范化只处理可以确定不改变资源身份的差异：

- 协议和主机名转小写；
- 删除 fragment；
- 删除 HTTP 80 / HTTPS 443 默认端口；
- 空路径规范为 `/`；
- 保留路径、尾部斜杠和全部查询参数。

之后计算：

```text
document_id = "doc_" + SHA256(normalized_url)
```

Source ID 只在一次 Agent 运行中唯一，不参与跨任务文档身份判断。

### 6.2 Content Hash

Content Hash 表示内容版本。正文统一换行、压缩行内空白并删除空行后计算：

```text
content_hash = SHA256(normalized_content)
```

- Document ID 相同且 Content Hash 相同：`unchanged`；不切分、不 Embedding、不写新向量。
- Document ID 相同但 Content Hash 不同：`updated`；生成并切换新版本。
- Document ID 尚不存在：`new`。

### 6.3 Chunk ID 与来源元数据

```text
chunk_id = document_id + content_hash + chunk_index
```

相同版本重试会得到相同 Chunk ID，因此 Chroma `upsert` 不会制造重复记录。Chunk metadata 保留 `document_id`、`source_url`、`source_title`、本次运行的 `source_id`、`content_hash`、`retrieved_at`、`ingested_at`、`chunk_index` 和字符范围。完整 metadata 被编码为 JSON 字符串，以符合 Chroma 实际支持的标量 metadata 类型。

## 7. 增量更新与失败恢复

Chroma 不提供本项目所需的跨向量与文件清单数据库事务，因此实现明确采用分阶段写入，而不声称具有数据库级原子事务：

```text
生成全部 Embedding
    → 用稳定 Chunk ID stage/upsert 新版本
    → 回读并校验全部 Chunk ID 已存在
    → 原子替换版本清单文件
    → 新版本对 Retriever 可见
    → 尝试清理旧版本向量
```

版本清单位于 `KNOWLEDGE_DB_PATH/<collection>.versions.json`。Retriever 以清单中的 `content_hash` 为逻辑可见性依据：

- Embedding 在写入前失败：Chroma 不变，旧版本继续可用；
- Chroma 部分写入后抛错或完整性校验失败：清单不切换，新 Chunk 不可检索；
- 程序在 stage 后崩溃：重试使用相同 ID 补写；
- 清单切换后清理旧向量失败：新版本仍可用，旧版本被查询过滤；后续相同内容重试会再次清理；
- 清单切换后状态文件尚未更新就崩溃：重试会识别为 `unchanged`，无需再次 Embedding。

这保证旧完整版本不会因新版本写入失败而失效。当前未实现多进程写入锁，因此不要并发运行多个写入同一 Collection 的入库进程。

## 8. 配置

```env
EMBEDDING_MODEL=text-embedding-3-small
KNOWLEDGE_DB_PATH=.knowledge/chroma
KNOWLEDGE_COLLECTION=competitor_knowledge
KNOWLEDGE_AUTO_INGEST=true
KNOWLEDGE_MIN_CONTENT_CHARS=200
KNOWLEDGE_CHUNK_SIZE=500
KNOWLEDGE_CHUNK_OVERLAP=50
```

`.knowledge/` 被 Git 忽略。更换 Embedding 模型可能改变向量维度或语义空间；当前没有自动迁移机制，应为不兼容模型使用新的 Collection。

## 9. 重试与手动验证

对已完成运行单独重试入库，不会重新执行 Agent：

```powershell
python main.py --ingest-existing artifacts\<run>\state.json
```

任务 A 入库、任务 B 复用的手动流程：

```powershell
$env:KNOWLEDGE_AUTO_INGEST = "true"
python main.py --task "调研产品 A 的定位、价格和核心能力" --output-dir artifacts\knowledge-task-a
Get-Content artifacts\knowledge-task-a\knowledge_ingestion.json

python main.py --task "基于已有知识比较产品 A 的价格与定位，并在资料不足时搜索网页" --output-dir artifacts\knowledge-task-b
Select-String -Path artifacts\knowledge-task-b\run.jsonl -Pattern 'retrieve_knowledge'
```

LLM 自主决定是否调用 `retrieve_knowledge`，因此第二条命令不保证固定调用。需要确定性验证时运行自动化测试：

```powershell
python -m unittest tests.test_knowledge_ingestion -v
python -m unittest discover -s tests -v
python main.py --demo
```

## 10. 已知限制

- 没有知识过期检测、定时刷新或自动重新抓取；`retrieved_at` 只帮助调用方判断新旧。
- 没有来源冲突消解、事实真实性验证或可信度评分。
- 向量召回质量依赖 Embedding、Chunk 参数和查询表达，当前没有检索评测集或 reranker。
- 为保证 staged/旧版本不会挤占 `top_k`，当前查询会读取 Collection 的全部候选后再按版本清单过滤；数据量大时需要改进索引布局。
- 版本清单使用原子文件替换，但没有跨进程写锁，也不是 Chroma 的数据库事务。
- 自动入库只处理本次 State 中通过 `read_web` 获得的正文，不自动导入 `retrieve_knowledge` Observation。
- 没有每轮 Context 自动检索、Long-term Memory、Multi-Agent 或 LLM 入库评审。
