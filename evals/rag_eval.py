from __future__ import annotations

import math
import time
import re
import unicodedata
from typing import Any, Callable, Dict, Iterable, List, Mapping, Sequence

from pydantic import BaseModel, ConfigDict, Field, model_validator

from evals.common import mean, percentile, read_jsonl


class EvidenceLabel(BaseModel):
    """Stable passage label that survives chunk ID/index rebuilds."""

    model_config = ConfigDict(extra="forbid")

    evidence_id: str = Field(min_length=1, max_length=120)
    source: str = Field(default="", max_length=500)
    all_terms: List[str] = Field(default_factory=list)
    any_terms: List[str] = Field(default_factory=list)
    grade: float = Field(default=1.0, gt=0.0, le=10.0)

    @model_validator(mode="after")
    def require_terms(self):
        if not (self.all_terms or self.any_terms):
            raise ValueError("evidence label requires all_terms or any_terms")
        return self


class RAGCase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    case_id: str = Field(min_length=1, max_length=120)
    query: str = Field(min_length=1, max_length=1000)
    relevant_ids: List[str] = Field(default_factory=list)
    relevant_sources: List[str] = Field(default_factory=list)
    relevance_grades: Dict[str, float] = Field(default_factory=dict)
    relevant_evidence: List[EvidenceLabel] = Field(default_factory=list)
    top_k: int | None = Field(default=None, ge=1, le=100)
    tags: List[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def require_labels(self):
        if not (
            self.relevant_ids
            or self.relevant_sources
            or self.relevance_grades
            or self.relevant_evidence
        ):
            raise ValueError("at least one relevance label is required")
        return self


def _source_keys(result: Mapping[str, Any]) -> set[str]:
    metadata = dict(result.get("metadata") or {})
    values: set[str] = set()
    for key in ("source_path", "source", "file_path", "file_name"):
        value = str(metadata.get(key) or "").replace("\\", "/").strip()
        if value:
            values.add(value)
            values.add(value.rsplit("/", 1)[-1])
    return values


def _normalise_text(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return re.sub(r"\s+", " ", text).strip()


def _evidence_matches(result: Mapping[str, Any], label: EvidenceLabel) -> bool:
    if label.source:
        expected = label.source.replace("\\", "/").strip()
        expected_keys = {expected, expected.rsplit("/", 1)[-1]}
        if not (expected_keys & _source_keys(result)):
            return False
    text = _normalise_text(result.get("text"))
    all_terms = [_normalise_text(term) for term in label.all_terms if term.strip()]
    any_terms = [_normalise_text(term) for term in label.any_terms if term.strip()]
    return all(term in text for term in all_terms) and (
        not any_terms or any(term in text for term in any_terms)
    )


def _matches_for_result(result: Mapping[str, Any], case: RAGCase) -> Dict[str, float]:
    result_id = str(result.get("id") or "")
    expected_sources: Dict[str, str] = {}
    for raw in case.relevant_sources:
        value = raw.replace("\\", "/").strip()
        if value:
            canonical = value.rsplit("/", 1)[-1]
            expected_sources[value] = canonical
            expected_sources[canonical] = canonical
    source_matches = set(expected_sources) & _source_keys(result)
    if source_matches:
        canonical = expected_sources[sorted(source_matches)[0]]
        matches_by_unit = {f"source:{canonical}": 1.0}
    else:
        matches_by_unit = {}
    if result_id in case.relevance_grades:
        matches_by_unit[f"id:{result_id}"] = max(
            0.0, float(case.relevance_grades[result_id])
        )
    if result_id in set(case.relevant_ids):
        matches_by_unit.setdefault(f"id:{result_id}", 1.0)
    for label in case.relevant_evidence:
        if _evidence_matches(result, label):
            matches_by_unit[f"evidence:{label.evidence_id}"] = label.grade
    return matches_by_unit


def _labeled_units(case: RAGCase) -> Dict[str, float]:
    units: Dict[str, float] = {f"id:{item}": 1.0 for item in case.relevant_ids}
    units.update({
        f"id:{item}": max(0.0, float(grade))
        for item, grade in case.relevance_grades.items()
    })
    for source in case.relevant_sources:
        canonical = source.replace("\\", "/").strip().rsplit("/", 1)[-1]
        if canonical:
            units[f"source:{canonical}"] = 1.0
    units.update({
        f"evidence:{label.evidence_id}": label.grade
        for label in case.relevant_evidence
    })
    return units


def _rank_case(
    results: Sequence[Mapping[str, Any]], case: RAGCase, k: int
) -> tuple[Dict[str, float], Dict[str, Any]]:
    top_results = list(results[:k])
    labeled_units = _labeled_units(case)
    seen_units: set[str] = set()
    grades: List[float] = []
    matched_ranks: Dict[str, int] = {}
    for rank, result in enumerate(top_results, start=1):
        result_matches = _matches_for_result(result, case)
        new_matches = {
            unit: grade
            for unit, grade in result_matches.items()
            if unit in labeled_units and unit not in seen_units and grade > 0
        }
        grades.append(max(new_matches.values(), default=0.0))
        for unit in new_matches:
            seen_units.add(unit)
            matched_ranks[unit] = rank
    relevant_total = sum(1 for grade in labeled_units.values() if grade > 0)
    relevant_total = max(1, relevant_total)
    binary = [1.0 if grade > 0 else 0.0 for grade in grades]
    hits = sum(binary)
    reciprocal_rank = next(
        (1.0 / rank for rank, value in enumerate(binary, start=1) if value),
        0.0,
    )
    dcg = sum(
        (2.0 ** grade - 1.0) / math.log2(rank + 1)
        for rank, grade in enumerate(grades, start=1)
    )
    ideal_labels = list(labeled_units.values())
    ideal = sorted((max(0.0, float(item)) for item in ideal_labels), reverse=True)[:k]
    idcg = sum(
        (2.0 ** grade - 1.0) / math.log2(rank + 1)
        for rank, grade in enumerate(ideal, start=1)
    )
    metrics = {
        "precision_at_k": hits / max(1, k),
        "recall_at_k": min(1.0, len(seen_units) / relevant_total),
        "hit_rate": 1.0 if hits else 0.0,
        "mrr": reciprocal_rank,
        "ndcg_at_k": dcg / idcg if idcg else 0.0,
    }
    return metrics, {
        "matched_units": sorted(seen_units),
        "missed_units": sorted(set(labeled_units) - seen_units),
        "matched_ranks": matched_ranks,
        "first_relevant_rank": min(matched_ranks.values(), default=0),
    }


def ranking_metrics(results: Sequence[Mapping[str, Any]], case: RAGCase, k: int) -> Dict[str, float]:
    metrics, _ = _rank_case(results, case, k)
    return metrics


def load_rag_cases(path: str) -> List[RAGCase]:
    return [RAGCase.model_validate(row) for row in read_jsonl(path)]


def run_rag_evaluation(
    cases: Iterable[RAGCase],
    *,
    retrieve_fn: Callable[[str, int], Sequence[Mapping[str, Any]]],
    top_k: int = 5,
    mode: str = "hybrid",
) -> Dict[str, Any]:
    case_results: List[Dict[str, Any]] = []
    latencies: List[float] = []
    for case in cases:
        effective_k = case.top_k or top_k
        started = time.perf_counter()
        error = ""
        try:
            results = list(retrieve_fn(case.query, effective_k) or [])
        except Exception as exc:
            results = []
            error = f"{type(exc).__name__}: {str(exc)[:200]}"
        latency_ms = (time.perf_counter() - started) * 1000
        latencies.append(latency_ms)
        metrics, match_details = _rank_case(results, case, effective_k)
        case_results.append({
            "case_id": case.case_id,
            "query": case.query,
            "status": "failed" if error else "success",
            "error": error,
            "top_k": effective_k,
            "tags": case.tags,
            "retrieved_ids": [str(item.get("id") or "") for item in results[:effective_k]],
            "latency_ms": round(latency_ms, 3),
            **match_details,
            **{key: round(value, 6) for key, value in metrics.items()},
        })

    total = len(case_results)
    metrics = {
        "case_count": total,
        "retrieval_success_rate": mean(
            1.0 if item["status"] == "success" else 0.0 for item in case_results
        ),
        "empty_result_rate": mean(
            1.0 if not item["retrieved_ids"] else 0.0 for item in case_results
        ),
        "precision_at_k": mean(item["precision_at_k"] for item in case_results),
        "recall_at_k": mean(item["recall_at_k"] for item in case_results),
        "hit_rate": mean(item["hit_rate"] for item in case_results),
        "mrr": mean(item["mrr"] for item in case_results),
        "ndcg_at_k": mean(item["ndcg_at_k"] for item in case_results),
        "p50_latency_ms": percentile(latencies, 0.50),
        "p95_latency_ms": percentile(latencies, 0.95),
    }
    tag_metrics: Dict[str, Dict[str, Any]] = {}
    for tag in sorted({tag for item in case_results for tag in item["tags"]}):
        tagged = [item for item in case_results if tag in item["tags"]]
        tag_metrics[tag] = {
            "case_count": len(tagged),
            "recall_at_k": round(mean(item["recall_at_k"] for item in tagged), 6),
            "hit_rate": round(mean(item["hit_rate"] for item in tagged), 6),
            "mrr": round(mean(item["mrr"] for item in tagged), 6),
            "ndcg_at_k": round(mean(item["ndcg_at_k"] for item in tagged), 6),
        }
    return {
        "evaluation": "rag_retrieval",
        "mode": mode,
        "top_k": top_k,
        "metrics": {key: round(value, 6) if isinstance(value, float) else value for key, value in metrics.items()},
        "metrics_by_tag": tag_metrics,
        "cases": case_results,
    }
