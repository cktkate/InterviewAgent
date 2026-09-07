"""CLI for RAG retrieval and Agent trajectory offline evaluations."""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

from evals.agent_eval import (
    build_case_template,
    collect_bad_case_templates,
    load_agent_cases,
    load_trace_groups,
    run_agent_evaluation,
)
from evals.common import evaluate_gates, mean, percentile, write_json
from evals.rag_eval import load_rag_cases, run_rag_evaluation


def _load_json(path: str) -> Dict[str, Any]:
    if not path:
        return {}
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def _section(value: Dict[str, Any], name: str) -> Dict[str, Any]:
    section = value.get(name)
    return dict(section) if isinstance(section, dict) else value


def _finish_report(
    report: Dict[str, Any],
    *,
    output: str,
    threshold_path: str,
    baseline_path: str,
    section_name: str,
) -> int:
    thresholds = _section(_load_json(threshold_path), section_name) if threshold_path else {}
    baseline_report = _load_json(baseline_path) if baseline_path else {}
    baseline_metrics = dict(baseline_report.get("metrics") or {})
    report["generated_at"] = datetime.now(timezone.utc).isoformat()
    report["gate"] = evaluate_gates(
        report.get("metrics") or {}, thresholds, baseline_metrics
    )
    if baseline_path:
        report["baseline_path"] = baseline_path
    if threshold_path:
        report["threshold_path"] = threshold_path
    write_json(output, report)
    print(json.dumps({
        "evaluation": report["evaluation"],
        "metrics": report["metrics"],
        "gate": report["gate"],
        "report": output,
    }, ensure_ascii=False, indent=2))
    return 0 if report["gate"]["passed"] else 2


def run_rag(args: argparse.Namespace) -> int:
    from rag.query_engine import query_engine
    from rag.metadata_filter import RetrievalMetadataFilter

    cases = load_rag_cases(args.dataset)
    diagnostics_by_query: Dict[str, Dict[str, Any]] = {}

    def metadata_filter_for(query: str) -> RetrievalMetadataFilter | None:
        if not args.metadata_filter_from_tags:
            return None
        case = next((item for item in cases if item.query == query), None)
        if case is None:
            return None
        domain_map = {
            "agent": "ai_agent",
            "rag": "rag",
            "llm": "llm",
            "backend": "backend",
            "python": "python",
        }
        domains = tuple(dict.fromkeys(
            domain_map[tag] for tag in case.tags if tag in domain_map
        ))
        return RetrievalMetadataFilter(
            requester_id="offline-eval-user",
            include_public=True,
            job_domains=domains,
            knowledge_points=domains,
        )

    def retrieve(query: str, k: int):
        metadata_filter = metadata_filter_for(query)
        if args.mode == "full":
            results = query_engine.full_query(
                query, top_k=k, metadata_filter=metadata_filter
            )
        else:
            results = query_engine.hybrid_query(
                query, top_k=k, metadata_filter=metadata_filter
            )
        diagnostics_by_query[query] = dict(query_engine.last_diagnostics)
        return results

    report = run_rag_evaluation(
        cases,
        retrieve_fn=retrieve,
        top_k=args.top_k,
        mode=args.mode,
    )
    for case_result in report["cases"]:
        case_result["retrieval_diagnostics"] = diagnostics_by_query.get(
            case_result["query"], {}
        )

    def diagnostic_rate(path: tuple[str, ...], expected: str) -> float:
        if not cases:
            return 0.0
        matches = 0
        for case in cases:
            value: Any = diagnostics_by_query.get(case.query, {})
            for key in path:
                value = value.get(key, {}) if isinstance(value, dict) else {}
            if value == expected:
                matches += 1
        return matches / len(cases)

    def diagnostic_values(path: tuple[str, ...]) -> list[float]:
        values: list[float] = []
        for case in cases:
            value: Any = diagnostics_by_query.get(case.query, {})
            for key in path:
                value = value.get(key, {}) if isinstance(value, dict) else None
            if isinstance(value, (int, float)):
                values.append(float(value))
        return values

    report["metrics"].update({
        "vector_success_rate": round(
            diagnostic_rate(("vector", "status"), "success"), 6
        ),
        "bm25_success_rate": round(
            diagnostic_rate(("bm25", "status"), "success"), 6
        ),
        "metadata_filter_applied_rate": round(
            diagnostic_rate(("metadata_filter", "status"), "applied"), 6
        ),
        "rerank_applied_rate": round(
            diagnostic_rate(("rerank", "status"), "applied"), 6
        ),
        "rerank_skipped_high_confidence_rate": round(
            diagnostic_rate(("rerank", "status"), "skipped_high_confidence"), 6
        ),
        "rerank_skipped_latency_budget_rate": round(
            diagnostic_rate(("rerank", "status"), "skipped_latency_budget"), 6
        ),
        "rerank_fallback_rate": round(
            diagnostic_rate(("rerank", "status"), "fallback"), 6
        ),
        "average_rerank_confidence": round(
            mean(diagnostic_values(("rerank", "confidence", "score"))), 6
        ),
        "p95_rerank_latency_ms": round(
            percentile(diagnostic_values(("rerank", "latency_ms")), 0.95), 6
        ),
    })
    report["dataset"] = args.dataset
    report["metadata_filter_mode"] = (
        "tag_domains" if args.metadata_filter_from_tags else "disabled"
    )
    return _finish_report(
        report,
        output=args.output,
        threshold_path=args.thresholds,
        baseline_path=args.baseline,
        section_name="rag",
    )


def run_agent(args: argparse.Namespace) -> int:
    report = run_agent_evaluation(
        load_agent_cases(args.dataset),
        load_trace_groups(args.trace_file),
    )
    report["dataset"] = args.dataset
    report["trace_file"] = args.trace_file
    return _finish_report(
        report,
        output=args.output,
        threshold_path=args.thresholds,
        baseline_path=args.baseline,
        section_name="agent",
    )


def make_agent_template(args: argparse.Namespace) -> int:
    groups = load_trace_groups(args.trace_file)
    spans = groups.get(args.trace_id)
    if not spans:
        raise ValueError(f"trace_id not found: {args.trace_id}")
    template = build_case_template(args.trace_id, spans)
    line = json.dumps(template, ensure_ascii=False)
    if args.output:
        target = Path(args.output)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(line + "\n", encoding="utf-8")
        print(f"Agent case template written to {target}")
    else:
        print(line)
    return 0


def collect_agent_bad_cases(args: argparse.Namespace) -> int:
    templates = collect_bad_case_templates(
        load_trace_groups(args.trace_file),
        max_duration_ms=args.max_duration_ms,
        structured_retry_threshold=args.structured_retry_threshold,
        limit=args.limit,
    )
    target = Path(args.output)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in templates),
        encoding="utf-8",
    )
    print(json.dumps({
        "bad_case_count": len(templates),
        "output": str(target),
        "requires_human_review": True,
    }, ensure_ascii=False, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="InterviewAgent offline evaluation")
    subparsers = parser.add_subparsers(dest="command", required=True)

    rag = subparsers.add_parser("rag", help="evaluate RAG retrieval")
    rag.add_argument("--dataset", required=True)
    rag.add_argument("--mode", choices=("hybrid", "full"), default="hybrid")
    rag.add_argument("--top-k", type=int, default=5)
    rag.add_argument("--output", default="evals/reports/rag_latest.json")
    rag.add_argument("--thresholds", default="evals/thresholds.example.json")
    rag.add_argument("--baseline", default="")
    rag.add_argument(
        "--metadata-filter-from-tags",
        action="store_true",
        help="derive job-domain metadata filters from each case's agent/rag/llm tags",
    )
    rag.set_defaults(handler=run_rag)

    agent = subparsers.add_parser("agent", help="evaluate recorded Agent traces")
    agent.add_argument("--dataset", required=True)
    agent.add_argument("--trace-file", default="logs/agent_traces.jsonl")
    agent.add_argument("--output", default="evals/reports/agent_latest.json")
    agent.add_argument("--thresholds", default="evals/thresholds.example.json")
    agent.add_argument("--baseline", default="")
    agent.set_defaults(handler=run_agent)

    template = subparsers.add_parser(
        "agent-template", help="create a human-reviewable case from one trace"
    )
    template.add_argument("--trace-file", default="logs/agent_traces.jsonl")
    template.add_argument("--trace-id", required=True)
    template.add_argument("--output", default="")
    template.set_defaults(handler=make_agent_template)

    bad_cases = subparsers.add_parser(
        "agent-badcases", help="collect failed, slow or degraded traces for review"
    )
    bad_cases.add_argument("--trace-file", default="logs/agent_traces.jsonl")
    bad_cases.add_argument(
        "--output", default="evals/datasets/agent_bad_cases.jsonl"
    )
    bad_cases.add_argument("--max-duration-ms", type=float, default=30000.0)
    bad_cases.add_argument("--structured-retry-threshold", type=int, default=1)
    bad_cases.add_argument("--limit", type=int, default=100)
    bad_cases.set_defaults(handler=collect_agent_bad_cases)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        return int(args.handler(args))
    except Exception as exc:
        print(f"offline evaluation failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    finally:
        # The RAG CLI owns the process-level query engine. Close its remote
        # client on both successful reports and failed evaluation runs.
        query_module = sys.modules.get("rag.query_engine")
        engine = getattr(query_module, "query_engine", None)
        close = getattr(engine, "close", None)
        if callable(close):
            close()


if __name__ == "__main__":
    raise SystemExit(main())
