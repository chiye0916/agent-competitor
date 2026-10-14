"""Registration, schema discovery, validation, and dispatch for tools."""

from __future__ import annotations

from typing import Any

from agent.state import AgentState
from retrieval.retriever import Retriever
from tools.base import BaseTool
from tools.knowledge import KnowledgeRetrievalTool
from tools.research import (
    FinishTool,
    ReadWebTool,
    ReplanTool,
    ResearchTools,
    SearchWebTool,
)


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, BaseTool] = {}

    def register(self, tool: BaseTool) -> None:
        if not isinstance(tool, BaseTool):
            raise TypeError("注册对象必须继承 BaseTool")
        if not tool.name:
            raise ValueError("工具名称不能为空")
        if tool.name in self._tools:
            raise ValueError(f"工具已注册: {tool.name}")
        self._tools[tool.name] = tool

    def schemas(self) -> list[dict[str, Any]]:
        return [tool.schema() for tool in self._tools.values()]

    def execute(self, tool_name: str, arguments: dict[str, Any]) -> tuple[bool, Any]:
        tool = self._tools.get(tool_name)
        if tool is None:
            raise ValueError(f"未知工具: {tool_name}")
        if not isinstance(arguments, dict):
            raise ValueError("工具参数必须是对象")
        tool.validate_arguments(arguments)
        return tool.ends_run, tool.execute(**arguments)


def build_default_registry(
    research_tools: ResearchTools,
    state: AgentState,
    retriever: Retriever | None = None,
) -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(SearchWebTool(research_tools, state))
    registry.register(ReadWebTool(research_tools, state))
    if retriever is not None:
        registry.register(KnowledgeRetrievalTool(retriever))
    registry.register(ReplanTool(state))
    registry.register(FinishTool(state))
    return registry
