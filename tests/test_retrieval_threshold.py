import unittest

from knowledge.chunk import Chunk
from retrieval.models import RetrievalMatch
from retrieval.retriever import Retriever


class FakeStore:
    distance_metric = "cosine"

    def __init__(self, distances):
        self.distances = list(distances)

    def query_with_distances(self, text, top_k=5):
        return [
            RetrievalMatch(
                rank=index,
                distance=distance,
                chunk=Chunk(
                    id=f"chunk-{index}",
                    document_id=f"doc-{index}",
                    text=f"candidate {index}",
                ),
            )
            for index, distance in enumerate(self.distances[:top_k], 1)
        ]


class RetrievalThresholdTests(unittest.TestCase):
    def test_relevant_result_passes_distance_threshold(self):
        retriever = Retriever(FakeStore([0.20]), relevance_threshold=0.40)

        result = retriever.diagnose("known fact")

        self.assertEqual([item.distance for item in result.accepted_matches], [0.20])
        self.assertEqual(result.status, "relevant_knowledge_found")

    def test_irrelevant_result_is_filtered(self):
        retriever = Retriever(FakeStore([0.60]), relevance_threshold=0.40)

        result = retriever.diagnose("unknown fact")

        self.assertEqual(result.accepted_matches, ())
        self.assertEqual([item.distance for item in result.rejected_matches], [0.60])

    def test_all_irrelevant_returns_no_relevant_knowledge(self):
        retriever = Retriever(FakeStore([0.60, 0.80]), relevance_threshold=0.40)

        result = retriever.diagnose("unknown fact")

        self.assertEqual(result.status, "no_relevant_knowledge")
        self.assertEqual(result.reason, "all_candidates_exceed_relevance_threshold")

    def test_partial_candidates_are_kept_in_original_order(self):
        retriever = Retriever(
            FakeStore([0.20, 0.31, 0.62, 0.80]),
            relevance_threshold=0.40,
        )

        result = retriever.diagnose("mixed fact", top_k=4)

        self.assertEqual(
            [item.distance for item in result.accepted_matches],
            [0.20, 0.31],
        )
        self.assertEqual(
            [item.distance for item in result.rejected_matches],
            [0.62, 0.80],
        )
        self.assertEqual([chunk.id for chunk in retriever.query("mixed fact", 4)], ["chunk-1", "chunk-2"])

    def test_missing_threshold_preserves_legacy_results(self):
        retriever = Retriever(FakeStore([0.20, 0.80]), relevance_threshold=None)

        result = retriever.diagnose("legacy behavior")

        self.assertEqual(len(result.accepted_matches), 2)
        self.assertEqual(result.rejected_matches, ())


if __name__ == "__main__":
    unittest.main()
