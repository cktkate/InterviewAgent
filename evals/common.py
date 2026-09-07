from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping


def read_jsonl(path: str | Path) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    with Path(path).open("r", encoding="utf-8") as stream:
        for line_number, raw in enumerate(stream, start=1):
            line = raw.strip()
            if not line:
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid JSONL at line {line_number}: {exc}") from exc
            if not isinstance(value, dict):
                raise ValueError(f"JSONL line {line_number} must be an object")
            rows.append(value)
    return rows


def write_json(path: str | Path, value: Mapping[str, Any]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(value, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def mean(values: Iterable[float]) -> float:
    items = list(values)
    return sum(items) / len(items) if items else 0.0


def percentile(values: Iterable[float], quantile: float) -> float:
    items = sorted(float(item) for item in values)
    if not items:
        return 0.0
    if len(items) == 1:
        return items[0]
    position = max(0.0, min(1.0, quantile)) * (len(items) - 1)
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return items[lower]
    fraction = position - lower
    return items[lower] * (1.0 - fraction) + items[upper] * fraction


def evaluate_gates(
    metrics: Mapping[str, Any],
    thresholds: Mapping[str, Any] | None = None,
    baseline: Mapping[str, Any] | None = None,
) -> Dict[str, Any]:
    """Apply absolute and baseline-relative regression gates."""
    thresholds = dict(thresholds or {})
    baseline = dict(baseline or {})
    violations: List[Dict[str, Any]] = []

    for metric, minimum in dict(thresholds.get("min") or {}).items():
        actual = float(metrics.get(metric, 0.0))
        if actual < float(minimum):
            violations.append({
                "metric": metric, "rule": "min", "expected": minimum, "actual": actual,
            })
    for metric, maximum in dict(thresholds.get("max") or {}).items():
        actual = float(metrics.get(metric, 0.0))
        if actual > float(maximum):
            violations.append({
                "metric": metric, "rule": "max", "expected": maximum, "actual": actual,
            })
    for metric, allowed_drop in dict(thresholds.get("max_drop") or {}).items():
        if metric not in baseline:
            continue
        actual = float(metrics.get(metric, 0.0))
        floor = float(baseline[metric]) - float(allowed_drop)
        if actual < floor:
            violations.append({
                "metric": metric, "rule": "max_drop", "expected": floor,
                "actual": actual, "baseline": baseline[metric],
            })
    for metric, allowed_increase in dict(thresholds.get("max_increase") or {}).items():
        if metric not in baseline:
            continue
        actual = float(metrics.get(metric, 0.0))
        ceiling = float(baseline[metric]) + float(allowed_increase)
        if actual > ceiling:
            violations.append({
                "metric": metric, "rule": "max_increase", "expected": ceiling,
                "actual": actual, "baseline": baseline[metric],
            })

    return {"passed": not violations, "violations": violations}

