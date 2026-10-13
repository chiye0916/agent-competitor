"""Agent runtime, state, and context construction."""

from typing import Any

from .context import ContextBuilder, ReportContextBuilder
from .state import AgentState, PlanStep, Source

__all__ = [
    "AgentState",
    "CompetitorAgent",
    "ContextBuilder",
    "PlanStep",
    "ReportContextBuilder",
    "Source",
]


def __getattr__(name: str) -> Any:
    if name == "CompetitorAgent":
        from .core import CompetitorAgent

        return CompetitorAgent
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
