"""Knowledge retrieval tool exposed through the Tool Registry."""

from __future__ import annotations

from typing import Any

from agent.evidence import RetrievedEvidence
from agent.state import AgentState
from retrieval.retriever import Retriever
from tools.base import BaseTool
from tools.schemas import (
    RETRIEVE_KNOWLEDGE_DESCRIPTION,
    RETRIEVE_KNOWLEDGE_INPUT_SCHEMA,
)


class KnowledgeRetrievalTool(BaseTool):
    name = "retrieve_knowledge"
    description = RETRIEVE_KNOWLEDGE_DESCRIPTION
    input_schema = RETRIEVE_KNOWLEDGE_INPUT_SCHEMA
    # OpenAI strict schemas require every property to be required; top_k is optional by design.
    strict = False

    def __init__(self, retriever: Retriever, state: AgentState) -> None:
        self.retriever = retriever
        self.state = state

    def execute(self, **kwargs: Any) -> dict[str, Any]:
        query = kwargs["query"]
        top_k = kwargs.get("top_k", 5)
        diagnostics = self.retriever.diagnose(query, top_k=top_k)
        results = []
        for match in diagnostics.accepted_matches:
            chunk = match.chunk
            evidence = self.state.add_retrieved_evidence(
                RetrievedEvidence.from_chunk(chunk)
            )
            results.append(
                {
                    "evidence_id": evidence.id,
                    "chunk_id": evidence.chunk_id,
                    "document_id": evidence.document_id,
                    "text": evidence.content,
                    "source_url": evidence.source_url,
                    "source_title": evidence.source_title,
                    "source_id": evidence.source_id,
                    "content_hash": evidence.content_hash,
                    "retrieved_at": evidence.retrieved_at,
                    "ingested_at": evidence.ingested_at,
                    "metadata": evidence.metadata,
                    "distance": match.distance,
                }
            )
        return {
            "success": True,
            "tool": self.name,
            "status": diagnostics.status,
            "query": query,
            "reason": diagnostics.reason,
            "distance_metric": diagnostics.distance_metric,
            "relevance_threshold": diagnostics.threshold,
            "raw_candidate_count": len(diagnostics.raw_matches),
            "result_count": len(diagnostics.accepted_matches),
            "rejected_count": len(diagnostics.rejected_matches),
            "results": results,
        }
