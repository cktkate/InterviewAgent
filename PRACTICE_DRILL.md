# 八股专项背诵

专项背诵是正式模拟面试之外的独立增量模块。正式面试继续使用 LangGraph、LLM
动态出题、追问和评分；专项背诵只读取结构化题库，并由用户在查看标准答案后自评，
运行时不调用大模型评分。

## 用户流程

1. 从左侧导航进入“专项背诵”。
2. 选择分类背诵、今日复习或正式面试错题。
3. 看到题目后先自行思考或口述，可选填口述要点。
4. 点击“查看标准答案”。
5. 选择“正确”“半对半错”或“不会”，系统自动安排下次复习。

复习间隔：

- 正确：掌握等级提升，按 1、3、7、14、30、60 天递增；
- 半对半错：次日复习，掌握等级保持；
- 不会：掌握等级归零，10 分钟后复习。

## 与正式面试的边界

- 新模块使用独立 REST API 和独立数据库表，不进入面试 WebSocket 或 LangGraph。
- 正式面试保存长期记忆时，会把未达标的主问题幂等加入用户私有复习题库。
- 面试错题写入为容错旁路，失败只记录日志，不影响原报告生成。
- 用户私有面试题按账户隔离，内置八股题为所有账户共享。

## API

- `GET /practice/overview`：题库、到期题、面试错题和今日完成统计；
- `GET /practice/catalog`：技术方向与知识点目录；
- `GET /practice/questions/next`：按模式抽取题干，不返回答案；
- `GET /practice/questions/{id}/answer`：用户主动查看标准答案；
- `POST /practice/attempts`：保存自评并更新复习计划。

相关实现：

- `memory/practice_repository.py`
- `api/routers/practice.py`
- `memory/models.py`
- `frontend/index.html`
- `frontend/static/js/main.js`
- `frontend/static/css/style.css`
