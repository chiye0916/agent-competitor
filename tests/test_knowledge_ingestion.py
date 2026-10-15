import json
import tempfile
import unittest
from collections.abc import Sequence
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import main as cli_main
from agent.state import AgentState, Source
from config.settings import Config
from embedding.base import EmbeddingModel
from knowledge.evaluator import KnowledgeEvaluator
from knowledge.ingestion import (
    KnowledgeIngestionPipeline,
    content_sha256,
    normalize_content,
    normalize_url,
    stable_document_id,
)
from knowledge.policy import KnowledgePolicy
from knowledge.service import KnowledgeIngestionService
from knowledge.splitter import TextSplitter
from retrieval.chroma_store import ChromaStore
from retrieval.retriever import Retriever
from storage.logger import RunLogger
from tools.demo import DemoResearchTools
from tools.registry import build_default_registry


class CountingEmbedding(EmbeddingModel):
    def __init__(self) -> None:
        self.document_calls = 0
        self.fail_next_documents = False

    @staticmethod
    def _vector(text: str) -> list[float]:
        lowered = text.lower()
        return [
            float("alpha" in lowered),
            float("beta" in lowered),
            float("pricing" in lowered or "price" in lowered),
            float("updated" in lowered or "new" in lowered),
        ]

    def embed_text(self, text: str) -> list[float]:
        return self._vector(text)

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        self.document_calls += 1
        if self.fail_next_documents:
            self.fail_next_documents = False
            raise RuntimeError("embedding timeout")
        return [self._vector(text) for text in texts]


class PartialUpsertCollection:
    def __init__(self, wrapped) -> None:
        self.wrapped = wrapped
        self.failed = False

    def upsert(self, **kwargs):
        if not self.failed:
            self.failed = True
            partial = {
                name: value[:1] if isinstance(value, list) else value
                for name, value in kwargs.items()
            }
            self.wrapped.upsert(**partial)
            raise RuntimeError("simulated partial Chroma write")
        return self.wrapped.upsert(**kwargs)

    def __getattr__(self, name):
        return getattr(self.wrapped, name)


class ExplodingPipeline:
    def ingest_sources(self, sources, status_path=None):
        raise RuntimeError("storage unavailable")


def source(
    source_id: str,
    url: str,
    text: str,
    *,
    via: str = "read_web",
    retrieved_at: str = "2026-10-15T00:00:00+00:00",
) -> Source:
    return Source(
        id=source_id,
        title=f"Title {source_id}",
        url=url,
        snippet="search summary only",
        content=text,
        retrieved_at=retrieved_at,
        via=via,
    )


class KnowledgeIngestionTests(unittest.TestCase):
    def make_pipeline(
        self,
        embedding=None,
        persist_directory=None,
        collection_name=None,
    ):
        embedding = embedding or CountingEmbedding()
        store = ChromaStore(
            embedding_model=embedding,
            collection_name=collection_name or f"ingestion_{uuid4().hex}",
            persist_directory=persist_directory,
        )
        pipeline = KnowledgeIngestionPipeline(
            evaluator=KnowledgeEvaluator(
                KnowledgePolicy(min_content_chars=40, min_meaningful_chars=20)
            ),
            splitter=TextSplitter(chunk_size=80, chunk_overlap=10),
            store=store,
        )
        return pipeline, store, embedding

    def test_policy_accepts_full_text_and_rejects_search_summary(self):
        evaluator = KnowledgeEvaluator(
            KnowledgePolicy(min_content_chars=40, min_meaningful_chars=20)
        )
        full = source("S1", "HTTPS://Example.COM:443/page#section", "alpha pricing " * 8)
        summary_only = source(
            "S2",
            "https://example.com/summary",
            "alpha pricing " * 8,
            via="search_web",
        )

        self.assertTrue(evaluator.evaluate(full).accepted)
        rejected = evaluator.evaluate(summary_only)
        self.assertFalse(rejected.accepted)
        self.assertEqual(rejected.reason, "missing_verified_full_text")
        self.assertEqual(normalize_url(full.url), "https://example.com/page")

    def test_normal_ingestion_filters_splits_and_retrieves_sources(self):
        pipeline, store, _ = self.make_pipeline()
        alpha = source("S1", "https://example.com/alpha", "alpha pricing details " * 15)
        beta = source("S2", "https://example.com/beta", "beta pricing controls " * 15)
        summary_only = source(
            "S3",
            "https://example.com/search",
            "alpha result " * 15,
            via="search_web",
        )

        result = pipeline.ingest_sources([alpha, beta, summary_only])
        chunks = store.query("alpha pricing", top_k=10)

        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["new"], 2)
        self.assertEqual(result["rejected"], 1)
        self.assertGreater(result["chunks_written"], 2)
        self.assertTrue(chunks)
        self.assertTrue(all(chunk.metadata["document_id"] == chunk.document_id for chunk in chunks))
        self.assertTrue(all(chunk.metadata["source_id"] in {"S1", "S2"} for chunk in chunks))
        self.assertTrue(all(chunk.metadata["source_url"].startswith("https://") for chunk in chunks))
        self.assertTrue(all(chunk.metadata["retrieved_at"] for chunk in chunks))

    def test_repeated_ingestion_is_unchanged_without_embedding_or_duplicates(self):
        pipeline, store, embedding = self.make_pipeline()
        item = source("S1", "https://example.com/alpha", "alpha pricing details " * 15)

        first = pipeline.ingest_sources([item])
        count_after_first = store.collection.count()
        calls_after_first = embedding.document_calls
        second = pipeline.ingest_sources([item])

        self.assertEqual(first["new"], 1)
        self.assertEqual(second["unchanged"], 1)
        self.assertEqual(second["chunks_written"], 0)
        self.assertEqual(store.collection.count(), count_after_first)
        self.assertEqual(embedding.document_calls, calls_after_first)

    def test_same_url_content_update_switches_version_and_removes_old_results(self):
        pipeline, store, _ = self.make_pipeline()
        old = source("S1", "https://example.com/product", "alpha old pricing " * 15)
        new = source(
            "S9",
            "https://example.com/product#latest",
            "alpha updated pricing " * 15,
            retrieved_at="2026-10-16T00:00:00+00:00",
        )

        pipeline.ingest_sources([old])
        result = pipeline.ingest_sources([new])
        chunks = store.query("alpha updated pricing", top_k=20)

        expected_hash = content_sha256(normalize_content(new.content))
        self.assertEqual(result["updated"], 1)
        self.assertTrue(chunks)
        self.assertTrue(all("updated" in chunk.text for chunk in chunks))
        self.assertTrue(all(chunk.metadata["content_hash"] == expected_hash for chunk in chunks))
        self.assertTrue(all(chunk.metadata["source_id"] == "S9" for chunk in chunks))
        document_id = stable_document_id(normalize_url(new.url))
        self.assertEqual(store.get_active_version(document_id)["content_hash"], expected_hash)

    def test_embedding_failure_preserves_active_version_and_retry_updates(self):
        pipeline, store, embedding = self.make_pipeline()
        old = source("S1", "https://example.com/product", "alpha old pricing " * 15)
        new = source("S2", "https://example.com/product", "alpha updated pricing " * 15)
        pipeline.ingest_sources([old])

        embedding.fail_next_documents = True
        failed = pipeline.ingest_sources([new])
        old_results = store.query("alpha old pricing", top_k=20)
        retried = pipeline.ingest_sources([new])
        new_results = store.query("alpha updated pricing", top_k=20)

        self.assertEqual(failed["status"], "failed")
        self.assertIn("embedding timeout", failed["errors"][0]["error"])
        self.assertTrue(old_results)
        self.assertTrue(all("old" in chunk.text for chunk in old_results))
        self.assertEqual(retried["updated"], 1)
        self.assertTrue(all("updated" in chunk.text for chunk in new_results))

    def test_partial_chroma_write_is_invisible_and_retry_is_safe(self):
        # Chroma's Windows mmap can outlive the client object until process exit.
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            collection_name = f"ingestion_{uuid4().hex}"
            embedding = CountingEmbedding()
            pipeline, store, _ = self.make_pipeline(
                embedding,
                tmp,
                collection_name,
            )
            old = source("S1", "https://example.com/product", "alpha old pricing " * 15)
            new = source("S2", "https://example.com/product", "alpha updated pricing " * 15)
            pipeline.ingest_sources([old])
            store.collection = PartialUpsertCollection(store.collection)

            failed = pipeline.ingest_sources([new])
            physical_after_failure = store.collection.count()

            # Reopen the persistent store to simulate process restart after interruption.
            retry_pipeline, retry_store, _ = self.make_pipeline(
                embedding,
                tmp,
                collection_name,
            )
            visible_after_failure = retry_store.query("alpha pricing", top_k=20)
            retried = retry_pipeline.ingest_sources([new])
            visible_after_retry = retry_store.query("alpha updated pricing", top_k=20)

            self.assertEqual(failed["status"], "failed")
            self.assertGreater(physical_after_failure, len(visible_after_failure))
            self.assertTrue(all("old" in chunk.text for chunk in visible_after_failure))
            self.assertEqual(retried["updated"], 1)
            self.assertTrue(all("updated" in chunk.text for chunk in visible_after_retry))
            self.assertEqual(retry_store.collection.count(), len(visible_after_retry))
            self.assertEqual(
                len({chunk.id for chunk in visible_after_retry}),
                len(visible_after_retry),
            )

    def test_cross_task_retrieval_reuses_chunks_without_mixing_state(self):
        pipeline, store, _ = self.make_pipeline()
        state_a = AgentState(
            task="task A",
            sources=[source("S1", "https://example.com/alpha", "alpha pricing details " * 15)],
        )
        pipeline.ingest_sources(state_a.sources)
        state_b = AgentState(task="task B")
        registry = build_default_registry(
            DemoResearchTools(Config()),
            state_b,
            Retriever(store),
        )

        _, observation = registry.execute(
            "retrieve_knowledge",
            {"query": "alpha pricing", "top_k": 5},
        )

        self.assertTrue(observation["results"])
        self.assertEqual(observation["results"][0]["metadata"]["source_id"], "S1")
        self.assertEqual(state_b.sources, [])

    def test_ingestion_service_logs_error_without_touching_existing_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            report = run_dir / "report.md"
            report.write_text("completed report", encoding="utf-8")
            logger = RunLogger(run_dir / "run.jsonl", echo=False)
            service = KnowledgeIngestionService(ExplodingPipeline())

            result = service.run([], run_dir / "knowledge_ingestion.json", logger)

            self.assertEqual(result["status"], "failed")
            self.assertEqual(report.read_text(encoding="utf-8"), "completed report")
            records = [
                json.loads(line)
                for line in (run_dir / "run.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(records[-1]["event"], "ingestion_error")

    def test_cli_triggers_ingestion_after_report_and_keeps_success_on_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            calls = []

            class FakeService:
                def run(self, sources, status_path, logger):
                    calls.append((list(sources), status_path, (run_dir / "report.md").exists()))
                    return {
                        "status": "partial_failed",
                        "new": 0,
                        "updated": 0,
                        "unchanged": 0,
                        "failed": 1,
                    }

            class FakeAgent:
                def __init__(self, llm, registry, config, target, state):
                    self.state_path = target / "state.json"
                    self.logger = RunLogger(target / "run.jsonl", echo=False)
                    self.state = state
                    self.target = target

                def run(self):
                    self.target.mkdir(parents=True, exist_ok=True)
                    report = self.target / "report.md"
                    report.write_text("completed", encoding="utf-8")
                    self.state.status = "completed"
                    self.state.sources.append(
                        source("S1", "https://example.com/a", "alpha pricing " * 20)
                    )
                    return report

            with (
                patch.object(cli_main.Config, "from_env", return_value=Config()),
                patch.object(cli_main, "OpenAIBackend", return_value=object()),
                patch.object(cli_main, "ResearchTools", return_value=object()),
                patch.object(
                    cli_main,
                    "build_knowledge_services",
                    return_value=(object(), FakeService()),
                ),
                patch.object(cli_main, "build_default_registry", return_value=object()),
                patch.object(cli_main, "CompetitorAgent", FakeAgent),
            ):
                exit_code = cli_main.main(
                    ["--task", "test", "--output-dir", str(run_dir)]
                )

            self.assertEqual(exit_code, 0)
            self.assertEqual(len(calls), 1)
            self.assertTrue(calls[0][2])
            self.assertEqual(calls[0][1], run_dir / "knowledge_ingestion.json")

    def test_ingest_existing_retries_completed_state_without_running_agent(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            report = run_dir / "report.md"
            report.write_text("completed", encoding="utf-8")
            state_path = run_dir / "state.json"
            state = AgentState(
                task="completed task",
                sources=[
                    source("S1", "https://example.com/a", "alpha pricing " * 20)
                ],
                status="completed",
                report_path=str(report),
            )
            state.save(state_path)
            calls = []

            class FakeService:
                def run(self, sources, status_path, logger):
                    calls.append((list(sources), status_path))
                    return {"status": "completed"}

            with (
                patch.object(cli_main.Config, "from_env", return_value=Config()),
                patch.object(
                    cli_main,
                    "build_knowledge_services",
                    return_value=(object(), FakeService()),
                ),
                patch.object(
                    cli_main,
                    "CompetitorAgent",
                    side_effect=AssertionError("不应重新执行 Agent"),
                ),
            ):
                exit_code = cli_main.main(["--ingest-existing", str(state_path)])

            self.assertEqual(exit_code, 0)
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0][1], run_dir / "knowledge_ingestion.json")


if __name__ == "__main__":
    unittest.main()
