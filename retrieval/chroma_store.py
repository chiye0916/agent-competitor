"""Chroma-backed storage and similarity search for knowledge chunks."""

from __future__ import annotations

import json
import os
from collections.abc import Sequence
from copy import deepcopy
from pathlib import Path
from typing import Any
from uuid import uuid4

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
        self.collection_name = collection_name
        self.persist_directory = (
            Path(persist_directory) if persist_directory is not None else None
        )
        self.manifest_path = (
            self.persist_directory / f"{collection_name}.versions.json"
            if self.persist_directory is not None
            else None
        )
        self._version_manifest = self._load_version_manifest()
        self.collection = client.get_or_create_collection(
            name=collection_name,
            metadata={"hnsw:space": "cosine"},
        )

    def _load_version_manifest(self) -> dict[str, Any]:
        empty = {"schema_version": 1, "documents": {}}
        if self.manifest_path is None or not self.manifest_path.exists():
            return empty
        try:
            payload = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"知识版本清单无法读取: {self.manifest_path}") from exc
        if payload.get("schema_version") != 1 or not isinstance(
            payload.get("documents"), dict
        ):
            raise RuntimeError(f"知识版本清单格式无效: {self.manifest_path}")
        return payload

    def _save_version_manifest(self, manifest: dict[str, Any]) -> None:
        if self.manifest_path is None:
            self._version_manifest = manifest
            return
        self.manifest_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.manifest_path.with_name(
            f".{self.manifest_path.name}.{uuid4().hex}.tmp"
        )
        try:
            temporary.write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            os.replace(temporary, self.manifest_path)
        finally:
            if temporary.exists():
                temporary.unlink()
        self._version_manifest = manifest

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

    def stage_document_version(
        self,
        chunks: Sequence[Chunk],
        embeddings: Sequence[Sequence[float]],
    ) -> None:
        """Write a deterministic version without making it query-visible yet."""

        values = list(chunks)
        self.add_chunks(values, embeddings)
        expected_ids = {chunk.id for chunk in values}
        staged = self.collection.get(ids=list(expected_ids), include=["metadatas"])
        actual_ids = {str(chunk_id) for chunk_id in staged.get("ids") or []}
        if actual_ids != expected_ids:
            raise RuntimeError("Chroma 未完整写入当前文档版本")

    def get_active_version(self, document_id: str) -> dict[str, Any] | None:
        value = self._version_manifest["documents"].get(document_id)
        return deepcopy(value) if value is not None else None

    def activate_document_version(
        self,
        document_id: str,
        content_hash: str,
        chunk_ids: Sequence[str],
        metadata: dict[str, Any],
    ) -> None:
        """Atomically switch logical visibility after every Chunk is staged."""

        manifest = deepcopy(self._version_manifest)
        manifest["documents"][document_id] = {
            "content_hash": content_hash,
            "chunk_ids": list(chunk_ids),
            "metadata": dict(metadata),
        }
        self._save_version_manifest(manifest)

    @classmethod
    def _decode_metadata(cls, stored: dict[str, Any] | None) -> dict[str, Any]:
        raw = (stored or {}).get(cls._METADATA_KEY, "{}")
        try:
            decoded = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            return {}
        return decoded if isinstance(decoded, dict) else {}

    def _is_query_visible(self, document_id: str, metadata: dict[str, Any]) -> bool:
        if not metadata.get("ingestion_managed"):
            # Preserve compatibility with Step 1 callers that added legacy chunks directly.
            return True
        active = self._version_manifest["documents"].get(document_id)
        return bool(active and active.get("content_hash") == metadata.get("content_hash"))

    def delete_inactive_versions(self, document_id: str) -> int:
        """Best-effort physical cleanup; manifest filtering provides correctness."""

        active = self._version_manifest["documents"].get(document_id)
        if active is None:
            return 0
        result = self.collection.get(include=["metadatas"])
        stale_ids: list[str] = []
        for chunk_id, stored in zip(
            result.get("ids") or [],
            result.get("metadatas") or [],
        ):
            stored_values = stored or {}
            metadata = self._decode_metadata(stored_values)
            if (
                metadata.get("ingestion_managed")
                and stored_values.get(self._DOCUMENT_ID_KEY) == document_id
                and metadata.get("content_hash") != active.get("content_hash")
            ):
                stale_ids.append(str(chunk_id))
        if stale_ids:
            self.collection.delete(ids=stale_ids)
        return len(stale_ids)

    def query(self, text: str, top_k: int = 5) -> list[Chunk]:
        if top_k <= 0:
            raise ValueError("top_k 必须大于 0")
        if not text.strip():
            raise ValueError("查询文本不能为空")
        collection_count = self.collection.count()
        if collection_count == 0:
            return []

        result = self.collection.query(
            query_embeddings=[self.embedding_model.embed_text(text)],
            # Staged and obsolete versions remain physically present until cleanup.
            # Query all candidates so they cannot crowd active versions out of top_k.
            n_results=collection_count,
            include=["documents", "metadatas"],
        )
        ids = (result.get("ids") or [[]])[0]
        documents = (result.get("documents") or [[]])[0]
        metadatas = (result.get("metadatas") or [[]])[0]
        chunks: list[Chunk] = []
        for chunk_id, document, metadata in zip(ids, documents, metadatas):
            stored = metadata or {}
            decoded = self._decode_metadata(stored)
            document_id = str(stored.get(self._DOCUMENT_ID_KEY, ""))
            if not self._is_query_visible(document_id, decoded):
                continue
            chunks.append(
                Chunk(
                    id=str(chunk_id),
                    document_id=document_id,
                    text=str(document or ""),
                    metadata=decoded,
                )
            )
            if len(chunks) == top_k:
                break
        return chunks
