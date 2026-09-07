# 两层记忆与能力证据闭环

## 目标

系统使用两种物理存储层：

- 短期记忆：Redis 保存实时会话快照，LangGraph Checkpoint 支持流程恢复。
- 长期记忆：MySQL 保存逐题证据、知识点能力画像、正式面试记录和专项复习队列。

正式面试与专项练习共享长期知识点画像，但两类数据的可信度和用途不同。专项练习只为下一场面试提供低权重先验，不参与当前正式面试总分。

## 数据流

```text
正式面试逐题评分 ──> ability_evidence（强证据） ──┐
                                                   ├─> knowledge_mastery
专项练习用户自评 ──> ability_evidence（弱证据） ──┘          │
                                                              ↓
下一场面试加载历史画像 ──> 自适应选题、难度匹配、弱点覆盖
```

正式面试未答对的主问题还会进入用户私有的专项复习队列；追问不会重复生成错题。

## 权重与公平性

| 证据 | 基础权重 |
| --- | ---: |
| 正式面试主问题 | 1.00 |
| 澄清/深挖追问 | 0.65 |
| 未通过的越级挑战题 | 0.25 |
| 专项练习自评 | 0.25 |

专项练习自评置信度固定为 `0.75`，所以单次有效证据质量为 `0.25 × 0.75 = 0.1875`。自评映射为：

- 正确：`observation = 0.90`
- 半对半错：`observation = 0.55`
- 不会：`observation = 0.10`

正式证据和练习证据分别维护 mastery 与 evidence mass，再生成组合画像。练习通道的组合占比使用硬上限：

```text
practice_share = min(0.25, practice_mass / (formal_mass + practice_mass + 1))
```

因此反复点击“正确”不能把练习证据提升为正式能力结论。最终报告仍只读取本场正式面试的 `score_records` 和难度校准结果；历史画像仅影响候选题优先级和难度匹配。

## 一致性与降级

- 每条能力证据使用稳定 ID，正式面试节点重放不会重复累计。
- `ability_evidence` 只追加，`knowledge_mastery` 保存可快速读取的聚合状态。
- 用户和求职目标按 `user_id + target_scope` 隔离。
- 面试来源的专项错题保留原求职目标；公共题库练习写入全局画像。
- 专项练习记录、复习调度与画像更新使用同一数据库事务和保存点。
- 长期存储不可用时采用 fail-open：正式面试继续使用会话内画像，练习调度也不因画像更新失败而丢失。

## 关键实现

- `memory/models.py`：`AbilityEvidence`、`KnowledgeMastery`、`PracticeQuestionScope`
- `memory/ability_repository.py`：证据权重、幂等写入、画像聚合与跨会话加载
- `orchestration/nodes.py`：正式面试逐题写入和新会话画像加载
- `memory/practice_repository.py`：练习自评、目标映射及低权重证据更新
