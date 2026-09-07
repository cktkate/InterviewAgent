# 自适应面试决策引擎

## 工作方式

系统启动面试时先生成大于实际题量的候选池。候选题包含知识点、难度分、岗位权重、区分度、答案要点和前置知识。每完成一道主问题，系统按以下顺序处理：

1. Evaluator 以 `correctness / depth / structure / example` 四个维度输出结构化评分。
2. 能力状态按知识点更新 `mastery / confidence / attempts`，评分置信度决定本次证据权重。
3. 追问被确定性分类为 `clarify / deepen / challenge`，模型只能生成追问内容，不能更改分类结果。
4. 策略引擎综合岗位权重、知识缺口、能力不确定性、覆盖度、题目区分度、难度匹配度和重复度，从剩余候选池选择下一题。
5. 每次选择写入 `policy_decisions`，能力状态写入 `ability_profile`；Trace 同时记录候选数量、决策次数、能力知识点数量和追问类型。

## 公平评分

- 系统根据结构化 JD 核心能力确定性生成前两道中等难度锚点题，不使用简历短板，也不再交给 LLM 改写；相同结构化 JD 会得到相同锚点。
- 能力更新采用 Elo/IRT 风格的残差：比较实际表现与当前能力在该题难度下的预期表现，而不是直接平均原始分。
- 澄清和深挖追问按较低证据权重更新；挑战题答错只使用 0.25 权重，答好则作为能力上限证据。
- 最终总分为 `锚点题 40% + 难度校准能力 35% + 岗位匹配 25% + 挑战加分`，并由代码覆盖 LLM 主观给出的总分和推荐等级。
- 报告页面展示锚点分、难度校准能力、岗位匹配、挑战加分、评分置信度和知识点覆盖率。

## 状态字段

- `question_candidates`：完整候选题池。
- `question_plan`：已经实际选择过的题目，不再表示固定题单。
- `ability_profile`：按知识点维护的能力后验状态。
- `policy_decisions`：每次选题的总优先级、分项分数和原因。
- `completed_primary_questions`：已完成的主问题数量；追问不占主问题预算。
- `current_followup_type`：当前追问类型。
- `calibrated_scoring`：确定性的公平评分分项与最终总分。

## 本地验证

```powershell
D:\software\anaconda3\envs\all-in-rag\python.exe -m pytest tests\test_adaptive_policy.py -q
```

修改代码后需要重启 Uvicorn，新的面试会话才会创建自适应候选池。升级前保存的旧会话会继续按原来的顺序题单运行。
