# InterviewAgent 项目详细介绍

## 1. 项目定位

InterviewAgent 是一个面向技术面试场景的 AI 模拟面试官系统。它不是简单的问答机器人，而是把多 Agent 协作、RAG 检索、长期/短期记忆、动态难度调节、技能模块、Web 前端、REST API、WebSocket、CLI 和语音能力组合在一起，形成一套相对完整的工程化 AI Agent 应用。

项目的核心目标是：

- 输入岗位 JD 和候选人简历。
- 自动分析岗位要求和候选人能力画像。
- 基于知识库和岗位要求生成面试题计划。
- 以真实面试官的方式逐题提问、追问、点评。
- 对候选人回答进行评分，动态调整难度。
- 面试结束后生成评估报告和个性化复习计划。
- 将薄弱点沉淀到长期记忆，供后续面试复用。

一句话概括：这是一个“多 Agent 编排 + RAG + 记忆系统 + 面试业务流程”的 AI 面试训练平台。

## 2. 使用场景

这个项目适合用于以下场景：

- 技术岗位模拟面试训练。
- 根据 JD 定制化出题。
- 根据简历挖掘候选人的项目亮点和薄弱点。
- AI Agent 工程实践展示。
- RAG、LangGraph、记忆系统、技能系统等模块的课程或论文项目演示。
- 毕设、研究生课程项目、AI 应用工程化案例。

## 3. 总体架构

项目可以理解为三层结构：

```text
用户交互层
  Web 前端 / REST API / WebSocket / CLI / 语音输入输出

Agent 编排层
  LangGraph 状态图
  IntentRouter / JDAnalyzer / ResumeAnalyzer / QuestionPlanner
  Interviewer / Evaluator / StudyPlanner / ChatAgent
  Skill 系统 / 难度 FSM / 记忆读写

基础能力层
  通义千问 LLM / DashScope Embedding
  RAG 检索 / Weaviate / BM25
  Redis 短期记忆 / MySQL 长期记忆
  MCP 外部工具 / STT / TTS
```

核心流程由 `orchestration/graph.py` 定义，所有节点共享一个 `InterviewState` 状态对象。每个 Agent 读取状态、更新状态，然后把状态交给下一个节点。

## 4. 核心流程

一场完整面试的大致流程如下：

```text
开始面试
  -> 加载长期记忆
  -> 分析 JD
  -> 分析简历
  -> RAG 检索相关知识
  -> 规划题目
  -> 面试官提问
  -> 用户回答
  -> 单题评估
  -> 判断追问 / 下一题 / 结束
  -> 生成报告
  -> 生成复习计划
  -> 保存长期记忆
```

在代码里，这个流程主要由以下节点组成：

- `route`：判断用户意图。
- `load_memory`：加载用户长期薄弱点。
- `jd_analyze`：解析岗位 JD。
- `resume_analyze`：解析简历。
- `rag_retrieve`：调用混合检索器获取知识上下文。
- `plan_questions`：生成题目计划。
- `ask`：面试官提问或追问。
- `evaluate_one`：评估用户当前回答。
- `generate_report`：生成最终面试报告。
- `study_plan`：生成复习计划。
- `save_memory`：保存本轮薄弱点。
- `chat`：普通聊天兜底。
- `skill_dispatch`：调用技能系统。

对应文件：

- `orchestration/graph.py`：LangGraph 主流程。
- `orchestration/nodes.py`：每个节点的具体实现。
- `orchestration/edges.py`：节点之间的条件路由逻辑。
- `orchestration/state.py`：全局状态结构定义。

## 5. 多 Agent 设计

项目把复杂面试任务拆成多个专职 Agent，每个 Agent 负责一个边界清晰的子任务。

| Agent | 文件 | 作用 |
| --- | --- | --- |
| `IntentRouterAgent` | `agents/intent_router.py` | 识别用户意图，决定进入面试、答题、技能调用还是闲聊 |
| `JDAnalyzerAgent` | `agents/jd_analyzer.py` | 解析岗位 JD，提取岗位名称、级别、技术栈、核心能力 |
| `ResumeAnalyzerAgent` | `agents/resume_analyzer.py` | 分析简历，提取候选人经历、能力优势、潜在短板 |
| `QuestionPlannerAgent` | `agents/question_planner.py` | 根据 JD、简历和 RAG 上下文规划题目列表 |
| `InterviewerAgent` | `agents/interviewer.py` | 模拟真实面试官提问、追问、过渡和结束语 |
| `EvaluatorAgent` | `agents/evaluator.py` | 对回答评分，判断是否需要追问，生成最终报告 |
| `StudyPlannerAgent` | `agents/study_planner.py` | 根据面试表现生成个性化学习计划 |
| `ChatAgent` | `agents/chat_agent.py` | 处理非面试主流程的普通对话 |

所有 Agent 都继承自 `BaseAgent`。`BaseAgent` 统一封装了 LLM 调用、JSON 输出解析、重试机制和日志能力。当前 LLM 调用使用 `langchain_openai.ChatOpenAI`，通过 DashScope 的 OpenAI 兼容接口访问通义千问模型。

## 6. RAG 检索系统

RAG 部分用于给题目规划和面试内容提供知识支撑，避免完全依赖模型自由发挥。

主要组成：

- `rag/indexer.py`：构建知识库索引。
- `rag/query_engine.py`：统一查询入口。
- `rag/retrievers/vector_retriever.py`：向量检索，依赖 Weaviate。
- `rag/retrievers/bm25_retriever.py`：关键词检索。
- `rag/retrievers/hybrid_retriever.py`：混合检索，融合向量结果和 BM25 结果。
- `rag/reranker.py`：LLM 精排。
- `rag/evaluator/`：RAG 评估相关代码。
- `rag/knowledge_base/knowledge/`：本地知识库文档。

检索流程：

```text
查询文本
  -> 向量检索
  -> BM25 检索
  -> RRF 融合去重
  -> 返回 Top-K 文档
  -> 可选 LLM Rerank 精排
```

`HybridRetriever` 使用 RRF，即 Reciprocal Rank Fusion，对两路检索结果打分融合。默认权重来自 `config/settings.py`：

- 向量检索权重：`rag_vector_weight = 0.6`
- BM25 权重：`rag_bm25_weight = 0.4`
- 默认 Top-K：`rag_top_k = 5`

## 7. 记忆系统

项目实现了短期记忆和长期记忆两层。

### 7.1 短期记忆

短期记忆主要保存当前会话状态，例如：

- 当前题目索引。
- 当前难度。
- 问答历史。
- 评分记录。
- 是否等待回答。
- 是否需要追问。
- 当前题目文本。

对应文件：

- `memory/short_term.py`
- `memory/memory_manager.py`

短期记忆使用 Redis，默认 TTL 为 24 小时。

### 7.2 长期记忆

长期记忆主要保存跨会话信息，例如：

- 用户历史薄弱点。
- 面试记录。
- 复习计划。
- 用户画像。

对应文件：

- `memory/long_term.py`
- `memory/models.py`
- `memory/memory_manager.py`

长期记忆使用 MySQL + SQLAlchemy。面试结束后，系统会把本轮识别出的薄弱点写入长期记忆，下次面试开始时再加载回来，让面试更有针对性。

## 8. 动态难度调节

项目在 `orchestration/difficulty_fsm.py` 中实现了一个简单的有限状态机，用于模拟真实面试官的难度调节策略。

难度等级包括：

- `easy`
- `medium`
- `hard`

基本策略：

- 连续答对若干题，难度上升。
- 连续答错若干题，难度下降。
- 默认从 `medium` 开始。

阈值由 `config/settings.py` 控制：

- `consecutive_correct_to_upgrade = 2`
- `consecutive_wrong_to_downgrade = 2`

## 9. Skill 技能系统

除了主面试流程，项目还实现了可插拔 Skill 系统。Skill 更像“有状态的能力模块”，可以在面试过程中被触发。

内置技能包括：

| Skill | 文件 | 作用 |
| --- | --- | --- |
| `quiz` | `skills/quiz_skill.py` | 快速知识测验 |
| `teach` | `skills/teach_skill.py` | 技术概念讲解 |
| `project` | `skills/project_skill.py` | 项目亮点提炼，偏 STAR 表达 |
| `compare` | `skills/compare_skill.py` | 技术方案对比 |

统一注册入口是 `skills/skill_registry.py`。调用时，`skill_dispatch` 节点会根据 `skill_name` 找到对应 Skill 并执行。

## 10. Web 后端 API

后端基于 FastAPI，入口文件是 `api/main.py`。

启动后提供：

- 前端首页：`GET /`
- Swagger 文档：`GET /docs`
- ReDoc 文档：`GET /redoc`
- 健康检查接口。
- 面试 REST API。
- 文件上传 API。
- WebSocket 实时面试接口。

### 10.1 面试 REST API

主要接口在 `api/routers/interview.py`：

| 接口 | 方法 | 作用 |
| --- | --- | --- |
| `/interview/start` | `POST` | 启动一场新面试，返回第一题 |
| `/interview/answer` | `POST` | 提交回答，返回下一题、追问或报告 |
| `/interview/skill` | `POST` | 调用技能模块 |
| `/interview/report/{session_id}` | `GET` | 获取完整评估报告 |

`StartInterviewRequest` 主要字段：

- `jd_text`：岗位 JD 文本，必填。
- `resume_text`：简历文本，可选。
- `user_id`：用户 ID，默认 `web_user`。
- `total_questions`：题目数量，范围 1 到 20，默认 5。
- `voice`：TTS 音色，可选。

### 10.2 文件上传 API

文件在 `api/routers/upload.py`：

- `POST /upload/resume`

支持上传并解析：

- PDF
- DOCX / DOC
- TXT / MD

解析后返回纯文本，供面试流程使用。

### 10.3 WebSocket API

WebSocket 路由在 `api/routers/websocket.py`：

- `/ws/{session_id}`

前端通过 WebSocket 实时收发消息，支持：

- 开始面试。
- 提交回答。
- 强制结束面试。
- 拉取记忆数据。
- 清除长期记忆。
- 查看 RAG 检索结果。
- 调用 TTS 朗读。
- 调用 Skill。
- 心跳 ping/pong。

WebSocket 比 REST API 更适合前端实时体验，因为它可以连续推送问题、追问、报告、TTS 状态和音频数据。

## 11. 前端

前端位于 `frontend/`：

- `frontend/index.html`
- `frontend/static/js/main.js`
- `frontend/static/css/style.css`

前端是一个轻量级原生 HTML/CSS/JavaScript 应用。它通过当前站点地址构造 API 和 WebSocket 地址：

```javascript
const WS_BASE  = `ws://${location.host}`;
const API_BASE = `${location.protocol}//${location.host}`;
```

主要功能包括：

- 输入 JD。
- 上传或粘贴简历。
- 设置题目数量。
- 开始面试。
- 展示面试官问题、追问和候选人回答。
- 展示面试报告和复习计划。
- 调用 Skill。
- 展示短期/长期记忆。
- 查看 RAG 检索结果。
- 播放 TTS 音频。

## 12. CLI 命令行入口

CLI 位于 `cli/main.py`，基于 Typer + Rich。

主要命令：

```bash
python -m cli.main interview --jd "岗位 JD 文本" --total 5
python -m cli.main build-index
python -m cli.main check-env
python -m cli.main serve --host 0.0.0.0 --port 8000
```

CLI 面试过程中支持快捷技能：

- `/quiz <主题>`：快速测验。
- `/teach <概念>`：概念讲解。
- `/project <项目描述>`：项目亮点提炼。
- `/compare <A> vs <B>`：技术对比。

## 13. 语音能力

语音模块位于 `audio/`：

- `audio/stt.py`：语音转文字。
- `audio/tts.py`：文字转语音。
- `audio/audio_manager.py`：音频管理辅助逻辑。

项目依赖 `faster-whisper` 实现 STT，TTS 则通过 DashScope 相关能力实现。WebSocket 会在 TTS 开始和结束时发送状态消息，并通过二进制帧推送音频块。

## 14. MCP 外部工具

MCP 相关代码位于 `mcp/`：

- `mcp/mcp_client.py`
- `mcp/tools/github_tool.py`
- `mcp/tools/web_scraper.py`

MCP 用于接入外部工具能力，例如：

- GitHub 项目分析。
- 网页抓取。
- 技术资料获取。

这部分体现了项目对外部工具调用协议的扩展能力。

## 15. 配置系统

配置集中在 `config/settings.py`，通过 `pydantic-settings` 自动读取 `.env`。

关键配置包括：

### LLM

- `dashscope_api_key`
- `llm_model`
- `embedding_model`
- `llm_temperature`
- `llm_max_tokens`

### MySQL

- `mysql_host`
- `mysql_port`
- `mysql_user`
- `mysql_password`
- `mysql_database`

### Redis

- `redis_host`
- `redis_port`
- `redis_password`
- `redis_db`
- `redis_short_term_ttl`

### Weaviate

- `weaviate_url`
- `weaviate_api_key`

### RAG

- `rag_top_k`
- `rag_bm25_weight`
- `rag_vector_weight`

### App

- `app_host`
- `app_port`
- `log_level`
- `public_url`

注意：`.env` 文件中通常包含 API Key 和数据库密码，不应该提交到公开仓库。

## 16. 运行方式

### 16.1 安装依赖

```bash
pip install -r requirements.txt
```

开发和测试依赖：

```bash
pip install -r requirements-dev.txt
```

### 16.2 准备配置

复制示例配置：

```bash
copy .env.example .env
```

然后补充 DashScope、MySQL、Redis、Weaviate 等配置。

### 16.3 检查环境

```bash
python scripts/check_env.py
```

或：

```bash
python -m cli.main check-env
```

### 16.4 初始化数据库

```bash
python scripts/init_db.py
```

### 16.5 构建 RAG 索引

```bash
python scripts/build_index.py
```

或：

```bash
python -m cli.main build-index
```

### 16.6 启动 Web 服务

```bash
uvicorn api.main:app --host 0.0.0.0 --port 8000
```

或：

```bash
python -m cli.main serve --host 0.0.0.0 --port 8000
```

访问：

- Web 首页：`http://localhost:8000/`
- API 文档：`http://localhost:8000/docs`

### 16.7 启动 CLI 面试

```bash
python -m cli.main interview --jd "这里填写岗位 JD" --total 5
```

也可以传入 JD 文件路径和简历文件路径，具体以 CLI 参数为准。

## 17. 项目目录说明

```text
.
├── agents/                 # 多 Agent 实现
├── api/                    # FastAPI 后端、路由、Schema、中间件
├── audio/                  # STT / TTS / 音频管理
├── cli/                    # 命令行入口
├── config/                 # 全局配置和日志配置
├── frontend/               # Web 前端
├── images/                 # README 和展示用图片
├── mcp/                    # MCP 客户端和工具
├── memory/                 # Redis 短期记忆、MySQL 长期记忆
├── orchestration/          # LangGraph 编排、状态、节点、边、难度 FSM
├── rag/                    # RAG 索引、检索、Rerank、评估、知识库
├── scripts/                # 环境检查、建库、建索引、启动脚本
├── skills/                 # 可插拔技能系统
├── tests/                  # 测试用例
├── .env.example            # 环境变量示例
├── requirements.txt        # 运行依赖
├── requirements-dev.txt    # 开发依赖
├── pyproject.toml          # 项目元数据和工具配置
└── README.md               # 原项目说明文档
```

## 18. 技术栈总结

| 类别 | 技术 |
| --- | --- |
| Web 后端 | FastAPI, Uvicorn, Pydantic |
| Agent 编排 | LangChain, LangGraph |
| LLM | 通义千问 / DashScope OpenAI 兼容接口 |
| Embedding | DashScope `text-embedding-v3` |
| RAG | LlamaIndex, Weaviate, BM25, RRF, LLM Rerank |
| 记忆 | Redis, MySQL, SQLAlchemy |
| 前端 | HTML, CSS, 原生 JavaScript, WebSocket |
| CLI | Typer, Rich |
| 语音 | faster-whisper, DashScope TTS |
| 工具扩展 | MCP |
| 日志与稳定性 | loguru, tenacity |
| 测试 | pytest |

## 19. 项目亮点

### 19.1 工程结构完整

项目不只是一个单文件 demo，而是包含后端、前端、CLI、RAG、记忆、Agent 编排、脚本和测试的完整应用结构。

### 19.2 多 Agent 职责清晰

每个 Agent 负责一个明确任务，主流程通过 LangGraph 串联，降低了单个 Prompt 过长和职责混杂的问题。

### 19.3 面试流程真实

系统支持：

- 题目前后自然衔接。
- 根据回答追问。
- 回答后点评。
- 根据表现调整难度。
- 结束后生成报告。

这比单轮“出题-回答”更接近真实面试。

### 19.4 有记忆能力

短期记忆保存当前会话，长期记忆保存跨会话薄弱点，让系统可以越用越了解用户。

### 19.5 RAG 不是单一路径

项目实现了向量检索和 BM25 双路召回，并用 RRF 融合，还预留了 LLM Rerank 精排能力。

### 19.6 支持多端交互

同一套后端能力同时服务：

- Web 页面。
- REST API。
- WebSocket。
- CLI。

这让项目既能演示，也能测试和集成。

## 20. 适合作为论文或汇报的切入点

如果把这个项目用于课程汇报、毕设或论文，可以从以下角度展开：

- 多 Agent 协作在模拟面试任务中的应用。
- LangGraph 状态机编排复杂 AI 工作流。
- RAG 在面试题生成中的知识增强作用。
- 短期/长期记忆对个性化面试的影响。
- 动态难度调节机制对面试体验的改进。
- Skill 系统作为可插拔能力模块的设计。
- WebSocket + TTS 提升实时交互体验。
- 面向工程落地的 AI Agent 系统设计。

## 21. 当前项目可能的改进方向

后续可以考虑继续增强：

- 增加更系统的单元测试和端到端测试。
- 给 RAG 评估准备固定 benchmark 数据集。
- 将 Agent Prompt 独立成配置文件，便于调参。
- 增加 Docker Compose，一键启动 MySQL、Redis、Weaviate 和后端。
- 增强用户认证和多用户权限隔离。
- 增加面试报告导出 PDF 功能。
- 增加更细粒度的岗位题库管理。
- 将 Web 前端升级为 Vue/React 等组件化架构。
- 对语音输入输出增加更完整的浏览器端交互状态。

## 22. 总结

InterviewAgent 是一个以模拟技术面试为业务场景的 AI Agent 工程项目。它的价值不只在于“能问面试题”，而在于把真实 AI 应用中常见的关键能力组织到了一套完整系统里：

- 多 Agent 分工。
- LangGraph 流程编排。
- RAG 知识增强。
- Redis/MySQL 记忆系统。
- 动态难度状态机。
- 技能插件。
- Web、API、CLI 多端交互。
- STT/TTS 语音能力。
- MCP 外部工具扩展。

因此，这个项目很适合用来学习和展示 AI Agent 从原型到工程化应用的完整设计思路。
