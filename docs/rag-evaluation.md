# RAG 人工验证与检索参数分析

## 1. 验证范围

本文首先记录第三阶段 Step 4.2 的修复前人工检索基线，再在第10节记录第一轮问题修复后的对照结果。Step 4.2 基线本身没有修改生产检索参数、Agent 决策逻辑或知识库内容，也没有引入 Reranker、自动评分器或完整 RAG Evaluation Framework。

验证日期为 2026-10-16。测试对象是本地 `.knowledge/chroma` 中已经入库的 2Cshop 资料：4 篇活跃 Document、35 个 Chunk。测试使用实际配置的 Voyage AI `voyage-4` 生成 Query Embedding，因此结果是当前知识库与当前 Embedding 配置的真实快照，不应外推为其他语料库的固定结论。

## 2. Step 4.2 基线配置（修复前）

| 项目 | 当前值 | 依据 |
|---|---|---|
| Embedding | `voyage-4` | 当前 `.env` 运行配置 |
| Chunk Size | 500 字符 | `KNOWLEDGE_CHUNK_SIZE` / `Config` |
| Chunk Overlap | 50 字符 | `KNOWLEDGE_CHUNK_OVERLAP` / `Config` |
| 默认 Top-K | 5 | `Retriever.query()`、`retrieve_knowledge` Schema |
| 距离度量 | cosine | Chroma Collection 的 `hnsw:space` |
| Similarity Threshold | 无 | 修复前查询结果未做距离阈值过滤 |
| 业务 Metadata Filter | 无 | 未按产品、来源类型或时间筛选 |
| 版本过滤 | 有 | 只返回版本清单中的活跃内容版本 |

Chroma 返回的是原始 cosine distance，数值越小表示越接近。本项目没有把 distance 转换为“相关性分数”，人工验证工具也明确使用 `distance` 名称。

## 3. 验证资产与使用方式

- `tests/data/rag_queries.json`：15 条基于当前真实资料设计的 Query，保留期望来源、主题和空白人工评价字段；
- `scripts/test_retrieval.py`：只读人工验证 CLI，展示排名、原始 distance、标题、URL、Chunk ID、Document ID、时间元数据和正文；
- `RetrievalMatch` / `query_with_distances()`：为诊断接口保留 Chroma 原始距离；原有 `query()` 仍只返回 `Chunk`，调用契约不变；
- `tests/test_retrieval_validation.py`：验证 Top-K、字段链路、排序、空结果、测试集覆盖和只读调用边界。

PowerShell 单条交互验证：

```powershell
python scripts\test_retrieval.py
```

直接传入 Query：

```powershell
python scripts\test_retrieval.py --query "2Cshop 标准版首年费用是多少？" --top-k 5
```

运行全部测试集：

```powershell
python scripts\test_retrieval.py --queries-file tests\data\rag_queries.json --top-k 5
```

Voyage 账户如果存在每分钟请求限制，可增加 Query 间隔：

```powershell
python scripts\test_retrieval.py --queries-file tests\data\rag_queries.json --top-k 5 --delay-seconds 21
```

可用 `--case-id Q01` 重复筛选个案，或用 `--json` 输出机器可读结果。脚本以 `create_if_missing=False` 打开现有 Collection，执行前后比较 Collection 数量和版本清单 SHA-256；它不调用任何写入、入库或版本切换方法。

## 4. 人工判断口径

- **Retrieval Hit**：期望来源是否出现在 Top-K；
- **Ranking Quality**：正确来源或正确 Chunk 是否足够靠前；
- **Answer Sufficiency**：候选内容是否真正包含回答所需事实，而不只是主题相近；
- **无答案行为**：知识库没有答案时，是否仍返回看似相关但无法支持答案的内容。

期望来源命中只是最小条件。来源正确但 Top 1 Chunk 没有答案时，仍不能把它记为 Top 1 的充分证据。

## 5. 15 条真实 Query 的人工结果

下表中的“正确来源排名”和“充分证据排名”由人工阅读返回正文判断；无答案 Query 不纳入 Hit@K 分母。

| ID | 类型 | Query 摘要 | 正确来源排名 | 充分证据排名 | Top 1 distance | 人工观察 |
|---|---|---|---:|---:|---:|---|
| Q01 | 精确 | 标准版首年费用 | 1 | 1 | 0.340955 | 官方价格页居首，但 Top 2～4 混入价格冲突的第三方旧文 |
| Q02 | 同义 | 搭建独立站一年多少钱 | 2 | 2 | 0.299670 | Top 1 是第三方旧价格，官方价格页在 Top 2 |
| Q03 | 属性 | 支持多少种语言 | 1 | 2 | 0.357158 | 正确文档居首，但“108 种语种”在同文档的 Top 2 Chunk |
| Q04 | 属性 | 是否抽取交易佣金 | 1 | 2 | 0.394250 | Top 1 来源正确但片段不含答案，“不抽佣金”在 Top 2 |
| Q05 | 属性 | 支付渠道与 ERP | 1 | 1 | 0.424484 | Top 1 直接列出支付与 ERP 对接，答案充分 |
| Q06 | 定位 | 适合哪些卖家 | 1 | 1 | 0.294157 | Top 1 直接描述目标卖家，答案充分 |
| Q07 | 对比 | 三种套餐差异 | 1 | 1 | 0.359710 | 官方套餐表居首；后续结果仍混入第三方旧价格 |
| Q08 | 属性 | 源码和私有部署 | 1 | 1 | 0.390208 | 官方套餐表居首并包含源码、独立部署信息 |
| Q09 | 跨表述 | 拖拽搭建多语言商城 | 1 | 2 | 0.417459 | Top 1 只有部分可视化信息，完整零代码/拖拽/多语言证据在 Top 2 |
| Q10 | 干扰 | 建站、推广和运营能力 | 2 | 4 | 0.294776 | Top 1 是价格页页脚；完整回答需要组合多个 Chunk |
| Q11 | 来源质量 | 功能页面当前内容 | 未进 Top 5 | 未进 Top 5 | 0.388686 | 已入库的目标 URL 实际是 404 页，但查询召回了其他功能介绍页 |
| Q12 | 跨表述 | Facebook、Google 与支付 | 1 | 2 | 0.316454 | 同一来源的 Top 1/2 分别覆盖支付和引流，需组合回答 |
| Q13 | 精确 | 4800/9800/29800 对应版本 | 3 | 3 | 0.486884 | Top 1/2 是冲突旧价格，含精确官方数字的页面到 Top 3 才出现 |
| Q14 | 无答案 | 原生 iOS 应用 | 不适用 | 无 | 0.522944 | 没有阈值，仍固定返回 5 条无法回答的产品介绍 Chunk |
| Q15 | 无答案 | 2027 年退款率 | 不适用 | 无 | 0.565568 | 没有该事实，仍固定返回 5 条无关 Chunk |

### 5.1 手工统计

13 条“知识库应有答案”的 Query：

| 指标 | 结果 |
|---|---:|
| Source Hit@1 | 9 / 13 = 69.2% |
| Source Hit@3 | 12 / 13 = 92.3% |
| Source Hit@5 | 12 / 13 = 92.3% |
| Answer Sufficiency@1 | 5 / 13 = 38.5% |
| Answer Sufficiency@3 | 11 / 13 = 84.6% |
| Answer Sufficiency@5 | 12 / 13 = 92.3% |

两条无答案 Query 都返回了 5 条候选，且没有任何候选足以回答问题。该结果不能写成“召回成功”，而是当前没有阈值和拒答边界的直接表现。

这些统计是人工标注汇总，不是自动 Recall、Precision、MRR 或 NDCG 评测。

## 6. 典型问题与假设

### 6.1 来源命中不等于答案充分

问题：Q03、Q04、Q09 的正确文档位于 Top 1，但真正包含答案的 Chunk 在 Top 2；Q12 需要组合 Top 1 和 Top 2。

证据：Source Hit@1 为 69.2%，而 Answer Sufficiency@1 只有 38.5%。

推测原因：当前字符窗口可能把一个完整语义单元切到相邻 Chunk；同时，同一长文的多个片段在语义空间中非常接近。该现象不能仅凭本轮结果归因于 Chunk Size。

建议：在独立 Collection 上固定同一文档与 Query，比较 500/50 基线和较大 Chunk/Overlap 候选，并单独记录答案充分性。本次不修改。

### 6.2 来源冲突影响排序

问题：Q02 和 Q13 的 Top 1 都是第三方旧价格，而当前官方价格页分别排在 Top 2 和 Top 3。

证据：Q13 明确查询官方页面中的三个数字，旧价格文章仍以更小 distance 排在前面。

推测原因：Embedding 更偏向“价格计划”整体语义，没有来源权威性、内容时间或冲突信息作为排序信号；这不是简单提高 Top-K 就能解决的问题。

建议：后续实验增加来源类型、域名、抓取时间等人工标签，比较仅向量排序与可解释的 Metadata 过滤/加权策略。未来可评估 Reranker，但本 Step 不实现。

### 6.3 无答案 Query 被强制返回结果

问题：Q14、Q15 都没有知识答案，但仍返回 5 条内容。

证据：两条 Query 的 Top 1 distance 分别为 0.522944 和 0.565568，正文不能回答问题。

推测原因：Retriever 当前没有距离阈值，Chroma 在 Collection 非空时总会返回最近邻。Query 语义也可能与产品介绍产生弱匹配。

建议：使用人工标签对距离阈值做离线扫描；阈值不能直接设为 0.50，因为 Q13 的正确官方 Chunk distance 约为 0.5353，会被一并过滤。本次不修改。

### 6.4 数据质量不能由检索参数补救

问题：活跃知识中包含一个正文为 404 的页面；Q11 也无法在 Top 5 找回该目标页。

证据：当前准入主要验证可用全文和长度，没有把 HTTP 页面语义上的“404/找不到页面”识别为无效内容。

推测原因：这是入库质量与页面有效性问题，不应仅通过 Top-K、阈值或 Chunk 参数修复。

建议：后续单独设计准入规则实验，识别错误页标题/正文和 HTTP 最终状态。本 Step 不修改入库策略。

## 7. 参数实验建议

| 参数 | 当前值 | 是否建议测试 | 建议候选值/方案 | 原因 | 本次是否修改 |
|---|---:|---|---|---|---|
| Top-K | 5 | 是 | 3 / 5 / 8 | 本轮 Hit@3 与 Hit@5 相同，但 Q10 的充分证据到 Rank 4；需要权衡覆盖、噪声和 Context 成本 | 否，等待用户审查 |
| Similarity Threshold | 无 | 是 | 保持关闭作为基线，对 cosine distance 上限 0.50 / 0.55 做离线扫描 | 无答案 Query 强制返回；但单阈值会误删 Q13 的低排名正确证据 | 否，等待用户审查 |
| Chunk Size | 500 | 是 | 500 / 750 / 1000，分别重建实验 Collection | 多个 Query 的答案落在相邻 Chunk；需验证更完整片段是否改善充分性而不稀释语义 | 否，等待用户审查 |
| Chunk Overlap | 50 | 是，低优先级 | 50 / 100，与 Chunk Size 联合对照 | 当前没有足够证据证明边界丢失，只能作为联合实验因素 | 否，等待用户审查 |
| Distance Metric | cosine | 否 | 保持 cosine | 当前失败更明显地指向排序信号、阈值和数据质量，暂无切换度量的证据 | 否 |
| Embedding Model | `voyage-4` | 否 | 保持当前模型 | 本轮先建立基线；更换模型会同时改变向量空间，难以隔离变量 | 否 |
| 业务 Metadata Filter | 无 | 是 | 实验性增加 `source_type`、官方域名、`retrieved_at` 标签；先人工标注再比较 | 价格来源冲突和时效差异无法由纯语义距离表达 | 否，等待用户审查 |

所有候选实验都应使用独立 Collection，固定同一 15 条 Query，并同时比较 Source Hit、Ranking Quality、Answer Sufficiency、无答案误返回数量和距离分布。一次只改变一个主要变量；在用户审查前不进入 Step 4.3，也不修改生产默认值。

## 8. 当前结论

当前纯 Embedding + Chroma 基线可以较稳定地在 Top 3 召回已知来源，但 Top 1 并不可靠，尤其在完整答案跨 Chunk、官方与第三方内容冲突、以及宽泛 Query 的场景。无答案 Query 缺少拒绝边界，知识入库中的 404 页面也说明检索质量不能脱离语料质量评估。

本 Step 已建立可重复、可观察、只读的人工实验方法；它的作用是暴露问题，而不是通过未经审查的参数调整让结果看起来更好。

## 9. 回归验证

2026-10-16 执行：

```powershell
python -m unittest discover -s tests -v
python main.py --demo --output-dir $env:TEMP\competitor-agent-step42-demo
```

结果为 48 个单元/集成测试全部通过，离线 Demo 在 6 轮内完成并生成报告。Demo 使用临时目录，未改动用户工作区中原有的 `artifacts/demo` 状态。

## 10. 第一轮问题修复后的对照

### 10.1 Knowledge Quality Gate

新链路为：

```text
read_web Source（保存 HTTP status）
    → Document.from_source
    → KnowledgeEvaluator.evaluate_document
    → accepted 才进入 Hash / Chunk / Embedding / Chroma
```

对历史 `artifacts/knowledge-task-a/state.json` 做只读复评：S1、S12仍为 `valid_full_text`；此前入库的S11被识别为 `not_found_page`。S11正文含“404错误，找不到您想要的页面”“返回首页”等组合信号，而正常的HTTP 404教程测试仍能通过。新门禁不会自动删除修复前已写入的历史向量。

### 10.2 临时阈值0.50

当前 Chroma 使用原始 cosine distance，越小越相似；候选条件为 `distance <= 0.50`。这是配置项 `RAG_RELEVANCE_THRESHOLD` 的临时候选值，可以用 `none` 关闭。

2026-10-16 对同一真实 Collection 复测：

| Query | Raw / Accepted / Rejected | 结果 |
|---|---:|---|
| Q01 标准版首年费用 | 5 / 5 / 0 | 官方价格页distance=0.340955，正常保留 |
| Q13 三个精确价格 | 5 / 1 / 4 | 第三方旧文0.486884通过；正确官方页0.535300被拒绝 |
| Q14 原生iOS应用（无答案） | 5 / 0 / 5 | 返回 `no_relevant_knowledge`，Top1 distance=0.522944 |

因此0.50能够处理已测Q14，但有效与无答案distance存在重叠，且Q13证明它会误删正确证据。当前数据不足以确定最终阈值；0.50不能被解释成“50%相关”，也不能解决来源冲突、正确Chunk排名靠后或多Chunk组合问题。

### 10.3 Tool无答案状态

所有候选被拒绝时，`retrieve_knowledge` 成功执行但返回：

```json
{
  "success": true,
  "tool": "retrieve_knowledge",
  "status": "no_relevant_knowledge",
  "reason": "all_candidates_exceed_relevance_threshold",
  "distance_metric": "cosine",
  "relevance_threshold": 0.5,
  "raw_candidate_count": 5,
  "result_count": 0,
  "rejected_count": 5,
  "results": []
}
```

该Observation由LLM正常读取，Runtime不会据此自动调用Web Search。

### 10.4 回归结果

- `python -m unittest discover -s tests -v`：63/63通过；
- `python -m compileall .`：通过；
- `python -m pip check`：`No broken requirements found`；
- 离线Demo：6轮正常完成并生成报告，输出位于系统临时目录，未改动工作区的`artifacts/demo`。
