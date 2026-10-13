"""A small, framework-free, single-agent competitor research system.

The LLM is intentionally stateless. ``CompetitorAgent`` is the harness that
persists state, builds a bounded context, executes model-selected tools, and
continues the loop until the model chooses ``finish``.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import ipaddress
import json
import os
import re
import socket
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Protocol


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def compact(value: Any, limit: int = 600) -> str:
    text = json.dumps(value, ensure_ascii=False, default=str)
    return text if len(text) <= limit else text[:limit] + "..."


@dataclass
class Config:
    model: str = "gpt-4.1-mini"
    base_url: str | None = None
    api_key: str | None = None
    tavily_api_key: str | None = None
    max_rounds: int = 12
    context_char_budget: int = 14_000
    report_context_char_budget: int = 70_000
    request_timeout: int = 20

    @classmethod
    def from_env(cls) -> "Config":
        try:
            from dotenv import load_dotenv

            load_dotenv()
        except ImportError:
            # Offline demo/tests deliberately have no third-party dependency.
            pass
        return cls(
            model=os.getenv("LLM_MODEL", "gpt-4.1-mini"),
            base_url=os.getenv("LLM_BASE_URL") or None,
            api_key=os.getenv("LLM_API_KEY") or os.getenv("OPENAI_API_KEY"),
            tavily_api_key=os.getenv("TAVILY_API_KEY"),
            max_rounds=int(os.getenv("MAX_ROUNDS", "12")),
            context_char_budget=int(os.getenv("CONTEXT_CHAR_BUDGET", "14000")),
            report_context_char_budget=int(os.getenv("REPORT_CONTEXT_CHAR_BUDGET", "70000")),
            request_timeout=int(os.getenv("HTTP_TIMEOUT", "20")),
        )


@dataclass
class PlanStep:
    id: str
    description: str
    status: str = "pending"


@dataclass
class Source:
    id: str
    title: str
    url: str
    snippet: str = ""
    content: str = ""
    retrieved_at: str = field(default_factory=utc_now)
    via: str = "search_web"


@dataclass
class AgentState:
    task: str
    plan: list[PlanStep] = field(default_factory=list)
    sources: list[Source] = field(default_factory=list)
    recent_events: list[dict[str, Any]] = field(default_factory=list)
    action_history: list[str] = field(default_factory=list)
    read_attempts: dict[str, int] = field(default_factory=dict)
    repetition_strikes: int = 0
    round: int = 0
    replan_count: int = 0
    status: str = "created"
    report_path: str | None = None
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AgentState":
        copy = dict(data)
        copy["plan"] = [PlanStep(**item) for item in copy.get("plan", [])]
        copy["sources"] = [Source(**item) for item in copy.get("sources", [])]
        return cls(**copy)

    def save(self, path: Path) -> None:
        self.updated_at = utc_now()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    @classmethod
    def load(cls, path: Path) -> "AgentState":
        return cls.from_dict(json.loads(path.read_text(encoding="utf-8")))

    def add_event(self, event: str, detail: dict[str, Any]) -> None:
        self.recent_events.append({"time": utc_now(), "event": event, "detail": detail})
        # State keeps a useful recent window; the JSONL log keeps the full history.
        self.recent_events = self.recent_events[-20:]

    def complete_steps(self, step_ids: list[str]) -> list[str]:
        changed: list[str] = []
        wanted = set(step_ids)
        for step in self.plan:
            if step.id in wanted and step.status != "completed":
                step.status = "completed"
                changed.append(step.id)
        return changed


class RunLogger:
    def __init__(self, path: Path, echo: bool = True, append: bool = False):
        self.path = path
        self.echo = echo
        path.parent.mkdir(parents=True, exist_ok=True)
        if not append:
            path.write_text("", encoding="utf-8")

    def emit(self, event: str, **detail: Any) -> None:
        record = {"time": utc_now(), "event": event, **detail}
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        if self.echo:
            print(f"[{record['time']}] {event}: {compact(detail)}", flush=True)


class VisibleTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self.title = ""
        self._skip_depth = 0
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style", "noscript", "svg"}:
            self._skip_depth += 1
        if tag == "title":
            self._in_title = True

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript", "svg"} and self._skip_depth:
            self._skip_depth -= 1
        if tag == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        text = data.strip()
        if not text or self._skip_depth:
            return
        if self._in_title:
            self.title += (" " if self.title else "") + text
        self.parts.append(text)


class ResearchTools:
    """Search API plus a small HTTP/HTML reader; no agent framework involved."""

    def __init__(self, config: Config):
        self.config = config

    def search_web(self, query: str, max_results: int = 5) -> dict[str, Any]:
        if not self.config.tavily_api_key:
            raise RuntimeError("缺少 TAVILY_API_KEY，无法调用搜索 API")
        payload = json.dumps(
            {
                "api_key": self.config.tavily_api_key,
                "query": query,
                "search_depth": "advanced",
                "max_results": min(max(max_results, 1), 8),
                "include_answer": False,
                "include_raw_content": False,
            }
        ).encode("utf-8")
        request = urllib.request.Request(
            "https://api.tavily.com/search",
            data=payload,
            headers={"Content-Type": "application/json", "User-Agent": "CompetitorAgent/1.0"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.config.request_timeout) as response:
                data = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"搜索 API 请求失败: {exc}") from exc
        results = [
            {
                "title": item.get("title", "Untitled"),
                "url": item.get("url", ""),
                "snippet": item.get("content", "")[:1200],
            }
            for item in data.get("results", [])
            if item.get("url")
        ]
        return {"query": query, "results": results}

    @staticmethod
    def _validate_public_url(url: str) -> None:
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("只允许读取公开的 http/https URL")
        if parsed.hostname.lower() == "localhost":
            raise ValueError("不允许读取本机地址")
        try:
            addresses = socket.getaddrinfo(parsed.hostname, parsed.port or 443)
        except socket.gaierror as exc:
            raise ValueError(f"域名解析失败: {parsed.hostname}") from exc
        for address in addresses:
            ip = ipaddress.ip_address(address[4][0])
            if not ip.is_global:
                raise ValueError("不允许读取内网或保留地址")

    def read_web(self, url: str) -> dict[str, Any]:
        self._validate_public_url(url)
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 (compatible; CompetitorAgent/1.0)",
                "Accept": "text/html,application/xhtml+xml,text/plain",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.config.request_timeout) as response:
                content_type = response.headers.get_content_type()
                charset = response.headers.get_content_charset() or "utf-8"
                raw = response.read(2_000_000)
        except (urllib.error.URLError, TimeoutError) as exc:
            raise RuntimeError(f"网页读取失败: {exc}") from exc
        decoded = raw.decode(charset, errors="replace")
        if content_type == "text/plain":
            title, text = url, decoded
        elif content_type in {"text/html", "application/xhtml+xml"}:
            parser = VisibleTextParser()
            parser.feed(decoded)
            title = html.unescape(parser.title) or url
            text = " ".join(parser.parts)
        else:
            raise RuntimeError(f"暂不支持网页类型: {content_type}")
        text = re.sub(r"\s+", " ", html.unescape(text)).strip()
        return {"url": url, "title": title[:300], "content": text[:50_000]}


class DemoResearchTools(ResearchTools):
    """Deterministic fixtures for a runnable demo; never presented as live research."""

    PAGES = {
        "https://example.test/alpha": {
            "title": "Alpha official product page",
            "content": "Alpha offers collaborative editing, a free tier, and a Pro plan at $20/month. It targets small product teams.",
        },
        "https://example.test/beta": {
            "title": "Beta official product page",
            "content": "Beta emphasizes automation, enterprise controls, and usage-based pricing. Public pricing is not listed.",
        },
    }

    def search_web(self, query: str, max_results: int = 5) -> dict[str, Any]:
        key = "beta" if "Beta" in query else "alpha"
        page = self.PAGES[f"https://example.test/{key}"]
        return {
            "query": query,
            "results": [
                {
                    "title": page["title"],
                    "url": f"https://example.test/{key}",
                    "snippet": page["content"],
                }
            ],
        }

    def read_web(self, url: str) -> dict[str, Any]:
        if url not in self.PAGES:
            raise RuntimeError(f"demo fixture 不存在: {url}")
        return {"url": url, **self.PAGES[url]}


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


class LLMBackend(Protocol):
    def create_plan(self, task: str) -> list[str]: ...

    def decide(self, context: dict[str, Any], tools: list[dict[str, Any]]) -> dict[str, Any]: ...

    def assess_progress(self, context: dict[str, Any], observation: dict[str, Any]) -> dict[str, Any]: ...

    def revise_plan(self, context: dict[str, Any], reason: str) -> list[str]: ...

    def write_report(self, context: dict[str, Any]) -> str: ...


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
        data = self._json_completion(
            "你是竞品研究规划器。把目标拆成 3-6 个可验证、需要外部证据的步骤。"
            "仅输出 JSON：{\"steps\":[\"...\"]}。不要直接写结论。",
            {"task": task},
        )
        steps = [str(item).strip() for item in data.get("steps", []) if str(item).strip()]
        if not steps:
            raise RuntimeError("模型未生成有效计划")
        return steps[:8]

    def decide(self, context: dict[str, Any], tools: list[dict[str, Any]]) -> dict[str, Any]:
        response = self.client.chat.completions.create(
            model=self.model,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "你是单 Agent 竞品研究员。根据提供的有限状态自主选择且只选择一个下一步工具。"
                        "先补齐高价值证据，优先官方来源；计划不适用时 replan；证据充分才 finish。"
                        "不要重复读取 source_index 中已有全文的 URL；工具执行后会单独评估步骤进度。"
                    ),
                },
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
            "你是严格的研究进度审查器。根据当前计划、现有证据和刚完成的工具观察，"
            "判断哪些 pending 步骤已经被充分覆盖。只完成有直接证据支撑的步骤。"
            "仅输出 JSON：{\"completed_step_ids\":[\"P1\"],\"reason\":\"...\"}。",
            {"context": context, "latest_observation": observation},
        )

    def revise_plan(self, context: dict[str, Any], reason: str) -> list[str]:
        data = self._json_completion(
            "你是竞品研究规划器。系统检测到重复行为，必须改写所有未完成步骤以避开已读 URL，"
            "允许改用已有证据、搜索替代官方页面或明确证据缺口。"
            "仅输出 JSON：{\"steps\":[\"...\"]}。",
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
                {
                    "role": "system",
                    "content": (
                        "你是严谨的竞品分析报告作者。根据 report_context 中的证据写一份内容充分的中文 Markdown 报告。"
                        "先在内部按‘产品 × 定位/价格/核心能力/适用场景’构造证据矩阵，再写执行摘要、"
                        "结构化对比表、逐产品分析、优劣势、选择建议、结论与证据边界。"
                        "primary_evidence 是已读取的网页正文，优先级高于 supplemental_evidence 搜索摘要；"
                        "官方域名优先于第三方来源，冲突时说明差异。每个事实都必须使用 [S1] 格式引用。"
                        "只有检查所有相关 primary_evidence 后仍无依据时，才写‘未明确’；"
                        "不得使用上下文之外的事实，也不要把页面标题或 URL 当成已验证的正文事实。"
                    ),
                },
                {"role": "user", "content": json.dumps(context, ensure_ascii=False)},
            ],
            temperature=0.2,
        )
        return response.choices[0].message.content or ""


class DemoLLMBackend:
    """A scripted model substitute used only to prove the harness end-to-end."""

    def create_plan(self, task: str) -> list[str]:
        return ["收集 Alpha 的定位与价格证据", "收集 Beta 的定位与价格证据", "交叉比较并形成结论"]

    def decide(self, context: dict[str, Any], tools: list[dict[str, Any]]) -> dict[str, Any]:
        round_no = context["progress"]["round"]
        actions = {
            1: {"name": "search_web", "arguments": {"query": "Alpha official pricing", "purpose": "查找 Alpha 一手资料", "target_step_id": "P1"}},
            2: {"name": "read_web", "arguments": {"url": "https://example.test/alpha", "purpose": "核验价格和定位", "target_step_id": "P1"}},
            3: {"name": "replan", "arguments": {"reason": "Alpha 资料显示协作能力是重要比较维度", "new_steps": ["收集 Beta 的定位、价格与协作/管控证据", "按价格、定位和协作/管控维度比较"]}},
            4: {"name": "search_web", "arguments": {"query": "Beta official pricing enterprise controls", "purpose": "查找 Beta 一手资料", "target_step_id": "R1-1"}},
            5: {"name": "read_web", "arguments": {"url": "https://example.test/beta", "purpose": "核验价格和企业能力", "target_step_id": "R1-1"}},
            6: {"name": "finish", "arguments": {"reason": "两个产品的一手定位信息已收集，且已明确公开价格信息的缺口", "completed_step_ids": ["R1-2"]}},
        }
        return actions.get(round_no, actions[6])

    def assess_progress(self, context: dict[str, Any], observation: dict[str, Any]) -> dict[str, Any]:
        completed = []
        if observation.get("tool") == "read_web":
            completed = [observation["arguments"]["target_step_id"]]
        return {"completed_step_ids": completed, "reason": "离线夹具按读取结果确认步骤"}

    def revise_plan(self, context: dict[str, Any], reason: str) -> list[str]:
        pending = [step["description"] for step in context["plan"] if step["status"] != "completed"]
        return pending or ["基于现有证据形成结论"]

    def write_report(self, context: dict[str, Any]) -> str:
        return """# Alpha 与 Beta 竞品分析（离线演示）

> 本报告使用固定测试夹具，只用于验证 Agent 执行链路，不代表真实市场信息。

## 执行摘要

Alpha 面向小型产品团队，强调协作编辑并提供免费层及每月 20 美元的 Pro 方案 [S1]。Beta 强调自动化、企业控制和按量计费，但测试资料未给出公开价格 [S2]。

## 对比

| 维度 | Alpha | Beta |
|---|---|---|
| 定位 | 小型产品团队 [S1] | 企业自动化与管控 [S2] |
| 价格 | 免费层；Pro $20/月 [S1] | 按量计费；公开价格缺失 [S2] |
| 差异点 | 协作编辑 [S1] | 企业控制 [S2] |

## 结论与局限

小团队可优先核验 Alpha，重视企业控制的组织可进一步评估 Beta。当前证据仅有两份演示页面，缺少真实用户评价、性能和完整条款，不能用于采购决策。
"""


class ContextBuilder:
    def __init__(self, char_budget: int):
        self.char_budget = char_budget

    @staticmethod
    def _likely_official(source: Source) -> bool:
        parsed = urllib.parse.urlparse(source.url)
        host = (parsed.hostname or "").lower()
        path = parsed.path.lower()
        title = source.title.lower()
        return (
            "official" in title
            or host.startswith(("docs.", "developers.", "developer."))
            or any(part in path for part in ("/docs", "/pricing", "/product"))
        )

    def build(self, state: AgentState) -> dict[str, Any]:
        source_index = [
            {
                "id": source.id,
                "title": source.title[:120],
                "url": source.url[:240],
                "via": source.via,
                "has_full_content": bool(source.content),
                "likely_official": self._likely_official(source),
            }
            for source in state.sources
        ]
        recent_events = [
            {
                "time": event.get("time"),
                "event": event.get("event"),
                "detail": compact(event.get("detail", {}), 350),
            }
            for event in state.recent_events[-6:]
        ]
        fixed: dict[str, Any] = {
            "task": state.task,
            "plan": [asdict(step) for step in state.plan],
            "progress": {
                "round": state.round,
                "replan_count": state.replan_count,
                "source_count": len(state.sources),
                "blocked_urls": [url for url, count in state.read_attempts.items() if count >= 2],
            },
            "recent_events": recent_events,
            "source_index": source_index,
            "evidence": [],
            "context_policy": (
                "source_index 列出全部来源；evidence 在预算内优先放近期、已读取且疑似官方的来源；"
                "另含最近 6 个压缩事件，不包含完整对话历史。"
            ),
        }
        prioritized = sorted(
            state.sources,
            key=lambda source: (
                self._likely_official(source),
                source.via == "read_web",
                source.retrieved_at,
            ),
            reverse=True,
        )
        for source in prioritized:
            body = source.content or source.snippet
            item = {
                "id": source.id,
                "title": source.title,
                "url": source.url,
                "text": body[:1800],
            }
            fixed["evidence"].append(item)
            encoded_size = len(json.dumps(fixed, ensure_ascii=False))
            if encoded_size <= self.char_budget:
                continue
            overflow = encoded_size - self.char_budget
            item["text"] = item["text"][: max(0, len(item["text"]) - overflow - 20)]
            if not item["text"] or len(json.dumps(fixed, ensure_ascii=False)) > self.char_budget:
                fixed["evidence"].pop()
                break
        return fixed


class ReportContextBuilder:
    """Build a broad, source-balanced context used only for final synthesis."""

    def __init__(self, char_budget: int, per_source_limit: int = 7_000):
        self.char_budget = char_budget
        self.per_source_limit = per_source_limit

    @staticmethod
    def _domain_group(url: str) -> str:
        host = (urllib.parse.urlparse(url).hostname or "unknown").lower()
        parts = host.split(".")
        return ".".join(parts[-2:]) if len(parts) >= 2 else host

    def _ordered_full_sources(self, sources: list[Source]) -> list[Source]:
        candidates = sorted(
            (source for source in sources if source.content.strip()),
            key=lambda source: (
                ContextBuilder._likely_official(source),
                source.retrieved_at,
            ),
            reverse=True,
        )
        # Search APIs often return aliases or redirects with identical page text.
        unique: list[Source] = []
        seen_content: set[str] = set()
        for source in candidates:
            digest = hashlib.sha256(source.content.strip().encode("utf-8")).hexdigest()
            if digest in seen_content:
                continue
            seen_content.add(digest)
            unique.append(source)

        # Round-robin domains so one heavily researched product cannot consume the report budget.
        groups: dict[str, list[Source]] = {}
        for source in unique:
            groups.setdefault(self._domain_group(source.url), []).append(source)
        ordered: list[Source] = []
        while any(groups.values()):
            for group in groups.values():
                if group:
                    ordered.append(group.pop(0))
        return ordered

    def _try_add(self, context: dict[str, Any], key: str, item: dict[str, Any]) -> bool:
        context[key].append(item)
        encoded_size = len(json.dumps(context, ensure_ascii=False))
        if encoded_size <= self.char_budget:
            return True
        text_key = "content" if "content" in item else "snippet"
        overflow = encoded_size - self.char_budget
        item[text_key] = item[text_key][: max(0, len(item[text_key]) - overflow - 20)]
        if not item[text_key] or len(json.dumps(context, ensure_ascii=False)) > self.char_budget:
            context[key].pop()
            return False
        return True

    def build(self, state: AgentState) -> dict[str, Any]:
        context: dict[str, Any] = {
            "task": state.task,
            "completed_plan": [asdict(step) for step in state.plan],
            "coverage_requirements": ["定位", "价格", "核心能力", "适用场景"],
            "source_catalog": [
                {
                    "id": source.id,
                    "title": source.title[:120],
                    "url": source.url[:240],
                    "via": source.via,
                    "has_full_content": bool(source.content),
                    "retrieved_at": source.retrieved_at,
                }
                for source in state.sources
            ],
            "primary_evidence": [],
            "supplemental_evidence": [],
            "report_policy": (
                "先按产品和四个维度整理 primary_evidence；优先官方域名和网页正文。"
                "supplemental_evidence 仅用于补缺并必须标明证据较弱。不要把 source_catalog 标题当成事实。"
            ),
        }
        selected_ids: set[str] = set()
        for source in self._ordered_full_sources(state.sources):
            item = {
                "id": source.id,
                "title": source.title,
                "url": source.url,
                "retrieved_at": source.retrieved_at,
                "likely_official": ContextBuilder._likely_official(source),
                "content": source.content[: self.per_source_limit],
            }
            if not self._try_add(context, "primary_evidence", item):
                break
            selected_ids.add(source.id)

        for source in state.sources:
            if source.id in selected_ids or not source.snippet.strip():
                continue
            item = {
                "id": source.id,
                "title": source.title,
                "url": source.url,
                "retrieved_at": source.retrieved_at,
                "snippet": source.snippet[:900],
            }
            if not self._try_add(context, "supplemental_evidence", item):
                break
        return context


class CompetitorAgent:
    MAX_SAME_ACTIONS = 2
    REPLAN_REPETITION_THRESHOLD = 2

    def __init__(
        self,
        llm: LLMBackend,
        tools: ResearchTools,
        config: Config,
        run_dir: Path,
        state: AgentState,
        echo_logs: bool = True,
    ):
        self.llm = llm
        self.tools = tools
        self.config = config
        self.run_dir = run_dir
        self.state = state
        self.state_path = run_dir / "state.json"
        self.logger = RunLogger(
            run_dir / "run.jsonl", echo=echo_logs, append=state.round > 0
        )
        self.context_builder = ContextBuilder(config.context_char_budget)
        self.report_context_builder = ReportContextBuilder(config.report_context_char_budget)
        self._bootstrap_guard_state()

    @staticmethod
    def _action_signature(action: dict[str, Any]) -> str | None:
        name = action.get("name")
        args = action.get("arguments") or {}
        if name == "read_web" and args.get("url"):
            url, _ = urllib.parse.urldefrag(str(args["url"]).strip())
            return f"read_web|{url.rstrip('/').lower()}"
        if name == "search_web" and args.get("query"):
            query = " ".join(str(args["query"]).lower().split())
            return f"search_web|{query}"
        return None

    def _bootstrap_guard_state(self) -> None:
        """Populate guard counters when resuming a state created by v1."""
        if self.state.action_history:
            return
        signatures: list[str] = []
        for event in self.state.recent_events:
            if event.get("event") != "agent_decision":
                continue
            action = event.get("detail", {}).get("action", {})
            signature = self._action_signature(action)
            if signature:
                signatures.append(signature)
                if signature.startswith("read_web|"):
                    url = signature.split("|", 1)[1]
                    self.state.read_attempts[url] = self.state.read_attempts.get(url, 0) + 1
        self.state.action_history = signatures[-40:]

    def _register_action(self, action: dict[str, Any]) -> dict[str, Any]:
        signature = self._action_signature(action)
        if not signature:
            return {"signature": None, "duplicate": False, "blocked": False, "previous_count": 0}
        previous_count = self.state.action_history.count(signature)
        duplicate = previous_count > 0
        blocked = previous_count >= self.MAX_SAME_ACTIONS
        self.state.action_history.append(signature)
        self.state.action_history = self.state.action_history[-40:]
        if signature.startswith("read_web|"):
            url = signature.split("|", 1)[1]
            previous_reads = self.state.read_attempts.get(url, 0)
            blocked = blocked or previous_reads >= self.MAX_SAME_ACTIONS
            self.state.read_attempts[url] = previous_reads + 1
        if duplicate:
            self.state.repetition_strikes += 1
        return {
            "signature": signature,
            "duplicate": duplicate,
            "blocked": blocked,
            "previous_count": previous_count,
            "repetition_strikes": self.state.repetition_strikes,
        }

    def _record(self, event: str, **detail: Any) -> None:
        self.logger.emit(event, **detail)
        self.state.add_event(event, detail)

    def _upsert_search_sources(self, output: dict[str, Any]) -> int:
        added = 0
        known = {source.url: source for source in self.state.sources}
        for result in output.get("results", []):
            if result["url"] in known:
                known[result["url"]].snippet = result.get("snippet", known[result["url"]].snippet)
                continue
            source = Source(
                id=f"S{len(self.state.sources) + 1}",
                title=result.get("title", "Untitled"),
                url=result["url"],
                snippet=result.get("snippet", ""),
            )
            self.state.sources.append(source)
            known[source.url] = source
            added += 1
        return added

    def _upsert_read_source(self, output: dict[str, Any]) -> str:
        for source in self.state.sources:
            if source.url == output["url"]:
                source.title = output.get("title") or source.title
                source.content = output.get("content", "")
                source.via = "read_web"
                source.retrieved_at = utc_now()
                return source.id
        source = Source(
            id=f"S{len(self.state.sources) + 1}",
            title=output.get("title", output["url"]),
            url=output["url"],
            content=output.get("content", ""),
            via="read_web",
        )
        self.state.sources.append(source)
        return source.id

    def _apply_replan(self, new_steps: list[str]) -> list[str]:
        completed = [step for step in self.state.plan if step.status == "completed"]
        replanned = [
            PlanStep(id=f"R{self.state.replan_count + 1}-{index}", description=text)
            for index, text in enumerate(new_steps, 1)
            if text.strip()
        ]
        if not replanned:
            raise ValueError("replan 至少需要一个非空步骤")
        self.state.plan = completed + replanned
        self.state.replan_count += 1
        self.state.repetition_strikes = 0
        return [step.id for step in replanned]

    def _force_replan(self, reason: str) -> list[str]:
        context = self.context_builder.build(self.state)
        new_steps = self.llm.revise_plan(context, reason)
        new_ids = self._apply_replan(new_steps)
        self._record(
            "forced_replan",
            reason=reason,
            new_step_ids=new_ids,
            new_steps=new_steps,
        )
        return new_ids

    def _assess_progress(self, action: dict[str, Any], observation: dict[str, Any]) -> list[str]:
        assessment_input = {
            "tool": action.get("name"),
            "arguments": action.get("arguments") or {},
            "observation": observation,
        }
        assessment = self.llm.assess_progress(
            self.context_builder.build(self.state), assessment_input
        )
        requested = assessment.get("completed_step_ids", [])
        if not isinstance(requested, list):
            requested = []
        completed = self.state.complete_steps([str(step_id) for step_id in requested])
        if completed:
            self.state.repetition_strikes = 0
        self._record(
            "progress_assessment",
            requested_step_ids=requested,
            completed_step_ids=completed,
            reason=str(assessment.get("reason", "")),
        )
        return completed

    def _execute(self, action: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
        name = action.get("name")
        args = action.get("arguments") or {}
        if name == "search_web":
            output = self.tools.search_web(args["query"])
            added = self._upsert_search_sources(output)
            return False, {"result_count": len(output.get("results", [])), "sources_added": added, "preview": output}
        if name == "read_web":
            output = self.tools.read_web(args["url"])
            source_id = self._upsert_read_source(output)
            return False, {"source_id": source_id, "content_chars": len(output.get("content", "")), "preview": {**output, "content": output.get("content", "")[:500]}}
        if name == "replan":
            new_ids = self._apply_replan(args["new_steps"])
            return False, {"reason": args["reason"], "new_step_ids": new_ids, "replan": True}
        if name == "finish":
            if not self.state.sources:
                raise RuntimeError("尚未收集任何来源，不能 finish")
            completed = self.state.complete_steps(args.get("completed_step_ids", []))
            return True, {"reason": args["reason"], "completed_steps": completed}
        raise ValueError(f"未知工具: {name}")

    def run(self) -> Path:
        self.run_dir.mkdir(parents=True, exist_ok=True)
        if not self.state.plan:
            steps = self.llm.create_plan(self.state.task)
            self.state.plan = [PlanStep(id=f"P{i}", description=text) for i, text in enumerate(steps, 1)]
            self.state.status = "running"
            self._record("planning", task=self.state.task, plan=[asdict(step) for step in self.state.plan])
            self.state.save(self.state_path)
        else:
            self.state.status = "running"
            self._record("resume", task=self.state.task, round=self.state.round)

        finished = False
        while self.state.round < self.config.max_rounds and not finished:
            self.state.round += 1
            context = self.context_builder.build(self.state)
            pending = [asdict(step) for step in self.state.plan if step.status != "completed"]
            self._record(
                "round_start",
                round=self.state.round,
                current_task=self.state.task,
                current_plan=[asdict(step) for step in self.state.plan],
                current_step=pending[0] if pending else None,
                context_chars=len(json.dumps(context, ensure_ascii=False)),
            )
            action = self.llm.decide(context, TOOL_SCHEMAS)
            self._record("agent_decision", round=self.state.round, action=action)
            guard = self._register_action(action)
            if guard["duplicate"]:
                self._record(
                    "repeated_action_detected",
                    round=self.state.round,
                    signature=guard["signature"],
                    previous_count=guard["previous_count"],
                    repetition_strikes=guard["repetition_strikes"],
                )
            if guard["blocked"]:
                reason = f"动作 {guard['signature']} 已执行至少 {self.MAX_SAME_ACTIONS} 次，拒绝再次执行"
                self._record("action_blocked", round=self.state.round, reason=reason)
                if self.state.repetition_strikes >= self.REPLAN_REPETITION_THRESHOLD:
                    try:
                        self._force_replan(reason)
                    except Exception as exc:
                        self._record("forced_replan_error", round=self.state.round, error=str(exc))
                self.state.save(self.state_path)
                continue
            try:
                finished, observation = self._execute(action)
                if action.get("name") in {"search_web", "read_web"}:
                    try:
                        observation["completed_steps"] = self._assess_progress(action, observation)
                    except Exception as exc:
                        observation["completed_steps"] = []
                        self._record(
                            "progress_assessment_error",
                            round=self.state.round,
                            error=str(exc),
                        )
                self._record(
                    "tool_result",
                    round=self.state.round,
                    tool=action.get("name"),
                    observation=observation,
                    state_change={
                        "source_count": len(self.state.sources),
                        "completed_steps": [step.id for step in self.state.plan if step.status == "completed"],
                        "replan_count": self.state.replan_count,
                        "replan_occurred": action.get("name") == "replan",
                    },
                )
            except Exception as exc:
                # Tool errors are observations: keep looping so the model can recover/replan.
                self._record("tool_error", round=self.state.round, tool=action.get("name"), error=str(exc))
            self.state.save(self.state_path)

        if not finished:
            self.state.status = "failed"
            self._record("run_failed", reason=f"达到最大轮数 {self.config.max_rounds}")
            self.state.save(self.state_path)
            raise RuntimeError(f"Agent 未在 {self.config.max_rounds} 轮内结束")

        report_context = self.report_context_builder.build(self.state)
        self._record(
            "report_context_built",
            context_chars=len(json.dumps(report_context, ensure_ascii=False)),
            primary_source_ids=[item["id"] for item in report_context["primary_evidence"]],
            supplemental_source_ids=[item["id"] for item in report_context["supplemental_evidence"]],
        )
        report = self.llm.write_report(report_context).strip()
        if not report:
            raise RuntimeError("模型返回了空报告")
        report += "\n\n## 来源\n\n" + "\n".join(
            f"- [{source.id}] [{source.title}]({source.url})（{source.retrieved_at}）"
            for source in self.state.sources
        )
        report_path = self.run_dir / "report.md"
        report_path.write_text(report + "\n", encoding="utf-8")
        self.state.status = "completed"
        self.state.report_path = str(report_path)
        self._record("run_completed", report_path=str(report_path), rounds=self.state.round, sources=len(self.state.sources), replans=self.state.replan_count)
        self.state.save(self.state_path)
        return report_path


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="从零实现的单 Agent 竞品分析系统")
    parser.add_argument("--task", help="竞品分析目标")
    parser.add_argument("--demo", action="store_true", help="使用固定夹具离线运行完整链路")
    parser.add_argument("--resume", type=Path, help="从已有 state.json 恢复")
    parser.add_argument("--output-dir", type=Path, help="本次产物目录")
    parser.add_argument("--max-rounds", type=int, help="覆盖最大轮数")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    # Keep Chinese logs readable when PowerShell's legacy code page differs.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    args = parse_args(argv)
    config = Config.from_env()
    if args.max_rounds:
        config.max_rounds = args.max_rounds
    if args.resume:
        state = AgentState.load(args.resume)
        run_dir = args.output_dir or args.resume.parent
    else:
        task = args.task or ("比较 Alpha 与 Beta 的定位、定价和适用场景" if args.demo else None)
        if not task:
            print("错误：真实运行必须传入 --task", file=sys.stderr)
            return 2
        state = AgentState(task=task)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        run_dir = args.output_dir or Path("artifacts") / ("demo" if args.demo else stamp)
    llm: LLMBackend = DemoLLMBackend() if args.demo else OpenAIBackend(config)
    tools = DemoResearchTools(config) if args.demo else ResearchTools(config)
    agent = CompetitorAgent(llm, tools, config, run_dir, state)
    try:
        report_path = agent.run()
    except Exception as exc:
        print(f"运行失败：{exc}", file=sys.stderr)
        return 1
    print(f"\n完成。报告：{report_path}\n状态：{agent.state_path}\n日志：{agent.logger.path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
