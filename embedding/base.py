"""Provider-independent embedding interface."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence


class EmbeddingModel(ABC):
    @abstractmethod
    def embed_text(self, text: str) -> list[float]:
        """Embed one text value."""

    @abstractmethod
    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        """Embed multiple text values in input order."""
