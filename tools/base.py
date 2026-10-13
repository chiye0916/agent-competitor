"""Base interface and lightweight input validation for executable tools."""

from __future__ import annotations

from copy import deepcopy
from typing import Any


class ToolValidationError(ValueError):
    """Raised when a model-generated tool call does not match its input schema."""


class BaseTool:
    name: str = ""
    description: str = ""
    input_schema: dict[str, Any] = {}
    ends_run: bool = False

    def schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": deepcopy(self.input_schema),
            },
        }

    def validate_arguments(self, arguments: dict[str, Any]) -> None:
        self._validate_value(arguments, self.input_schema, "arguments")

    @classmethod
    def _validate_value(cls, value: Any, schema: dict[str, Any], path: str) -> None:
        expected = schema.get("type")
        if expected == "object":
            if not isinstance(value, dict):
                raise ToolValidationError(f"{path} 必须是对象")
            required = schema.get("required", [])
            missing = [name for name in required if name not in value]
            if missing:
                raise ToolValidationError(f"{path} 缺少必填参数: {', '.join(missing)}")
            properties = schema.get("properties", {})
            if schema.get("additionalProperties") is False:
                unexpected = [name for name in value if name not in properties]
                if unexpected:
                    raise ToolValidationError(f"{path} 包含未知参数: {', '.join(unexpected)}")
            for name, item in value.items():
                if name in properties:
                    cls._validate_value(item, properties[name], f"{path}.{name}")
            return
        if expected == "array":
            if not isinstance(value, list):
                raise ToolValidationError(f"{path} 必须是数组")
            if len(value) < schema.get("minItems", 0):
                raise ToolValidationError(f"{path} 至少需要 {schema['minItems']} 项")
            item_schema = schema.get("items")
            if item_schema:
                for index, item in enumerate(value):
                    cls._validate_value(item, item_schema, f"{path}[{index}]")
            return
        if expected == "string" and not isinstance(value, str):
            raise ToolValidationError(f"{path} 必须是字符串")
        if expected == "integer" and (not isinstance(value, int) or isinstance(value, bool)):
            raise ToolValidationError(f"{path} 必须是整数")
        if expected == "number" and (not isinstance(value, (int, float)) or isinstance(value, bool)):
            raise ToolValidationError(f"{path} 必须是数字")
        if expected == "boolean" and not isinstance(value, bool):
            raise ToolValidationError(f"{path} 必须是布尔值")

    def execute(self, **kwargs: Any) -> Any:
        raise NotImplementedError
