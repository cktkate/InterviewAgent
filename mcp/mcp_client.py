"""
MCP Client 实现：通过 Model Context Protocol 标准协议接入外部工具
支持连接本地 stdio 服务或 HTTP 服务。
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Any, AsyncGenerator, Dict, List

from loguru import logger
from tool_runtime import (
    RiskLevel,
    ToolContext,
    ToolDefinition,
    tool_executor,
)

try:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
except ImportError:
    ClientSession = None  # type: ignore
    StdioServerParameters = None  # type: ignore
    stdio_client = None  # type: ignore


class MCPClient:
    """MCP 客户端封装"""

    def __init__(self) -> None:
        self.session: ClientSession | None = None  # type: ignore
        self._ctx = None
        self._tool_specs: Dict[str, Dict[str, Any]] = {}

    @asynccontextmanager
    async def connect_stdio(
        self, command: str, args: List[str], env: Dict[str, str] | None = None
    ) -> AsyncGenerator[ClientSession, None]:  # type: ignore
        """连接本地 stdio MCP 服务"""
        if StdioServerParameters is None:
            raise RuntimeError("mcp SDK 未安装")
        params = StdioServerParameters(command=command, args=args, env=env)
        async with stdio_client(params) as (read, write):  # type: ignore
            async with ClientSession(read, write) as session:  # type: ignore
                await session.initialize()
                logger.info("MCP stdio 会话已建立")
                yield session

    async def list_tools(self, session: ClientSession) -> List[Dict[str, Any]]:  # type: ignore
        """列出可用工具"""
        result = await session.list_tools()  # type: ignore
        tools = []
        for item in result.tools:
            annotations = getattr(item, "annotations", None)
            read_only = bool(
                getattr(annotations, "readOnlyHint", False)
                if annotations is not None else False
            )
            destructive = bool(
                getattr(annotations, "destructiveHint", False)
                if annotations is not None else False
            )
            if destructive:
                risk = RiskLevel.WRITE_HIGH
            elif read_only:
                risk = RiskLevel.READ_ONLY
            else:
                # MCP tools without safety annotations are never assumed safe.
                risk = RiskLevel.WRITE_LOW
            schema = dict(getattr(item, "inputSchema", None) or {"type": "object"})
            spec = {
                "name": item.name,
                "description": item.description or "",
                "input_schema": schema,
                "risk_level": risk.value,
                "idempotent": read_only,
            }
            self._tool_specs[item.name] = spec
            tools.append(spec)
        return tools

    async def call_tool(
        self,
        session: ClientSession,
        tool_name: str,
        arguments: Dict[str, Any],
        *,
        context: ToolContext | None = None,
    ) -> Any:
        """Validate and execute one previously discovered MCP tool."""
        spec = self._tool_specs.get(tool_name)
        if spec is None:
            raise RuntimeError("MCP tool must be discovered with list_tools before use")

        risk = RiskLevel(spec["risk_level"])
        permission = "external:read" if risk == RiskLevel.READ_ONLY else "external:write"

        async def invoke_mcp(**validated_arguments: Any) -> Any:
            result = await session.call_tool(tool_name, validated_arguments)  # type: ignore
            return result.content

        definition = ToolDefinition(
            name=f"mcp.{tool_name}",
            description=spec["description"],
            handler=invoke_mcp,
            input_schema=spec["input_schema"],
            risk_level=risk,
            required_permissions=frozenset({permission}),
            timeout_seconds=15.0,
            max_retries=1 if risk == RiskLevel.READ_ONLY else 0,
            idempotent=bool(spec["idempotent"]),
            rate_limit_per_minute=30,
        )
        effective_context = context or ToolContext.system_readonly()
        result = await tool_executor.aexecute(
            definition.name,
            arguments,
            context=effective_context,
            definition_override=definition,
        )
        if not result.ok:
            raise RuntimeError(
                f"MCP tool rejected or failed: {result.error_code}: {result.error_message}"
            )
        return result.data


# 全局便捷函数
def run_async(coro):
    """同步包装器：在已有 event loop 下创建新 loop 运行"""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop and loop.is_running():
        import concurrent.futures

        with concurrent.futures.ThreadPoolExecutor(1) as pool:
            return pool.submit(asyncio.run, coro).result()
    return asyncio.run(coro)
