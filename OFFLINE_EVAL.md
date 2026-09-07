# RAG 与 Agent 离线评测

本阶段不评价候选人回答与人工评分的一致性，只覆盖：

- RAG召回和排序质量
- Agent工作流轨迹、终止行为、工具状态和延迟

评测不会使用另一个LLM充当裁判。RAG使用人工相关文档标签，Agent使用已落盘的OpenTelemetry Trace回放，因此结果可重复且不会产生额外模型调用。

## 目录

```text
evals/
├── datasets/
│   ├── rag_retrieval.example.jsonl
│   ├── rag_retrieval.expanded.jsonl
│   ├── agent_flows.example.jsonl
│   └── agent_current.jsonl
├── reports/
├── common.py
├── rag_eval.py
├── agent_eval.py
├── run_offline.py
└── thresholds.example.json
```

## RAG评测

### 数据标注

每行是一个查询，可以标注精确Chunk ID、来源文件或分级相关性：

```json
{"case_id":"rag-001","query":"MCP是什么","relevant_sources":["Agent.md"],"tags":["mcp"]}
```

```json
{"case_id":"rag-002","query":"工具安全","relevant_ids":["chunk-a","chunk-b"],"relevance_grades":{"chunk-a":2,"chunk-b":1}}
```

来源级标注会对同一文件的重复Chunk去重，防止重复命中导致Recall或nDCG虚高。

正式测试推荐使用稳定的证据单元标注。它按来源和正文关键词确认相关
passage，不依赖可变的Chunk ID，重新切分或重建索引后仍可继续使用：

```json
{"case_id":"rag-agent-mcp","query":"外部工具失败后如何降级","relevant_evidence":[{"evidence_id":"tool-fallback","source":"Agent.md","all_terms":["超时熔断","失败重试 2 次"],"grade":2}],"tags":["agent","mcp","hard"]}
```

报告除总指标外还输出 `metrics_by_tag`，可按Agent/LLM、难度和技术主题定位弱项；
每个Case同时记录 `matched_units`、`missed_units` 和 `first_relevant_rank`。

### 运行

只评估混合召回，不调用LLM Rerank：

```powershell
python -m evals.run_offline rag `
  --dataset evals/datasets/rag_retrieval.expanded.jsonl `
  --mode hybrid `
  --top-k 5 `
  --output evals/reports/rag_initial.json
```

评估完整的RRF + LLM Rerank链路：

```powershell
python -m evals.run_offline rag `
  --dataset evals/datasets/rag_retrieval.example.jsonl `
  --mode full `
  --top-k 5 `
  --output evals/reports/rag_full.json
```

输出指标：

- `Precision@K`：前K个结果中的不重复相关单元比例
- `Recall@K`：标注相关单元被召回的比例
- `HitRate`：至少命中一个相关结果的查询比例
- `MRR`：首个相关结果的倒数排名
- `nDCG@K`：考虑顺序和分级相关性的排序质量
- 空结果率、检索成功率、P50/P95延迟

## Agent轨迹评测

Agent评测直接读取 `logs/agent_traces.jsonl`，不重新调用Agent。

先从一个真实Trace生成待审核模板：

```powershell
python -m evals.run_offline agent-template `
  --trace-file logs/agent_traces.jsonl `
  --trace-id <trace_id> `
  --output evals/datasets/agent_current.jsonl
```

模板中的 `expected_nodes` 来自真实轨迹，只是标注起点。必须人工检查并补充：

- 必须经过的节点
- 禁止经过的节点
- 正确终止节点
- 预期工具
- 每个节点允许访问的最大次数
- 允许的最大耗时

运行回放：

```powershell
python -m evals.run_offline agent `
  --dataset evals/datasets/agent_current.jsonl `
  --trace-file logs/agent_traces.jsonl `
  --output evals/reports/agent_initial.json
```

输出指标：

- Trace匹配率和任务成功率
- 完整路径匹配率、必经节点覆盖率
- 终止节点准确率
- 禁止节点与循环违规率
- 预期工具召回率、工具成功率
- P50/P95端到端延迟

## 回归门槛

默认门槛位于 `evals/thresholds.example.json`。命令退出码：

- `0`：全部门槛通过
- `2`：评测完成，但至少一个质量门槛失败
- `1`：数据集、Trace或运行器本身失败

将一次确认过的报告保存为Baseline，后续对比：

```powershell
python -m evals.run_offline rag `
  --dataset evals/datasets/rag_retrieval.example.jsonl `
  --baseline evals/reports/rag_baseline.json `
  --output evals/reports/rag_candidate.json
```

## Bad Case 自动沉淀

从Trace中筛选运行失败、超时、预算超限、结构化重试或协作降级的调用，生成待人工复核数据：

```powershell
python -m evals.run_offline agent-badcases `
  --trace-file logs/agent_traces.jsonl `
  --max-duration-ms 30000 `
  --output evals/datasets/agent_bad_cases.jsonl
```

生成的Case必须人工确认预期路径、终止节点和工具要求，再合并到正式回归集，避免把当前错误行为直接固化为Baseline。

门槛同时支持绝对最小/最大值，以及相对Baseline的最大下降和最大延迟增长。

## 当前初始结果

使用30条分层数据（18条Agent、12条LLM）运行真实Hybrid链路：

```text
Recall@5  = 0.9333
HitRate   = 0.9333
MRR       = 0.8611
nDCG@5    = 0.8798
P95       = 869ms
```

30条数据已经能区分不同排序指标并定位具体失败Case，但仍属于项目内初始Baseline。
对外形成稳定结论前，建议继续扩到至少50条，由人工复核每条证据，并补充口语化、缩写、错别字和跨段问题。

扩样测试还发现并修复了两个检索工程问题：无 `jieba` 环境下中文BM25整句分词失效；
两路索引Chunk ID不一致导致RRF无法合并同一段落。当前BM25带中文二元词兜底，
RRF按“来源+文本指纹”去重并保留两路原始ID。

查询术语扩展目前由 `RAG_QUERY_EXPANSION_ENABLED` 控制。实验版本必须在向量与BM25
两路均健康时通过Baseline回归门禁后才能默认开启，避免只看Recall而牺牲MRR和排序质量。

现有一个正式面试启动Trace的路径检查全部通过，但端到端耗时为65.6秒，超过30秒门槛；节点耗时显示 `plan_questions` 约51.5秒，是下一步性能优化目标。
