"""Append-only run logging and shared serialization helpers."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def compact(value: Any, limit: int = 600) -> str:
    text = json.dumps(value, ensure_ascii=False, default=str)
    return text if len(text) <= limit else text[:limit] + "..."


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
