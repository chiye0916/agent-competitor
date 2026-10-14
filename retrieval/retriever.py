"""Application-facing retrieval service over the vector store."""

from __future__ import annotations

from knowledge.chunk import Chunk
from retrieval.chroma_store import ChromaStore


class Retriever:
    """Keep tools independent from Chroma collection and embedding details."""

    def __init__(self, store: ChromaStore) -> None:
        self.store = store

    def query(self, text: str, top_k: int = 5) -> list[Chunk]:
        return self.store.query(text, top_k=top_k)
