"""Agent runtime, state, and context construction."""

from typing import Any

from .context import ContextBuilder, ReportContextBuilder
from .evidence import ReportCitationRenderer, RetrievedEvidence
from .state import AgentState, PlanStep, Source, infer_knowledge_mode

__all__ = [
    "AgentState",
    "CompetitorAgent",
    "ContextBuilder",
    "PlanStep",
    "ReportCitationRenderer",
    "ReportContextBuilder",
    "RetrievedEvidence",
    "Source",
    "infer_knowledge_mode",
]


def __getattr__(name: str) -> Any:
    if name == "CompetitorAgent":
        from .core import CompetitorAgent

        return CompetitorAgent
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
