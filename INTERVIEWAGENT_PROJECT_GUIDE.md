# InterviewAgent 项目总结与面试复习手册

> 适用岗位：AI Agent 开发工程师、AI 应用工程师、RAG 应用工程师。  
> 本文以当前代码实现为准，区分“已实现能力”“实验指标”和“生产化改进”，避免面试时过度承诺。

## 1. 项目定位

InterviewAgent 是一个面向技术岗位的 AI 模拟面试系统。系统根据岗位 JD、候选人简历及历史能力画像，动态完成知识检索、题目规划、提问追问、回答评估、报告生成和专项训练，形成“正式面试—发现薄弱点—专项练习—能力画像回流”的训练闭环。

它不是一次 LLM 调用，也不是固定题库播放器，而是由确定性工作流控制、多 Agent 承担语义任务、RAG 提供知识证据、记忆系统保存能力状态、工具运行时接入外部能力的有状态 AI 应用。

### 1.1 核心技术栈

| 领域 | 技术 |
| --- | --- |
| Agent 编排 | LangGraph、共享 State、条件路由、Coordinator、Reviewer |
| Agent Runtime | 执行预算、Checkpoint、OpenTelemetry Trace、Bad Case 回归 |
| 大模型 | 通义千问 `qwen-max`，DashScope OpenAI 兼容接口 |
| RAG | Weaviate、BM25、加权 RRF、Metadata Filter、LLM Rerank |
| Embedding | DashScope `text-embedding-v3` |
| 记忆与数据 | Redis、MySQL、SQLAlchemy、可配置 SQLite/MySQL Checkpointer |
| 实时交互 | FastAPI、WebSocket、Whisper STT、DashScope TTS |
| 异步任务 | Celery、Redis Broker/Backend/PubSub、MySQL任务状态表 |
| 工具调用 | Tool Runtime、GitHub/Web 工具、动态 MCP stdio 接入 |
| 评测观测 | OpenTelemetry、JSONL Trace、离线 RAG/Agent 轨迹评测 |

## 2. 简历版本

### InterviewAgent：AI 智能模拟面试系统（独立开发）

**项目描述：** 面向传统模拟面试题目固定、缺乏动态追问与持续反馈的问题，设计 AI 智能模拟面试系统，根据 JD、简历及历史表现动态出题和追问，自动完成能力评估、薄弱点分析及个性化学习建议，形成面试与学习闭环。

- **Agent 协作与 Harness：** 基于 LangGraph 编排岗位分析、检索、规划与评估 Agent，以 State、任务板和 Artifact 管理协作；实现受约束的 Plan-Tool-Review-Replan 闭环，由 Planner 按证据缺口选择权限受控的 RAG 工具、Reviewer 拒绝后限次重规划；Harness 统一控制预算、共享 Checkpoint、Trace 与输入信任边界，支持断点恢复和安全 Bad Case 回归。
- **自适应决策：** 解耦 LLM 内容生成、结构化评分与确定性策略，基于岗位权重、知识缺口及能力置信度动态选题；通过 Context Budget 筛选画像与 RAG 证据，结合锚点题和 IRT/Elo 风格校准，模拟评测中技术点覆盖率提升 31%、深挖率提升 42%，评审一致率达 90%。
- **混合检索：** 基于 Weaviate 与 BM25 并行召回，通过 Metadata Filter、加权 RRF 与内容指纹去重完成融合，并依据双路一致性和延迟预算动态启用 LLM Rerank；30 条离线集 Recall@5 达 96.67%、MRR 达 85.11%，P95 为 9.43 秒。
- **应用工程化：** 使用 MySQL 沉淀面试、练习能力证据与异步任务状态，打通训练画像回流，模拟训练中薄弱知识点得分提升 35%；基于 Celery、Redis Pub/Sub 与 WebSocket 构建幂等可重试的跨进程报告链路，保留 Broker 故障时的同步降级；通过 liveness/readiness/startup 探针与 Prometheus 指标监控报告任务、LLM Token 和工具调用，并以 SLO/告警规则约束可用性与延迟。

### 2.1 指标口径

- RAG 指标来自 30 条自建、人工证据标注的离线集。旧全量 Rerank 为 `Recall@5 96.67%/MRR 86.67%/P95 14.93秒`；当前自适应策略为 `Recall@5 96.67%/MRR 85.11%/P95 9.43秒`，通过回归门禁。两次云端网络状态并不完全相同，因此这里只证明当前版本满足门禁，不能把延迟差全部归因于策略。
- `31%/42%/90%/35%` 属于自建模拟对照结果，不是线上生产指标；面试时必须说明数据来源、Baseline 和样本范围。
- 当前仓库尚未为上述四个提升指标保留与 RAG 报告同等完整的正式报告，后续应补齐原始样本、计算脚本和报告文件。

## 3. 一分钟项目介绍

> InterviewAgent 是我独立开发的 AI 智能模拟面试系统。用户输入 JD 和简历后，系统通过 LangGraph 编排岗位分析、RAG 检索、题目规划、面试评估和学习规划等 Agent。流程层由共享 State 和确定性条件路由控制，LLM 主要负责内容生成和结构化语义评分，避免让模型直接控制业务流程。面试中系统结合岗位权重、知识缺口和能力置信度动态选题，并用锚点题与 IRT/Elo 风格残差更新缓解动态难度造成的评分偏差。知识侧采用 Weaviate 与 BM25 双路召回，经 Metadata Filter、RRF、指纹去重和 LLM Rerank 精排。工程上使用 Redis 保存热会话、MySQL沉淀跨会话能力证据；LangGraph Checkpoint 本地使用 SQLite、部署时可切换共享 MySQL 后端，工具限流、熔断和幂等状态也可切换 Redis；通过 OpenTelemetry Trace、离线评测和 Bad Case 回归定位流程问题，交互侧使用 FastAPI、WebSocket、Whisper STT 和 TTS 支持实时语音面试。

## 4. 总体架构与完整链路

```text
浏览器 / CLI
  │ HTTP、WebSocket、音频二进制帧
  ▼
FastAPI 交互层
  │ JWT鉴权、会话归属校验、协议事件
  ▼
Agent Runtime Harness
  │ run_id、thread_id、预算、Checkpoint、Trace
  ▼
LangGraph 工作流
  ├─ route / load_memory
  ├─ jd_analyze / resume_analyze
  ├─ rag_retrieve / plan_questions
  ├─ ask / evaluate_one / follow-up
  └─ generate_report / study_plan / save_memory
  │
  ├─ LLM：qwen-max
  ├─ RAG：Weaviate + BM25 + RRF + Rerank
  ├─ 热状态：Redis
  ├─ 业务证据：MySQL
  ├─ 图状态：SQLite（本地）/ MySQL（多实例）Checkpointer
  └─ 工具：GitHub / Web / 动态 MCP
```

一场正式面试的典型路径：

```text
开始面试
  → 加载短期会话与长期画像
  → 解析JD和简历
  → 检索岗位相关知识
  → Planner生成候选题并按证据缺口请求只读检索工具
  → Reviewer审核；拒绝则带原因限次重规划
  → 重试耗尽则使用确定性恢复题单
  → 选择首题
  → 用户回答
  → 结构化评分
  → 更新能力状态
  → 判断澄清/深挖/挑战/下一题
  → 结束面试
  → 生成报告和学习计划
  → 保存能力证据并加入练习队列
```

## 5. Agent 编排与协作

### 5.1 为什么使用 LangGraph

模拟面试是多轮、有状态、带循环和条件分支的业务流程。LangGraph 用节点表示任务，用边表示确定性流转，用 State 保存跨节点数据，并通过 Checkpointer 记录执行位置。相比把所有逻辑写在一个 Prompt 或大量 `if-else` 中，它更便于检查路径、恢复状态和插入新节点。

### 5.2 多 Agent 的职责边界

| Agent | 主要职责 | 典型产物 |
| --- | --- | --- |
| Intent Router | 识别开始面试、回答、聊天、Skill 等意图 | `intent` |
| JD Analyzer | 提取岗位、技术栈、能力权重 | `jd_analysis` |
| Resume Analyzer | 提取经历、优势和能力缺口 | `resume_analysis` |
| Question Planner | 结合 JD、画像和 RAG 生成候选题 | `candidate_pool` |
| Interviewer | 根据策略生成首题或追问文本 | `current_question` |
| Evaluator | 输出正确性、深度、结构、案例等维度评分 | `score_record` |
| Study Planner | 根据薄弱点生成学习计划和资源 | `study_plan` |

拆分的依据不是“为了多 Agent 而多 Agent”，而是不同阶段具有不同 Prompt、输出 Schema、工具权限和失败处理方式。纯确定性计算仍由普通 Python 代码完成。

### 5.3 State、任务板、Artifact、Reviewer、Coordinator 的层级

```text
LangGraph State：整个工作流共享的业务状态
  └─ Collaboration：协作控制区
      ├─ Task Board：任务、依赖、执行状态
      ├─ Artifact：Agent提交的结构化产物与元信息
      ├─ Reviewer：规则审核产物
      └─ Coordinator：汇总审核结果并记录采纳/降级决策
```

- **State** 是全流程数据载体，包含 JD、题目、问答、评分、运行预算和协作信息。
- **任务板** 描述任务是否可执行、依赖谁、是否完成，解决协作过程不可见的问题。
- **Artifact** 是 Agent 对某项任务的交付封装，包含业务结果及来源、状态、审核信息；它不是 State 的替代品。
- **Reviewer** 通过确定性规则检查缺字段、重复题、岗位相关性、证据绑定等问题，并输出 `accepted/revise/accepted_with_warnings`。
- **Coordinator** 汇总任务与审核状态，只有审核通过或确定性恢复后才记录采纳决策。

当前真实边界：LangGraph 条件边仍然是跨阶段调度器；Coordinator 管理题目规划阶段的任务依赖、审核采纳和降级，Reviewer 的 `revise` 会真实触发限次重规划，但 Coordinator 没有全面接管整张图。因此准确说法是“LangGraph 编排 + Coordinator 协作控制”。

### 5.4 Reviewer 当前规则

- 题目 Prompt 或知识点为空：拒绝。
- 与已有题目相似度超过约 `0.92`：判定重复。
- 根据岗位关键词检查相关性；锚点题可以作为统一基准保留。
- `expected_points` 缺失：记录警告。
- 题目声称使用 RAG 证据但无法绑定来源：拒绝或降级。
- 审核后候选池不足或弱岗位相关题过多：返回结构化原因触发重规划。
- 重规划次数耗尽：切换固定锚点和确定性恢复题单，避免循环或服务中断。

规则审核稳定、便宜、可解释，但只能检查结构与显式规则，不能完全判断题目语义质量；后续可以增加独立模型审核与人工抽检。

## 6. Agent Harness

Harness 是包裹 LangGraph 的运行控制层，不是另一个 Agent，也不等于任务板。它统一一轮 Agent Run 的身份、资源、持久化和观测边界。

### 6.1 当前能力

- 将稳定的 `session_id` 映射为 LangGraph `thread_id`。
- 为每次运行生成 `run_id/trace_id`。
- 最大图步骤：30。
- 最大 LLM 调用：12。
- 最大工具调用：6。
- 最大总 Token：30000。
- Agent 总 Deadline：120 秒。
- 单次 LLM 超时：45 秒。
- 动态上下文默认上限为1800 Token，按分区配额和价值密度选择，并记录来源、信任级别、压缩及淘汰清单。
- 统一调用 Checkpointer 和 OpenTelemetry Trace。
- 提供同步执行与 `astream` 节点事件输出。

### 6.2 Harness 与任务板的区别

| 组件 | 回答的问题 |
| --- | --- |
| Harness | 这次 Run 能用多少资源、如何恢复、如何追踪、失败如何退出？ |
| 任务板 | 当前有哪些协作任务、依赖关系和交付状态？ |
| Trace | 实际执行了什么、每一步耗时和状态怎样？ |

### 6.3 当前局限

- Token 通常在模型返回后才能准确统计，不能真正中途截断生成。
- Deadline 主要在调用前检查，不等于硬终止正在执行的同步请求。
- 当前预算是单 Run、单进程上下文，不是跨实例的租户总预算。
- 还没有完整 Sandbox、Human-in-the-loop 审批和租户配额系统。

## 7. Checkpoint、Trace 与 Bad Case

### 7.1 Checkpoint

Checkpoint 保存 LangGraph State 和执行位置，用于服务重启或连接断开后恢复流程。本地默认使用：

```text
data/langgraph_checkpoints.sqlite3
```

加载顺序为：

```text
Redis热会话
  → 未命中时读取LangGraph Checkpoint（本地SQLite/部署MySQL）
  → 可选读取旧MySQL会话快照
  → 恢复后重新写热Redis
```

### 7.2 Trace

Trace 记录一次 Run 的真实执行过程。一个 Trace 包含多个 Span：

- `trace_id`：整条请求链路的共同标识。
- `span_id`：某一个操作的标识。
- `parent_span_id`：当前操作的父操作，用于重建调用树。
- Run Span：整次 Agent 调用。
- Node Span：每个 LangGraph 节点。
- Tool Span：每次工具调用。

本地 Trace 落盘在：

```text
logs/agent_traces.jsonl
```

如配置 OTLP Endpoint，也可以发送给兼容 OpenTelemetry 的观测后端。

### 7.3 两者区别

| Checkpoint | Trace |
| --- | --- |
| 保存 State 和执行边界 | 保存执行过程和耗时 |
| 用于恢复 | 用于诊断、审计和评测 |
| 回答“从哪里继续” | 回答“为什么失败/变慢” |

### 7.4 Bad Case 回归

系统从 Trace 中筛选运行失败、延迟超过30秒、预算超限、结构化输出重试、协作降级和子 Span 失败等记录，生成待人工审核的 Case。审核者补充：

- 必经节点与禁止节点。
- 正确终态。
- 预期工具。
- 节点最大访问次数。
- LLM、Token 和延迟预算。

后续修改 Prompt 或工作流后再次运行评测，若路径、终态、工具或预算违反规则，就由质量门禁阻止回归。当前是基于已落盘 Trace 的确定性轨迹校验，不是完整重放外部 LLM 和工具。

## 8. 自适应选题与评分校准

### 8.1 为什么解耦 LLM 与确定性策略

LLM 擅长生成题目、理解回答和输出语义评分，但不适合直接控制题目数量、追问次数、分数计算和结束条件。项目采用：

```text
LLM：生成候选题、生成追问文本、输出结构化维度评分
Python策略：选哪道题、追问类型、难度变化、能力更新、是否结束
```

这样可以减少随机性，使业务规则可测试、可回放、可解释。

### 8.2 候选题选择

系统不会提前固定完整题单顺序，而是维护候选池。每答完一题，根据最新能力状态重新计算剩余题目优先级：

```text
priority =
  0.25 × 岗位权重
+ 0.20 × 知识缺口
+ 0.15 × 能力不确定性
+ 0.15 × 覆盖缺口
+ 0.15 × 题目区分度
- 0.05 × 难度不匹配
- 0.05 × 重复度
```

- `mastery`：当前对知识点的掌握程度。
- `confidence`：系统对该判断的置信程度。
- `attempts`：该知识点已有多少条证据。
- 不确定性高意味着信息不足，适合继续提问以缩小估计误差。

### 8.3 三类追问

- `clarify`：回答含糊或基础概念不清时要求澄清。
- `deepen`：基础正确但缺少机制、边界或实现细节时继续深挖。
- `challenge`：回答较好时增加并发、异常、规模或反例约束。

大致规则：无需追问则结束；正确性低于5分倾向澄清；正确性不低于8且深度不低于7倾向挑战；其余倾向深挖。追问总数有限制，避免无限循环。

### 8.4 锚点题

锚点题是固定中等难度、评分标准相对稳定的题目，用于不同用户或不同场次间提供共同参照。动态选题会让能力强的人遇到更难的问题，如果直接平均原始分，强者可能反而更低；锚点题可以缓解这种不可比性。

当前最多加入两道确定性中等难度锚点题。它们是工程校准手段，不是经过大规模参数标定的标准心理测量题库。

### 8.5 IRT/Elo 风格更新

项目先根据当前能力 `ability` 和题目难度 `difficulty` 计算预期表现：

```text
expected = sigmoid(4 × (ability - difficulty))
residual = observation - expected
new_ability = ability + learning_rate × residual × evidence_weight
```

实际表现高于预期，能力上调；低于预期，能力下调。因此“难题得7分”和“简单题得7分”不会贡献完全相同的能力证据。

它是 IRT/Elo 思想的工程化简化，不是标准 IRT 论文复现。标准方案需要大规模重叠作答数据、题目参数标定、模型拟合、DIF/拟合检验和能力估计标准误差。

### 8.6 最终得分

当前思路为：

```text
锚点题表现 40%
+ 难度校准能力 35%
+ 岗位匹配 25%
+ 仅正向的挑战题加分
```

失败的挑战题证据权重较低，避免用户因系统主动提高难度而被明显惩罚。

## 9. Context Budget

长期训练后，能力画像、历史问答和 RAG 文档会持续增加，全部塞入 Prompt 会造成：

- 超过模型上下文上限。
- Token成本和延迟持续增加。
- 无关信息稀释当前任务。
- 历史噪声干扰选题。

当前题目规划默认使用1800 Token预算，并划分以下上下文分区：

1. 能力画像20%，优先保留低掌握度、低置信度的知识点。
2. 长期薄弱点10%，优先保留与当前岗位和主题相关的内容。
3. 最近问答25%，综合时间衰减与主题相关性排序。
4. RAG证据45%，综合召回排名、双路命中和词项重合度排序。

各分区先获得保底预算，未使用的Token按优先级回流其他分区；分区内部按“价值分/Token成本平方根”选择。单项过长时先做与当前主题相关的抽取式压缩，仍超预算才淘汰，不额外调用LLM。

系统同时生成 Context Manifest，记录每项原始Token、入选Token、价值密度、压缩/淘汰动作和信任级别。只有Manifest中实际进入Prompt的RAG来源才能绑定到题目，避免“未读证据”被错误标记为引用；相关指标同步进入Trace和Agent离线评测。

当前使用`tiktoken/cl100k_base`做一致的本地预算计数，它不是Qwen服务端Tokenizer的绝对精确复现，因此真实请求Token仍以模型API返回的usage为准；部署时可通过配置替换计数编码。

## 10. Hybrid RAG 检索链路

### 10.1 数据来源与索引

当前核心知识文档：

```text
rag/knowledge_base/knowledge/Agent.md
rag/knowledge_base/knowledge/LLM.md
```

文档经过结构化切分，Chunk大小约512、重叠64，当前约35个Chunk。BM25语料序列化到：

```text
data/bm25_corpus.pkl
```

向量索引存储在 Weaviate；实际Embedding默认使用DashScope `text-embedding-v3`。项目目录中下载的BGE-M3不会自动被当前RAG代码使用。

### 10.2 完整链路

```text
用户问题
  ├─ Weaviate向量召回：处理语义近似
  └─ BM25关键词召回：处理术语、缩写和精确词
       ↓
内容指纹去重
       ↓
加权RRF融合（向量0.6，BM25 0.4，k=60）
       ↓
Metadata Filter
       ↓
LLM Rerank
       ↓
Top K证据进入题目规划上下文
```

两路检索通过线程池并行执行，单路超时约8秒；其中一路失败时保留另一条链路。

### 10.3 为什么使用内容指纹

同一段文本在Weaviate和BM25中可能因为索引方式不同拥有不同ID。如果直接按ID融合，系统会把同一内容当成两条证据，既占据Top K，又影响RRF得分。

内容指纹使用“来源文件名+规范化正文”的哈希识别同一语义段落，同时保留两路原始ID用于追踪。长期方案是在入库阶段统一确定性Chunk ID，指纹仍可防御历史索引和外部数据不一致。

### 10.4 Metadata Filter

过滤分两类：

- **硬安全过滤：** 只允许公共文档，或 `owner_id` 等于当前用户的私有文档；该条件不能降级放宽。
- **软业务过滤：** 根据岗位领域、知识点、难度和文档类型筛选；结果不足时可从严格条件逐步放宽到主题条件，再放宽到仅安全条件。

当前Weaviate服务端主要执行可见性/所有者过滤，部分软过滤在召回后完成。用户登录并不自动保证向量库隔离，必须把JWT中的 `user_id` 传入检索过滤条件。

### 10.5 RRF与Rerank

RRF只利用多个召回列表中的排名，不要求向量相似度和BM25分数处在相同量纲：

```text
score(d) = Σ weight_i / (k + rank_i(d))
```

RRF擅长稳定融合，但不深入理解题目与段落的逻辑关系；LLM Rerank读取查询和候选片段，重新输出最相关顺序。完整评测时通常先召回约30条、过滤到最多15条进入Rerank，最终返回Top 5；正式面试可返回Top 10上下文。

Rerank失败时回退RRF顺序。当前默认采用自适应策略：根据向量/BM25分支健康度、Top结果双路命中、排名一致性、双路覆盖率和Top间隔估算融合置信度；置信度不低于0.90时直接返回RRF结果，低置信且前置检索未超过3秒预算时调用LLM Rerank，预算耗尽则降级直出，避免检索超时与模型延迟叠加。

### 10.6 离线结果

30条人工证据标注数据集的当前自适应链路结果：

| 指标 | 结果 | 含义 |
| --- | ---: | --- |
| Recall@5 | 96.67% | 标注证据进入Top 5的比例 |
| MRR | 85.11% | 第一个正确证据排名的倒数均值 |
| nDCG@5 | 88.04% | 考虑相关等级和顺序的综合质量 |
| P95 | 9.426秒 | 95%的完整检索请求在该时间内完成 |
| 高置信跳过率 | 53.33% | 双路结果高度一致，跳过LLM精排 |
| 延迟预算降级率 | 40.00% | 前置检索超过预算，避免继续叠加LLM耗时 |
| LLM Rerank执行率 | 6.67% | 低置信且仍有延迟预算的请求 |

旧Hybrid链路P95约869ms但不包含LLM Rerank；旧全量Rerank链路P95约14.925秒。当前报告见 `evals/reports/rag_adaptive_rerank_final.json`。

## 11. 分层记忆与训练闭环

### 11.1 Redis短期记忆

保存当前会话快照和最近对话：

```text
iam:short:{session_id}:snapshot
iam:short:{session_id}:turns
```

- 最近最多20轮。
- 默认TTL 24小时。
- 连接失败后指数退避重连，间隔1秒增长到30秒。
- Redis暂时不可用时退回进程内存；连接恢复后尝试回填。

Redis解决高频读写和会话响应速度，不作为永久业务事实的唯一来源。

### 11.2 MySQL长期数据

主要保存：

- 用户和求职目标。
- 正式面试记录与报告。
- `ability_evidence` 追加式能力证据。
- `knowledge_mastery` 聚合能力画像。
- 专项练习题、调度和作答记录。

能力证据采用确定性事件ID防重复，并在一个事务内完成“追加事件+更新聚合画像”。数据库失败时当前会话仍可继续，但长期画像可能暂时未更新。

### 11.3 为什么还要LangGraph Checkpoint

- Redis保存热会话，强调快速访问和短期存活。
- MySQL保存业务事实和跨会话画像。
- Checkpointer保存LangGraph节点级State和执行位置；本地使用SQLite，多实例部署可切换共享MySQL。

三者数据生命周期和恢复语义不同。MySQL业务表不能自动告诉LangGraph从哪个节点继续；Checkpoint也不适合代替可查询、可统计的业务数据。

### 11.4 正式面试与专项练习如何打通

```text
正式面试答错主问题
  → 生成能力证据
  → 更新正式面试mastery
  → 题干指纹去重后加入私有练习队列
  → 用户查看答案并自评：正确/半对半错/不会
  → 按自评结果安排下次复习
  → 写入practice能力证据
  → 回流综合能力画像
```

练习不调用LLM评分，用户先口述、再查看标准答案并自评。调度间隔大致为：正确后1/3/7/14/30/60天，半对半错约1天，不会约10分钟。

练习证据权重低于正式面试，并将练习占比上限限制在25%，避免自评数据覆盖正式面试证据。

当前需要注意：综合mastery会参与后续选题，也可能间接影响后续正式评分。更严格的方案应让综合画像只用于选题，最终正式报告只使用正式面试能力证据。

## 12. WebSocket、STT与TTS

### 12.1 WebSocket协议

连接地址：

```text
/ws/{session_id}?token=<JWT>
```

服务端先校验JWT，再检查当前用户是否拥有该面试会话。协议同时使用：

- JSON控制帧：开始、提交回答、进度、文本、错误、报告状态。
- 二进制帧：用户录音和TTS音频。

### 12.2 当前“混合流式”

项目存在三种流：

1. LangGraph `astream(stream_mode="updates")` 推送节点完成后的处理阶段。
2. 最终文本通过校验后按约16字符分块推送。
3. TTS音频通过二进制帧推送。

这里不是LLM Token原生流式。文本必须先完整生成并通过结构化校验，再分块展示，因此改善的是交互感受，不会显著降低首Token等待时间。前端显示的是安全的“正在分析JD/检索知识/规划题目”等阶段，不展示模型内部推理。

### 12.3 STT链路

```text
浏览器MediaRecorder录音
  → 生成WebM/Opus等Blob
  → ArrayBuffer
  → WebSocket二进制帧
  → 后端解码、单声道16kHz重采样
  → faster-whisper base / CPU / int8
  → 文本回填输入框
```

当前是整段录音停止后上传，不是实时流式ASR。后端使用线程池避免CPU推理阻塞事件循环，并通过进程内Semaphore限制最多2个并发STT任务。

### 12.4 当前不足

- 没有真正的LLM Token流。
- STT不是边说边识别。
- TTS上游有时仍返回一个完整音频块。
- 图事件桥接队列没有完整背压机制。
- WebSocket缺少完善的序号、ACK、断线续传和同会话互斥。
- Token放在URL参数中存在日志泄露风险，生产应改为短期票据或安全Cookie。

### 12.5 跨进程报告任务

最终报告不依赖WebSocket连接持续存在：Web进程先按会话生成稳定任务ID和幂等键，在MySQL `background_tasks`表落下任务状态，再将任务发布给Celery Worker。Worker分阶段生成报告、学习计划并回写能力画像，同时把进度发布到Redis Pub/Sub；任意FastAPI实例均可订阅事件并转发给当前WebSocket连接。

- Celery启用`acks_late`、Worker丢失时拒绝确认、单Worker预取1个任务，并设置软/硬超时。
- 瞬时失败执行指数退避重试；Broker发布失败时同步降级，保留原功能。
- HTTP/WebSocket进程通过W3C Trace Context将父Trace传入Celery消息，Worker Span可与原请求串成同一条链路。
- Pub/Sub不保存历史事件，因此前端保留低频状态轮询；断线重连后从MySQL任务表和会话状态恢复最终进度。
- 配置`TASK_EVENT_TRANSPORT=streams`可在支持Redis Streams的环境切换为可回放事件流；当前本地Redis不支持`XADD`，默认使用Pub/Sub。

## 13. Tool Runtime与MCP

### 13.1 MCP与Tool Runtime的区别

- MCP解决“工具如何被发现、描述和调用”的协议标准化问题。
- Tool Runtime解决“调用前后如何校验、授权、限流、重试、熔断、幂等和追踪”的执行可靠性问题。

### 13.2 当前真实接入

已注册本地工具适配器：

- `github.search_learning_repos`：为学习计划推荐GitHub资源。
- `github.analyze_repo`：仓库分析能力，未进入主要业务链路。
- `web.fetch_public_page`：抓取公共JD网页。
- `rag.retrieve_interview_evidence`：由规划Agent发现证据缺口时提出，Harness注入用户隔离条件后执行只读补充检索。

动态MCP客户端支持通过stdio连接Server、`list_tools`和`call_tool`。当前没有在主业务图中自动连接一个生产MCP Server，HTTP/Streamable HTTP连接也尚未实现。因此准确表述是“封装GitHub/Web工具，并支持动态MCP stdio工具接入”。

### 13.3 Runtime控制

- Registry白名单和工具风险等级。
- 内置工具使用Pydantic严格参数Schema。
- 动态MCP工具解析常见JSON Schema字段。
- 权限分为外部读写等类别；高风险写操作需要显式确认。
- 默认工具超时约12秒，瞬时故障有限次数重试。
- GitHub约30次/分钟、Web约20次/分钟的滑动窗口限流；本地使用进程内状态，部署时可切换Redis原子计数。
- 连续失败达到阈值后熔断约30秒。
- Tool Span记录工具名、调用ID、输入哈希、用户、状态、重试次数和耗时，不记录原始敏感参数。
- Web抓取限制协议、拒绝私网/回环地址、重新校验重定向、限制内容类型和大小，降低SSRF风险。

### 13.4 当前不足

- 只有启用`TOOL_RUNTIME_STATE_BACKEND=redis`后，限流、熔断与幂等状态才会跨实例共享。
- 同步超时无法真正杀死已经运行的底层线程。
- 同步和异步执行接口都支持幂等键，但仍需确保每条业务工具链传入稳定请求ID。
- 当前在线工具均为只读，高风险工具确认能力主要由测试验证。

## 14. 离线评测体系

### 14.1 RAG评测

每条样本包含问题、来源文件、稳定证据单元、关键术语、相关等级和标签。评测真实检索链路，计算：

- Precision@K、Recall@K、HitRate。
- MRR、nDCG@K。
- 空结果率、检索成功率。
- P50/P95延迟。
- 按Agent、LLM、难度、主题切分的分组指标。

稳定证据采用“来源+正文术语”匹配，而不是只依赖可能因重切分而变化的Chunk ID。

### 14.2 Agent轨迹评测

读取 `logs/agent_traces.jsonl`，不重新调用LLM，确定性检查：

- Trace是否存在、Run是否成功。
- 精确路径、必经节点覆盖率、禁止节点违规率。
- 正确终态、循环违规。
- 预期工具召回和工具成功率。
- LLM调用、Token和预算违规。
- P50/P95端到端延迟。

当前正式Agent轨迹集只有1条，路径检查通过，但P95约65.6秒，超过30秒全局门禁；其余6条自动Bad Case仍需人工审核。因此不能在简历中宣称Agent轨迹评测已达到成熟的100%成功率。

### 14.3 回归门禁

RAG门禁示例：Recall不低于0.8、MRR不低于0.6、nDCG不低于0.65、P95不高于15秒，同时限制相对Baseline的质量下降和延迟增长。

Agent门禁示例：任务成功率不低于95%、必经节点和终态100%、禁止节点/循环/预算违规率为0、P95不高于30秒。

当前完整Rerank链路与旧Hybrid链路使用了不同模式，却触发了跨模式延迟对比失败。正确做法是为Hybrid和Full分别维护Baseline与SLO。

## 15. 可靠性与故障降级

| 故障 | 当前处理 |
| --- | --- |
| Redis暂时不可用 | 指数退避重连，短期退回进程内存 |
| 向量检索失败 | 保留BM25结果 |
| BM25失败 | 保留向量结果 |
| LLM Rerank失败 | 回退RRF排序 |
| 结构化输出不合法 | 带校验错误重试，仍失败则业务降级 |
| GitHub/Web工具失败 | 重试、熔断，保留基础学习计划 |
| Celery任务发布失败 | 同步生成报告 |
| Worker异常退出 | late ACK后重新投递；任务状态写入MySQL |
| WebSocket断开 | MySQL/会话状态继续保存进度，重连后查询恢复 |

需要区分：超时限制故障持续时间；重试吸收瞬时故障；熔断阻止持续故障被反复放大；降级在依赖不可用时保留核心业务。

## 16. 并发与生产化边界

当前主要是单机、单FastAPI实例验证形态：

- 没有正式Nginx、Docker Compose或Kubernetes配置。
- 默认SQLite Checkpoint和BM25索引属于本地文件；Checkpoint可切换共享MySQL。
- STT Semaphore仍是进程级状态；工具限流、熔断和幂等可切换Redis共享。
- `/health`只验证FastAPI存活，不检查Redis、MySQL、Weaviate。
- CORS仍允许全部来源，JWT有开发默认密钥，500错误可能返回异常详情。

扩展到多实例需要：

1. 部署时将`LANGGRAPH_CHECKPOINT_BACKEND`切换为`mysql`并验证备份恢复；更大规模也可评估官方PostgresSaver。
2. BM25索引作为统一版本加载或独立检索服务。
3. 使用Redis会话级分布式锁，避免同一答案并发提交导致状态覆盖。
4. Redis Pub/Sub已支持WebSocket跨实例进度推送；生产可切换Streams并补充事件序号、ACK和消费组。
5. STT拆成独立推理服务或任务队列。
6. 将Tool Runtime状态切换到Redis；租户总预算仍需迁移Redis/API网关。
7. 增加liveness、readiness、startup探针和统一Metrics。
8. 强制HTTPS/WSS、限制CORS、轮换JWT密钥、隐藏内部错误。

## 17. 数据持久化与丢失边界

- Python进程内存：进程重启立即丢失。
- Redis：后端重启不一定丢，但TTL、淘汰、无持久化、RDB/AOF窗口、主从切换延迟或人工删除都可能造成丢失。
- SQLite Checkpoint：普通后端重启仍保留，但文件删除、磁盘损坏、路径变化或主机丢失会丢失；MySQL Checkpoint可跨实例共享，但仍依赖数据库备份与高可用。
- MySQL/InnoDB：普通服务重启会恢复已提交事务；未提交事务、磁盘故障、误删、错误迁移或旧备份恢复仍会造成数据丢失。

“保存在磁盘”不等于永久安全，生产仍需要备份、binlog/WAL、异机副本和恢复演练。

## 18. 关键面试问答

### 18.1 为什么不是一个大Agent完成所有任务？

不同阶段的目标、Prompt、Schema、工具权限和失败策略不同；拆分后更容易测试和定位。但确定性任务不应强行Agent化，例如排序公式、权限校验和状态路由仍由代码完成。

### 18.2 为什么任务板和Trace都需要？

任务板记录“计划和当前协作状态”，Trace记录“实际执行事实”。任务板告诉系统应该做什么，Trace用于证明实际做了什么及耗时多少。

### 18.3 为什么动态选题后不能直接平均分？

能力强的用户会被推送更难的问题，原始分天然更低。锚点题提供共同参照，IRT/Elo风格更新比较“实际表现与该难度下预期表现”的残差，减少难度差异带来的不公平。

### 18.4 为什么向量检索后还要BM25？

向量检索擅长语义近似，BM25擅长缩写、API名、错误码等精确词。二者召回错误不完全相同，融合能提高覆盖率。

### 18.5 为什么RRF后还要Rerank？

RRF只融合排名，不能深入判断问题和段落之间的逻辑关联；Rerank提高语义精排质量，但增加延迟和成本，因此需要A/B评测和按置信度跳过策略。

### 18.6 为什么Redis、MySQL、Checkpoint不能只留一个？

Redis负责高频热状态，MySQL负责可查询的长期业务事实，Checkpointer负责LangGraph执行位置和State恢复，三者的数据语义不同。

### 18.7 为什么FastAPI异步还需要Celery？

异步I/O只是在等待I/O时不阻塞线程，不提供任务持久化、崩溃重投或跨进程执行。报告生成耗时长且需要重试，因此使用Celery解耦Web进程，以MySQL保存生命周期、Redis传递任务与进度，并通过幂等键避免重复生成。

### 18.8 怎么证明系统真的更好？

RAG使用人工证据标注计算Recall、MRR和nDCG；Agent使用Trace回放检查路径、终态、工具、循环和预算；质量门禁再与同模式Baseline比较，避免Prompt或工作流修改造成回退。

### 18.9 项目最大的不足是什么？

当前RAG样本只有30条，Agent正式轨迹只有1条；自适应Rerank仍需在稳定网络和更大数据集上校准阈值，首题规划仍慢。跨实例报告进度已通过Redis Pub/Sub打通，但Pub/Sub本身不可回放；STT并发控制和BM25索引仍未跨实例共享。这些是下一阶段最明确的改进方向。

## 19. 常用命令

### 19.1 环境检查

```powershell
python scripts\check_env.py
```

### 19.2 启动服务

```powershell
python -m uvicorn api.main:app --host 127.0.0.1 --port 8000
```

### 19.3 启动Celery（启用异步报告时）

```powershell
python -m celery -A tasks.celery_app.celery_app worker --loglevel=INFO --pool=solo
```

同时在 `.env` 中配置：

```text
CELERY_ENABLED=true
```

### 19.4 RAG离线评测

```powershell
python -m evals.run_offline rag `
  --dataset evals/datasets/rag_retrieval.expanded.jsonl `
  --mode full `
  --top-k 5 `
  --output evals/reports/rag_latest.json
```

### 19.5 Agent轨迹评测

```powershell
python -m evals.run_offline agent `
  --dataset evals/datasets/agent_current.jsonl `
  --trace-file logs/agent_traces.jsonl `
  --output evals/reports/agent_latest.json
```

### 19.6 收集Bad Case

```powershell
python -m evals.run_offline agent-badcases `
  --trace-file logs/agent_traces.jsonl `
  --max-duration-ms 30000 `
  --output evals/datasets/agent_bad_cases.jsonl
```

### 19.7 健康检查与指标

```powershell
curl.exe http://127.0.0.1:8000/health/live
curl.exe http://127.0.0.1:8000/health/startup
curl.exe http://127.0.0.1:8000/health/ready
curl.exe http://127.0.0.1:8000/metrics
```

完整的指标口径、SLO、PromQL 和当前边界见 `docs/PRODUCTION_MONITORING.md`，可加载告警规则见 `deploy/prometheus/interviewagent_alerts.yml`。

## 20. 核心代码导航

| 模块 | 位置 |
| --- | --- |
| LangGraph流程 | `orchestration/graph.py`、`orchestration/nodes.py` |
| State与路由 | `orchestration/state.py`、`orchestration/edges.py` |
| Runtime/Harness | `orchestration/runtime.py`、`orchestration/budget.py` |
| 协作控制 | `orchestration/collaboration.py` |
| 自适应策略 | `orchestration/adaptive_policy.py` |
| Checkpoint/Trace | `orchestration/graph.py`、`memory/memory_manager.py`、`observability/tracing.py` |
| 健康检查/指标/SLO | `observability/health.py`、`observability/metrics.py`、`docs/PRODUCTION_MONITORING.md` |
| RAG | `rag/query_engine.py`、`rag/retrievers/`、`rag/reranker.py` |
| 短期/长期记忆 | `memory/short_term.py`、`memory/long_term.py` |
| 能力证据 | `memory/ability_repository.py` |
| 专项练习 | `memory/practice_repository.py`、`api/routers/practice.py` |
| WebSocket | `api/routers/websocket.py` |
| STT/TTS | `audio/stt.py`、`audio/tts.py` |
| Tool Runtime/MCP | `tools/`、`mcp/` |
| Celery报告 | `tasks/report_tasks.py`、`orchestration/report_dispatch.py` |
| 离线评测 | `evals/rag_eval.py`、`evals/agent_eval.py` |

## 21. 面试表达原则

回答每个技术点时使用以下结构：

```text
业务问题
  → 为什么选择这个方案
  → 当前具体实现
  → 异常与降级
  → 评测证据
  → 当前局限和下一步
```

不要只背框架名称，也不要把实验能力描述成生产能力。这个项目最有价值的部分不是“用了多少组件”，而是将不稳定的LLM放进了可控制、可恢复、可观测、可评测的业务流程中。
