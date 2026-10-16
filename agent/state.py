"""Serializable task state for the single-agent runtime."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from agent.evidence import RetrievedEvidence
from storage.logger import utc_now


KNOWLEDGE_MODES = {"existing", "latest"}
LATEST_KNOWLEDGE_MARKERS = (
    "最新",
    "当前",
    "截至今天",
    "实时",
    "检查最新信息",
    "重新核实",
    "latest",
    "current",
    "as of today",
    "real-time",
    "revalidate",
)


def infer_knowledge_mode(task: str) -> str:
    normalized = task.lower()
    return (
        "latest"
        if any(marker.lower() in normalized for marker in LATEST_KNOWLEDGE_MARKERS)
        else "existing"
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
    knowledge_mode: str = "existing"
    retrieved_evidence: list[RetrievedEvidence] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.knowledge_mode not in KNOWLEDGE_MODES:
            raise ValueError("knowledge_mode 必须是 existing 或 latest")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AgentState":
        copy = dict(data)
        copy["plan"] = [PlanStep(**item) for item in copy.get("plan", [])]
        copy["sources"] = [Source(**item) for item in copy.get("sources", [])]
        copy["retrieved_evidence"] = [
            RetrievedEvidence.from_dict(item)
            for item in copy.get("retrieved_evidence", [])
        ]
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

    def add_retrieved_evidence(self, evidence: RetrievedEvidence) -> RetrievedEvidence:
        for existing in self.retrieved_evidence:
            if existing.chunk_id == evidence.chunk_id:
                return existing
        used_ids = {item.id for item in self.retrieved_evidence}
        next_index = 1
        while f"E{next_index}" in used_ids:
            next_index += 1
        evidence.id = f"E{next_index}"
        self.retrieved_evidence.append(evidence)
        return evidence
