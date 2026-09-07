"""
BM25 关键词检索：
- 使用 rank_bm25 库
- 支持中英文分词（jieba）
"""
from __future__ import annotations

import json
import pickle
import re
from pathlib import Path
from typing import Any, Dict, List

from loguru import logger
from rank_bm25 import BM25Okapi

from rag.metadata_filter import (
    RetrievalMetadataFilter,
    normalize_chunk_metadata,
    security_matches,
)

try:
    import jieba
except ImportError:
    jieba = None  # type: ignore

CACHE_PATH = Path("data/bm25_corpus.pkl")


def _tokenize(text: str) -> List[str]:
    if jieba is not None:
        return [t for t in jieba.cut(text) if t.strip()]
    # A whitespace split turns an entire Chinese sentence into one token and
    # makes BM25 effectively unusable when jieba is not installed.  Character
    # bigrams retain keyword overlap while keeping the fallback deterministic.
    tokens: List[str] = []
    for value in re.findall(r"[\u4e00-\u9fff]+|[a-zA-Z0-9_+.=-]+", text.casefold()):
        if "\u4e00" <= value[0] <= "\u9fff":
            tokens.extend(value if len(value) == 1 else (
                value[index:index + 2] for index in range(len(value) - 1)
            ))
        else:
            tokens.append(value)
    return tokens


class BM25Retriever:
    """BM25 关键词检索器"""

    def __init__(self) -> None:
        self.corpus: List[Dict[str, Any]] = []
        self.tokenized: List[List[str]] = []
        self.bm25: BM25Okapi | None = None
        self.last_filter_diagnostics: Dict[str, Any] = {}
        self._load_cache()

    @staticmethod
    def build_corpus_cache(nodes: List[Dict[str, Any]]) -> None:
        """构建 BM25 语料缓存"""
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(CACHE_PATH, "wb") as f:
            pickle.dump(nodes, f)
        logger.info("BM25 语料缓存已写入：{} 条", len(nodes))

    def _load_cache(self) -> None:
        if not CACHE_PATH.exists():
            logger.warning("BM25 缓存不存在，retriever 为空")
            return
        with open(CACHE_PATH, "rb") as f:
            self.corpus = pickle.load(f)
        for item in self.corpus:
            item["metadata"] = normalize_chunk_metadata(
                item.get("metadata") or {}, text=str(item.get("text") or "")
            )
        self.tokenized = [_tokenize(n["text"]) for n in self.corpus]
        self.bm25 = BM25Okapi(self.tokenized)
        logger.debug("BM25 加载 {} 条文档", len(self.corpus))

    def retrieve(
        self,
        query: str,
        top_k: int = 5,
        *,
        metadata_filter: RetrievalMetadataFilter | None = None,
    ) -> List[Dict[str, Any]]:
        if not self.bm25:
            return []
        tokens = _tokenize(query)
        scores = self.bm25.get_scores(tokens)
        ranked_idx = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)
        results = []
        for i in ranked_idx:
            if scores[i] <= 0:
                continue
            metadata = self.corpus[i].get("metadata", {})
            if metadata_filter and not security_matches(metadata, metadata_filter):
                continue
            results.append({
                "id": self.corpus[i]["id"],
                "text": self.corpus[i]["text"],
                "metadata": metadata,
                "score": float(scores[i]),
                "source": "bm25",
            })
            if len(results) >= top_k:
                break
        self.last_filter_diagnostics = {
            "status": "client" if metadata_filter else "disabled",
            "result_count": len(results),
        }
        return results
