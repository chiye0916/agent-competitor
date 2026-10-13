"""OpenAI SDK implementation of the LLM backend protocol."""

from __future__ import annotations

import json
from typing import Any

from config.settings import Config
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
        response = self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": DECIDE_PROMPT},
                {"role": "user", "content": json.dumps(context, ensure_ascii=False)},
            ],
            tools=tools,
            tool_choice="required",
            temperature=0,
        )
        calls = response.choices[0].message.tool_calls or []
        if not calls:
            raise RuntimeError("模型没有返回工具调用")
        call = calls[0]
        return {"name": call.function.name, "arguments": json.loads(call.function.arguments or "{}")}

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
