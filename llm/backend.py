"""Protocol implemented by model backends used by the agent runtime."""

from __future__ import annotations

from typing import Any, Protocol


class LLMResponseError(RuntimeError):
    """Raised when an LLM response cannot be converted into the expected shape."""

    def __init__(
        self,
        message: str,
        *,
        attempts: int,
        tool_name: str | None = None,
        raw_arguments: str = "",
        position: int | None = None,
    ) -> None:
        super().__init__(message)
        self.attempts = attempts
        self.tool_name = tool_name
        self.raw_arguments_length = len(raw_arguments)
        self.raw_arguments_preview = self._preview(raw_arguments)
        self.position = position

    @staticmethod
    def _preview(value: str, limit: int = 600) -> str:
        if len(value) <= limit:
            return value
        half = limit // 2
        return f"{value[:half]} ... {value[-half:]}"


class LLMBackend(Protocol):
    def create_plan(self, task: str) -> list[str]: ...

    def decide(self, context: dict[str, Any], tools: list[dict[str, Any]]) -> dict[str, Any]: ...

    def assess_progress(self, context: dict[str, Any], observation: dict[str, Any]) -> dict[str, Any]: ...

    def revise_plan(self, context: dict[str, Any], reason: str) -> list[str]: ...

    def write_report(self, context: dict[str, Any]) -> str: ...
