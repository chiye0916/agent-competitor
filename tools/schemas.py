"""Input schemas and descriptions owned by the built-in tools."""

from typing import Any


SEARCH_WEB_DESCRIPTION = "当已有知识库缺少所需资料或需要最新信息时，使用搜索 API 查找新来源。"
SEARCH_WEB_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "query": {"type": "string"},
        "purpose": {"type": "string"},
        "target_step_id": {"type": "string"},
    },
    "required": ["query", "purpose", "target_step_id"],
    "additionalProperties": False,
}

READ_WEB_DESCRIPTION = "读取一个候选来源全文；优先读取官方或一手来源。"
READ_WEB_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "url": {"type": "string"},
        "purpose": {"type": "string"},
        "target_step_id": {"type": "string"},
    },
    "required": ["url", "purpose", "target_step_id"],
    "additionalProperties": False,
}

RETRIEVE_KNOWLEDGE_DESCRIPTION = (
    "从已有知识库中检索与当前任务相关的历史资料；适合优先复用已有知识，"
    "不能替代对最新信息的 Web 搜索。若没有候选通过相关性阈值，工具会明确返回 "
    "no_relevant_knowledge，由你自主决定下一步，不会自动触发 Web 搜索。"
)
RETRIEVE_KNOWLEDGE_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "query": {"type": "string"},
        "top_k": {"type": "integer", "default": 5, "minimum": 1},
    },
    "required": ["query"],
    "additionalProperties": False,
}

REPLAN_DESCRIPTION = "信息不足、出现新维度或原计划不可执行时，替换尚未完成的计划。"
REPLAN_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "reason": {"type": "string"},
        "new_steps": {"type": "array", "items": {"type": "string"}, "minItems": 1},
    },
    "required": ["reason", "new_steps"],
    "additionalProperties": False,
}

FINISH_DESCRIPTION = "证据已足够覆盖任务时结束研究，并进入最终报告生成。"
FINISH_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "reason": {"type": "string"},
        "completed_step_ids": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["reason", "completed_step_ids"],
    "additionalProperties": False,
}
