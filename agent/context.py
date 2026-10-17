"""Bounded decision and report context builders."""

from __future__ import annotations

import hashlib
import json
import urllib.parse
from dataclasses import asdict
from typing import Any

from agent.evidence import RetrievedEvidence
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
            "knowledge_mode": state.knowledge_mode,
            "knowledge_policy": (
                "用户要求检查最新信息；可以复用已有知识，但应自主判断是否用 search_web/read_web "
                "核验关键时效性事实。"
                if state.knowledge_mode == "latest"
                else "默认使用已有知识；不要仅因资料较旧就自动搜索，资料缺失时仍可自主搜索。"
            ),
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
            "retrieved_evidence": [],
            "context_policy": (
                "source_index 列出全部网页来源；evidence 是本次网页证据；"
                "retrieved_evidence 是本次任务实际从知识库召回的 Chunk；"
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
        def add_bounded(key: str, item: dict[str, Any], text_key: str) -> bool:
            fixed[key].append(item)
            encoded_size = len(json.dumps(fixed, ensure_ascii=False))
            if encoded_size <= self.char_budget:
                return True
            overflow = encoded_size - self.char_budget
            item[text_key] = item[text_key][
                : max(0, len(item[text_key]) - overflow - 20)
            ]
            if not item[text_key] or len(json.dumps(fixed, ensure_ascii=False)) > self.char_budget:
                fixed[key].pop()
                return False
            return True

        def add_web_sources() -> None:
            for source in prioritized:
                body = source.content or source.snippet
                if not add_bounded(
                    "evidence",
                    {
                        "id": source.id,
                        "title": source.title,
                        "url": source.url,
                        "retrieved_at": source.retrieved_at,
                        "text": body[:1800],
                    },
                    "text",
                ):
                    break

        def add_knowledge_evidence() -> None:
            for evidence in state.retrieved_evidence:
                if not add_bounded(
                    "retrieved_evidence",
                    self._knowledge_item(evidence, 1800, "text"),
                    "text",
                ):
                    break

        if state.knowledge_mode == "latest":
            add_web_sources()
            add_knowledge_evidence()
        else:
            add_knowledge_evidence()
            add_web_sources()
        return fixed

    @staticmethod
    def _knowledge_item(
        evidence: RetrievedEvidence,
        text_limit: int,
        text_key: str,
    ) -> dict[str, Any]:
        return {
            "id": evidence.id,
            "chunk_id": evidence.chunk_id,
            "document_id": evidence.document_id,
            "source_id": evidence.source_id,
            "source_title": evidence.source_title,
            "source_url": evidence.source_url,
            "content_hash": evidence.content_hash,
            "retrieved_at": evidence.retrieved_at,
            "ingested_at": evidence.ingested_at,
            text_key: evidence.content[:text_limit],
        }


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
            "knowledge_mode": state.knowledge_mode,
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
            "retrieved_evidence": [],
            "report_policy": (
                "先按产品和四个维度整理证据；latest 模式优先本次网页正文，existing 模式优先已召回知识。"
                "supplemental_evidence 仅用于补缺并必须标明证据较弱。不要把 source_catalog 标题当成事实。"
            ),
            "citation_policy": (
                "网页证据只能引用其 S# ID，知识证据只能引用其 E# ID。"
                "不要输出 URL 或参考来源列表，程序会把实际使用的有效 ID 转换为编号并追加可信来源。"
            ),
        }
        selected_ids: set[str] = set()

        def add_web_sources() -> None:
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

        def add_knowledge_evidence() -> None:
            for evidence in state.retrieved_evidence:
                item = ContextBuilder._knowledge_item(
                    evidence,
                    self.per_source_limit,
                    "content",
                )
                if not self._try_add(context, "retrieved_evidence", item):
                    break

        if state.knowledge_mode == "latest":
            add_web_sources()
            add_knowledge_evidence()
        else:
            add_knowledge_evidence()
            add_web_sources()
        return context
