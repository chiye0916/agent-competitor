"""Dependency-free overlapping character splitter."""

from __future__ import annotations

from knowledge.chunk import Chunk
from knowledge.document import Document


class TextSplitter:
    def __init__(self, chunk_size: int = 500, chunk_overlap: int = 50):
        if chunk_size <= 0:
            raise ValueError("chunk_size 必须大于 0")
        if chunk_overlap < 0:
            raise ValueError("chunk_overlap 不能小于 0")
        if chunk_overlap >= chunk_size:
            raise ValueError("chunk_overlap 必须小于 chunk_size")
        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap

    def split(self, document: Document) -> list[Chunk]:
        if not document.content:
            return []

        chunks: list[Chunk] = []
        step = self.chunk_size - self.chunk_overlap
        for index, start in enumerate(range(0, len(document.content), step)):
            end = min(start + self.chunk_size, len(document.content))
            metadata = dict(document.metadata)
            metadata.update(
                {
                    "document_title": document.title,
                    "document_url": document.url,
                    "chunk_index": index,
                    "start_char": start,
                    "end_char": end,
                }
            )
            chunks.append(
                Chunk(
                    id=f"{document.id}:chunk:{index}",
                    document_id=document.id,
                    text=document.content[start:end],
                    metadata=metadata,
                )
            )
            if end == len(document.content):
                break
        return chunks

    def split_documents(self, documents: list[Document]) -> list[Chunk]:
        return [chunk for document in documents for chunk in self.split(document)]
