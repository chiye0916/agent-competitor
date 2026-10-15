"""Post-report ingestion boundary that cannot invalidate completed research."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

from agent.state import Source
from knowledge.ingestion import KnowledgeIngestionPipeline
from storage.logger import RunLogger


class KnowledgeIngestionService:
    def __init__(self, pipeline: KnowledgeIngestionPipeline) -> None:
        self.pipeline = pipeline

    def run(
        self,
        sources: Sequence[Source],
        status_path: Path,
        logger: RunLogger,
    ) -> dict[str, Any]:
        logger.emit(
            "knowledge_ingestion_started",
            sources=len(sources),
            status_path=str(status_path),
        )
        try:
            summary = self.pipeline.ingest_sources(sources, status_path=status_path)
        except Exception as exc:
            summary = {
                "status": "failed",
                "total_sources": len(sources),
                "failed": len(sources),
                "error": str(exc),
            }
            logger.emit("ingestion_error", **summary)
            return summary
        if summary["status"] in {"failed", "partial_failed"}:
            logger.emit("ingestion_error", summary=summary)
        else:
            logger.emit("knowledge_ingestion_completed", summary=summary)
        return summary
