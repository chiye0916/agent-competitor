"""Function-calling schemas exposed to the model."""

from typing import Any


TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "search_web",
            "description": "使用搜索 API 查找与当前计划有关的来源。",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "purpose": {"type": "string"},
                    "target_step_id": {"type": "string"},
                },
                "required": ["query", "purpose", "target_step_id"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_web",
            "description": "读取一个候选来源全文；优先读取官方或一手来源。",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string"},
                    "purpose": {"type": "string"},
                    "target_step_id": {"type": "string"},
                },
                "required": ["url", "purpose", "target_step_id"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "replan",
            "description": "信息不足、出现新维度或原计划不可执行时，替换尚未完成的计划。",
            "parameters": {
                "type": "object",
                "properties": {
                    "reason": {"type": "string"},
                    "new_steps": {"type": "array", "items": {"type": "string"}, "minItems": 1},
                },
                "required": ["reason", "new_steps"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "finish",
            "description": "证据已足够覆盖任务时结束研究，并进入最终报告生成。",
            "parameters": {
                "type": "object",
                "properties": {
                    "reason": {"type": "string"},
                    "completed_step_ids": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["reason", "completed_step_ids"],
                "additionalProperties": False,
            },
        },
    },
]
