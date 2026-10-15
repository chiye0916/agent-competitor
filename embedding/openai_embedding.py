"""OpenAI SDK implementation of the provider-independent embedding interface."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from config.settings import Config
from embedding.base import EmbeddingModel


class OpenAIEmbedding(EmbeddingModel):
    def __init__(
        self,
        config: Config,
        model: str | None = None,
        client: Any | None = None,
    ) -> None:
        if client is None:
            api_key = config.embedding_api_key or config.api_key
            base_url = config.embedding_base_url or config.base_url
            if not api_key:
                raise RuntimeError(
                    "缺少 EMBEDDING_API_KEY（未配置时可回退使用 "
                    "LLM_API_KEY 或 OPENAI_API_KEY）"
                )
            try:
                from openai import OpenAI
            except ImportError as exc:
                raise RuntimeError("缺少 openai SDK，请先安装 requirements.txt") from exc
            kwargs: dict[str, Any] = {"api_key": api_key}
            if base_url:
                kwargs["base_url"] = base_url
            client = OpenAI(**kwargs)
        self.client = client
        self.model = model or config.embedding_model

    def embed_text(self, text: str) -> list[float]:
        return self.embed_documents([text])[0]

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        values = list(texts)
        if not values:
            return []
        response = self.client.embeddings.create(model=self.model, input=values)
        ordered = sorted(response.data, key=lambda item: item.index)
        if len(ordered) != len(values):
            raise RuntimeError("Embedding API 返回的向量数量与输入不一致")
        return [list(item.embedding) for item in ordered]
