"""Metadata normalization and progressive filtering for RAG retrieval."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Sequence, Tuple


_TAG_TERMS = {
    "ai_agent": ("agent", "智能体", "langgraph", "mcp", "tool calling"),
    "rag": ("rag", "retrieval", "检索增强", "向量检索", "bm25", "rerank"),
    "llm": ("llm", "大模型", "transformer", "prompt", "微调", "推理"),
    "backend": ("fastapi", "redis", "mysql", "websocket", "celery", "后端"),
    "python": ("python", "pydantic", "asyncio"),
}


def _values(value: Any) -> List[str]:
    if value is None:
        return []
    items = value if isinstance(value, (list, tuple, set)) else [value]
    return list(dict.fromkeys(
        str(item).strip().casefold() for item in items if str(item).strip()
    ))


def infer_tags(*values: Any) -> List[str]:
    text = " ".join(str(value or "") for value in values).casefold()
    return [tag for tag, terms in _TAG_TERMS.items() if any(term in text for term in terms)]


def normalize_chunk_metadata(
    metadata: Mapping[str, Any] | None,
    *,
    text: str = "",
) -> Dict[str, Any]:
    """Return a stable metadata contract for both sparse and vector indexes."""
    normalized = dict(metadata or {})
    source = str(
        normalized.get("source_path")
        or normalized.get("file_path")
        or normalized.get("file_name")
        or normalized.get("source")
        or ""
    ).replace("\\", "/")
    explicit_domains = _values(
        normalized.get("job_domains") or normalized.get("job_domain")
    )
    explicit_points = _values(
        normalized.get("knowledge_points") or normalized.get("knowledge_point")
    )
    tech_stack = _values(normalized.get("tech_stack"))
    inferred = infer_tags(source, text, " ".join(tech_stack))
    visibility = str(normalized.get("visibility") or "public").strip().casefold()
    if visibility not in {"public", "private"}:
        visibility = "public"
    difficulty = str(normalized.get("difficulty") or "any").strip().casefold()
    if difficulty not in {"easy", "medium", "hard", "any"}:
        difficulty = "any"

    normalized.update({
        "source_path": source,
        "source_type": str(
            normalized.get("source_type") or Path(source).suffix.lstrip(".") or "text"
        ).casefold(),
        "visibility": visibility,
        "owner_id": str(normalized.get("owner_id") or ""),
        "job_domains": list(dict.fromkeys([*explicit_domains, *inferred])),
        "knowledge_points": list(dict.fromkeys([
            *explicit_points, *tech_stack, *inferred,
        ])),
        "question_types": _values(
            normalized.get("question_types") or normalized.get("question_type")
        ),
        "difficulty": difficulty,
        "tech_stack": tech_stack,
    })
    return normalized


@dataclass(frozen=True)
class RetrievalMetadataFilter:
    requester_id: str = ""
    include_public: bool = True
    job_domains: Tuple[str, ...] = ()
    knowledge_points: Tuple[str, ...] = ()
    difficulties: Tuple[str, ...] = ()
    question_types: Tuple[str, ...] = ()
    allow_legacy_public: bool = True

    @classmethod
    def from_interview_state(cls, state: Mapping[str, Any]) -> "RetrievalMetadataFilter":
        jd = state.get("jd_parsed") or {}
        raw_points = (
            [state.get("interview_topic", "")]
            + list(jd.get("tech_stack") or [])
            + list(jd.get("core_competencies") or [])
        )
        inferred_domains = infer_tags(
            state.get("interview_topic", ""),
            jd.get("title", ""),
            jd.get("industry", ""),
            " ".join(str(item) for item in raw_points),
        )
        points = list(dict.fromkeys([
            *_values(raw_points),
            *inferred_domains,
        ]))
        return cls(
            requester_id=str(state.get("user_id") or ""),
            include_public=True,
            job_domains=tuple(inferred_domains),
            knowledge_points=tuple(points[:24]),
        )

    def to_dict(self) -> Dict[str, Any]:
        value = asdict(self)
        return {key: list(item) if isinstance(item, tuple) else item for key, item in value.items()}


def security_matches(
    metadata: Mapping[str, Any], metadata_filter: RetrievalMetadataFilter
) -> bool:
    raw_visibility = metadata.get("visibility")
    if raw_visibility is None and metadata_filter.allow_legacy_public:
        return True
    visibility = str(raw_visibility or "private").casefold()
    if visibility == "public":
        return metadata_filter.include_public
    return bool(
        visibility == "private"
        and metadata_filter.requester_id
        and str(metadata.get("owner_id") or "") == metadata_filter.requester_id
    )


def _criterion_matches(actual: Iterable[str], expected: Sequence[str]) -> bool:
    expected_values = set(_values(expected))
    if not expected_values:
        return True
    actual_values = set(_values(actual))
    # Missing legacy metadata is unknown rather than irrelevant.
    return not actual_values or bool(actual_values & expected_values)


def soft_matches(
    metadata: Mapping[str, Any],
    metadata_filter: RetrievalMetadataFilter,
    *,
    include_shape: bool,
) -> bool:
    normalized = normalize_chunk_metadata(metadata)
    if not _criterion_matches(normalized.get("job_domains") or [], metadata_filter.job_domains):
        return False
    if not _criterion_matches(
        normalized.get("knowledge_points") or [], metadata_filter.knowledge_points
    ):
        return False
    if include_shape:
        difficulty = str(normalized.get("difficulty") or "any")
        if (
            metadata_filter.difficulties
            and difficulty != "any"
            and difficulty not in metadata_filter.difficulties
        ):
            return False
        if not _criterion_matches(
            normalized.get("question_types") or [], metadata_filter.question_types
        ):
            return False
    return True


def apply_security_filter(
    results: Iterable[Mapping[str, Any]],
    metadata_filter: RetrievalMetadataFilter | None,
) -> List[Dict[str, Any]]:
    items = [dict(item) for item in results]
    if metadata_filter is None:
        return items
    return [
        item for item in items
        if security_matches(item.get("metadata") or {}, metadata_filter)
    ]


def apply_progressive_filter(
    results: Iterable[Mapping[str, Any]],
    metadata_filter: RetrievalMetadataFilter | None,
    *,
    top_k: int,
) -> tuple[List[Dict[str, Any]], Dict[str, Any]]:
    items = apply_security_filter(results, metadata_filter)
    if metadata_filter is None:
        return items[:top_k], {"status": "disabled", "level": "none"}

    strict = [
        item for item in items
        if soft_matches(item.get("metadata") or {}, metadata_filter, include_shape=True)
    ]
    topic_only = [
        item for item in items
        if soft_matches(item.get("metadata") or {}, metadata_filter, include_shape=False)
    ]
    selected = list(strict)
    selected_keys = {
        (str(item.get("id") or ""), str(item.get("text") or "")) for item in selected
    }
    level = "strict"
    if len(selected) < top_k:
        for item in topic_only:
            key = (str(item.get("id") or ""), str(item.get("text") or ""))
            if key not in selected_keys:
                selected.append(item)
                selected_keys.add(key)
            if len(selected) >= top_k:
                break
        if len(selected) > len(strict):
            level = "topic_relaxed"
    if len(selected) < top_k:
        before_security_fill = len(selected)
        for item in items:
            key = (str(item.get("id") or ""), str(item.get("text") or ""))
            if key not in selected_keys:
                selected.append(item)
                selected_keys.add(key)
            if len(selected) >= top_k:
                break
        if len(selected) > before_security_fill:
            level = "security_only"
    return selected[:top_k], {
        "status": "applied",
        "level": level,
        "input_count": len(list(results)) if isinstance(results, list) else len(items),
        "security_count": len(items),
        "strict_count": len(strict),
        "topic_count": len(topic_only),
        "result_count": min(top_k, len(selected)),
    }
