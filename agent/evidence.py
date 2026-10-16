"""Current-task knowledge evidence and deterministic report citations."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

if TYPE_CHECKING:
    from knowledge.chunk import Chunk


@dataclass
class RetrievedEvidence:
    """A knowledge Chunk actually returned to the Agent in the current task."""

    id: str
    chunk_id: str
    document_id: str
    content: str
    source_url: str
    source_title: str
    source_id: str | None = None
    content_hash: str | None = None
    retrieved_at: str | None = None
    ingested_at: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RetrievedEvidence":
        copy = dict(data)
        copy["metadata"] = dict(copy.get("metadata") or {})
        return cls(**copy)

    @classmethod
    def from_chunk(cls, chunk: "Chunk", evidence_id: str = "") -> "RetrievedEvidence":
        metadata = dict(chunk.metadata or {})
        return cls(
            id=evidence_id,
            chunk_id=chunk.id,
            document_id=chunk.document_id,
            content=chunk.text,
            source_url=str(metadata.get("source_url") or metadata.get("url") or ""),
            source_title=str(
                metadata.get("source_title") or metadata.get("title") or chunk.document_id
            ),
            source_id=_optional_string(metadata.get("source_id")),
            content_hash=_optional_string(metadata.get("content_hash")),
            retrieved_at=_optional_string(metadata.get("retrieved_at")),
            ingested_at=_optional_string(metadata.get("ingested_at")),
            metadata=metadata,
        )


def _optional_string(value: Any) -> str | None:
    return str(value) if value not in (None, "") else None


class ReportCitationRenderer:
    """Map model-selected internal evidence IDs to trusted numbered references."""

    _INTERNAL_ID_RE = re.compile(r"\[((?:S|E)\d+)\]")
    _NUMBERED_CITATION_RE = re.compile(r"\[\d+\]")
    _REFERENCE_SECTION_RE = re.compile(
        r"(?ims)(?:^|\n)#{2,6}[ \t]*(?:参考来源|来源|references?|sources?)[ \t]*\n.*\Z"
    )
    _MARKDOWN_URL_RE = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")
    _URL_RE = re.compile(r"https?://[^\s<>)\]]+")

    @classmethod
    def render(
        cls,
        report: str,
        report_context: dict[str, Any],
    ) -> tuple[str, list[dict[str, Any]]]:
        index = cls._build_index(report_context)
        allowed_urls = {item["url"] for item in index.values()}
        # The model selects only internal IDs; final numbering and the source list
        # belong exclusively to the runtime.
        body = cls._REFERENCE_SECTION_RE.sub("", report.strip()).rstrip()
        body = cls._NUMBERED_CITATION_RE.sub("", body)
        body = cls._remove_untrusted_urls(body, allowed_urls)
        ordered_ids: list[str] = []

        def replace_id(match: re.Match[str]) -> str:
            internal_id = match.group(1)
            if internal_id not in index:
                return ""
            if internal_id not in ordered_ids:
                ordered_ids.append(internal_id)
            return f"[{ordered_ids.index(internal_id) + 1}]"

        body = cls._INTERNAL_ID_RE.sub(replace_id, body).rstrip()
        citations: list[dict[str, Any]] = []
        for number, internal_id in enumerate(ordered_ids, 1):
            citation = {"number": number, "internal_id": internal_id, **index[internal_id]}
            citations.append(citation)

        if citations:
            body += "\n\n## 参考来源\n\n" + "\n\n".join(
                cls._format_citation(item) for item in citations
            )
        return body, citations

    @classmethod
    def _build_index(cls, context: dict[str, Any]) -> dict[str, dict[str, Any]]:
        index: dict[str, dict[str, Any]] = {}
        for item in context.get("primary_evidence", []) + context.get(
            "supplemental_evidence", []
        ):
            cls._add_candidate(
                index,
                internal_id=item.get("id"),
                title=item.get("title"),
                url=item.get("url"),
                retrieved_at=item.get("retrieved_at"),
                kind="web",
                source_id=item.get("id"),
            )
        for item in context.get("retrieved_evidence", []):
            cls._add_candidate(
                index,
                internal_id=item.get("id"),
                title=item.get("source_title"),
                url=item.get("source_url"),
                retrieved_at=item.get("retrieved_at"),
                ingested_at=item.get("ingested_at"),
                kind="knowledge",
                source_id=item.get("source_id"),
                document_id=item.get("document_id"),
                chunk_id=item.get("chunk_id"),
                content_hash=item.get("content_hash"),
            )
        return index

    @classmethod
    def _add_candidate(
        cls,
        index: dict[str, dict[str, Any]],
        *,
        internal_id: Any,
        title: Any,
        url: Any,
        **metadata: Any,
    ) -> None:
        identifier = str(internal_id or "")
        source_url = str(url or "")
        if not identifier or not cls._is_public_url(source_url):
            return
        index[identifier] = {
            "title": " ".join(str(title or identifier).split()),
            "url": source_url,
            **metadata,
        }

    @staticmethod
    def _is_public_url(url: str) -> bool:
        parsed = urlparse(url)
        return parsed.scheme in {"http", "https"} and bool(parsed.netloc)

    @classmethod
    def _remove_untrusted_urls(cls, report: str, allowed_urls: set[str]) -> str:
        def replace_markdown(match: re.Match[str]) -> str:
            return match.group(0) if match.group(2) in allowed_urls else match.group(1)

        cleaned = cls._MARKDOWN_URL_RE.sub(replace_markdown, report)
        return cls._URL_RE.sub(
            lambda match: match.group(0) if match.group(0) in allowed_urls else "",
            cleaned,
        )

    @staticmethod
    def _format_citation(citation: dict[str, Any]) -> str:
        lines = [
            f"[{citation['number']}] {citation['title']}",
            f"    URL: {citation['url']}",
        ]
        if citation.get("retrieved_at"):
            lines.append(f"    获取时间: {citation['retrieved_at']}")
        if citation["kind"] == "knowledge":
            if citation.get("ingested_at"):
                lines.append(f"    入库时间: {citation['ingested_at']}")
            lines.append(f"    Evidence ID: {citation['internal_id']}")
            if citation.get("source_id"):
                lines.append(f"    Source ID: {citation['source_id']}")
            lines.append(f"    Document ID: {citation['document_id']}")
            lines.append(f"    Chunk ID: {citation['chunk_id']}")
            if citation.get("content_hash"):
                lines.append(f"    Content Hash: {citation['content_hash']}")
        else:
            lines.append(f"    Source ID: {citation['source_id']}")
        return "\n".join(lines)
