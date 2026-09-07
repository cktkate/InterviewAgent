"""Built-in tool definitions used by the interview application."""
from __future__ import annotations

from typing import Any, Dict, List

from pydantic import BaseModel, ConfigDict, Field

from tool_runtime.core import (
    RiskLevel,
    ToolDefinition,
    ToolExecutor,
    ToolRegistry,
)


class StrictArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class GitHubSearchArguments(StrictArguments):
    topic: str = Field(min_length=1, max_length=160)
    max_results: int = Field(default=3, ge=1, le=10)
    min_stars: int = Field(default=100, ge=0, le=1_000_000)


class GitHubAnalyzeArguments(StrictArguments):
    owner: str = Field(pattern=r"^[A-Za-z0-9_.-]{1,100}$")
    repo: str = Field(pattern=r"^[A-Za-z0-9_.-]{1,100}$")


class WebFetchArguments(StrictArguments):
    url: str = Field(min_length=8, max_length=2048)


class RagRetrieveArguments(StrictArguments):
    query: str = Field(min_length=2, max_length=300)
    top_k: int = Field(default=5, ge=1, le=10)
    requester_id: str = Field(default="", max_length=128)
    job_domains: List[str] = Field(default_factory=list, max_length=24)
    knowledge_points: List[str] = Field(default_factory=list, max_length=24)


def _github_search(topic: str, max_results: int, min_stars: int) -> List[Dict[str, Any]]:
    from mcp.tools.github_tool import search_learning_repos

    return search_learning_repos(
        topic,
        max_results=max_results,
        min_stars=min_stars,
        timeout=10.0,
        raise_on_error=True,
    )


def _github_analyze(owner: str, repo: str) -> Dict[str, Any]:
    from mcp.tools.github_tool import analyze_repo

    return analyze_repo(owner, repo, timeout=10.0, raise_on_error=True)


def _web_fetch(url: str) -> Dict[str, Any]:
    from mcp.tools.web_scraper import fetch_webpage

    return fetch_webpage(url, timeout=10.0)


def _rag_retrieve(
    query: str,
    top_k: int,
    requester_id: str,
    job_domains: List[str],
    knowledge_points: List[str],
) -> List[Dict[str, Any]]:
    """Run a policy-filtered internal retrieval action proposed by an Agent."""
    from rag.metadata_filter import RetrievalMetadataFilter
    from rag.query_engine import query_engine

    metadata_filter = RetrievalMetadataFilter(
        requester_id=requester_id,
        include_public=True,
        job_domains=tuple(job_domains),
        knowledge_points=tuple(knowledge_points),
    )
    return query_engine.full_query(
        query,
        top_k=top_k,
        metadata_filter=metadata_filter,
    )


tool_registry = ToolRegistry()
tool_registry.register(ToolDefinition(
    name="github.search_learning_repos",
    description="Search public GitHub repositories for verified learning resources.",
    handler=_github_search,
    argument_model=GitHubSearchArguments,
    risk_level=RiskLevel.READ_ONLY,
    required_permissions=frozenset({"external:read"}),
    timeout_seconds=12.0,
    max_retries=1,
    idempotent=True,
    rate_limit_per_minute=30,
    circuit_breaker_threshold=3,
))
tool_registry.register(ToolDefinition(
    name="rag.retrieve_interview_evidence",
    description=(
        "Retrieve additional interview evidence when the planning Agent identifies "
        "a groundedness or coverage gap."
    ),
    handler=_rag_retrieve,
    argument_model=RagRetrieveArguments,
    risk_level=RiskLevel.READ_ONLY,
    required_permissions=frozenset({"knowledge:read"}),
    timeout_seconds=15.0,
    max_retries=1,
    idempotent=True,
    rate_limit_per_minute=20,
    circuit_breaker_threshold=3,
))
tool_registry.register(ToolDefinition(
    name="github.analyze_repo",
    description="Read public metadata for one GitHub repository.",
    handler=_github_analyze,
    argument_model=GitHubAnalyzeArguments,
    risk_level=RiskLevel.READ_ONLY,
    required_permissions=frozenset({"external:read"}),
    timeout_seconds=12.0,
    max_retries=1,
    idempotent=True,
    rate_limit_per_minute=30,
    circuit_breaker_threshold=3,
))
tool_registry.register(ToolDefinition(
    name="web.fetch_public_page",
    description="Fetch readable text from one public HTTP(S) page with SSRF protection.",
    handler=_web_fetch,
    argument_model=WebFetchArguments,
    risk_level=RiskLevel.READ_ONLY,
    required_permissions=frozenset({"external:read"}),
    timeout_seconds=12.0,
    max_retries=1,
    idempotent=True,
    rate_limit_per_minute=20,
    circuit_breaker_threshold=3,
))

tool_executor = ToolExecutor(tool_registry)
