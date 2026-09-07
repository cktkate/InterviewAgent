"""Low-latency deterministic query normalization and terminology expansion."""
from __future__ import annotations

import re
from typing import Any, Dict, List


ALIASES = {
    "rag": "retrieval augmented generation 检索增强生成",
    "llm": "large language model 大语言模型",
    "mcp": "model context protocol 模型上下文协议",
    "stt": "speech to text 语音识别",
    "tts": "text to speech 语音合成",
    "irt": "item response theory 项目反应理论",
    "bm25": "关键词检索 sparse retrieval",
    "rerank": "重排序 精排",
    "agent": "智能体 agentic",
}


def enhance_query(query: str, *, enabled: bool = True) -> tuple[str, Dict[str, Any]]:
    original = " ".join(str(query or "").split())
    if not enabled or not original:
        return original, {"status": "disabled" if not enabled else "empty", "added_terms": []}

    lowered_terms = {
        token.lower()
        for token in re.findall(r"[A-Za-z][A-Za-z0-9_-]*", original)
    }
    added: List[str] = []
    for alias, expansion in ALIASES.items():
        if alias in lowered_terms:
            added.extend(expansion.split())
    added = list(dict.fromkeys(term for term in added if term.lower() not in lowered_terms))
    enhanced = original if not added else f"{original} {' '.join(added)}"
    return enhanced, {
        "status": "expanded" if added else "unchanged",
        "added_terms": added,
        "original_chars": len(original),
        "enhanced_chars": len(enhanced),
    }
