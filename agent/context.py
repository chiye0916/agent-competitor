"""Bounded decision and report context builders."""

from __future__ import annotations

import hashlib
import json
import urllib.parse
from dataclasses import asdict
from typing import Any

from agent.state import AgentState, Source
from storage.logger import compact


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
