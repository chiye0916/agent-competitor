"""Environment-backed runtime settings."""

from __future__ import annotations

import os
from dataclasses import dataclass


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} 必须是 true 或 false")


@dataclass
class Config:
    model: str = "gpt-4.1-mini"
    base_url: str | None = None
    api_key: str | None = None
    embedding_model: str = "text-embedding-3-small"
    knowledge_db_path: str = ".knowledge/chroma"
    knowledge_collection: str = "competitor_knowledge"
    knowledge_auto_ingest: bool = True
    knowledge_min_content_chars: int = 200
    knowledge_chunk_size: int = 500
    knowledge_chunk_overlap: int = 50
    tavily_api_key: str | None = None
    max_rounds: int = 12
    context_char_budget: int = 14_000
    report_context_char_budget: int = 70_000
    request_timeout: int = 20
    decision_max_retries: int = 2

    @classmethod
    def from_env(cls) -> "Config":
        try:
            from dotenv import load_dotenv

            load_dotenv()
        except ImportError:
            # Offline demo/tests deliberately have no third-party dependency.
            pass
        return cls(
            model=os.getenv("LLM_MODEL", "gpt-4.1-mini"),
            base_url=os.getenv("LLM_BASE_URL") or None,
            api_key=os.getenv("LLM_API_KEY") or os.getenv("OPENAI_API_KEY"),
            embedding_model=os.getenv("EMBEDDING_MODEL", "text-embedding-3-small"),
            knowledge_db_path=os.getenv("KNOWLEDGE_DB_PATH", ".knowledge/chroma"),
            knowledge_collection=os.getenv("KNOWLEDGE_COLLECTION", "competitor_knowledge"),
            knowledge_auto_ingest=_env_bool("KNOWLEDGE_AUTO_INGEST", True),
            knowledge_min_content_chars=int(
                os.getenv("KNOWLEDGE_MIN_CONTENT_CHARS", "200")
            ),
            knowledge_chunk_size=int(os.getenv("KNOWLEDGE_CHUNK_SIZE", "500")),
            knowledge_chunk_overlap=int(os.getenv("KNOWLEDGE_CHUNK_OVERLAP", "50")),
            tavily_api_key=os.getenv("TAVILY_API_KEY"),
            max_rounds=int(os.getenv("MAX_ROUNDS", "12")),
            context_char_budget=int(os.getenv("CONTEXT_CHAR_BUDGET", "14000")),
            report_context_char_budget=int(os.getenv("REPORT_CONTEXT_CHAR_BUDGET", "70000")),
            request_timeout=int(os.getenv("HTTP_TIMEOUT", "20")),
            decision_max_retries=max(0, int(os.getenv("LLM_DECISION_RETRIES", "2"))),
        )
