"""Single-agent orchestration loop."""

from __future__ import annotations

import json
import urllib.parse
from dataclasses import asdict
from pathlib import Path
from typing import Any

from agent.context import ContextBuilder, ReportContextBuilder
from agent.state import AgentState, PlanStep
from config.settings import Config
from llm.backend import LLMBackend
from storage.logger import RunLogger
from tools.registry import ToolRegistry


class CompetitorAgent:
    MAX_SAME_ACTIONS = 2
    REPLAN_REPETITION_THRESHOLD = 2

    def __init__(
        self,
        llm: LLMBackend,
        tool_registry: ToolRegistry,
        config: Config,
        run_dir: Path,
        state: AgentState,
        echo_logs: bool = True,
    ):
        self.llm = llm
        self.tool_registry = tool_registry
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

    def _force_replan(self, reason: str) -> list[str]:
        context = self.context_builder.build(self.state)
        new_steps = self.llm.revise_plan(context, reason)
        _, observation = self.tool_registry.execute(
            "replan",
            {"reason": reason, "new_steps": new_steps},
        )
        new_ids = observation["new_step_ids"]
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

    def _execute(self, action: dict[str, Any]) -> tuple[bool, Any]:
        return self.tool_registry.execute(
            action.get("name"),
            action.get("arguments") or {},
        )

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
            action = self.llm.decide(context, self.tool_registry.schemas())
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
