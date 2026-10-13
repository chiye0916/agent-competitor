"""Serializable source document used by the knowledge layer."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from agent.state import Source


@dataclass
class Document:
    """Original document before chunking and embedding."""

    id: str
    title: str
    url: str | None
    content: str
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Document":
        copy = dict(data)
        copy["metadata"] = dict(copy.get("metadata") or {})
        return cls(**copy)

    @classmethod
    def from_source(cls, source: "Source") -> "Document":
        """Convert an Agent Source without changing the Source lifecycle."""

        return cls(
            id=source.id,
            title=source.title,
            url=source.url,
            content=source.content or source.snippet,
            metadata={
                "source_id": source.id,
                "snippet": source.snippet,
                "retrieved_at": source.retrieved_at,
                "via": source.via,
            },
        )
