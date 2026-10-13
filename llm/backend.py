"""Protocol implemented by model backends used by the agent runtime."""

from __future__ import annotations

from typing import Any, Protocol


class LLMBackend(Protocol):
    def create_plan(self, task: str) -> list[str]: ...

    def decide(self, context: dict[str, Any], tools: list[dict[str, Any]]) -> dict[str, Any]: ...

    def assess_progress(self, context: dict[str, Any], observation: dict[str, Any]) -> dict[str, Any]: ...

    def revise_plan(self, context: dict[str, Any], reason: str) -> list[str]: ...

    def write_report(self, context: dict[str, Any]) -> str: ...
