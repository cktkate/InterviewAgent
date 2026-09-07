# RAG 混合检索链路

## 当前流程

```text
Query
  ↓ 权限Metadata Filter（public或本人private）
  ├─ Weaviate 向量召回 ─┐
  └─ BM25 关键词召回 ──┤（并行、独立超时/降级）
                       ↓
                 加权 RRF 融合
                       ↓
        岗位/知识点/难度/题型逐级过滤补足
                       ↓
                 LLM 结构化精排
                       ↓
                 TopK 上下文与证据
```

每个知识片段根据 `source_path + chunk_index + normalized_text` 生成确定性 `chunk_id`。BM25 直接使用该 ID，Weaviate 使用由该 ID 派生的 UUID 并在属性中保留原始 `chunk_id`，因此 RRF 可以识别并累加同一片段的两路排名。

每条结果的 `retrieval` 字段保存：

- `vector_rank / vector_score`
- `bm25_rank / bm25_score`
- `rrf_rank / rrf_score / matched_branches`
- `rerank_status / rerank_rank / rerank_score`

运行诊断保存在会话状态 `rag_diagnostics`，并将两路状态、结果数和重排状态写入 Agent Runtime Trace。

## Metadata Filter

统一元数据包含：`visibility`、`owner_id`、`job_domains`、`knowledge_points`、
`difficulty`、`question_types`、`source_type`和`tech_stack`。

- 权限条件是强过滤：只允许公共数据或当前用户拥有的私有数据。
- 岗位、知识点、难度和题型是软过滤：严格命中优先，不足时逐级补入宽松结果，避免Recall骤降。
- 旧BM25缓存缺少新字段时按可信公共知识库兼容；重建索引后Weaviate会在服务端执行权限过滤。
- 过滤层级和候选数量写入`rag_diagnostics.metadata_filter`及OpenTelemetry Trace。

## 重建索引

索引结构增加了统一ID与Metadata字段。代码升级后需要重建一次索引，Weaviate服务端权限过滤和完整业务标签才能生效：

```powershell
conda activate all-in-rag
python -m cli.main build-index --kb rag/knowledge_base
```

完整构建会重建项目使用的 `InterviewQuestions` collection，并覆盖本地 `data/bm25_corpus.pkl`；不会修改知识库源文件。

## 降级策略

- 向量召回失败或超时：继续使用 BM25。
- BM25 失败或不可用：继续使用向量结果。
- Reranker 输出失败：立即退回 RRF 顺序，不进行多轮结构修复。
- 两路均无结果：返回空上下文，面试流程继续运行。

相关环境变量见 `.env.example`：检索超时、RRF 权重、是否启用重排以及重排候选倍率均可配置。
