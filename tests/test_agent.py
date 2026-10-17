import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from main import (
    AgentState,
    BaseTool,
    CompetitorAgent,
    Config,
    ContextBuilder,
    DemoLLMBackend,
    DemoResearchTools,
    LLMResponseError,
    OpenAIBackend,
    PlanStep,
    ReportContextBuilder,
    ResearchTools,
    Source,
    ToolRegistry,
    ToolValidationError,
    build_default_registry,
)


class FakeHeaders:
    def __init__(self, content_type="text/html", charset="utf-8"):
        self.content_type = content_type
        self.charset = charset

    def get_content_type(self):
        return self.content_type

    def get_content_charset(self):
        return self.charset


class FakeResponse:
    def __init__(self, body, content_type="application/json", status=200):
        self.body = body
        self.headers = FakeHeaders(content_type)
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, *args):
        return self.body


class RepeatingLLM(DemoLLMBackend):
    def __init__(self):
        self.assessment_calls = 0
        self.revise_calls = 0

    def create_plan(self, task):
        return ["读取目标页面"]

    def decide(self, context, tools):
        if context["progress"]["round"] <= 3:
            return {
                "name": "read_web",
                "arguments": {
                    "url": "https://example.test/alpha",
                    "purpose": "重复读取测试",
                    "target_step_id": "P1",
                },
            }
        return {
            "name": "finish",
            "arguments": {"reason": "重复保护已生效", "completed_step_ids": ["R1-1"]},
        }

    def assess_progress(self, context, observation):
        self.assessment_calls += 1
        return {"completed_step_ids": [], "reason": "故意保持 pending 以测试循环保护"}

    def revise_plan(self, context, reason):
        self.revise_calls += 1
        return ["使用已读取证据完成分析"]


class InvalidThenRecoverLLM(DemoLLMBackend):
    def create_plan(self, task):
        return ["收集来源"]

    def decide(self, context, tools):
        if context["progress"]["round"] == 1:
            return {
                "name": "search_web",
                "arguments": {"query": "Alpha official pricing"},
            }
        if context["progress"]["round"] == 2:
            return {
                "name": "search_web",
                "arguments": {
                    "query": "Alpha official pricing",
                    "purpose": "恢复后搜索",
                    "target_step_id": "P1",
                },
            }
        return {
            "name": "finish",
            "arguments": {"reason": "验证恢复", "completed_step_ids": ["P1"]},
        }

    def write_report(self, context):
        return "# 参数校验恢复测试\n\n已恢复并完成 [S1]。"


class DecisionErrorThenRecoverLLM(DemoLLMBackend):
    def create_plan(self, task):
        return ["收集来源"]

    def decide(self, context, tools):
        if context["progress"]["round"] == 1:
            raise LLMResponseError(
                "工具参数不是合法 JSON",
                attempts=3,
                tool_name="search_web",
                raw_arguments='{"query":"broken" "purpose":"missing comma"}',
                position=18,
            )
        if context["progress"]["round"] == 2:
            return {
                "name": "search_web",
                "arguments": {
                    "query": "Alpha official pricing",
                    "purpose": "恢复后搜索",
                    "target_step_id": "P1",
                },
            }
        return {
            "name": "finish",
            "arguments": {"reason": "验证决策恢复", "completed_step_ids": ["P1"]},
        }


class DistinctRepeatingLLM(DemoLLMBackend):
    def __init__(self):
        self.revise_calls = 0

    def create_plan(self, task):
        return ["读取目标页面"]

    def decide(self, context, tools):
        actions = {
            1: "https://example.test/alpha",
            2: "https://example.test/beta",
            3: "https://example.test/alpha",
            4: "https://example.test/beta",
        }
        if context["progress"]["round"] in actions:
            return {
                "name": "read_web",
                "arguments": {
                    "url": actions[context["progress"]["round"]],
                    "purpose": "累计重复测试",
                    "target_step_id": "P1",
                },
            }
        return {
            "name": "finish",
            "arguments": {"reason": "累计重复保护已生效", "completed_step_ids": ["R1-1"]},
        }

    def assess_progress(self, context, observation):
        return {"completed_step_ids": [], "reason": "保留 pending 以测试累计重复保护"}

    def revise_plan(self, context, reason):
        self.revise_calls += 1
        return ["使用已有证据完成分析"]


class CalculatorTool(BaseTool):
    name = "calculator"
    description = "将两个整数相加。"
    input_schema = {
        "type": "object",
        "properties": {
            "left": {"type": "integer"},
            "right": {"type": "integer"},
        },
        "required": ["left", "right"],
        "additionalProperties": False,
    }

    def execute(self, **kwargs):
        return kwargs["left"] + kwargs["right"]


class AgentTests(unittest.TestCase):
    def test_offline_end_to_end_has_multi_round_replan_and_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            config = Config(max_rounds=8)
            state = AgentState(task="比较 Alpha 与 Beta")
            registry = build_default_registry(DemoResearchTools(config), state)
            agent = CompetitorAgent(
                DemoLLMBackend(), registry, config, run_dir, state, echo_logs=False
            )

            report_path = agent.run()

            self.assertTrue(report_path.exists())
            self.assertEqual(agent.state.status, "completed")
            self.assertEqual(agent.state.round, 6)
            self.assertEqual(agent.state.replan_count, 1)
            self.assertEqual(len(agent.state.sources), 2)
            report = report_path.read_text(encoding="utf-8")
            self.assertIn("[1]", report)
            self.assertIn("URL: https://example.test/alpha", report)
            events = [json.loads(line)["event"] for line in (run_dir / "run.jsonl").read_text(encoding="utf-8").splitlines()]
            tool_results = [
                json.loads(line)["tool"]
                for line in (run_dir / "run.jsonl").read_text(encoding="utf-8").splitlines()
                if json.loads(line)["event"] == "tool_result"
            ]
            self.assertEqual(events.count("agent_decision"), 6)
            self.assertEqual(events.count("progress_assessment"), 4)
            self.assertEqual(
                tool_results,
                ["search_web", "read_web", "replan", "search_web", "read_web", "finish"],
            )
            self.assertIn("report_context_built", events)
            self.assertIn("report_citations_built", events)
            self.assertIn("run_completed", events)

    def test_state_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            state = AgentState(task="task", plan=[PlanStep("P1", "step")])
            state.save(path)
            loaded = AgentState.load(path)
            self.assertEqual(loaded.task, "task")
            self.assertEqual(loaded.plan[0].id, "P1")
            self.assertEqual(loaded.knowledge_mode, "existing")

    def test_context_is_bounded_and_only_keeps_recent_events(self):
        state = AgentState(task="task", plan=[PlanStep("P1", "step")])
        state.sources = [Source("S1", "title", "https://example.com", content="x" * 50_000)]
        state.recent_events = [{"event": str(i), "detail": {}} for i in range(20)]
        context = ContextBuilder(4000).build(state)
        encoded = json.dumps(context, ensure_ascii=False)
        self.assertLessEqual(len(encoded), 4000)
        self.assertEqual(len(context["recent_events"]), 6)
        self.assertNotIn('"event": "0"', encoded)
        self.assertEqual(len(context["source_index"]), 1)

    def test_context_prioritizes_recent_official_content_and_indexes_all_sources(self):
        state = AgentState(task="task", plan=[PlanStep("P1", "step")])
        state.sources = [
            Source("S1", "Third-party blog", "https://blog.example/a", content="old", retrieved_at="2026-01-01T00:00:00+00:00"),
            Source("S2", "Official docs", "https://docs.vendor.example/product", content="new official evidence", retrieved_at="2026-02-01T00:00:00+00:00", via="read_web"),
        ]
        context = ContextBuilder(4000).build(state)
        self.assertEqual({item["id"] for item in context["source_index"]}, {"S1", "S2"})
        self.assertEqual(context["evidence"][0]["id"], "S2")

    def test_report_context_keeps_richer_text_balances_domains_and_deduplicates(self):
        state = AgentState(task="compare", plan=[PlanStep("P1", "step", "completed")])
        repeated = "official evidence " * 400
        state.sources = [
            Source("S1", "A docs", "https://docs.a.example/product", content=repeated, via="read_web"),
            Source("S2", "A alias", "https://a.example/docs", content=repeated, via="read_web"),
            Source("S3", "B pricing", "https://b.example/pricing", content="pricing evidence " * 400, via="read_web"),
            Source("S4", "Search only", "https://blog.example/post", snippet="weak evidence"),
        ]
        context = ReportContextBuilder(20_000, per_source_limit=5_000).build(state)
        encoded = json.dumps(context, ensure_ascii=False)
        primary_ids = {item["id"] for item in context["primary_evidence"]}
        self.assertLessEqual(len(encoded), 20_000)
        self.assertEqual(len(primary_ids & {"S1", "S2"}), 1)
        self.assertIn("S3", primary_ids)
        self.assertGreater(max(len(item["content"]) for item in context["primary_evidence"]), 1800)
        self.assertIn("S4", {item["id"] for item in context["supplemental_evidence"]})

    def test_repeated_url_is_blocked_and_forces_replan(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            config = Config(max_rounds=5)
            backend = RepeatingLLM()
            state = AgentState(task="repeat guard")
            registry = build_default_registry(DemoResearchTools(config), state)
            agent = CompetitorAgent(
                backend,
                registry,
                config,
                run_dir,
                state,
                echo_logs=False,
            )
            agent.run()
            events = [json.loads(line)["event"] for line in (run_dir / "run.jsonl").read_text(encoding="utf-8").splitlines()]
            self.assertEqual(backend.assessment_calls, 2)
            self.assertEqual(backend.revise_calls, 1)
            self.assertIn("repeated_action_detected", events)
            self.assertIn("action_blocked", events)
            self.assertIn("forced_replan", events)
            self.assertEqual(agent.state.replan_count, 1)

    def test_registry_uses_new_tool_after_registration_only(self):
        registry = ToolRegistry()
        registry.register(CalculatorTool())

        schema = registry.schemas()[0]
        self.assertEqual(schema["function"]["name"], "calculator")
        self.assertTrue(schema["function"]["strict"])
        self.assertEqual(registry.execute("calculator", {"left": 2, "right": 3}), (False, 5))
        with self.assertRaises(ToolValidationError):
            registry.execute("calculator", {"left": 2})

    def test_invalid_tool_arguments_are_logged_and_agent_recovers(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            config = Config(max_rounds=3)
            state = AgentState(task="invalid arguments recovery")
            registry = build_default_registry(DemoResearchTools(config), state)
            agent = CompetitorAgent(
                InvalidThenRecoverLLM(),
                registry,
                config,
                run_dir,
                state,
                echo_logs=False,
            )

            agent.run()

            records = [
                json.loads(line)
                for line in (run_dir / "run.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            errors = [record for record in records if record["event"] == "tool_error"]
            self.assertEqual(len(errors), 1)
            self.assertIn("缺少必填参数", errors[0]["error"])
            self.assertEqual(agent.state.status, "completed")
            self.assertEqual(agent.state.round, 3)

    def test_decision_response_error_is_logged_and_agent_recovers(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            config = Config(max_rounds=3)
            state = AgentState(task="decision response recovery")
            registry = build_default_registry(DemoResearchTools(config), state)
            agent = CompetitorAgent(
                DecisionErrorThenRecoverLLM(),
                registry,
                config,
                run_dir,
                state,
                echo_logs=False,
            )

            agent.run()

            records = [
                json.loads(line)
                for line in (run_dir / "run.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            errors = [record for record in records if record["event"] == "decision_error"]
            self.assertEqual(len(errors), 1)
            self.assertEqual(errors[0]["attempts"], 3)
            self.assertEqual(errors[0]["tool_name"], "search_web")
            self.assertEqual(errors[0]["error_position"], 18)
            self.assertEqual(agent.state.status, "completed")
            self.assertEqual(agent.state.round, 3)

    def test_distinct_duplicate_actions_trigger_cumulative_replan(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            config = Config(max_rounds=5)
            backend = DistinctRepeatingLLM()
            state = AgentState(task="cumulative repeat guard")
            registry = build_default_registry(DemoResearchTools(config), state)
            agent = CompetitorAgent(
                backend,
                registry,
                config,
                run_dir,
                state,
                echo_logs=False,
            )

            agent.run()

            records = [
                json.loads(line)
                for line in (run_dir / "run.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            blocked = [record for record in records if record["event"] == "action_blocked"]
            self.assertEqual(backend.revise_calls, 1)
            self.assertEqual(len(blocked), 1)
            self.assertIn("累计检测到 2 次重复动作", blocked[0]["reason"])
            self.assertEqual(agent.state.replan_count, 1)

    def test_openai_backend_retries_malformed_tool_arguments(self):
        responses = [
            SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(tool_calls=[SimpleNamespace(
                    function=SimpleNamespace(name="search_web", arguments='{"query":"x" "purpose":"y"}')
                )]))]
            ),
            SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(tool_calls=[SimpleNamespace(
                    function=SimpleNamespace(
                        name="search_web",
                        arguments=json.dumps(
                            {"query": "x", "purpose": "y", "target_step_id": "P1"}
                        ),
                    )
                )]))]
            ),
        ]
        create = unittest.mock.Mock(side_effect=responses)
        backend = OpenAIBackend.__new__(OpenAIBackend)
        backend.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        backend.model = "test-model"
        backend.decision_max_retries = 2

        action = backend.decide({"task": "test"}, [])

        self.assertEqual(action["name"], "search_web")
        self.assertEqual(action["arguments"]["target_step_id"], "P1")
        self.assertEqual(create.call_count, 2)
        retry_messages = create.call_args_list[1].kwargs["messages"]
        self.assertIn("不是合法 JSON", retry_messages[-1]["content"])

    def test_openai_backend_raises_diagnostic_error_after_retry_limit(self):
        response = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(tool_calls=[SimpleNamespace(
                function=SimpleNamespace(name="read_web", arguments='{"url":"x" "purpose":"y"}')
            )]))]
        )
        create = unittest.mock.Mock(return_value=response)
        backend = OpenAIBackend.__new__(OpenAIBackend)
        backend.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        backend.model = "test-model"
        backend.decision_max_retries = 1

        with self.assertRaises(LLMResponseError) as raised:
            backend.decide({"task": "test"}, [])

        self.assertEqual(raised.exception.attempts, 2)
        self.assertEqual(raised.exception.tool_name, "read_web")
        self.assertGreater(raised.exception.raw_arguments_length, 0)
        self.assertIsNotNone(raised.exception.position)

    def test_old_state_without_guard_fields_still_loads(self):
        old = {
            "task": "old",
            "plan": [],
            "sources": [
                {
                    "id": "S1",
                    "title": "Legacy source",
                    "url": "https://example.com/legacy",
                    "via": "read_web",
                }
            ],
            "recent_events": [],
        }
        loaded = AgentState.from_dict(old)
        self.assertEqual(loaded.action_history, [])
        self.assertEqual(loaded.read_attempts, {})
        self.assertEqual(loaded.knowledge_mode, "existing")
        self.assertEqual(loaded.retrieved_evidence, [])
        self.assertIsNone(loaded.sources[0].http_status)

    @patch("urllib.request.urlopen")
    def test_search_api_response_is_normalized(self, urlopen):
        payload = {"results": [{"title": "Official", "url": "https://example.com/p", "content": "fact"}]}
        urlopen.return_value = FakeResponse(json.dumps(payload).encode())
        result = ResearchTools(Config(tavily_api_key="test")).search_web("query")
        self.assertEqual(result["results"][0]["snippet"], "fact")
        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "https://api.tavily.com/search")

    @patch.object(ResearchTools, "_validate_public_url")
    @patch("urllib.request.urlopen")
    def test_html_reader_extracts_visible_text(self, urlopen, validate):
        body = b"<html><head><title>Product</title><style>hidden</style></head><body><h1>Visible fact</h1><script>hidden()</script></body></html>"
        urlopen.return_value = FakeResponse(body, "text/html")
        result = ResearchTools(Config()).read_web("https://example.com")
        self.assertEqual(result["title"], "Product")
        self.assertEqual(result["http_status"], 200)
        self.assertIn("Visible fact", result["content"])
        self.assertNotIn("hidden()", result["content"])
        validate.assert_called_once()


if __name__ == "__main__":
    unittest.main()
