"""Idempotent, retryable source-to-Chroma ingestion pipeline."""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

from agent.state import Source
from knowledge.document import Document
from knowledge.evaluator import KnowledgeEvaluator
from knowledge.splitter import TextSplitter
from retrieval.chroma_store import ChromaStore
from storage.logger import utc_now


def normalize_url(url: str) -> str:
    """Conservatively normalize URL identity without discarding query parameters."""

    parsed = urlsplit(url.strip())
    scheme = parsed.scheme.lower()
    hostname = (parsed.hostname or "").lower()
    if not scheme or not hostname:
        raise ValueError("URL 缺少协议或主机名")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("知识文档 URL 不支持用户凭据")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("URL 端口无效") from exc
    display_host = f"[{hostname}]" if ":" in hostname else hostname
    default_port = (scheme == "http" and port == 80) or (scheme == "https" and port == 443)
    netloc = display_host if port is None or default_port else f"{display_host}:{port}"
    return urlunsplit((scheme, netloc, parsed.path or "/", parsed.query, ""))


def normalize_content(content: str) -> str:
    """Normalize insignificant whitespace before hashing and chunking."""

    normalized_lines = []
    for line in content.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        normalized_lines.append(re.sub(r"[\t\f\v ]+", " ", line).strip())
    return "\n".join(line for line in normalized_lines if line).strip()


def stable_document_id(normalized_url: str) -> str:
    digest = hashlib.sha256(normalized_url.encode("utf-8")).hexdigest()
    return f"doc_{digest}"


def content_sha256(normalized_content: str) -> str:
    return hashlib.sha256(normalized_content.encode("utf-8")).hexdigest()


class KnowledgeIngestionPipeline:
    """Evaluate, version, chunk, embed, and atomically activate knowledge sources."""

    def __init__(
        self,
        evaluator: KnowledgeEvaluator,
        splitter: TextSplitter,
        store: ChromaStore,
    ) -> None:
        self.evaluator = evaluator
        self.splitter = splitter
        self.store = store

    @staticmethod
    def _write_status(path: Path | None, payload: dict[str, Any]) -> None:
        if path is None:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        try:
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            os.replace(temporary, path)
        finally:
            if temporary.exists():
                temporary.unlink()

    @staticmethod
    def _summary(total_sources: int) -> dict[str, Any]:
        return {
            "status": "pending",
            "total_sources": total_sources,
            "accepted": 0,
            "rejected": 0,
            "new": 0,
            "unchanged": 0,
            "updated": 0,
            "failed": 0,
            "chunks_written": 0,
            "evaluations": [],
            "errors": [],
            "warnings": [],
            "updated_at": utc_now(),
        }

    def ingest_sources(
        self,
        sources: Sequence[Source],
        status_path: Path | None = None,
    ) -> dict[str, Any]:
        values = list(sources)
        summary = self._summary(len(values))
        self._write_status(status_path, summary)
        summary["status"] = "running"
        summary["updated_at"] = utc_now()
        self._write_status(status_path, summary)

        for source in values:
            document = Document.from_source(source)
            evaluation = self.evaluator.evaluate_document(document)
            evaluation_record = evaluation.to_dict()
            summary["evaluations"].append(evaluation_record)
            if not evaluation.accepted:
                summary["rejected"] += 1
                summary["updated_at"] = utc_now()
                self._write_status(status_path, summary)
                continue
            summary["accepted"] += 1
            try:
                result = self._ingest_source(source, document)
                summary[result["outcome"]] += 1
                summary["chunks_written"] += result["chunks_written"]
                evaluation_record.update(result)
                if result.get("warning"):
                    summary["warnings"].append(result["warning"])
            except Exception as exc:
                summary["failed"] += 1
                error = {
                    "source_id": source.id,
                    "url": source.url,
                    "error": str(exc),
                }
                summary["errors"].append(error)
                evaluation_record.update({"outcome": "failed", "error": str(exc)})
            summary["updated_at"] = utc_now()
            self._write_status(status_path, summary)

        if summary["failed"]:
            successful = summary["new"] + summary["unchanged"] + summary["updated"]
            summary["status"] = "partial_failed" if successful else "failed"
        else:
            summary["status"] = "completed"
        summary["updated_at"] = utc_now()
        self._write_status(status_path, summary)
        return summary

    def _ingest_source(self, source: Source, document: Document) -> dict[str, Any]:
        normalized_url = normalize_url(source.url)
        normalized_text = normalize_content(source.content)
        document_id = stable_document_id(normalized_url)
        content_hash = content_sha256(normalized_text)
        current = self.store.get_active_version(document_id)
        if current and current["content_hash"] == content_hash:
            warning = self._cleanup_inactive(document_id)
            return {
                "outcome": "unchanged",
                "document_id": document_id,
                "content_hash": content_hash,
                "chunks_written": 0,
                "warning": warning,
            }

        ingested_at = utc_now()
        document.id = document_id
        document.url = normalized_url
        document.content = normalized_text
        document.metadata.update(
            {
                "ingestion_managed": True,
                "document_id": document_id,
                "source_url": normalized_url,
                "source_title": source.title,
                "source_id": source.id,
                "content_hash": content_hash,
                "retrieved_at": source.retrieved_at,
                "ingested_at": ingested_at,
            }
        )
        chunks = self.splitter.split(document)
        if not chunks:
            raise RuntimeError("正文切分后没有可写入 Chunk")
        for index, chunk in enumerate(chunks):
            chunk.id = f"{document_id}:{content_hash}:chunk:{index}"
            chunk.document_id = document_id
            chunk.metadata.update(
                {
                    "chunk_index": index,
                    "content_hash": content_hash,
                    "ingestion_managed": True,
                }
            )

        embeddings = self.store.embedding_model.embed_documents(
            [chunk.text for chunk in chunks]
        )
        self.store.stage_document_version(chunks, embeddings)
        self.store.activate_document_version(
            document_id=document_id,
            content_hash=content_hash,
            chunk_ids=[chunk.id for chunk in chunks],
            metadata={
                "source_url": normalized_url,
                "source_title": source.title,
                "source_id": source.id,
                "retrieved_at": source.retrieved_at,
                "ingested_at": ingested_at,
            },
        )
        warning = self._cleanup_inactive(document_id)
        return {
            "outcome": "updated" if current else "new",
            "document_id": document_id,
            "content_hash": content_hash,
            "chunks_written": len(chunks),
            "warning": warning,
        }

    def _cleanup_inactive(self, document_id: str) -> dict[str, str] | None:
        try:
            self.store.delete_inactive_versions(document_id)
        except Exception as exc:
            return {
                "document_id": document_id,
                "reason": "inactive_cleanup_failed",
                "error": str(exc),
            }
        return None
