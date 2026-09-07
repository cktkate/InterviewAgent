# 可审计 Tool Runtime

## 目标

大模型只负责提出结构化工具请求；确定性 Runtime 负责工具发现、参数校验、权限策略、真实执行和失败处理。这样可以避免模型直接拼接命令、URL 或绕过业务权限。

```text
Agent / MCP tool proposal
          ↓
Tool Registry + input schema
          ↓
Permission and risk policy
          ↓
Timeout / retry / rate limit / circuit breaker
          ↓
Local function or MCP server
          ↓
Structured ToolResult + OpenTelemetry span
```

## 已接入的真实工具

| 工具 | 风险 | 参数模型 | 当前调用位置 |
| --- | --- | --- | --- |
| `github.search_learning_repos` | read_only | `GitHubSearchArguments` | 面试复习计划、长期学习计划 |
| `github.analyze_repo` | read_only | `GitHubAnalyzeArguments` | 仓库元数据分析 |
| `web.fetch_public_page` | read_only | `WebFetchArguments` | JD 招聘页面抓取 |

GitHub 和网页抓取仍使用真实 HTTP API。原函数保留为底层适配器，Agent 默认调用链已经迁移到 Tool Runtime。

## 策略控制

每个 `ToolDefinition` 显式声明：

- 名称、描述和 Pydantic/JSON Schema
- `read_only`、`write_low` 或 `write_high` 风险等级
- 所需权限，例如 `external:read`、`external:write`
- 超时、重试次数、每分钟限流
- 熔断阈值和熔断时间
- 是否支持幂等键

只读工具在具备 `external:read` 权限时自动执行；高风险写工具除写权限外，还必须在 `ToolContext.approved_risks` 中获得显式批准。未标注安全属性的 MCP 工具默认按写工具处理，不会被当成只读工具自动运行。

## MCP 动态工具

`MCPClient.list_tools` 会保存服务端返回的名称、描述、`inputSchema` 和安全 annotations。调用前必须完成工具发现；`call_tool` 再把动态定义交给同一个 Executor：

1. 按 JSON Schema 校验 required、类型、长度、范围和额外字段。
2. 根据 `readOnlyHint`、`destructiveHint` 推导风险。
3. 校验调用者权限，高风险操作要求确认。
4. 使用异步超时和受限重试调用 MCP Server。

项目本地 `mcp` 包通过 namespace path 同时暴露官方 SDK，解决了本地包名遮蔽 `mcp.client.stdio` 的问题；真实 stdio SDK 类型已可加载。

## 可靠性

- 仅 `RetryableToolError` 或未分类的瞬时异常进入有限重试。
- `ToolExecutionFailure` 等确定性错误立即停止。
- 同一工具连续失败达到阈值后打开熔断器，避免持续拖慢面试。
- 滑动窗口限制每分钟调用量。
- 只对显式提供幂等键的幂等工具复用结果。
- Agent 接到失败的 `ToolResult` 后降级为空资源，不阻塞正式面试或计划生成。

## 可观测性

每次实际执行生成 `agent.tool.<tool_name>` OpenTelemetry span，记录：

- `tool_call_id`、工具名和风险等级
- 参数字段名和输入摘要哈希，不记录原始敏感参数
- 用户、会话、执行状态、尝试次数和耗时
- 失败代码和异常类型

业务 State 只保存 `ToolResult.summary()` 到 `tool_decisions`，并由 Agent Runtime 汇总工具调用数量。

## 使用示例

```python
from tool_runtime import ToolContext, tool_executor

result = tool_executor.execute(
    "github.search_learning_repos",
    {"topic": "RAG retrieval", "max_results": 3, "min_stars": 100},
    context=ToolContext(
        user_id="user-id",
        session_id="session-id",
        permissions=frozenset({"external:read"}),
    ),
)

repos = result.data if result.ok else []
```
