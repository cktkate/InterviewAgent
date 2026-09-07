"""Per-run execution budget shared by the graph, agents and tool-like LLM calls."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
import time
from typing import Dict, Iterator, Optional


class AgentBudgetExceeded(RuntimeError):
    """Raised before an operation would exceed a configured run budget."""

    def __init__(self, dimension: str, limit: float) -> None:
        self.dimension = dimension
        self.limit = limit
        super().__init__(f"agent run budget exceeded: {dimension} limit={limit}")


@dataclass
class RuntimeBudget:
    max_llm_calls: int
    max_total_tokens: int
    deadline_seconds: float
    max_tool_calls: int = 8


@dataclass
class RuntimeUsage:
    budget: RuntimeBudget
    started: float = field(default_factory=time.monotonic)
    llm_calls: int = 0
    tool_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    structured_retries: int = 0
    violations: list[str] = field(default_factory=list)

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    @property
    def elapsed_seconds(self) -> float:
        return max(0.0, time.monotonic() - self.started)

    def check_deadline(self) -> None:
        if self.elapsed_seconds > self.budget.deadline_seconds:
            self.violations.append("deadline_seconds")
            raise AgentBudgetExceeded("deadline_seconds", self.budget.deadline_seconds)

    def before_llm_call(self) -> None:
        self.check_deadline()
        if self.llm_calls >= self.budget.max_llm_calls:
            self.violations.append("llm_calls")
            raise AgentBudgetExceeded("llm_calls", self.budget.max_llm_calls)
        self.llm_calls += 1

    def before_tool_call(self) -> None:
        self.check_deadline()
        if self.tool_calls >= self.budget.max_tool_calls:
            self.violations.append("tool_calls")
            raise AgentBudgetExceeded("tool_calls", self.budget.max_tool_calls)
        self.tool_calls += 1

    def record_tokens(self, input_tokens: int = 0, output_tokens: int = 0) -> None:
        self.input_tokens += max(0, int(input_tokens or 0))
        self.output_tokens += max(0, int(output_tokens or 0))
        if self.total_tokens > self.budget.max_total_tokens:
            self.violations.append("total_tokens")
            raise AgentBudgetExceeded("total_tokens", self.budget.max_total_tokens)

    def snapshot(self) -> Dict[str, object]:
        return {
            "llm_calls": self.llm_calls,
            "tool_calls": self.tool_calls,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "structured_retries": self.structured_retries,
            "budget_exceeded": bool(self.violations),
            "budget_dimension": self.violations[-1] if self.violations else "",
            "elapsed_ms": round(self.elapsed_seconds * 1000.0, 3),
            "limits": {
                "max_llm_calls": self.budget.max_llm_calls,
                "max_tool_calls": self.budget.max_tool_calls,
                "max_total_tokens": self.budget.max_total_tokens,
                "deadline_seconds": self.budget.deadline_seconds,
            },
        }


_CURRENT_USAGE: ContextVar[Optional[RuntimeUsage]] = ContextVar(
    "agent_runtime_usage", default=None
)


@contextmanager
def runtime_budget(budget: RuntimeBudget) -> Iterator[RuntimeUsage]:
    usage = RuntimeUsage(budget=budget)
    token = _CURRENT_USAGE.set(usage)
    try:
        yield usage
    finally:
        _CURRENT_USAGE.reset(token)


def current_usage() -> Optional[RuntimeUsage]:
    return _CURRENT_USAGE.get()


def before_llm_call() -> None:
    usage = current_usage()
    if usage is not None:
        usage.before_llm_call()


def before_tool_call() -> None:
    usage = current_usage()
    if usage is not None:
        usage.before_tool_call()


def record_llm_tokens(input_tokens: int = 0, output_tokens: int = 0) -> None:
    usage = current_usage()
    if usage is not None:
        usage.record_tokens(input_tokens, output_tokens)


def record_structured_retry() -> None:
    usage = current_usage()
    if usage is not None:
        usage.structured_retries += 1
