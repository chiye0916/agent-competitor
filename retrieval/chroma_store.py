"""Chroma-backed storage and similarity search for knowledge chunks."""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from embedding.base import EmbeddingModel
from knowledge.chunk import Chunk


class ChromaStore:
    _DOCUMENT_ID_KEY = "knowledge_document_id"
    _METADATA_KEY = "knowledge_metadata_json"

    def __init__(
        self,
        embedding_model: EmbeddingModel,
        collection_name: str = "competitor_knowledge",
        persist_directory: Path | str | None = None,
        client: Any | None = None,
    ) -> None:
        try:
            import chromadb
        except ImportError as exc:
            raise RuntimeError("缺少 chromadb，请先安装 requirements.txt") from exc

        if client is None:
            if persist_directory is None:
                client = chromadb.Client()
            else:
                path = Path(persist_directory)
                path.mkdir(parents=True, exist_ok=True)
                client = chromadb.PersistentClient(path=str(path))
        self.embedding_model = embedding_model
        self.client = client
        self.collection = client.get_or_create_collection(
            name=collection_name,
            metadata={"hnsw:space": "cosine"},
        )

    @staticmethod
    def _metadata_for_storage(chunk: Chunk) -> dict[str, str]:
        return {
            ChromaStore._DOCUMENT_ID_KEY: chunk.document_id,
            ChromaStore._METADATA_KEY: json.dumps(
                chunk.metadata,
                ensure_ascii=False,
                sort_keys=True,
                default=str,
            ),
        }

    def add_chunks(
        self,
        chunks: Sequence[Chunk],
        embeddings: Sequence[Sequence[float]] | None = None,
    ) -> None:
        values = list(chunks)
        if not values:
            return
        vectors = (
            self.embedding_model.embed_documents([chunk.text for chunk in values])
            if embeddings is None
            else [list(vector) for vector in embeddings]
        )
        if len(vectors) != len(values):
            raise ValueError("chunks 与 embeddings 数量必须一致")
        self.collection.upsert(
            ids=[chunk.id for chunk in values],
            documents=[chunk.text for chunk in values],
            metadatas=[self._metadata_for_storage(chunk) for chunk in values],
            embeddings=vectors,
        )

    def query(self, text: str, top_k: int = 5) -> list[Chunk]:
        if top_k <= 0:
            raise ValueError("top_k 必须大于 0")
        if not text.strip():
            raise ValueError("查询文本不能为空")
        if self.collection.count() == 0:
            return []

        result = self.collection.query(
            query_embeddings=[self.embedding_model.embed_text(text)],
            n_results=min(top_k, self.collection.count()),
            include=["documents", "metadatas"],
        )
        ids = (result.get("ids") or [[]])[0]
        documents = (result.get("documents") or [[]])[0]
        metadatas = (result.get("metadatas") or [[]])[0]
        chunks: list[Chunk] = []
        for chunk_id, document, metadata in zip(ids, documents, metadatas):
            stored = metadata or {}
            raw_metadata = stored.get(self._METADATA_KEY, "{}")
            chunks.append(
                Chunk(
                    id=str(chunk_id),
                    document_id=str(stored.get(self._DOCUMENT_ID_KEY, "")),
                    text=str(document or ""),
                    metadata=json.loads(raw_metadata),
                )
            )
        return chunks
