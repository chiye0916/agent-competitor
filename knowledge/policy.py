"""Deterministic policy settings for knowledge ingestion."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class KnowledgePolicy:
    """Configurable, provider-independent rules for candidate web sources."""

    min_content_chars: int = 200
    min_meaningful_chars: int = 40
    allowed_url_schemes: tuple[str, ...] = ("http", "https")

    def __post_init__(self) -> None:
        if self.min_content_chars <= 0:
            raise ValueError("knowledge_min_content_chars 必须大于 0")
        if self.min_meaningful_chars <= 0:
            raise ValueError("min_meaningful_chars 必须大于 0")
