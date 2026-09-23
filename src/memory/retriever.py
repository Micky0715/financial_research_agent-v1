"""Memory retrieval.

Similarity is computed with the project's existing local embedding model when
it is available, and falls back to token-overlap otherwise - the same
degradation contract the rest of the repo uses. Retrieval never depends on a
network service.

The important behaviour is not the similarity function but the floor: when
nothing scores above `RetrievalPolicy.min_score`, retrieval returns **nothing**.
Injecting the least-bad match is how memory starts adding tokens and errors
instead of value.
"""
from __future__ import annotations

import math
import re
from typing import Any, Optional

from src.memory.policies import RetrievalPolicy
from src.memory.schemas import MemoryKind, MemoryRecord, RetrievalResult
from src.memory.stores.sqlite_store import SqliteMemoryStore
from src.runtime.budget import estimate_tokens

_TOKEN_RE = re.compile(r"[A-Za-z0-9]+|[一-鿿]")


def _tokens(text: str) -> list[str]:
    return _TOKEN_RE.findall((text or "").lower())


def keyword_similarity(query: str, text: str) -> float:
    """Dice coefficient over unigrams plus CJK bigrams.

    Bigrams matter for Chinese: unigram overlap alone rates "比亚迪财务" and
    "宁德时代财务" as highly similar because they share 财 and 务.
    """
    query_tokens, text_tokens = _tokens(query), _tokens(text)
    if not query_tokens or not text_tokens:
        return 0.0

    def grams(tokens: list[str]) -> set[str]:
        out = set(tokens)
        out.update(a + b for a, b in zip(tokens, tokens[1:]))
        return out

    a, b = grams(query_tokens), grams(text_tokens)
    intersection = len(a & b)
    return round(2 * intersection / (len(a) + len(b)), 4) if (a or b) else 0.0


class _Embedder:
    """Lazy wrapper over the repo's local embedding model, with a hard fallback."""

    def __init__(self) -> None:
        self._model: Any = None
        self._tried = False
        self._available = False

    def _load(self) -> None:
        if self._tried:
            return
        self._tried = True
        try:
            from tools.semantic_scorer import _load_model

            self._model = _load_model()
            self._available = self._model is not None
        except Exception:  # noqa: BLE001 - embedding is an optimisation, never a dependency
            self._model = None
            self._available = False

    def similarity(self, query: str, texts: list[str]) -> Optional[list[float]]:
        self._load()
        if not self._available or not texts:
            return None
        try:
            import numpy as np

            vectors = np.asarray(self._model.encode([query] + texts, normalize_embeddings=True))
            query_vector, doc_vectors = vectors[0], vectors[1:]
            return [float(max(0.0, min(1.0, float(v @ query_vector)))) for v in doc_vectors]
        except Exception:  # noqa: BLE001
            return None


class MemoryRetriever:
    """Scores and selects memories for injection."""

    def __init__(self, store: SqliteMemoryStore, *, policy: Optional[RetrievalPolicy] = None,
                 use_embeddings: bool = True) -> None:
        self.store = store
        self.policy = policy or RetrievalPolicy()
        self._embedder = _Embedder() if use_embeddings else None
        self.last_similarity_mode = "none"

    # ------------------------------------------------------------------ #
    def retrieve(self, *, namespace: str, query: str, subject: str = "",
                 kinds: Optional[list[MemoryKind]] = None,
                 limit: Optional[int] = None) -> list[RetrievalResult]:
        """Return the memories worth injecting, best first. May be empty."""
        candidates: list[MemoryRecord] = []
        for kind in (kinds or [MemoryKind.SEMANTIC, MemoryKind.EPISODIC, MemoryKind.PROCEDURAL]):
            candidates.extend(self.store.list_active(namespace, kind=kind, subject=subject))

        seen: set[str] = set()
        unique: list[MemoryRecord] = []
        for record in candidates:
            if record.memory_id in seen:
                continue
            seen.add(record.memory_id)
            if self.policy.drop_expired and record.is_expired():
                continue
            unique.append(record)
        if not unique:
            self.last_similarity_mode = "none"
            return []

        texts = [f"{r.key} {r.content}" for r in unique]
        similarities = self._embedder.similarity(query, texts) if self._embedder else None
        if similarities is None:
            similarities = [keyword_similarity(query, text) for text in texts]
            self.last_similarity_mode = "keyword"
        else:
            self.last_similarity_mode = "embedding"

        scored: list[RetrievalResult] = []
        for record, similarity in zip(unique, similarities):
            # Relevance is a gate, not a weighted vote: an off-topic memory must
            # not ride into the context on importance and confidence alone.
            if similarity < self.policy.min_similarity:
                continue
            score, components = self.policy.score(similarity, record, record.age_days())
            if score >= self.policy.min_score:
                scored.append(RetrievalResult(record=record, score=score, components=components))

        scored.sort(key=lambda r: r.score, reverse=True)
        return self._apply_budget(scored, limit or self.policy.max_items)

    def _apply_budget(self, results: list[RetrievalResult], limit: int) -> list[RetrievalResult]:
        """Enforce both the item cap and the independent memory token budget."""
        selected: list[RetrievalResult] = []
        tokens = 0
        for result in results[:limit]:
            cost = estimate_tokens(result.record.content)
            if tokens + cost > self.policy.max_tokens:
                break
            selected.append(result)
            tokens += cost
        return selected

    # ------------------------------------------------------------------ #
    def retrieve_for_injection(self, **kwargs: Any) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """Retrieval plus the diagnostics that go into the trace."""
        results = self.retrieve(**kwargs)
        items = [r.to_injection() for r in results]
        diagnostics = {
            "retrieved": len(items),
            "similarity_mode": self.last_similarity_mode,
            "tokens": sum(estimate_tokens(i["content"]) for i in items),
            "top_score": results[0].score if results else 0.0,
            "min_score_threshold": self.policy.min_score,
            "memory_ids": [i["memory_id"] for i in items],
            "note": ("no memory scored above the injection threshold; nothing injected"
                     if not items else ""),
        }
        return items, diagnostics
