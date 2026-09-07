# InterviewAgent 生产监控与 SLO

本文描述当前代码已经实现的监控能力与建议的初始 SLO。阈值是上线初期的工程目标，不是已达到的线上数据；需要在获得真实流量基线后复核。

## 1. 健康探针

| 接口 | 语义 | 失败条件 | 用途 |
| --- | --- | --- | --- |
| `/health/live`、`/livez` | 进程存活且事件循环可响应 | API 无法响应 | 容器重启判断 |
| `/health/startup`、`/startupz` | FastAPI lifespan 已完成 | 应用仍在启动或关闭 | 慢启动保护 |
| `/health/ready`、`/readyz` | 可承接新流量 | 任一必选依赖检查失败 | 从负载均衡摘流 |
| `/health`、`/api/health` | 兼容旧客户端的 liveness | API 无法响应 | 保留原功能 |

Readiness 并发执行并限制单项超时，默认缓存 5 秒，避免高频探针反向压垮依赖：

- MySQL：建立连接并执行 `SELECT 1`。
- Redis：对业务状态库执行 `PING`。
- Weaviate：请求 `/v1/.well-known/ready`。
- Celery：仅当 `CELERY_ENABLED=true` 时要求至少一个 Worker 回复 `ping`；同步模式下显示 `disabled/required=false`。

readiness 失败不会删除业务降级。Redis/MySQL 的原有内存降级和 Celery 发布失败后的同步报告生成仍然保留；探针只决定实例是否继续接收新流量。

## 2. 指标端点与指标

API 进程暴露 `GET /metrics`。Celery 是独立进程，Worker 启动后在 `METRICS_WORKER_PORT`（默认 `9109`）暴露相同 Prometheus 文本格式；Prometheus 应同时抓取 API 和 Worker，并在查询中按实例求和。

| 指标 | 类型 | 说明 |
| --- | --- | --- |
| `interviewagent_report_tasks_total{outcome,execution}` | Counter | 报告最终成功/失败；区分 Celery 和同步降级 |
| `interviewagent_report_task_retries_total` | Counter | Celery 发起的重试次数 |
| `interviewagent_report_task_duration_seconds` | Histogram | 有 MySQL 任务记录时为入队到终态耗时，否则为当前执行耗时 |
| `interviewagent_llm_calls_total{model,outcome}` | Counter | LLM 每次传输尝试的成功/失败 |
| `interviewagent_llm_tokens_total{model,direction}` | Counter | Provider 实际返回的输入/输出 Token；未返回 usage 时不猜测 |
| `interviewagent_tool_calls_total{tool,outcome}` | Counter | Tool Runtime 最终成功/失败 |
| `interviewagent_tool_call_duration_seconds` | Histogram | 含受控重试的工具调用耗时 |
| `interviewagent_agent_runs_total{operation,outcome}` | Counter | Agent Run 终态 |
| `interviewagent_agent_run_duration_seconds` | Histogram | Agent Run 耗时 |
| `interviewagent_dependency_ready{dependency}` | Gauge | 最近一次依赖检查结果 |

标签只使用有限集合，不放 `user_id/session_id/task_id/trace_id`，避免 Prometheus 高基数。P95 不在单进程内计算，而由直方图聚合：

```promql
histogram_quantile(
  0.95,
  sum by (le) (rate(interviewagent_report_task_duration_seconds_bucket{execution="celery"}[15m]))
)
```

Prometheus 中成功率、失败率和重试率由单调递增 Counter 派生，避免进程内滑动窗口状态：

```promql
# 成功率
sum(rate(interviewagent_report_tasks_total{outcome="success"}[10m]))
/
sum(rate(interviewagent_report_tasks_total[10m]))

# 失败率
sum(rate(interviewagent_report_tasks_total{outcome="failure"}[10m]))
/
sum(rate(interviewagent_report_tasks_total[10m]))

# 每个终态任务对应的重试次数
sum(rate(interviewagent_report_task_retries_total[15m]))
/
sum(rate(interviewagent_report_tasks_total[15m]))
```

## 3. 初始 SLO 与告警

| SLI | 初始 SLO（30 天滚动） | 告警阈值 |
| --- | --- | --- |
| 必选依赖 readiness | 99.9% | 连续 2 分钟失败告警，5 分钟升级严重 |
| 报告任务成功率 | ≥ 99% | 10 分钟窗口成功率 < 95%，且至少 5 个终态任务 |
| 报告生成 P95 | ≤ 150 秒 | 15 分钟窗口 P95 > 150 秒 |
| 报告重试次数/终态任务 | ≤ 5% | 15 分钟窗口 > 10%，且至少 5 个终态任务 |
| Tool Runtime 成功率 | ≥ 98% | 10 分钟窗口 < 95%，且至少 20 次调用 |
| Agent Run 成功率 | ≥ 99% | 10 分钟窗口 < 95%，且至少 10 次 Run |
| 平均 Token/成功 Run | 监控项，暂不承诺 SLO | 15 分钟平均值 > 24000（单 Run 预算 30000 的 80%） |

告警规则已提供在 `deploy/prometheus/interviewagent_alerts.yml`。低流量条件用于减少小样本误报。成功率按终态统计；处于队列或执行中的任务不进入分母。

## 4. 配置与启动

`.env`：

```text
METRICS_ENABLED=true
METRICS_WORKER_PORT=9109
HEALTH_CHECK_TIMEOUT_SECONDS=2
READINESS_CACHE_SECONDS=5
```

```powershell
python -m uvicorn api.main:app --host 127.0.0.1 --port 8000
python -m celery -A tasks.celery_app.celery_app worker --loglevel=INFO --pool=solo
```

验证：

```powershell
curl.exe http://127.0.0.1:8000/health/live
curl.exe http://127.0.0.1:8000/health/startup
curl.exe http://127.0.0.1:8000/health/ready
curl.exe http://127.0.0.1:8000/metrics
curl.exe http://127.0.0.1:9109/metrics
```

## 5. 当前边界

- 指标是进程内累积值，进程重启后 Counter 清零；Prometheus 负责持久化与跨实例聚合。
- 当前 Worker 启动一个独立指标端口，适合现有单 Worker/`solo` 部署。扩展为同机多 Worker 时需要给每个 Worker 分配端口，或改为 Prometheus multiprocess 模式。
- Celery Worker 被硬终止且未进入任务异常处理时，不会立即产生 `failure` Counter；任务依靠 late ACK 重投，最终终态仍由 MySQL 任务表记录。生产上还应补充“任务长时间无进展”的数据库巡检。
- LLM Token 只统计 Provider 返回的 usage，不用字符数伪造 Token。
