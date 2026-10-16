import json
import os
import sys
import unittest
from collections.abc import Sequence
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from agent.state import Source
from config.settings import Config
from embedding.base import EmbeddingModel
from embedding.openai_embedding import OpenAIEmbedding
from knowledge.document import Document
from knowledge.splitter import TextSplitter
from retrieval.chroma_store import ChromaStore


class KeywordEmbedding(EmbeddingModel):
    """Small deterministic embedding used only by offline tests."""

    def embed_text(self, text: str) -> list[float]:
        return self.embed_documents([text])[0]

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for text in texts:
            lowered = text.lower()
            vectors.append(
                [
                    float("pricing" in lowered or "price" in lowered),
                    float(
                        any(term in lowered for term in ("ai", "model", "openai", "deepseek"))
                    ),
                    float("python" in lowered or "tutorial" in lowered),
                ]
            )
        return vectors


class FakeEmbeddingsAPI:
    def __init__(self):
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            data=[
                SimpleNamespace(index=index, embedding=[float(index), float(len(text))])
                for index, text in reversed(list(enumerate(kwargs["input"])))
            ]
        )


class KnowledgeLayerTests(unittest.TestCase):
    def test_config_reads_independent_embedding_provider(self):
        environment = {
            "LLM_API_KEY": "llm-key",
            "LLM_BASE_URL": "https://llm.example/v1",
            "EMBEDDING_API_KEY": "embedding-key",
            "EMBEDDING_BASE_URL": "https://embedding.example/v1",
            "EMBEDDING_MODEL": "embedding-model",
        }
        dotenv = SimpleNamespace(load_dotenv=lambda: None)

        with patch.dict(os.environ, environment, clear=True), patch.dict(
            sys.modules, {"dotenv": dotenv}
        ):
            config = Config.from_env()

        self.assertEqual(config.api_key, "llm-key")
        self.assertEqual(config.base_url, "https://llm.example/v1")
        self.assertEqual(config.embedding_api_key, "embedding-key")
        self.assertEqual(config.embedding_base_url, "https://embedding.example/v1")
        self.assertEqual(config.embedding_model, "embedding-model")

    def test_config_falls_back_to_llm_provider_for_embeddings(self):
        environment = {
            "LLM_API_KEY": "shared-key",
            "LLM_BASE_URL": "https://shared.example/v1",
        }
        dotenv = SimpleNamespace(load_dotenv=lambda: None)

        with patch.dict(os.environ, environment, clear=True), patch.dict(
            sys.modules, {"dotenv": dotenv}
        ):
            config = Config.from_env()

        self.assertEqual(config.embedding_api_key, "shared-key")
        self.assertEqual(config.embedding_base_url, "https://shared.example/v1")

    def test_openai_embedding_uses_independent_provider_configuration(self):
        constructor_calls = []

        def create_client(**kwargs):
            constructor_calls.append(kwargs)
            return SimpleNamespace()

        openai_module = SimpleNamespace(OpenAI=create_client)
        config = Config(
            api_key="llm-key",
            base_url="https://llm.example/v1",
            embedding_api_key="embedding-key",
            embedding_base_url="https://embedding.example/v1",
        )

        with patch.dict(sys.modules, {"openai": openai_module}):
            OpenAIEmbedding(config)

        self.assertEqual(
            constructor_calls,
            [
                {
                    "api_key": "embedding-key",
                    "base_url": "https://embedding.example/v1",
                }
            ],
        )

    def test_document_serialization_and_source_conversion(self):
        source = Source(
            id="S1",
            title="Official pricing",
            url="https://example.com/pricing",
            snippet="pricing summary",
            content="full pricing content",
            via="read_web",
        )

        document = Document.from_source(source)
        restored = Document.from_dict(json.loads(json.dumps(document.to_dict())))

        self.assertEqual(restored.id, source.id)
        self.assertEqual(restored.content, source.content)
        self.assertEqual(restored.metadata["via"], "read_web")

    def test_splitter_creates_overlapping_chunks_with_source_metadata(self):
        document = Document(
            id="doc-a",
            title="A",
            url="https://example.com/a",
            content="abcdefghij",
            metadata={"kind": "pricing"},
        )

        chunks = TextSplitter(chunk_size=6, chunk_overlap=2).split(document)

        self.assertEqual([chunk.text for chunk in chunks], ["abcdef", "efghij"])
        self.assertEqual(chunks[1].document_id, "doc-a")
        self.assertEqual(chunks[1].metadata["start_char"], 4)
        self.assertEqual(chunks[1].metadata["kind"], "pricing")

    def test_openai_embedding_batches_and_preserves_input_order(self):
        api = FakeEmbeddingsAPI()
        client = SimpleNamespace(embeddings=api)
        embedding = OpenAIEmbedding(Config(), client=client)

        vectors = embedding.embed_documents(["first", "second"])

        self.assertEqual(vectors, [[0.0, 5.0], [1.0, 6.0]])
        self.assertEqual(len(api.calls), 1)
        self.assertEqual(api.calls[0]["model"], "text-embedding-3-small")
        self.assertEqual(embedding.embed_documents([]), [])

    def test_document_chunk_embedding_chroma_query_pipeline(self):
        documents = [
            Document(
                "openai",
                "OpenAI pricing",
                "https://openai.com/pricing",
                "OpenAI AI model pricing plans",
                {"vendor": "OpenAI", "tags": ["ai", "pricing"]},
            ),
            Document(
                "deepseek",
                "DeepSeek pricing",
                "https://deepseek.com/pricing",
                "DeepSeek AI model pricing details",
                {"vendor": "DeepSeek", "tags": ["ai", "pricing"]},
            ),
            Document(
                "python",
                "Python tutorial",
                "https://python.org/tutorial",
                "Python tutorial for functions and classes",
                {"vendor": "Python", "tags": ["tutorial"]},
            ),
        ]
        splitter = TextSplitter(chunk_size=100, chunk_overlap=10)
        chunks = splitter.split_documents(documents)
        embedding = KeywordEmbedding()
        vectors = embedding.embed_documents([chunk.text for chunk in chunks])

        store = ChromaStore(
            embedding_model=embedding,
            collection_name=f"knowledge_pipeline_{uuid4().hex}",
        )
        store.add_chunks(chunks, vectors)
        results = store.query("AI model pricing", top_k=2)
        detailed = store.query_with_distances("AI model pricing", top_k=2)

        self.assertEqual({chunk.document_id for chunk in results}, {"openai", "deepseek"})
        self.assertEqual({chunk.metadata["vendor"] for chunk in results}, {"OpenAI", "DeepSeek"})
        self.assertTrue(all(chunk.metadata["tags"] == ["ai", "pricing"] for chunk in results))
        self.assertEqual(
            [match.chunk.document_id for match in detailed],
            [chunk.document_id for chunk in results],
        )
        self.assertTrue(all(match.distance is not None for match in detailed))
        self.assertEqual([match.rank for match in detailed], [1, 2])


if __name__ == "__main__":
    unittest.main()
