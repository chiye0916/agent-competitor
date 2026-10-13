"""OpenAI SDK implementation of the LLM backend protocol."""

from __future__ import annotations

import json
from typing import Any

from config.settings import Config
from llm.backend import LLMResponseError
from llm.prompts import (
    ASSESS_PROGRESS_PROMPT,
    CREATE_PLAN_PROMPT,
    DECIDE_PROMPT,
    REVISE_PLAN_PROMPT,
    WRITE_REPORT_PROMPT,
)


class OpenAIBackend:
    def __init__(self, config: Config):
        if not config.api_key:
            raise RuntimeError("缺少 LLM_API_KEY（或 OPENAI_API_KEY）")
        try:
            from openai import OpenAI
        except ImportError as exc:
            raise RuntimeError("缺少 openai SDK，请先安装 requirements.txt") from exc
        kwargs: dict[str, Any] = {"api_key": config.api_key}
        if config.base_url:
            kwargs["base_url"] = config.base_url
        self.client = OpenAI(**kwargs)
        self.model = config.model
        self.decision_max_retries = config.decision_max_retries

    def _json_completion(self, system: str, user: dict[str, Any]) -> dict[str, Any]:
        response = self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": json.dumps(user, ensure_ascii=False)},
            ],
            response_format={"type": "json_object"},
            temperature=0,
        )
        return json.loads(response.choices[0].message.content or "{}")

    def create_plan(self, task: str) -> list[str]:
        data = self._json_completion(CREATE_PLAN_PROMPT, {"task": task})
        steps = [str(item).strip() for item in data.get("steps", []) if str(item).strip()]
        if not steps:
            raise RuntimeError("模型未生成有效计划")
        return steps[:8]

    def decide(self, context: dict[str, Any], tools: list[dict[str, Any]]) -> dict[str, Any]:
        base_messages = [
            {"role": "system", "content": DECIDE_PROMPT},
            {"role": "user", "content": json.dumps(context, ensure_ascii=False)},
        ]
        retry_note: str | None = None
        total_attempts = self.decision_max_retries + 1
        last_error: LLMResponseError | None = None

        for attempt in range(1, total_attempts + 1):
            messages = list(base_messages)
            if retry_note:
                messages.append({"role": "user", "content": retry_note})
            response = self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                tools=tools,
                tool_choice="required",
                temperature=0,
            )
            calls = response.choices[0].message.tool_calls or []
            if not calls:
                last_error = LLMResponseError(
                    "模型没有返回工具调用",
                    attempts=attempt,
                )
                retry_note = "上一次响应没有工具调用。请重新选择一个工具，并返回严格符合 Schema 的参数。"
                continue

            call = calls[0]
            tool_name = str(call.function.name)
            raw_arguments = call.function.arguments or "{}"
            try:
                arguments = json.loads(raw_arguments)
            except (json.JSONDecodeError, TypeError) as exc:
                position = exc.pos if isinstance(exc, json.JSONDecodeError) else None
                last_error = LLMResponseError(
                    f"工具 {tool_name} 的参数不是合法 JSON: {exc}",
                    attempts=attempt,
                    tool_name=tool_name,
                    raw_arguments=str(raw_arguments),
                    position=position,
                )
                retry_note = (
                    f"上一次为工具 {tool_name} 生成的参数不是合法 JSON。"
                    "请重新选择一个工具，确保 arguments 是严格 JSON，字符串已正确转义且字段之间包含逗号。"
                )
                continue
            return {"name": tool_name, "arguments": arguments}

        if last_error is not None:
            raise last_error
        raise LLMResponseError("模型决策失败", attempts=total_attempts)

    def assess_progress(self, context: dict[str, Any], observation: dict[str, Any]) -> dict[str, Any]:
        return self._json_completion(
            ASSESS_PROGRESS_PROMPT,
            {"context": context, "latest_observation": observation},
        )

    def revise_plan(self, context: dict[str, Any], reason: str) -> list[str]:
        data = self._json_completion(
            REVISE_PLAN_PROMPT,
            {"reason": reason, "context": context},
        )
        steps = [str(item).strip() for item in data.get("steps", []) if str(item).strip()]
        if not steps:
            raise RuntimeError("模型未生成有效的强制 Replan")
        return steps[:8]

    def write_report(self, context: dict[str, Any]) -> str:
        response = self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": WRITE_REPORT_PROMPT},
                {"role": "user", "content": json.dumps(context, ensure_ascii=False)},
            ],
            temperature=0.2,
        )
        return response.choices[0].message.content or ""
