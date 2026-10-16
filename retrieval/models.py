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
