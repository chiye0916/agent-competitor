"""Application-facing retrieval service over the vector store."""

from __future__ import annotations

import math

from knowledge.chunk import Chunk
from retrieval.chroma_store import ChromaStore
from retrieval.models import RetrievalDiagnostics, RetrievalMatch


class Retriever:
    """Keep tools independent from Chroma collection and embedding details."""

    def __init__(
        self,
        store: ChromaStore,
        relevance_threshold: float | None = None,
    ) -> None:
        if relevance_threshold is not None and (
            not math.isfinite(relevance_threshold) or relevance_threshold < 0
        ):
            raise ValueError("relevance_threshold 必须是非负数或 None")
        self.store = store
        self.relevance_threshold = relevance_threshold

    def query(self, text: str, top_k: int = 5) -> list[Chunk]:
        diagnostics = self.diagnose(text, top_k=top_k)
        return [match.chunk for match in diagnostics.accepted_matches]

    def query_with_distances(
        self,
        text: str,
        top_k: int = 5,
    ) -> list[RetrievalMatch]:
        """Expose ranked raw distances for read-only manual inspection."""

        return self.store.query_with_distances(text, top_k=top_k)

    def diagnose(self, text: str, top_k: int = 5) -> RetrievalDiagnostics:
        """Apply the configured distance ceiling after Chroma's Top-K lookup."""

        raw_matches = tuple(self.query_with_distances(text, top_k=top_k))
        if self.relevance_threshold is None:
            accepted = raw_matches
            rejected: tuple[RetrievalMatch, ...] = ()
        else:
            accepted = tuple(
                match
                for match in raw_matches
                if match.distance is not None
                and match.distance <= self.relevance_threshold
            )
            rejected = tuple(match for match in raw_matches if match not in accepted)
        return RetrievalDiagnostics(
            query=text,
            distance_metric=self.store.distance_metric,
            threshold=self.relevance_threshold,
            raw_matches=raw_matches,
            accepted_matches=accepted,
            rejected_matches=rejected,
        )
