"""Knowledge retrieval tool exposed through the Tool Registry."""

from __future__ import annotations

from typing import Any

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

    def __init__(self, retriever: Retriever) -> None:
        self.retriever = retriever

    def execute(self, **kwargs: Any) -> dict[str, Any]:
        query = kwargs["query"]
        top_k = kwargs.get("top_k", 5)
        chunks = self.retriever.query(query, top_k=top_k)
        return {
            "success": True,
            "tool": self.name,
            "query": query,
            "result_count": len(chunks),
            "results": [
                {
                    "chunk_id": chunk.id,
                    "document_id": chunk.document_id,
                    "text": chunk.text,
                    "metadata": chunk.metadata,
                }
                for chunk in chunks
            ],
        }
