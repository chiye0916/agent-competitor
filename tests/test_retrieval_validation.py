import tempfile
import unittest
from pathlib import Path

from knowledge.chunk import Chunk
from retrieval.models import RetrievalMatch
from scripts.test_retrieval import (
    execute_validation,
    format_validation,
    load_query_cases,
)


class FakeRetriever:
    def __init__(self, matches):
        self.matches = list(matches)
        self.calls = []
        self.mutation_calls = 0

    def query_with_distances(self, text, top_k=5):
        self.calls.append((text, top_k))
        return self.matches[:top_k]

    def add_chunks(self, *args, **kwargs):
        self.mutation_calls += 1
        raise AssertionError("manual validation must not write")


def match(rank, distance, suffix):
    return RetrievalMatch(
        rank=rank,
        distance=distance,
        chunk=Chunk(
            id=f"chunk-{suffix}",
            document_id=f"doc-{suffix}",
            text=f"content {suffix}",
            metadata={
                "source_title": f"Title {suffix}",
                "source_url": f"https://example.com/{suffix}",
            },
        ),
    )


class RetrievalValidationTests(unittest.TestCase):
    def test_query_returns_deterministic_top_k(self):
        retriever = FakeRetriever([match(1, 0.1, "a"), match(2, 0.2, "b")])
        records = execute_validation(
            retriever,
            [{"id": "Q1", "query": "pricing"}],
            top_k=1,
            content_chars=100,
        )

        self.assertEqual([item["chunk_id"] for item in records[0]["results"]], ["chunk-a"])
        self.assertEqual(retriever.calls, [("pricing", 1)])

    def test_result_contains_distance_and_source_chain(self):
        retriever = FakeRetriever([match(1, 0.125, "a")])
        records = execute_validation(
            retriever,
            [{"id": "Q1", "query": "pricing"}],
            top_k=5,
            content_chars=100,
        )
        rendered = format_validation(records, "cosine")
        result = records[0]["results"][0]

        self.assertEqual(result["chunk_id"], "chunk-a")
        self.assertEqual(result["document_id"], "doc-a")
        self.assertEqual(result["source_url"], "https://example.com/a")
        self.assertIn("distance (cosine, 越小越近): 0.125000", rendered)

    def test_result_order_preserves_retriever_ranking(self):
        retriever = FakeRetriever([match(1, 0.4, "first"), match(2, 0.1, "second")])
        records = execute_validation(
            retriever,
            [{"id": "Q1", "query": "ambiguous"}],
            top_k=5,
            content_chars=100,
        )

        self.assertEqual(
            [item["chunk_id"] for item in records[0]["results"]],
            ["chunk-first", "chunk-second"],
        )

    def test_empty_knowledge_result_is_preserved(self):
        records = execute_validation(
            FakeRetriever([]),
            [{"id": "Q1", "query": "missing", "expected_answer_status": "absent"}],
            top_k=5,
            content_chars=100,
        )

        self.assertEqual(records[0]["results"], [])
        self.assertIn("结果: 无", format_validation(records, "cosine"))

    def test_validation_is_read_only_and_dataset_has_required_categories(self):
        retriever = FakeRetriever([match(1, 0.1, "a")])
        execute_validation(
            retriever,
            [{"id": "Q1", "query": "pricing"}],
            top_k=5,
            content_chars=100,
        )
        cases = load_query_cases(Path("tests/data/rag_queries.json"))
        categories = {case["category"] for case in cases}

        self.assertEqual(retriever.mutation_calls, 0)
        self.assertGreaterEqual(len(cases), 10)
        self.assertLessEqual(len(cases), 20)
        self.assertTrue(
            {"exact", "synonym", "attribute", "cross_expression", "interference", "no_answer"}
            <= categories
        )

    def test_invalid_dataset_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "invalid.json"
            path.write_text('[{"id":"Q1","query":""}]', encoding="utf-8")

            with self.assertRaises(ValueError):
                load_query_cases(path)


if __name__ == "__main__":
    unittest.main()
