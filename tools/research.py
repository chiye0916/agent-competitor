"""Search and public-web reading tools."""

from __future__ import annotations

import html
import ipaddress
import json
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from typing import Any

from agent.state import AgentState, PlanStep, Source
from config.settings import Config
from storage.logger import utc_now
from tools.base import BaseTool
from tools.schemas import (
    FINISH_DESCRIPTION,
    FINISH_INPUT_SCHEMA,
    READ_WEB_DESCRIPTION,
    READ_WEB_INPUT_SCHEMA,
    REPLAN_DESCRIPTION,
    REPLAN_INPUT_SCHEMA,
    SEARCH_WEB_DESCRIPTION,
    SEARCH_WEB_INPUT_SCHEMA,
)


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
                http_status = getattr(response, "status", None)
                if http_status is None and hasattr(response, "getcode"):
                    http_status = response.getcode()
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
        return {
            "url": url,
            "title": title[:300],
            "content": text[:50_000],
            "http_status": int(http_status) if http_status is not None else None,
        }


class SearchWebTool(BaseTool):
    name = "search_web"
    description = SEARCH_WEB_DESCRIPTION
    input_schema = SEARCH_WEB_INPUT_SCHEMA

    def __init__(self, research_tools: ResearchTools, state: AgentState):
        self.research_tools = research_tools
        self.state = state

    def execute(self, **kwargs: Any) -> dict[str, Any]:
        output = self.research_tools.search_web(kwargs["query"])
        added = 0
        known = {source.url: source for source in self.state.sources}
        for result in output.get("results", []):
            if result["url"] in known:
                known[result["url"]].snippet = result.get(
                    "snippet", known[result["url"]].snippet
                )
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
        return {
            "result_count": len(output.get("results", [])),
            "sources_added": added,
            "preview": output,
        }


class ReadWebTool(BaseTool):
    name = "read_web"
    description = READ_WEB_DESCRIPTION
    input_schema = READ_WEB_INPUT_SCHEMA

    def __init__(self, research_tools: ResearchTools, state: AgentState):
        self.research_tools = research_tools
        self.state = state

    def execute(self, **kwargs: Any) -> dict[str, Any]:
        output = self.research_tools.read_web(kwargs["url"])
        source_id = self._upsert_source(output)
        return {
            "source_id": source_id,
            "content_chars": len(output.get("content", "")),
            "preview": {**output, "content": output.get("content", "")[:500]},
        }

    def _upsert_source(self, output: dict[str, Any]) -> str:
        for source in self.state.sources:
            if source.url == output["url"]:
                source.title = output.get("title") or source.title
                source.content = output.get("content", "")
                source.via = "read_web"
                source.retrieved_at = utc_now()
                source.http_status = output.get("http_status")
                return source.id
        source = Source(
            id=f"S{len(self.state.sources) + 1}",
            title=output.get("title", output["url"]),
            url=output["url"],
            content=output.get("content", ""),
            via="read_web",
            http_status=output.get("http_status"),
        )
        self.state.sources.append(source)
        return source.id


class ReplanTool(BaseTool):
    name = "replan"
    description = REPLAN_DESCRIPTION
    input_schema = REPLAN_INPUT_SCHEMA

    def __init__(self, state: AgentState):
        self.state = state

    def execute(self, **kwargs: Any) -> dict[str, Any]:
        completed = [step for step in self.state.plan if step.status == "completed"]
        replanned = [
            PlanStep(
                id=f"R{self.state.replan_count + 1}-{index}",
                description=text,
            )
            for index, text in enumerate(kwargs["new_steps"], 1)
            if text.strip()
        ]
        if not replanned:
            raise ValueError("replan 至少需要一个非空步骤")
        self.state.plan = completed + replanned
        self.state.replan_count += 1
        self.state.repetition_strikes = 0
        return {
            "reason": kwargs["reason"],
            "new_step_ids": [step.id for step in replanned],
            "replan": True,
        }


class FinishTool(BaseTool):
    name = "finish"
    description = FINISH_DESCRIPTION
    input_schema = FINISH_INPUT_SCHEMA
    ends_run = True

    def __init__(self, state: AgentState):
        self.state = state

    def execute(self, **kwargs: Any) -> dict[str, Any]:
        if not self.state.sources and not self.state.retrieved_evidence:
            raise RuntimeError("尚未收集任何网页来源或知识证据，不能 finish")
        completed = self.state.complete_steps(kwargs.get("completed_step_ids", []))
        return {
            "reason": kwargs["reason"],
            "completed_steps": completed,
        }
