import json
import tempfile
import unittest
from pathlib import Path

from agent.core import CompetitorAgent
from agent.state import AgentState, Source
from config.settings import Config
from knowledge.chunk import Chunk
from retrieval.models import RetrievalDiagnostics, RetrievalMatch
from tools.demo import DemoLLMBackend, DemoResearchTools
from tools.registry import build_default_registry


class FakeRetriever:
    def __init__(self):
        self.calls: list[tuple[str, int]] = []

    def query(self, text: str, top_k: int = 5) -> list[Chunk]:
        self.calls.append((text, top_k))
        return [
            Chunk(
                id="doc-pricing:chunk:0",
                document_id="doc-pricing",
                text="Historical AI model pricing evidence",
                metadata={
                    "source_title": "Pricing archive",
                    "source_url": "https://example.com/archive",
                    "source_id": "S4",
                    "content_hash": "hash-pricing",
                    "retrieved_at": "2026-01-01T00:00:00+00:00",
                    "ingested_at": "2026-01-02T00:00:00+00:00",
                },
            )
        ][:top_k]

    def diagnose(self, text: str, top_k: int = 5) -> RetrievalDiagnostics:
        chunks = self.query(text, top_k=top_k)
        matches = tuple(
            RetrievalMatch(rank=index, chunk=chunk, distance=0.2)
            for index, chunk in enumerate(chunks, 1)
        )
        return RetrievalDiagnostics(
            query=text,
            distance_metric="cosine",
            threshold=0.5,
            raw_matches=matches,
            accepted_matches=matches,
            rejected_matches=(),
        )


class FailingRetriever:
    def diagnose(self, text: str, top_k: int = 5) -> RetrievalDiagnostics:
        raise RuntimeError("知识库暂时不可用")


class NoRelevantRetriever:
    def diagnose(self, text: str, top_k: int = 5) -> RetrievalDiagnostics:
        chunk = Chunk(
            id="doc-unrelated:chunk:0",
            document_id="doc-unrelated",
            text="unrelated product overview",
            metadata={"source_url": "https://example.com/unrelated"},
        )
        match = RetrievalMatch(rank=1, chunk=chunk, distance=0.72)
        return RetrievalDiagnostics(
            query=text,
            distance_metric="cosine",
            threshold=0.5,
            raw_matches=(match,),
            accepted_matches=(),
            rejected_matches=(match,),
        )


class RetrieveThenFinishLLM(DemoLLMBackend):
    def create_plan(self, task):
        return ["查询已有知识"]

    def decide(self, context, tools):
        if context["progress"]["round"] == 1:
            return {
                "name": "retrieve_knowledge",
                "arguments": {"query": "AI model pricing", "top_k": 3},
            }
        return {
            "name": "finish",
            "arguments": {"reason": "已有知识足够", "completed_step_ids": ["P1"]},
        }


class RetrieveFailureThenFinishLLM(RetrieveThenFinishLLM):
    pass


class KnowledgeToolTests(unittest.TestCase):
    def test_default_registry_registers_knowledge_tool_and_schema(self):
        config = Config()
        state = AgentState(task="registry test")
        retriever = FakeRetriever()
        registry = build_default_registry(DemoResearchTools(config), state, retriever)

        schemas = {item["function"]["name"]: item for item in registry.schemas()}

        self.assertIn("retrieve_knowledge", schemas)
        knowledge_schema = schemas["retrieve_knowledge"]["function"]
        self.assertIn("已有知识库", knowledge_schema["description"])
        self.assertEqual(
            knowledge_schema["parameters"]["properties"]["top_k"]["default"],
            5,
        )
        self.assertFalse(knowledge_schema["strict"])
        self.assertTrue(schemas["search_web"]["function"]["strict"])
        self.assertIn("最新信息", schemas["search_web"]["function"]["description"])

    def test_registry_executes_knowledge_tool_with_unified_observation(self):
        config = Config()
        state = AgentState(task="tool execution test")
        retriever = FakeRetriever()
        registry = build_default_registry(DemoResearchTools(config), state, retriever)

        ends_run, observation = registry.execute(
            "retrieve_knowledge",
            {"query": "AI model pricing"},
        )

        self.assertFalse(ends_run)
        self.assertTrue(observation["success"])
        self.assertEqual(observation["tool"], "retrieve_knowledge")
        self.assertEqual(observation["result_count"], 1)
        self.assertEqual(observation["status"], "relevant_knowledge_found")
        self.assertEqual(observation["distance_metric"], "cosine")
        self.assertEqual(observation["results"][0]["distance"], 0.2)
        self.assertEqual(observation["results"][0]["evidence_id"], "E1")
        self.assertEqual(observation["results"][0]["document_id"], "doc-pricing")
        self.assertEqual(
            observation["results"][0]["source_url"],
            "https://example.com/archive",
        )
        self.assertEqual(state.retrieved_evidence[0].chunk_id, "doc-pricing:chunk:0")
        self.assertEqual(retriever.calls, [("AI model pricing", 5)])

    def test_no_relevant_knowledge_is_explicit_and_adds_no_evidence(self):
        state = AgentState(task="unknown future fact")
        registry = build_default_registry(
            DemoResearchTools(Config()),
            state,
            NoRelevantRetriever(),
        )

        ends_run, observation = registry.execute(
            "retrieve_knowledge",
            {"query": "2035 Mars revenue"},
        )

        self.assertFalse(ends_run)
        self.assertTrue(observation["success"])
        self.assertEqual(observation["status"], "no_relevant_knowledge")
        self.assertEqual(
            observation["reason"],
            "all_candidates_exceed_relevance_threshold",
        )
        self.assertEqual(observation["raw_candidate_count"], 1)
        self.assertEqual(observation["result_count"], 0)
        self.assertEqual(observation["rejected_count"], 1)
        self.assertEqual(observation["results"], [])
        self.assertEqual(state.retrieved_evidence, [])

    def test_no_relevant_knowledge_does_not_force_web_search(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = AgentState(
                task="unknown future fact",
                sources=[
                    Source(
                        "S1",
                        "Existing source",
                        "https://example.com/source",
                        content="existing evidence",
                    )
                ],
            )
            registry = build_default_registry(
                DemoResearchTools(Config()),
                state,
                NoRelevantRetriever(),
            )
            agent = CompetitorAgent(
                RetrieveThenFinishLLM(),
                registry,
                Config(max_rounds=2),
                Path(tmp),
                state,
                echo_logs=False,
            )

            agent.run()

            records = [
                json.loads(line)
                for line in (Path(tmp) / "run.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            results = [record for record in records if record["event"] == "tool_result"]
            self.assertEqual([record["tool"] for record in results], ["retrieve_knowledge", "finish"])
            self.assertEqual(
                results[0]["observation"]["status"],
                "no_relevant_knowledge",
            )

    def test_agent_executes_retrieve_knowledge_without_core_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Config(max_rounds=2)
            state = AgentState(
                task="reuse knowledge",
                sources=[
                    Source(
                        "S1",
                        "Existing source",
                        "https://example.com/source",
                        content="evidence",
                    )
                ],
            )
            retriever = FakeRetriever()
            registry = build_default_registry(DemoResearchTools(config), state, retriever)
            agent = CompetitorAgent(
                RetrieveThenFinishLLM(),
                registry,
                config,
                Path(tmp),
                state,
                echo_logs=False,
            )

            agent.run()

            records = [
                json.loads(line)
                for line in (Path(tmp) / "run.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            results = [record for record in records if record["event"] == "tool_result"]
            self.assertEqual(results[0]["tool"], "retrieve_knowledge")
            self.assertTrue(results[0]["observation"]["success"])
            self.assertEqual(state.status, "completed")
            self.assertEqual(retriever.calls, [("AI model pricing", 3)])
            self.assertEqual([item.id for item in state.retrieved_evidence], ["E1"])

    def test_retrieval_failure_becomes_tool_error_and_agent_recovers(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Config(max_rounds=2)
            state = AgentState(
                task="retrieval failure",
                sources=[
                    Source(
                        "S1",
                        "Existing source",
                        "https://example.com/source",
                        content="evidence",
                    )
                ],
            )
            registry = build_default_registry(
                DemoResearchTools(config),
                state,
                FailingRetriever(),
            )
            agent = CompetitorAgent(
                RetrieveFailureThenFinishLLM(),
                registry,
                config,
                Path(tmp),
                state,
                echo_logs=False,
            )

            agent.run()

            records = [
                json.loads(line)
                for line in (Path(tmp) / "run.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            errors = [record for record in records if record["event"] == "tool_error"]
            self.assertEqual(len(errors), 1)
            self.assertEqual(errors[0]["tool"], "retrieve_knowledge")
            self.assertIn("知识库暂时不可用", errors[0]["error"])
            self.assertEqual(state.status, "completed")


if __name__ == "__main__":
    unittest.main()
