# Knowledge Layer、检索与增量入库

## 1. 当前能力与边界

Knowledge Layer 目前包含三条相互独立的职责链，并由 Agent 层补充当前任务 Evidence：

1. Document、Chunk、TextSplitter、Embedding 与 Chroma 基础设施；
2. 由 LLM 自主选择的 `retrieve_knowledge` Tool 检索链；
3. 真实研究报告成功落盘后的自动知识入库链。

`Knowledge` 是 Chroma 中可跨任务复用的长期资料；`RetrievedEvidence` 是本次任务由 `retrieve_knowledge` 实际召回的 Chunk 快照；`Source` 是当前任务搜索或读取到的网页来源。三者不会互相替代。

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

agent/
└── evidence.py       # 当前任务 Evidence 与程序化 Citation
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
    → RetrievedEvidence（state.json）
    → Agent 下一轮决策 / ReportContextBuilder
```

`retrieve_knowledge` 用于已有资料，`search_web` 用于知识库缺失或需要最新信息的场景。Prompt 只说明边界，不强制任一工具。检索结果不会转成 Source，也不会触发自动检索；Tool 会把实际返回的每个 Chunk 转为 `RetrievedEvidence`，按 `chunk_id` 去重、分配 `E#`，保存到 AgentState，之后由 ContextBuilder 正常读取。

### 3.1 Evidence 来源链

每个 `RetrievedEvidence` 至少保存 `chunk_id`、`document_id`、正文、`source_url` 和 `source_title`，并复用现有 Chunk metadata 中的 `source_id`、`content_hash`、`retrieved_at`、`ingested_at`。因此可以沿以下链路反向审计：

```text
E# → RetrievedEvidence → Chunk ID → Document ID → Source ID / Source URL
```

Evidence 只来自本次任务实际得到的 Chunk，不会扫描或自动引用知识库中未召回的其他文档。

### 3.2 报告引用

报告模型对网页证据写内部 `[S#]`，对知识证据写内部 `[E#]`。`ReportCitationRenderer` 只接受本次 Report Context 中真实存在、具有 HTTP(S) URL 且被模型实际写入正文的 ID；随后按首次出现顺序转换为 `[1]`、`[2]`，并从系统数据追加标题、URL、获取时间以及知识证据的 Document/Chunk/Content Hash。不存在的 ID 和模型输出的非证据 URL 会被移除。

这是 Evidence-level Citation，不是 Claim-Level Citation：程序能证明引用指向哪条 Evidence，但不自动判断该 Evidence 是否充分支持具体句子。

### 3.3 任务级新鲜度模式

`knowledge_mode=existing` 是默认值：优先复用已有知识，不因为 `retrieved_at` 或 `ingested_at` 较早就自动联网。用户可通过 `--knowledge-mode latest` 显式选择，或在新任务中明确写“最新”“当前”“截至今天”“实时”“检查最新信息”“重新核实”等要求，由 CLI 推断为 `latest`。

`latest` 会进入决策和报告 Context，并让本次有效网页 Source 优先于旧知识 Evidence；它只是授权 LLM 自主判断是否调用 `search_web` / `read_web`，不会强制工具顺序、按天数触发搜索、定时刷新或改动 Step 3 的版本管理。

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
EMBEDDING_API_KEY=<embedding-provider-key>
EMBEDDING_BASE_URL=https://api.openai.com/v1
EMBEDDING_MODEL=text-embedding-3-small
KNOWLEDGE_DB_PATH=.knowledge/chroma
KNOWLEDGE_COLLECTION=competitor_knowledge
KNOWLEDGE_AUTO_INGEST=true
KNOWLEDGE_MIN_CONTENT_CHARS=200
KNOWLEDGE_CHUNK_SIZE=500
KNOWLEDGE_CHUNK_OVERLAP=50
```

`OpenAIEmbedding` 使用独立的 `EMBEDDING_API_KEY` 和 `EMBEDDING_BASE_URL`，因此对话可继续使用 DeepSeek 等不提供 Embedding 的服务。为了兼容原有单提供商配置，这两项留空时分别回退到 `LLM_API_KEY` 和 `LLM_BASE_URL`。`EMBEDDING_MODEL` 必须是 Embedding 提供商实际支持的模型。

2026-10-15 已使用 Voyage AI `voyage-4` 完成真实入库和查询验证。当 Key 来自 Voyage AI Dashboard 时，配置为：

```env
EMBEDDING_API_KEY=<voyage-api-key>
EMBEDDING_BASE_URL=https://api.voyageai.com/v1
EMBEDDING_MODEL=voyage-4
```

`EMBEDDING_BASE_URL` 只填到 `/v1`，SDK 会自动请求 `/embeddings`。Voyage AI Key 不能与 MongoDB Atlas 的 `https://ai.mongodb.com/v1` 端点混用。未绑定付款方式的实测账户受到 3 RPM 限制；这是提供商限流，不是 Chroma 或 Tool Registry 故障。

`.knowledge/` 被 Git 忽略。更换 Embedding 模型可能改变向量维度或语义空间；当前没有自动迁移机制，应为不兼容模型使用新的 Collection。

## 9. 重试与手动验证

对已完成运行单独重试入库，不会重新执行 Agent：

```powershell
python main.py --ingest-existing artifacts\<run>\state.json
```

任务 A 入库、任务 B 复用的手动流程（任务 B 必须写明产品名，不要使用“产品 A”占位符）：

```powershell
$env:KNOWLEDGE_AUTO_INGEST = "true"
python main.py --task "调研 2Cshop 的定位、价格和核心能力" --output-dir artifacts\knowledge-task-a
Get-Content artifacts\knowledge-task-a\knowledge_ingestion.json

python main.py --task "优先查询已有知识，总结并分析 2Cshop 的价格方案与产品定位；如果知识库资料不足，再搜索网页" --output-dir artifacts\knowledge-task-b-2 --max-rounds 20
Select-String -Path artifacts\knowledge-task-b-2\run.jsonl -Pattern 'retrieve_knowledge'

python main.py --task "核实 2Cshop 当前价格" --knowledge-mode latest
```

LLM 自主决定是否调用 `retrieve_knowledge`，因此第二条命令不保证固定调用。需要确定性验证时运行自动化测试：

```powershell
python -m unittest tests.test_knowledge_ingestion -v
python -m unittest discover -s tests -v
python main.py --demo
```

### 9.1 2026-10-15/16 真实验证结果

`artifacts/knowledge-task-a` 首次入库完成：14 个 Source 中 3 个 `read_web` 正文被接受，11 个搜索摘要被拒绝，新增 3 篇文档和 31 个 Chunk，失败数为 0。

`artifacts/knowledge-task-b-2` 证明了跨任务复用：前 3 轮 `retrieve_knowledge` 每次返回 8 个相关 Chunk。第 4–6 轮因 Voyage 账户的 3 RPM 限制返回 429，Agent 随后改用 `search_web` 和 `read_web`，在第 9 轮成功生成报告。报告后自动入库同样因 429 失败，等待限流窗口后执行：

```powershell
python main.py --ingest-existing artifacts\knowledge-task-b-2\state.json
```

手动重试新增 1 篇文档和 4 个 Chunk。在 2026-10-16 再次执行同一命令，结果为 `new=0`、`unchanged=1`、`chunks_written=0`、`failed=0`，真实验证了幂等去重。当前版本清单中共有 4 篇活跃文档、35 个 Chunk。

### 9.2 只读 RAG 人工验证

Step 4.2 增加了 Agent 运行链之外的只读验证入口：

```powershell
python scripts\test_retrieval.py --query "2Cshop 支持多少种语言？" --top-k 5
python scripts\test_retrieval.py --queries-file tests\data\rag_queries.json --top-k 5 --delay-seconds 21
```

诊断查询返回 `RetrievalMatch`，其中保留 Chroma 的原始 cosine distance；原有 `Retriever.query()` 和 `retrieve_knowledge` Tool 仍返回/消费 `Chunk`，行为不变。脚本以只读意图打开已存在的 Collection，不调用写入方法，并对比执行前后的 Collection 数量和版本清单摘要。

当前 15 条真实 Query 的人工结果、失败案例和参数实验建议记录在 `docs/rag-evaluation.md`。本轮 Source Hit@1/3/5 分别为 69.2% / 92.3% / 92.3%；两条知识库无答案的 Query 都仍返回 5 条候选。该结果用于暴露排序、来源冲突、答案充分性和无答案拒绝边界问题，不代表已经实现自动 RAG Evaluation。

## 10. 已知限制

- 没有知识过期检测、定时刷新或自动重新抓取；`retrieved_at` 只帮助调用方判断新旧。
- 没有来源冲突消解、事实真实性验证或可信度评分。
- 向量召回质量依赖 Embedding、Chunk 参数和查询表达；当前只有 15 条 Query 的人工验证集和只读诊断脚本，没有自动评分器或 reranker。
- 为保证 staged/旧版本不会挤占 `top_k`，当前查询会读取 Collection 的全部候选后再按版本清单过滤；数据量大时需要改进索引布局。
- 版本清单使用原子文件替换，但没有跨进程写锁，也不是 Chroma 的数据库事务。
- 自动入库只处理本次 State 中通过 `read_web` 获得的正文，不会把 `retrieve_knowledge` 返回的 Evidence 再次入库。Evidence 不转成 Source，但会单独进入 Decision/Report Context。
- `retrieve_knowledge` 尚未纳入重复动作保护，也没有查询缓存、429 退避或提供商级速率控制；相同/相近查询可能在短时间内重复请求 Embedding。
- 当前通用 `OpenAIEmbedding` 调用未传递 Voyage 的 `input_type=query/document`，功能已验证可用，但还不是面向 Voyage 检索质量的原生适配。
- 没有每轮 Context 自动检索、自动知识刷新、Claim-Level Citation、自动化 RAG Evaluation、Long-term Memory、Multi-Agent 或 LLM 入库评审。
