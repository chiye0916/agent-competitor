"""Explainable source evaluation for the ingestion pipeline."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from urllib.parse import urlsplit

from agent.state import Source
from knowledge.document import Document
from knowledge.policy import KnowledgePolicy


@dataclass(frozen=True)
class EvaluationResult:
    accepted: bool
    reason: str
    source_id: str
    quality_flags: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


class KnowledgeEvaluator:
    """Apply deterministic policy rules without mutating State or calling storage."""

    _NOT_FOUND_TITLE = re.compile(
        r"^\s*(?:404(?:\s*(?:not\s+found|error))?|page\s+not\s+found|"
        r"not\s+found|页面不存在|页面未找到|找不到页面)"
        r"(?:\s*[|:_-]\s*[^|:_-]{1,80})?\s*$",
        re.IGNORECASE,
    )
    _NOT_FOUND_BODY = re.compile(
        r"(?:page\s+not\s+found|page\s+(?:doesn['’]?t|does\s+not)\s+exist|"
        r"can['’]?t\s+find\s+the\s+page|找不到.{0,12}页面|页面(?:不存在|未找到))",
        re.IGNORECASE,
    )
    _ERROR_CODE_BODY = re.compile(r"(?:^|\s)404\s*(?:错误|error|not\s+found)", re.IGNORECASE)
    _RECOVERY_ACTION = re.compile(
        r"(?:返回首页|回到首页|back\s+to\s+(?:home|homepage)|return\s+home|go\s+to\s+homepage)",
        re.IGNORECASE,
    )
    _GENERIC_ERROR_TITLE = re.compile(
        r"^\s*(?:error|server\s+error|service\s+unavailable|访问错误|系统错误)\s*$",
        re.IGNORECASE,
    )
    _GENERIC_ERROR_BODY = re.compile(
        r"(?:something\s+went\s+wrong|service\s+unavailable|internal\s+server\s+error|"
        r"系统(?:发生|出现)错误|服务暂不可用)",
        re.IGNORECASE,
    )

    def __init__(self, policy: KnowledgePolicy) -> None:
        self.policy = policy

    def evaluate(self, source: Source) -> EvaluationResult:
        """Compatibility wrapper for callers that still hold Agent Sources."""

        return self.evaluate_document(Document.from_source(source))

    def evaluate_document(self, document: Document) -> EvaluationResult:
        source_id = str(document.metadata.get("source_id") or document.id)
        flags: list[str] = []
        try:
            parsed = urlsplit((document.url or "").strip())
            _ = parsed.port
            valid_url = (
                parsed.scheme.lower() in self.policy.allowed_url_schemes
                and bool(parsed.hostname)
                and parsed.username is None
                and parsed.password is None
            )
        except ValueError:
            valid_url = False
        if not valid_url:
            return EvaluationResult(False, "invalid_http_url", source_id, flags)
        if document.metadata.get("via") != "read_web":
            return EvaluationResult(False, "missing_verified_full_text", source_id, flags)
        raw_status = document.metadata.get("http_status")
        if raw_status is not None:
            try:
                http_status = int(raw_status)
            except (TypeError, ValueError):
                return EvaluationResult(False, "http_error", source_id, flags)
            if not 200 <= http_status < 300:
                reason = "http_404" if http_status == 404 else "http_error"
                return EvaluationResult(False, reason, source_id, flags)
        if not document.content or not document.content.strip():
            return EvaluationResult(False, "empty_content", source_id, flags)
        if "\x00" in document.content:
            return EvaluationResult(False, "unsupported_non_text_content", source_id, flags)

        normalized = " ".join(document.content.split())
        title = " ".join(document.title.split())
        opening = normalized[:1500]
        title_not_found = bool(self._NOT_FOUND_TITLE.search(title))
        body_not_found = bool(self._NOT_FOUND_BODY.search(opening))
        body_404 = bool(self._ERROR_CODE_BODY.search(opening))
        recovery_action = bool(self._RECOVERY_ACTION.search(opening))
        if (title_not_found and (body_not_found or body_404)) or (
            body_not_found and body_404 and recovery_action
        ):
            return EvaluationResult(False, "not_found_page", source_id, flags)
        if self._GENERIC_ERROR_TITLE.match(title) and self._GENERIC_ERROR_BODY.search(opening):
            return EvaluationResult(False, "invalid_page", source_id, flags)
        if len(normalized) < self.policy.min_content_chars:
            return EvaluationResult(False, "content_too_short", source_id, flags)
        meaningful = sum(character.isalnum() for character in normalized)
        if meaningful < self.policy.min_meaningful_chars:
            return EvaluationResult(False, "low_text_quality", source_id, flags)
        if not document.title.strip():
            flags.append("missing_title")
        return EvaluationResult(True, "valid_full_text", source_id, flags)
