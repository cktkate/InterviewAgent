"""编排层公共入口。

Graph imports every agent node, so importing it eagerly here creates a cycle
when an agent imports a lightweight orchestration helper.  Keep the public API
but resolve graph functions only when callers actually request them.
"""
from __future__ import annotations

from typing import Any


__all__ = ["build_interview_graph", "get_compiled_graph"]


def __getattr__(name: str) -> Any:
    if name in __all__:
        from orchestration.graph import build_interview_graph, get_compiled_graph

        exports = {
            "build_interview_graph": build_interview_graph,
            "get_compiled_graph": get_compiled_graph,
        }
        return exports[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
