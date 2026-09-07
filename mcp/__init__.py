"""Project MCP integration plus the installed official MCP SDK namespace.

The project historically used the same top-level package name as the SDK.
Extending the package path keeps backward-compatible imports such as
``mcp.tools.github_tool`` while allowing real SDK modules like
``mcp.client.stdio`` to resolve from site-packages.
"""
from pkgutil import extend_path

__path__ = extend_path(__path__, __name__)

try:
    from mcp.client.session import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client
except ImportError:  # pragma: no cover - optional dependency fallback
    ClientSession = None  # type: ignore
    StdioServerParameters = None  # type: ignore
    stdio_client = None  # type: ignore

from mcp.mcp_client import MCPClient

__all__ = [
    "ClientSession",
    "MCPClient",
    "StdioServerParameters",
    "stdio_client",
]
