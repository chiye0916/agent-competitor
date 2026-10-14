# Knowledge Layer 与 Tool 化检索

## 1. 阶段范围

阶段三 Step 1 建立了 Document、Chunk、文本切分、Embedding 和 Chroma 向量存储基础设施。Step 2 在不修改 Agent Loop、State、Checkpoint、Planning 和 ContextBuilder 核心逻辑的前提下，通过 `retrieve_knowledge` Tool 将查询能力接入真实运行模式。

检索由 LLM 自主决定是否调用，不是自动 Context RAG。系统不会自动索引 `AgentState.sources`，检索结果也不会自动写回 State、转成 Source 或注入最终报告证据。

## 2. 目录与职责

```text
knowledge/
├── __init__.py
├── document.py          # 原始知识文档及 Source 显式转换入口
├── chunk.py             # 可检索文本片段
└── splitter.py          # 重叠字符窗口切分

embedding/
├── __init__.py
├── base.py              # EmbeddingModel 抽象接口
└── openai_embedding.py  # OpenAI-compatible Embedding 实现

retrieval/
├── __init__.py
├── chroma_store.py      # Chroma 写入和向量查询
└── retriever.py         # 查询服务边界，隔离 Tool 与 Chroma 细节

tools/
└── knowledge.py         # KnowledgeRetrievalTool 与统一 Observation
```

### `Document`

`Document` 保存 `id`、`title`、可选 `url`、完整 `content` 和 `metadata`，支持 `to_dict()` / `from_dict()` 序列化。`Document.from_source()` 可以显式复制 Agent `Source` 的正文和来源信息，但不会修改原 Source。

### `Chunk` 与 `TextSplitter`

`Chunk` 保存唯一 ID、所属 `document_id`、片段文本和元数据。`TextSplitter(chunk_size=500, chunk_overlap=50)` 使用无第三方依赖的重叠字符窗口切分，并补充标题、URL、片段序号和字符位置。

### `EmbeddingModel` 与 `OpenAIEmbedding`

`EmbeddingModel` 定义：

```python
embed_text(text) -> list[float]
embed_documents(texts) -> list[list[float]]
```

`OpenAIEmbedding` 复用当前 `Config` 的 API Key 和兼容服务 Base URL，默认模型由 `EMBEDDING_MODEL` 指定。批量文本通过一次 SDK `embeddings.create()` 调用提交，并按响应 `index` 恢复输入顺序。

### `ChromaStore` 与 `Retriever`

`ChromaStore` 支持进程内 Client 和指定 `persist_directory` 的本地持久化 Client。`add_chunks()` 可接受预计算向量，也可调用注入的 EmbeddingModel；`query(text, top_k)` 使用相同模型生成查询向量并返回 `list[Chunk]`。

`Retriever` 是 Tool 与存储实现之间的服务层。它接收查询文本和 `top_k`，委托 ChromaStore 检索并返回 Chunk，使 Tool 不依赖 Chroma 的集合、向量和持久化细节。

### `KnowledgeRetrievalTool`

`KnowledgeRetrievalTool` 注册名为 `retrieve_knowledge`，参数为必填 `query` 和默认值为 5 的可选 `top_k`。成功时返回统一 Observation：

```json
{
  "success": true,
  "tool": "retrieve_knowledge",
  "query": "AI model pricing",
  "result_count": 2,
  "results": [
    {
      "chunk_id": "...",
      "document_id": "...",
      "text": "...",
      "metadata": {}
    }
  ]
}
```

参数非法、工具不存在或底层检索异常时，沿用 Agent 已有 Tool 错误路径产生 `tool_error`，下一轮仍可恢复。

## 3. 数据流

知识入库仍需独立调用方显式执行：

```text
Document / Document.from_source
    → TextSplitter
list[Chunk]
    → EmbeddingModel.embed_documents
list[Embedding]
    → ChromaStore.add_chunks
Chroma Collection
```

Agent 运行时的查询链路为：

```text
LLM decide
    → retrieve_knowledge(query, top_k)
ToolRegistry
    → KnowledgeRetrievalTool
Retriever
    → ChromaStore.query
EmbeddingModel + Chroma Collection
    → list[Chunk]
KnowledgeRetrievalTool
    → observation
Agent 下一轮决策
```

`search_web` 用于知识库缺少资料或需要最新信息的场景；`retrieve_knowledge` 用于复用已入库资料。Prompt 只说明两者边界，不强制任何工具或固定调用顺序。

## 4. 配置与运行模式

真实模式初始化持久化 Chroma、EmbeddingModel 和 Retriever，并将 KnowledgeRetrievalTool 注册到默认 Tool Registry：

```env
LLM_API_KEY=...
LLM_BASE_URL=
EMBEDDING_MODEL=text-embedding-3-small
KNOWLEDGE_DB_PATH=.knowledge/chroma
KNOWLEDGE_COLLECTION=competitor_knowledge
```

`.knowledge/` 默认被 Git 忽略。Demo 模式不初始化远程 Embedding 或 Chroma，仍只注册原有四个工具，以保持固定演示流程和离线可运行性。

## 5. 测试

只运行 Knowledge Layer 基础设施测试：

```powershell
python -m unittest tests.test_knowledge -v
```

只运行 Tool 化检索测试：

```powershell
python -m unittest tests.test_knowledge_tool -v
```

运行全部测试：

```powershell
python -m unittest discover -s tests -v
```

Tool 测试覆盖注册与动态 Schema、默认 `top_k`、统一 Observation、模拟 LLM 选择 `retrieve_knowledge` 后由未改动 Agent Core 完成执行，以及底层异常转换为 `tool_error` 后继续恢复。

## 6. 当前边界

- 没有自动 Context 检索；是否查询完全由 LLM 的 Tool Calling 决定。
- 没有自动知识入库；Document、Chunk 和向量仍由显式流程创建。
- Chunk 不会自动转成 Source 或写入 AgentState。
- 检索结果当前作为 Tool Observation 参与后续决策，不会自动进入 ReportContextBuilder 的证据集合。
- 没有 LangChain、LangGraph 或其他 Agent 编排框架。

这些边界使本阶段只增加知识库访问能力，不改变现有 Agent 的编排与状态语义。
