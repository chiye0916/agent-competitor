"""Explainable source evaluation for the ingestion pipeline."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from urllib.parse import urlsplit

from agent.state import Source
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

    def __init__(self, policy: KnowledgePolicy) -> None:
        self.policy = policy

    def evaluate(self, source: Source) -> EvaluationResult:
        flags: list[str] = []
        try:
            parsed = urlsplit(source.url.strip())
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
            return EvaluationResult(False, "invalid_http_url", source.id, flags)
        if source.via != "read_web":
            return EvaluationResult(False, "missing_verified_full_text", source.id, flags)
        if not source.content or not source.content.strip():
            return EvaluationResult(False, "empty_content", source.id, flags)
        if "\x00" in source.content:
            return EvaluationResult(False, "unsupported_non_text_content", source.id, flags)

        normalized = " ".join(source.content.split())
        if len(normalized) < self.policy.min_content_chars:
            return EvaluationResult(False, "content_too_short", source.id, flags)
        meaningful = sum(character.isalnum() for character in normalized)
        if meaningful < self.policy.min_meaningful_chars:
            return EvaluationResult(False, "low_text_quality", source.id, flags)
        if not source.title.strip():
            flags.append("missing_title")
        return EvaluationResult(True, "valid_full_text", source.id, flags)
