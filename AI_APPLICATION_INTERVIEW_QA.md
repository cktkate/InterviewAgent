# InterviewAgent AI 应用开发面试手册

本文根据 InterviewAgent 简历项目整理，适用于 AI 应用开发工程师岗位。回答时建议遵循：

```text
业务问题 -> 技术方案 -> 具体实现 -> 异常处理 -> 改进方向
```

## 1. 简历项目描述

InterviewAgent 是面向技术岗位的智能模拟面试系统。系统根据岗位 JD 和候选人简历自动规划面试题，通过多 Agent 协作完成提问、追问、评分、报告生成及学习建议推荐，并结合 RAG、记忆机制和实时语音交互提升面试的专业性与连贯性。

核心技术：

- 基于 LangGraph 构建多 Agent 状态机，通过共享 State 实现协作与流程分支。
- 基于 LLM 结构化输出设计多维回答评估和动态追问策略。
- 基于 Weaviate、BM25 和 RRF 构建混合检索引擎。
- 基于 Redis 和 MySQL 实现短期会话状态与长期用户画像。
- 基于 FastAPI、WebSocket、STT 和 TTS 实现实时语音交互。
- 基于 MCP 设计外部工具接入层，并通过 GitHub API 推荐学习资源。

## 2. 一分钟项目介绍

> InterviewAgent 是一套面向技术岗位的 AI 模拟面试系统，我主要完成了整体架构和核心功能开发。用户输入面试方向、JD 和简历后，系统通过 LangGraph 编排岗位分析、简历解析、RAG 检索、问题规划、回答评估和学习规划等 Agent。面试过程中，Evaluator 会通过结构化输出完成多维评分，并根据关键知识缺失和追问次数决定追问、下一题或结束。工程上使用 Redis 保存当前会话 State，使用 MySQL 保存用户账号、长期薄弱点和面试报告，通过 FastAPI 和 WebSocket 集成 STT/TTS，实现多轮实时语音交互，同时预留 MCP 工具扩展能力并通过 GitHub API 推荐学习资源。

## 3. 系统流程

```text
用户登录
  -> 自然语言意图识别
  -> JD Analyzer / Resume Analyzer
  -> RAG 混合检索
  -> Question Planner 规划题目
  -> Interviewer 提问
  -> 用户回答
  -> Evaluator 多维评分
       -> 需要追问：Interviewer 追问
       -> 不需追问：进入下一题
       -> 题目完成：生成报告
  -> Study Planner 生成学习计划
  -> Redis 保存会话 / MySQL 沉淀长期记忆
```

## 4. LangGraph 与多 Agent

### 4.1 为什么使用 LangGraph？

> 面试不是一次模型调用，而是一个多轮、有状态、存在条件分支的流程。LangGraph 可以把岗位分析、检索、出题、评分和报告生成定义为节点，通过共享 State 传递数据，并根据 `should_followup`、当前题号和结束状态进行条件路由。相比手写大量 `if-else`，流程更清晰，也更容易增加节点、测试分支和恢复状态。

### 4.2 为什么使用多个 Agent？

> JD 分析、问题规划、回答评分和学习规划的目标、Prompt 和输出结构不同。拆成多个 Agent 后，每个 Agent 职责更单一，输出更稳定，也方便单独测试、更换模型和定位问题。项目采用“LangGraph 控制流程、Agent 完成语义任务、Tool 执行确定性操作”的分层方式。

### 4.3 多 Agent 如何通信？

Agent 不直接互相发送消息，而是通过 `InterviewState` 共享数据：

```text
JDAnalyzer       -> jd_parsed
ResumeAnalyzer   -> resume_parsed、strengths、weaknesses
RAG Node         -> rag_context、rag_sources
QuestionPlanner  -> question_plan
Interviewer      -> current_question_text
Evaluator        -> score_records、should_followup、key_missing
ReportGenerator  -> final_report
StudyPlanner     -> study_plan
```

### 4.4 节点什么时候执行？

> 节点不是自己决定是否运行，而是由 LangGraph 的边和条件路由调度。启动面试时执行分析、检索和问题规划；用户提交答案后进入评估节点；评估结果决定进入追问、下一题或报告节点。

### 4.5 State 为什么叫“有状态”？

> 因为系统会记住上一轮执行结果。当前题号、历史问答、追问次数、难度和评分都会保存在 State 中，下一轮节点基于这些历史状态继续工作，而不是每次都从零开始。

### 4.6 如何扩展新的 Agent？

> 新增 Agent 时先定义它读取和写入的 State 字段，再实现节点函数，将节点注册到 LangGraph，并配置普通边或条件边。只要不破坏已有 State 契约，就可以较低成本插入新能力。

## 5. LLM 结构化评估与动态追问

### 5.1 什么是结构化输出？

不是让模型返回自由文本，而是要求模型按照固定 JSON 结构返回：

```json
{
  "overall_score": 75,
  "correctness": 78,
  "completeness": 68,
  "clarity": 82,
  "key_missing": ["RRF 参数的作用"],
  "should_followup": true,
  "followup_question": "RRF 公式中的 k 有什么作用？"
}
```

> 后端可以直接解析字段并进行流程路由，同时为缺失字段设置默认值，避免依赖自由文本字符串匹配。

### 5.2 回答如何评分？

> Evaluator 综合当前问题、考察点和用户回答，从正确性、完整性、技术深度、表达清晰度和岗位相关性等维度评分。评分结果写入 `score_records`，供动态难度、最终报告和长期薄弱点提取使用。

### 5.3 什么时候追问？

同时满足以下条件时适合追问：

- 回答总体方向基本正确。
- 缺失的是影响能力判断的关键知识点。
- 模型返回 `should_followup=true`。
- `followup_count` 没有达到 `max_followup`。

轻微遗漏不一定追问；回答已经完整、严重偏题或达到追问上限时进入下一题。

### 5.4 如何防止无限追问？

> State 保存 `followup_count` 和 `max_followup`。每追问一次就增加计数，达到上限后即使仍有缺失点，也会进入下一题，从规则层限制 LLM 决策。

### 5.5 动态难度如何实现？

```text
连续高分达到阈值 -> 难度升档
连续低分达到阈值 -> 难度降档
其他情况         -> 保持当前难度
```

> State 维护 `current_difficulty`、`consecutive_correct` 和 `consecutive_wrong`。使用连续表现而不是单题分数，可以降低偶然回答造成的频繁波动。

### 5.6 如何降低 LLM 输出不稳定？

- Prompt 明确角色、输入范围、判断标准和 JSON 字段。
- 对字段类型、取值范围和题目数量进行校验。
- 对缺失字段设置默认值。
- 使用最大追问次数和题目数量做规则兜底。
- 调用失败时记录日志并进行降级。
- 后续可以增加 Pydantic Schema、重试和输出修复器。

## 6. RAG 混合检索

### 6.1 RAG 在项目中的作用是什么？

> 当前主流程中，RAG 主要为问题规划提供外部知识上下文。系统根据面试主题、JD 技术栈和核心能力检索相关技术资料，QuestionPlanner 结合检索结果生成更贴近岗位的问题。当前 Evaluator 主要依赖模型和评分 Prompt，不能夸大为已经完成基于 RAG 标准答案的严格判分。

### 6.2 Weaviate 和 BM25 分别做什么？

| 检索方式 | 作用 | 适合场景 |
| --- | --- | --- |
| Weaviate 向量检索 | 召回语义相近内容 | 同义表达、自然语言问题 |
| BM25 关键词检索 | 精确匹配关键词 | RRF、HNSW、框架名、版本号 |

> 纯向量检索可能弱化精确术语，纯 BM25 又难以理解同义表达，因此项目使用混合检索。

### 6.3 RRF 如何融合？

```text
RRF(d) = sum(1 / (k + rank_i(d)))
```

> RRF 不直接比较两种检索器不可比的原始分数，而是按照文档在每路结果中的排名计算融合分数。文档在多路结果中排名越靠前，最终得分越高；`k` 用于减弱头部排名差异。

### 6.4 文档如何入库？

```text
PDF / DOCX / TXT
  -> 文件类型对应的解析器
  -> 文本提取和基础清洗
  -> 文档分块
  -> Embedding
  -> 写入 Weaviate，并保留文本与元数据
```

> 文件解析器负责读取文档，不是使用大模型解析。分块需要在上下文完整性、召回精度和 Token 成本之间权衡。

### 6.5 Top-K 为什么需要实验？

> Top-K 太小可能漏掉正确证据，太大又会引入噪声、增加上下文长度和模型成本。可以在标注查询集上对不同 K 值比较 Recall@K、MRR、NDCG 和 Context Precision，再结合延迟与 Token 成本选择参数。

### 6.6 RAG 如何评估？

检索层指标：

- Recall@K：相关文档是否进入前 K。
- MRR：第一个相关结果排名是否靠前。
- NDCG：多个相关结果的整体排序质量。
- Context Precision：检索上下文中有多少内容真正相关。

生成层指标：

- Faithfulness：回答是否能被检索上下文支持。
- Answer Relevancy：回答是否真正回应问题。
- Context Relevancy：上下文是否与问题相关。

### 6.7 项目有没有 Rerank？

> 当前核心链路主要使用向量检索、BM25 和 RRF 融合，没有接入独立的 Cross-Encoder 或 LLM 精排模型。后续可以对 RRF 候选集增加 BGE Reranker，但需要评估精度收益与额外延迟。

### 6.8 Weaviate 不可用怎么办？

> RAG 节点捕获连接和检索异常，将 `rag_context` 和 `rag_sources` 降级为空，QuestionPlanner 继续根据 `interview_topic`、JD、简历和历史薄弱点出题。RAG 是增强能力，不应成为核心面试流程的单点故障。

## 7. Redis 与 MySQL 分层记忆

### 7.1 为什么同时使用 Redis 和 MySQL？

```text
Redis：这场面试进行到哪里
MySQL：这个用户过去表现如何
```

> Redis 以 `session_id` 保存当前面试 State 和最近对话，更新频繁且设置 TTL；MySQL 以 `user_id` 保存账号、长期薄弱点、用户画像、报告和学习计划，需要跨会话持久化。

### 7.2 Redis 如何保存 State？

Redis Key：

```text
iam:short:{session_id}:snapshot
iam:short:{session_id}:turns
```

实现方式：

```text
State -> 安全序列化 -> JSON -> SET key value EX 86400
对话 -> RPUSH -> LTRIM 保留最近 20 条 -> EXPIRE
```

State 包括题目计划、当前题号、问答历史、评分、追问和难度等，但不保存原始音频、网络数据包和全部日志。

### 7.3 Redis 数据什么时候会丢失？

- 24 小时 TTL 到期。
- 未开启持久化时 Redis 重启。
- 内存不足触发淘汰。
- 人工删除 Key 或清库。
- Docker 容器删除且没有挂载 Volume。
- 降级到 Python 进程内存后，后端进程重启。

### 7.4 MySQL 保存什么？

| 表 | 主要内容 |
| --- | --- |
| `users` | 账号、密码哈希、账户状态 |
| `user_profiles` | 长期薄弱点、优势、累计面试次数 |
| `interview_records` | 分数、报告、学习计划、弱点 |
| `knowledge_notes` | 用户收藏的知识点 |

### 7.5 MySQL 如何实现？

> 项目使用 SQLAlchemy ORM 定义模型，通过 `create_engine` 创建连接池、`sessionmaker` 管理会话和事务、`Base.metadata.create_all` 创建表。查询始终使用当前认证用户的 `user_id` 作为条件。

### 7.6 Redis 和 MySQL 如何配合？

```text
用户登录 -> MySQL 确认身份
开始面试 -> MySQL 读取长期薄弱点
面试过程 -> Redis 持续保存 State
面试结束 -> MySQL 保存报告、学习计划和新薄弱点
```

### 7.7 如何保证跨存储一致性？

> 当前采用应用层最终一致性：过程状态写 Redis，结束后写 MySQL，还没有完整的分布式事务。生产环境可以通过消息队列、重试任务或 Outbox Pattern 保证面试结果最终写入 MySQL。

### 7.8 Redis 挂了怎么办？

> 项目会临时降级到 Python 进程内存，核心流程可以继续，但后端重启或切换 Worker 后状态会丢失。生产环境需要 Redis Sentinel 或 Cluster、AOF 持久化、健康检查和监控。

## 8. 用户注册与身份认证

### 8.1 注册流程

```text
前端提交用户名和密码
  -> 后端校验格式
  -> 生成 UUID user_id
  -> PBKDF2-SHA256 + 随机盐生成密码哈希
  -> 保存 MySQL users 表
  -> 签发 JWT
  -> 前端保存 Token
```

### 8.2 PBKDF2、SHA-256 和随机盐分别做什么？

- PBKDF2：通过大量迭代增加暴力破解成本。
- SHA-256：PBKDF2 内部使用的哈希函数。
- 随机盐：让相同密码产生不同哈希，防止彩虹表攻击。
- 密码哈希：三者共同计算后保存到数据库的结果。

### 8.3 登录后如何确定用户？

> REST 请求通过 `Authorization: Bearer <JWT>` 携带 Token；WebSocket 连接通过查询参数携带 Token。后端验证签名和过期时间后，从 JWT 的 `sub` 读取可信 `user_id`，不相信前端直接提交的用户 ID。

### 8.4 如何防止访问他人会话？

> Redis State 同时保存会话所属 `user_id`。提交答案、获取报告或恢复 WebSocket 时，后端检查 `state.user_id == token.user_id`，不一致返回 403 或关闭连接。

## 9. FastAPI、WebSocket 与语音

### 9.1 为什么使用 FastAPI？

> FastAPI 适合 Python AI 应用，支持异步接口、Pydantic 参数校验、依赖注入、WebSocket 和自动 API 文档，便于与模型、Redis和 MySQL 集成。

### 9.2 为什么使用 WebSocket？

> 面试过程中服务端需要主动推送问题、追问、状态和 TTS 音频。WebSocket 建立长连接后可以双向传输文本和二进制，减少轮询和重复握手，更适合实时多轮交互。

### 9.3 STT/TTS 链路

```text
前端录音 -> WebSocket 上传音频 -> STT 转文本
-> Evaluator 评分 -> 生成下一题
-> CosyVoice 生成音频二进制
-> WebSocket 推送 -> 前端组装并播放
```

### 9.4 如何体现实时性？

> 问题、追问、识别结果和音频由服务端通过 WebSocket 主动推送，前端不需要轮询。语音使用二进制传输，并即时更新面试状态。后续可通过 LLM 流式输出和异步任务进一步降低首包延迟。

### 9.5 TTS 为什么会出现音色不一致？

> 部分消息没有继续携带音色，或者前端浏览器语音兜底与服务端音频发生播放竞争。解决方式是将 `tts_voice` 持久化到 State，统一使用 `tts_start -> 二进制音频 -> tts_end` 协议，并且只有服务端没有有效音频时才触发浏览器兜底。

## 10. MCP 与外部工具

### 10.1 MCP Host、Client 和 Server 的关系

```text
模型 / Agent
    |
MCP Host：管理上下文和工具定义
    |
MCP Client：list_tools / call_tool
    |
stdio 或 HTTP/SSE
    |
MCP Server：实现具体工具
```

### 10.2 模型如何知道调用哪个工具？

> Host 通过 `list_tools` 获取工具名称、描述和参数 Schema，再将工具定义提供给模型。模型根据用户意图选择工具并生成结构化参数，Host 校验后通过 Client 调用 `call_tool`，最后将结果返回模型继续生成回答。

### 10.3 GitHub 推荐是 MCP 吗？

> GitHub 学习资源推荐的底层仍是真实 GitHub REST API，但默认调用链已经接入统一 Tool Runtime。Runtime 使用 Pydantic 校验参数，并负责只读权限、超时、有限重试、限流、熔断和 Trace。MCP 动态工具则在 `list_tools` 后读取服务端 JSON Schema 与安全 annotations，再进入同一个 Executor。因此 GitHub 不是伪装成 MCP 的接口，而是与 MCP 工具共享执行治理层。

### 10.4 网页抓取工具需要考虑什么？

- 请求超时、状态码、重定向和编码。
- HTML 清洗、正文提取和最大响应长度。
- 反爬机制和页面结构变化。
- 禁止访问内网地址，降低 SSRF 风险。
- 域名白名单、调用超时和内容类型校验。
- 工具失败时返回结构化错误，不阻塞核心流程。

### 10.5 如何防止大模型随意调用工具？

> 大模型只生成工具名和结构化参数，不直接执行。每个工具在 Registry 中声明参数 Schema、风险等级、权限、超时、重试、限流和幂等属性。Executor 先执行 Schema 与权限校验；高风险写操作必须显式确认，未标注安全属性的 MCP 工具默认按写工具处理。调用过程只在 Trace 中记录字段名和参数摘要哈希，不记录原始敏感参数。

## 11. 项目问题与解决方案

### 11.1 输入 RAG 却生成数据结构题

现象：用户输入“我想面试 RAG”，第一题却是栈和队列。

定位：意图识别正确，但短文本无法被 JDAnalyzer 稳定解析，导致 `jd_parsed` 和 `rag_context` 为空，QuestionPlanner 退化为通用出题。

解决：

```text
提取 interview_topic=RAG
-> 自动构造最小 JD
-> RAG Query 优先使用 interview_topic
-> QuestionPlanner 增加主题硬约束
```

回答：

> 我没有只修改 Prompt，而是修复了自然语言入口、共享 State、RAG 查询和出题规划四层链路，保证主题贯穿多个 Agent。

### 11.2 Weaviate 不可用导致启动失败

> 我先检查 URL、端口、API Key 和 Meta 接口，确认连接配置或云端实例状态问题。随后将 RAG 调用放到异常隔离边界，检索失败时使用空上下文继续出题，避免增强能力成为单点故障。

### 11.3 TTS 成功但播放了浏览器声音

> 后端日志证明音频已生成，因此进一步检查 WebSocket 二进制接收和前端播放时序，发现会话音色丢失以及浏览器兜底与服务端音频竞争。通过在 State 保存音色、明确 TTS 开始和结束协议、延后兜底时机解决。

### 11.4 WebSocket 断开后状态丢失

> 将 LangGraph State 以 `session_id` 保存到 Redis，在启动、回答、评分、Skill 调用、结束和断开连接等关键节点更新快照。重新连接时恢复 State，并校验会话所属 `user_id`。

### 11.5 LLM 返回字段缺失

> Prompt 要求固定 JSON；解析后使用默认值和字段校验；流程层通过最大追问次数、题目数量和结束条件做规则兜底。后续可加入严格 Schema 和自动重试。

## 12. 测试与工程化

### 12.1 如何测试多 Agent 系统？

- 单元测试：JWT、密码哈希、状态路由、难度 FSM 等确定性逻辑。
- Agent 测试：使用 Mock LLM 验证 Prompt 输入和结构化输出。
- 节点测试：检查每个节点读取和写入的 State 字段。
- 分支测试：覆盖追问、下一题、强制结束和正常结束。
- 集成测试：Redis、MySQL、Weaviate、GitHub API 和 MCP 工具。
- 端到端测试：通过 WebSocket 完成启动、回答、追问和报告流程。

### 12.2 如何处理并发提交？

> 当前 State 采用读取、修改、整体覆盖，极端情况下可能出现后写覆盖前写。生产环境可以使用 Redis Lua、`WATCH/MULTI`、分布式锁或 State 版本号实现乐观并发控制，前端也要禁止重复提交。

### 12.3 如何优化延迟和成本？

- 对 JD 和简历解析结果做缓存。
- 独立选择不同 Agent 的模型，不必全部使用大模型。
- 并行执行没有依赖的分析任务。
- 缩短 RAG 上下文，控制 Top-K。
- 使用流式输出降低用户感知延迟。
- 对外部 API 设置超时、重试和熔断。
- 记录每个节点的耗时、Token 数和失败率。

### 12.4 Celery 和 Redis 如何配合？

> 项目支持可选的 Celery 异步报告链路。最后一题评分完成后，先将最终答题 State 保存到 Redis DB 0，再把报告任务投递到 Redis DB 1；Celery Worker 读取 State，生成报告和学习计划，并将任务结果写回 Redis。前端通过 WebSocket 查询 `queued`、`processing`、`retrying`、`completed` 或 `failed` 状态。Broker 发布失败时自动降级为同步生成。单题评分和下一题生成仍保持同步，以满足实时交互要求。

### 12.5 项目目前的不足是什么？

> 当前单题评分和下一题生成仍为同步调用；Celery主要覆盖最终报告链路，还没有扩展到STT、TTS和知识库批量入库；RAG没有独立Cross-Encoder精排；Evaluator尚未完全基于检索证据评分；多实例并发控制仍可加强。这些属于明确的后续优化方向。

## 13. 高频压力追问

### 13.1 如果不用 LangGraph 怎么实现？

> 可以使用普通状态机或业务 Service 配合条件判断实现。小规模流程这样更轻量；当节点、分支、恢复和可观测需求增加时，LangGraph 更有优势。技术选择应根据流程复杂度，而不是为了使用框架。

### 13.2 如果模型评分不公平怎么办？

> 建立人工标注集，比较模型评分与人工评分的一致性；固定评分 Rubric；降低温度；引入参考证据；对关键评分使用多次评估或规则校准；持续分析误差案例，而不是只观察单次输出。

### 13.3 如果并发用户增加怎么办？

> API 层采用多实例部署，Redis 共享会话状态，MySQL 使用连接池并增加必要索引；耗时模型任务放入异步队列；TTS 和 STT 独立限流；对 Weaviate 和外部 API 增加超时、熔断和监控。

### 13.4 如何保证安全？

> 密码使用 PBKDF2-SHA256 和随机盐保存；JWT 提供身份认证；会话按 `user_id` 校验所有权；上传文件限制类型和大小；外部抓取防止 SSRF；日志避免记录密码、Token、完整简历等敏感信息。生产环境还应使用 HTTPS/WSS、HttpOnly Cookie、限流和 Token 撤销机制。

## 14. 最应该背熟的十个问题

1. 项目的一分钟介绍和完整流程。
2. 为什么使用 LangGraph 和多 Agent。
3. Agent 如何通过共享 State 通信。
4. 结构化评估和动态追问如何实现。
5. Weaviate、BM25 和 RRF 各自的作用。
6. RAG 的评估指标和当前实现边界。
7. Redis 和 MySQL 为什么分层。
8. WebSocket 和 STT/TTS 的实时链路。
9. MCP Host、Client、Server 的关系。
10. 一个真实问题的定位过程、解决方案和改进方向。

## 15. 回答真实性边界

面试时需要明确区分已经实现和后续规划：

| 能力 | 当前状态 |
| --- | --- |
| LangGraph 多 Agent 流程 | 已实现 |
| LLM 结构化评估和动态追问 | 已实现 |
| Weaviate + BM25 + RRF | 已实现 |
| 独立 Cross-Encoder / LLM Rerank | 当前核心链路未接入 |
| Redis 短期记忆 + MySQL 长期记忆 | 已实现 |
| FastAPI + WebSocket + STT/TTS | 已实现 |
| GitHub REST API 推荐 | 已实现 |
| Tool Runtime + MCP 动态工具治理 | 已实现 Schema 校验、权限风险控制、超时重试、限流熔断与 Trace |
| 基于 RAG 标准答案的严格评分 | 尚未完整实现 |
| 线上 A/B、真实高并发量化指标 | 没有真实数据时不要虚构 |

最后遵循一个原则：已经实现的能力讲清楚代码链路、状态字段和异常处理；尚未实现的能力说明设计方案和权衡，不编造准确率、并发量或性能提升比例。
