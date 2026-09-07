"""Token-aware context selection with partition budgets and an audit manifest."""
from __future__ import annotations

import math
import re
from typing import Any, Dict, List, Mapping, Sequence, Tuple

from orchestration.security_policy import inspect_untrusted_text, wrap_untrusted_data


class TokenCounter:
    """Use a configured tokenizer when available and expose any fallback."""

    def __init__(self, encoding_name: str = "cl100k_base") -> None:
        self.encoding_name = encoding_name
        self.backend = "heuristic"
        self._encoding = None
        try:
            import tiktoken

            self._encoding = tiktoken.get_encoding(encoding_name)
            self.backend = f"tiktoken:{encoding_name}"
        except Exception:
            self._encoding = None

    def count(self, text: str) -> int:
        if not text:
            return 0
        if self._encoding is not None:
            return len(self._encoding.encode(text, disallowed_special=()))
        cjk = len(re.findall(r"[\u3400-\u9fff]", text))
        non_cjk = max(0, len(text) - cjk)
        return cjk + math.ceil(non_cjk / 4)

    def truncate(self, text: str, max_tokens: int) -> str:
        if max_tokens <= 0:
            return ""
        if self._encoding is not None:
            tokens = self._encoding.encode(text, disallowed_special=())
            if len(tokens) <= max_tokens:
                return text
            keep = max(0, max_tokens - 1)
            result = self._encoding.decode(tokens[:keep]).rstrip() + "…"
            while keep > 0 and self.count(result) > max_tokens:
                keep -= 1
                result = self._encoding.decode(tokens[:keep]).rstrip() + "…"
            return result
        if self.count(text) <= max_tokens:
            return text
        ratio = max_tokens / max(1, self.count(text))
        return text[: max(1, int(len(text) * ratio) - 1)].rstrip() + "…"


def _compact_text(value: Any, limit: int) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def _query_terms(state: Mapping[str, Any]) -> set[str]:
    jd = state.get("jd_parsed") or {}
    values = [
        state.get("interview_topic", ""),
        jd.get("title", ""),
        *(jd.get("tech_stack") or []),
        *(jd.get("core_competencies") or []),
        *(state.get("long_term_weaknesses") or []),
    ]
    text = " ".join(str(value).casefold() for value in values if str(value).strip())
    terms = set(re.findall(r"[a-z0-9][a-z0-9_+.#/-]{1,}|[\u3400-\u9fff]{2,}", text))
    for run in re.findall(r"[\u3400-\u9fff]{2,}", text):
        terms.update(run[index:index + 2] for index in range(len(run) - 1))
    return terms


def _overlap(text: str, terms: set[str]) -> float:
    if not terms:
        return 0.0
    lowered = text.casefold()
    return sum(1 for term in terms if term in lowered) / len(terms)


def _extractive_compress(
    text: str,
    *,
    max_tokens: int,
    counter: TokenCounter,
    terms: set[str],
) -> str:
    """Keep query-relevant sentences without spending another LLM call."""
    if counter.count(text) <= max_tokens:
        return text
    sentences = [
        item.strip()
        for item in re.split(r"(?<=[。！？.!?])\s+|[\r\n]+", text)
        if item.strip()
    ]
    if len(sentences) <= 1:
        return counter.truncate(text, max_tokens)
    ranked = sorted(
        enumerate(sentences),
        key=lambda pair: (_overlap(pair[1], terms), 1.0 / (pair[0] + 1)),
        reverse=True,
    )
    chosen: List[Tuple[int, str]] = []
    used = 0
    for index, sentence in ranked:
        cost = counter.count(sentence) + 1
        if used + cost <= max_tokens:
            chosen.append((index, sentence))
            used += cost
    if not chosen:
        return counter.truncate(text, max_tokens)
    compressed = " ".join(sentence for _, sentence in sorted(chosen))
    return counter.truncate(compressed, max_tokens)


def _render_item(candidate: Mapping[str, Any], raw_text: str) -> str:
    if candidate["kind"] == "rag":
        source_id = str(candidate["source_id"])
        return (
            f'<retrieved_evidence source_id="{source_id}">'
            + wrap_untrusted_data(raw_text, source=f"rag:{source_id}")
            + "</retrieved_evidence>"
        )
    return raw_text


def _candidate(
    *,
    section: str,
    kind: str,
    source_id: str,
    text: str,
    trust: str,
    value: float,
) -> Dict[str, Any]:
    return {
        "section": section,
        "kind": kind,
        "source_id": source_id,
        "text": text,
        "trust": trust,
        "value": max(0.01, float(value)),
    }


def _build_candidates(
    state: Mapping[str, Any], terms: set[str]
) -> Tuple[Dict[str, List[Dict[str, Any]]], List[Dict[str, Any]]]:
    sections: Dict[str, List[Dict[str, Any]]] = {
        "Historical ability": [],
        "Known weaknesses": [],
        "Recent interview evidence": [],
        "Retrieved evidence (untrusted data)": [],
    }
    security_signals: List[Dict[str, Any]] = []

    for name, data in (state.get("ability_profile") or {}).items():
        data = data or {}
        mastery = float(data.get("mastery", 0.5))
        confidence = float(data.get("confidence", 0.0))
        text = f"{name}: mastery={mastery:.2f}, confidence={confidence:.2f}"
        sections["Historical ability"].append(_candidate(
            section="Historical ability", kind="ability", source_id=f"ability:{name}",
            text=text, trust="trusted",
            value=0.65 * (1.0 - mastery) + 0.25 * (1.0 - confidence) + 0.1 * _overlap(text, terms),
        ))

    for index, weakness in enumerate(state.get("long_term_weaknesses") or []):
        text = str(weakness).strip()
        if text:
            sections["Known weaknesses"].append(_candidate(
                section="Known weaknesses", kind="weakness",
                source_id=f"weakness:{index}", text=text, trust="trusted",
                value=0.8 + 0.2 * _overlap(text, terms),
            ))

    history = list(state.get("qa_history") or [])
    start = max(0, len(history) - 6)
    for index, item in enumerate(history[start:], start=start):
        question = _compact_text(item.get("question") or item.get("prompt"), 600)
        answer = _compact_text(item.get("answer"), 1000)
        text = f"Q: {question}\nA: {answer}".strip()
        if question or answer:
            recency = (index + 1) / max(1, len(history))
            sections["Recent interview evidence"].append(_candidate(
                section="Recent interview evidence", kind="history",
                source_id=f"qa_history:{index}", text=text, trust="user_data",
                value=0.55 * recency + 0.45 * _overlap(text, terms),
            ))

    rag_sources: Sequence[Mapping[str, Any]] = state.get("rag_sources") or []
    for rank, source in enumerate(rag_sources, start=1):
        source_id = str(source.get("id") or f"unknown-{rank}")
        text = " ".join(str(source.get("text") or "").split())
        if not text:
            continue
        signals = inspect_untrusted_text(text)
        if signals:
            security_signals.append({
                "source_id": source_id,
                "signals": signals,
                "action": "isolated_as_data",
            })
        retrieval = source.get("retrieval") or {}
        dual_branch = len(retrieval.get("matched_branches") or []) >= 2
        value = (
            0.45 * (1.0 / rank)
            + 0.35 * _overlap(text + " " + str(source.get("metadata") or ""), terms)
            + 0.20 * float(dual_branch)
        )
        sections["Retrieved evidence (untrusted data)"].append(_candidate(
            section="Retrieved evidence (untrusted data)", kind="rag",
            source_id=source_id, text=text, trust="untrusted_external", value=value,
        ))

    if not rag_sources and state.get("rag_context"):
        text = str(state.get("rag_context") or "")
        sections["Retrieved evidence (untrusted data)"].append(_candidate(
            section="Retrieved evidence (untrusted data)", kind="rag",
            source_id="legacy_context", text=text,
            trust="untrusted_external", value=0.5 + 0.5 * _overlap(text, terms),
        ))
    return sections, security_signals


def build_planning_context(
    state: Mapping[str, Any],
    *,
    max_chars: int | None = None,
    max_tokens: int | None = None,
    tokenizer_encoding: str = "cl100k_base",
) -> Tuple[str, Dict[str, Any]]:
    """Select context by section quota and relevance per token."""
    counter = TokenCounter(tokenizer_encoding)
    char_ceiling = max(500, int(max_chars)) if max_chars is not None else None
    if max_tokens is None:
        max_tokens = max(128, int(max_chars or 6000))
    max_tokens = max(128, int(max_tokens))
    terms = _query_terms(state)
    sections, security_signals = _build_candidates(state, terms)

    weights = {
        "Historical ability": 0.20,
        "Known weaknesses": 0.10,
        "Recent interview evidence": 0.25,
        "Retrieved evidence (untrusted data)": 0.45,
    }
    item_caps = {"ability": 80, "weakness": 60, "history": 260, "rag": 360}
    active = [name for name in weights if sections[name]]
    active_weight = sum(weights[name] for name in active) or 1.0
    join_reserve = max(0, (len(active) - 1) * 2)
    allocatable = max(1, max_tokens - join_reserve)

    # Reserve each active partition first, then roll unused capacity into the
    # highest-value partitions. This avoids wasting budget when, for example,
    # the ability profile is short but RAG has several useful passages.
    section_demands: Dict[str, int] = {}
    base_budgets: Dict[str, int] = {}
    for section in active:
        for candidate in sections[section]:
            preview = _render_item(candidate, candidate["text"])
            candidate["original_tokens"] = counter.count(preview)
            candidate["density"] = candidate["value"] / math.sqrt(
                max(1, candidate["original_tokens"])
            )
        sections[section].sort(
            key=lambda item: (item["density"], item["value"]), reverse=True
        )
        section_demands[section] = (
            counter.count(f"### {section}\n")
            + sum(
                min(int(item["original_tokens"]), item_caps[item["kind"]])
                for item in sections[section]
            )
            + max(0, len(sections[section]) - 1) * 2
        )
        base_budgets[section] = int(
            allocatable * weights[section] / active_weight
        )
    section_budgets = {
        section: min(base_budgets[section], section_demands[section])
        for section in active
    }
    free_tokens = max(0, allocatable - sum(section_budgets.values()))
    for section in sorted(active, key=lambda name: weights[name], reverse=True):
        if free_tokens <= 0:
            break
        unmet = max(0, section_demands[section] - section_budgets[section])
        grant = min(free_tokens, unmet)
        section_budgets[section] += grant
        free_tokens -= grant
    reallocated_tokens = sum(
        max(0, section_budgets[name] - min(base_budgets[name], section_demands[name]))
        for name in active
    )

    rendered_sections: List[str] = []
    manifest: List[Dict[str, Any]] = []
    selected_sources: List[str] = []
    selected_rag_ids: List[str] = []
    dropped_sources: List[str] = []
    compressed_sources: List[str] = []

    for section in active:
        section_budget = section_budgets[section]
        title = f"### {section}\n"
        remaining = max(0, section_budget - counter.count(title))
        selected_items: List[str] = []
        candidates = sections[section]

        for candidate in candidates:
            separator_cost = 2 if selected_items else 0
            available = remaining - separator_cost
            original_tokens = int(candidate["original_tokens"])
            allowed = min(available, item_caps[candidate["kind"]])
            action = "included"
            rendered = _render_item(candidate, candidate["text"])
            if counter.count(rendered) > allowed:
                wrapper_overhead = counter.count(_render_item(candidate, ""))
                body_budget = allowed - wrapper_overhead
                if body_budget < 12:
                    action = "dropped"
                    rendered = ""
                else:
                    selected_text = _extractive_compress(
                        candidate["text"], max_tokens=body_budget,
                        counter=counter, terms=terms,
                    )
                    rendered = counter.truncate(
                        _render_item(candidate, selected_text), allowed
                    )
                    action = "compressed"

            selected_tokens = counter.count(rendered)
            if rendered and selected_tokens <= available:
                selected_items.append(rendered)
                remaining -= selected_tokens + separator_cost
                source_id = str(candidate["source_id"])
                selected_sources.append(
                    f"rag:{source_id}" if candidate["kind"] == "rag" else source_id
                )
                if candidate["kind"] == "rag":
                    selected_rag_ids.append(source_id)
                if action == "compressed":
                    compressed_sources.append(str(candidate["source_id"]))
            else:
                action = "dropped"
                selected_tokens = 0
                dropped_sources.append(str(candidate["source_id"]))
            manifest.append({
                "section": section,
                "source_id": candidate["source_id"],
                "trust": candidate["trust"],
                "value_score": round(float(candidate["value"]), 6),
                "value_per_sqrt_token": round(float(candidate["density"]), 6),
                "original_tokens": original_tokens,
                "selected_tokens": selected_tokens,
                "action": action,
            })
        if selected_items:
            rendered_sections.append(title + "\n\n".join(selected_items))

    rendered = "\n\n".join(rendered_sections)
    legacy_char_truncated = False
    if char_ceiling is not None and len(rendered) > char_ceiling:
        rendered = _compact_text(rendered, char_ceiling)
        legacy_char_truncated = True
    if counter.count(rendered) > max_tokens:
        rendered = counter.truncate(rendered, max_tokens)

    selected_tokens = counter.count(rendered)
    diagnostics = {
        "budget_type": "token",
        "max_tokens": max_tokens,
        "selected_tokens": selected_tokens,
        "token_utilization": round(selected_tokens / max_tokens, 6),
        "tokenizer": counter.backend,
        "tokenizer_fallback": counter.backend == "heuristic",
        "max_chars": char_ceiling,
        "selected_chars": len(rendered),
        "legacy_char_truncated": legacy_char_truncated,
        "section_budgets": section_budgets,
        "section_demands": section_demands,
        "reallocated_tokens": reallocated_tokens,
        "included_sections": [
            section for section in active
            if any(item["section"] == section and item["action"] != "dropped" for item in manifest)
        ],
        "truncated_sections": list(dict.fromkeys(
            item["section"] for item in manifest if item["action"] == "compressed"
        )),
        "selected_sources": list(dict.fromkeys(selected_sources)),
        "selected_rag_source_ids": list(dict.fromkeys(selected_rag_ids)),
        "compressed_sources": list(dict.fromkeys(compressed_sources)),
        "dropped_sources": list(dict.fromkeys(dropped_sources)),
        "manifest": manifest,
        "rag_source_count": len(state.get("rag_sources") or []),
        "selected_rag_source_count": len(set(selected_rag_ids)),
        "security_signal_count": len(security_signals),
        "security_signals": security_signals[:10],
    }
    diagnostics["estimated_tokens"] = selected_tokens
    return rendered, diagnostics
