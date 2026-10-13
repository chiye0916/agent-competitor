"""Embedding model interfaces and implementations."""

from embedding.base import EmbeddingModel
from embedding.openai_embedding import OpenAIEmbedding

__all__ = ["EmbeddingModel", "OpenAIEmbedding"]
