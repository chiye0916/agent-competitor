"""Environment-backed runtime settings."""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass
class Config:
    model: str = "gpt-4.1-mini"
    base_url: str | None = None
    api_key: str | None = None
    embedding_model: str = "text-embedding-3-small"
    knowledge_db_path: str = ".knowledge/chroma"
    knowledge_collection: str = "competitor_knowledge"
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
            tavily_api_key=os.getenv("TAVILY_API_KEY"),
            max_rounds=int(os.getenv("MAX_ROUNDS", "12")),
            context_char_budget=int(os.getenv("CONTEXT_CHAR_BUDGET", "14000")),
            report_context_char_budget=int(os.getenv("REPORT_CONTEXT_CHAR_BUDGET", "70000")),
            request_timeout=int(os.getenv("HTTP_TIMEOUT", "20")),
            decision_max_retries=max(0, int(os.getenv("LLM_DECISION_RETRIES", "2"))),
        )
