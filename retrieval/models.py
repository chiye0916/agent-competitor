"""Read-only retrieval result types used by diagnostics and manual evaluation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from knowledge.chunk import Chunk


@dataclass(frozen=True)
class RetrievalMatch:
    """A ranked Chunk with Chroma's raw distance value."""

    rank: int
    chunk: Chunk
    distance: float | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "rank": self.rank,
            "distance": self.distance,
            "chunk_id": self.chunk.id,
            "document_id": self.chunk.document_id,
            "text": self.chunk.text,
            "metadata": dict(self.chunk.metadata),
        }


@dataclass(frozen=True)
class RetrievalDiagnostics:
    """Raw candidates and threshold decisions for one retrieval query."""

    query: str
    distance_metric: str
    threshold: float | None
    raw_matches: tuple[RetrievalMatch, ...]
    accepted_matches: tuple[RetrievalMatch, ...]
    rejected_matches: tuple[RetrievalMatch, ...]

    @property
    def status(self) -> str:
        return (
            "relevant_knowledge_found"
            if self.accepted_matches
            else "no_relevant_knowledge"
        )

    @property
    def reason(self) -> str:
        if self.accepted_matches:
            return "candidates_within_relevance_threshold"
        if not self.raw_matches:
            return "no_candidates"
        if self.threshold is None:
            return "no_candidates"
        return "all_candidates_exceed_relevance_threshold"

    def to_dict(self) -> dict[str, Any]:
        return {
            "query": self.query,
            "status": self.status,
            "reason": self.reason,
            "distance_metric": self.distance_metric,
            "threshold": self.threshold,
            "raw_candidate_count": len(self.raw_matches),
            "accepted_count": len(self.accepted_matches),
            "rejected_count": len(self.rejected_matches),
            "raw_candidates": [match.to_dict() for match in self.raw_matches],
            "accepted": [match.to_dict() for match in self.accepted_matches],
            "rejected": [match.to_dict() for match in self.rejected_matches],
        }
