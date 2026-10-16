import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from agent.context import ContextBuilder
from agent.core import CompetitorAgent
from agent.state import AgentState, infer_knowledge_mode
from config.settings import Config
from knowledge.chunk import Chunk
from main import parse_args
from tools.demo import DemoResearchTools
from tools.registry import build_default_registry


class EvidenceRetriever:
    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []

    def query(self, text: str, top_k: int = 5) -> list[Chunk]:
        self.calls.append((text, top_k))
        values = [
            Chunk(
                id="doc-a:hash-a:chunk:0",
                document_id="doc-a",
                text="Product A costs 20 dollars.",
                metadata={
                    "source_url": "https://example.com/a",
                    "source_title": "Product A pricing",
                    "source_id": "S7",
                    "content_hash": "hash-a",
                    "retrieved_at": "2026-01-01T00:00:00+00:00",
                    "ingested_at": "2026-01-02T00:00:00+00:00",
                },
            ),
            Chunk(
                id="doc-b:hash-b:chunk:1",
                document_id="doc-b",
                text="Product B targets enterprise teams.",
                metadata={
                    "source_url": "https://example.com/b",
                    "source_title": "Product B positioning",
                    "source_id": "S9",
                    "content_hash": "hash-b",
                    "retrieved_at": "2026-02-01T00:00:00+00:00",
                    "ingested_at": "2026-02-02T00:00:00+00:00",
                },
            ),
        ]
        return values[:top_k]


class EvidenceReportLLM:
    def __init__(self) -> None:
        self.report_context: dict[str, Any] | None = None

    def create_plan(self, task: str) -> list[str]:
        return ["复用已有价格与定位证据"]

    def decide(self, context: dict[str, Any], tools: list[dict[str, Any]]) -> dict[str, Any]:
        if context["progress"]["round"] == 1:
            return {
                "name": "retrieve_knowledge",
                "arguments": {"query": "product pricing positioning", "top_k": 2},
            }
        return {
            "name": "finish",
            "arguments": {"reason": "证据充分", "completed_step_ids": ["P1"]},
        }

    def assess_progress(
        self, context: dict[str, Any], observation: dict[str, Any]
    ) -> dict[str, Any]:
        return {"completed_step_ids": [], "reason": "not used"}

    def revise_plan(self, context: dict[str, Any], reason: str) -> list[str]:
        return ["使用已有证据"]

    def write_report(self, context: dict[str, Any]) -> str:
        self.report_context = context
        return (
            "# Evidence report\n\n"
            "Product A has a stated price [E1]. "
            "Unknown evidence [E999]. Fabricated numbered citation [99]. "
            "Fabricated URL: https://fabricated.example/report\n\n"
            "## References\n\n[1] Fabricated source"
        )


class LatestAwareLLM:
    def __init__(self) -> None:
        self.seen_modes: list[str] = []

    def create_plan(self, task: str) -> list[str]:
        return ["核验最新信息"]

    def decide(self, context: dict[str, Any], tools: list[dict[str, Any]]) -> dict[str, Any]:
        self.seen_modes.append(context["knowledge_mode"])
        if context["progress"]["round"] == 1:
            return {
                "name": "search_web",
                "arguments": {
                    "query": "Alpha latest pricing",
                    "purpose": "用户要求核验最新信息",
                    "target_step_id": "P1",
                },
            }
        return {
            "name": "finish",
            "arguments": {"reason": "最新来源已获得", "completed_step_ids": ["P1"]},
        }

    def assess_progress(
        self, context: dict[str, Any], observation: dict[str, Any]
    ) -> dict[str, Any]:
        return {"completed_step_ids": ["P1"], "reason": "搜索结果已覆盖测试步骤"}

    def revise_plan(self, context: dict[str, Any], reason: str) -> list[str]:
        return ["核验最新信息"]

    def write_report(self, context: dict[str, Any]) -> str:
        return "# Latest report\n\nLatest search evidence [S1]."


class EvidenceTests(unittest.TestCase):
    def test_retrieval_result_and_state_keep_complete_provenance(self):
        state = AgentState(task="trace evidence")
        retriever = EvidenceRetriever()
        registry = build_default_registry(DemoResearchTools(Config()), state, retriever)

        _, observation = registry.execute(
            "retrieve_knowledge", {"query": "pricing", "top_k": 1}
        )

        result = observation["results"][0]
        self.assertEqual(result["evidence_id"], "E1")
        self.assertEqual(result["chunk_id"], "doc-a:hash-a:chunk:0")
        self.assertEqual(result["document_id"], "doc-a")
        self.assertEqual(result["source_id"], "S7")
        self.assertEqual(result["source_url"], "https://example.com/a")
        self.assertEqual(result["source_title"], "Product A pricing")
        self.assertEqual(result["content_hash"], "hash-a")
        self.assertEqual(len(state.retrieved_evidence), 1)
        self.assertEqual(state.retrieved_evidence[0].content, result["text"])

    def test_evidence_state_round_trip_preserves_chunk_document_source_chain(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            state = AgentState(task="persist evidence", knowledge_mode="latest")
            registry = build_default_registry(
                DemoResearchTools(Config()), state, EvidenceRetriever()
            )
            registry.execute("retrieve_knowledge", {"query": "pricing", "top_k": 1})
            state.save(path)

            restored = AgentState.load(path)

        evidence = restored.retrieved_evidence[0]
        self.assertEqual(restored.knowledge_mode, "latest")
        self.assertEqual(evidence.id, "E1")
        self.assertEqual(evidence.chunk_id, "doc-a:hash-a:chunk:0")
        self.assertEqual(evidence.document_id, "doc-a")
        self.assertEqual(evidence.source_id, "S7")
        self.assertEqual(evidence.source_url, "https://example.com/a")

    def test_report_citations_are_program_generated_from_real_evidence_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = AgentState(task="cite retrieved evidence")
            retriever = EvidenceRetriever()
            backend = EvidenceReportLLM()
            registry = build_default_registry(DemoResearchTools(Config()), state, retriever)
            agent = CompetitorAgent(
                backend,
                registry,
                Config(max_rounds=2),
                Path(tmp),
                state,
                echo_logs=False,
            )

            report_path = agent.run()
            report = report_path.read_text(encoding="utf-8")
            records = [
                json.loads(line)
                for line in (Path(tmp) / "run.jsonl").read_text(encoding="utf-8").splitlines()
            ]

        self.assertIn("Product A has a stated price [1]", report)
        self.assertIn("URL: https://example.com/a", report)
        self.assertNotIn("URL: https://example.com/b", report)
        self.assertIn("Evidence ID: E1", report)
        self.assertIn("Chunk ID: doc-a:hash-a:chunk:0", report)
        self.assertNotIn("E999", report)
        self.assertNotIn("[99]", report)
        self.assertNotIn("Fabricated source", report)
        self.assertNotIn("fabricated.example", report)
        self.assertEqual(
            [item["id"] for item in backend.report_context["retrieved_evidence"]],
            ["E1", "E2"],
        )
        citation_events = [
            record for record in records if record["event"] == "report_citations_built"
        ]
        self.assertEqual(citation_events[0]["citation_count"], 1)
        self.assertEqual(
            [record["tool"] for record in records if record["event"] == "tool_result"],
            ["retrieve_knowledge", "finish"],
        )

    def test_default_mode_uses_existing_policy_without_age_based_search(self):
        state = AgentState(task="use archived pricing")
        registry = build_default_registry(
            DemoResearchTools(Config()), state, EvidenceRetriever()
        )
        registry.execute("retrieve_knowledge", {"query": "pricing", "top_k": 1})

        context = ContextBuilder(10_000).build(state)

        self.assertEqual(state.knowledge_mode, "existing")
        self.assertEqual(context["knowledge_mode"], "existing")
        self.assertIn("不要仅因资料较旧就自动搜索", context["knowledge_policy"])
        self.assertEqual(context["retrieved_evidence"][0]["id"], "E1")

    def test_latest_mode_reaches_llm_without_runtime_forcing_tool_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = AgentState(task="检查最新价格", knowledge_mode="latest")
            backend = LatestAwareLLM()
            registry = build_default_registry(DemoResearchTools(Config()), state)
            agent = CompetitorAgent(
                backend,
                registry,
                Config(max_rounds=2),
                Path(tmp),
                state,
                echo_logs=False,
            )

            agent.run()

        self.assertEqual(backend.seen_modes, ["latest", "latest"])
        self.assertEqual(state.knowledge_mode, "latest")

    def test_latest_mode_can_be_inferred_or_selected_explicitly(self):
        self.assertEqual(infer_knowledge_mode("比较历史产品定位"), "existing")
        self.assertEqual(infer_knowledge_mode("截至今天重新核实最新价格"), "latest")
        args = parse_args(["--task", "compare", "--knowledge-mode", "latest"])
        self.assertEqual(args.knowledge_mode, "latest")


if __name__ == "__main__":
    unittest.main()
