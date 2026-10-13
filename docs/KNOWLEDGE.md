# Knowledge Layer 与 Chroma 基础设施

## 1. 阶段范围

本阶段只建立后续 RAG 所需的知识管理基础设施：文档表示、文本切分、Embedding 抽象、OpenAI Embedding 实现和 Chroma 向量存储。

当前没有修改 `CompetitorAgent`、`ContextBuilder`、LLM 决策逻辑或 Tool Registry。Knowledge Layer 不会自动读取 `AgentState.sources`，检索结果也不会自动进入每轮 Context，因此现有 Agent 行为保持不变。

## 2. 目录与职责

```text
knowledge/
├── __init__.py
├── document.py      # Document 与 Source 转换入口
├── chunk.py         # Chunk 数据结构
└── splitter.py      # 重叠字符窗口切分

embedding/
├── __init__.py
├── base.py          # EmbeddingModel 抽象接口
└── openai_embedding.py

retrieval/
├── __init__.py
└── chroma_store.py  # Chroma 写入和查询
```

### `Document`

`Document` 保存 `id`、`title`、可选 `url`、完整 `content` 和任意 `metadata`，通过 `to_dict()` / `from_dict()` 序列化。`Document.from_source()` 可以显式复制当前 Agent `Source` 的正文和来源信息，但不会修改原 Source。

### `Chunk`

`Chunk` 保存唯一 ID、所属 `document_id`、片段文本和元数据。切分器会补充标题、URL、片段序号及字符起止位置。

### `TextSplitter`

`TextSplitter(chunk_size=500, chunk_overlap=50)` 使用无第三方依赖的字符窗口切分。约束为：`chunk_size > 0`、`chunk_overlap >= 0` 且 overlap 小于 size。

### `EmbeddingModel` 与 `OpenAIEmbedding`

`EmbeddingModel` 定义：

```python
embed_text(text) -> list[float]
embed_documents(texts) -> list[list[float]]
```

`OpenAIEmbedding` 复用当前 `Config` 的 API Key 和兼容服务 Base URL，默认模型来自 `EMBEDDING_MODEL`，默认值为 `text-embedding-3-small`。批量输入通过一次 SDK `embeddings.create()` 调用提交，并按响应 `index` 恢复输入顺序。

### `ChromaStore`

`ChromaStore` 支持两种初始化方式：

- 不传 `persist_directory`：使用进程内 Chroma Client；
- 传入路径：使用 `PersistentClient` 保存本地 Collection。

`add_chunks()` 接受 Chunk 和可选的预计算 Embedding；未提供向量时调用注入的 `EmbeddingModel`。写入使用 `upsert`，相同 Chunk ID 可重复更新。由于 Chroma Metadata 只接受有限标量类型，完整 Chunk metadata 会编码为 JSON 字符串，查询时再还原。

`query(text, top_k)` 使用同一 EmbeddingModel 生成查询向量，执行余弦相似度查询并返回 `list[Chunk]`。

## 3. 数据流

```text
Source（可选显式转换）
    ↓ Document.from_source
Document
    ↓ TextSplitter
list[Chunk]
    ↓ EmbeddingModel.embed_documents
list[Embedding]
    ↓ ChromaStore.add_chunks
Chroma Collection
    ↓ ChromaStore.query(text, top_k)
相关 Chunk
```

这条数据流当前由独立调用方执行，不在 Agent Loop 内自动发生。

## 4. 配置与依赖

安装依赖：

```powershell
pip install -r requirements.txt
```

真实 OpenAI Embedding 需要：

```env
LLM_API_KEY=...
LLM_BASE_URL=
EMBEDDING_MODEL=text-embedding-3-small
```

测试使用本地确定性 Embedding，不消耗 API 额度，也不需要网络请求。

## 5. 测试

只运行 Knowledge Layer 测试：

```powershell
python -m unittest tests.test_knowledge -v
```

运行全部测试：

```powershell
python -m unittest discover -s tests -v
```

端到端测试创建 OpenAI pricing、DeepSeek pricing 和 Python tutorial 三个 Document，依次完成切分、向量化和 Chroma 写入，再查询 `AI model pricing`，验证前两类定价文档被召回。

## 6. 为什么尚未接入 Agent

本阶段目标是先稳定数据契约和存储边界。若现在直接修改 ContextBuilder 或 Agent Loop，会同时引入入库时机、检索触发、Context 预算、去重、来源引用和恢复语义等决策，难以单独验证基础设施。

后续阶段可以在当前接口之上设计 Retriever 与 Context 注入策略；在此之前，现有 Agent 仍只使用 `AgentState.sources` 构造 Context 和报告。
