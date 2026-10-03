"""Sliding-window chunking.

Moved from ``backend/app/rag/ingest.py`` (ragkit plan 02).
Splits text into overlapping chunks based on word count: each
chunk holds ``chunk_size`` words and consecutive chunks overlap
by ``overlap`` words, so context is preserved across chunk
boundaries.
"""

from __future__ import annotations

import uuid
from typing import Any

from ragkit.types import Chunk


def sliding_window_chunk(
    text: str,
    chunk_size: int = 600,
    overlap: int = 100,
) -> list[str]:
    """Split text into overlapping chunks based on word count.

    Uses a sliding window approach where each chunk contains ``chunk_size``
    words and subsequent chunks overlap by ``overlap`` words. This preserves
    context across chunk boundaries, which improves retrieval quality for
    queries that span section boundaries.

    Args:
        text: The input text to split into chunks.
        chunk_size: Maximum number of words per chunk. Defaults to 600.
        overlap: Number of words to overlap between consecutive chunks.
            Defaults to 100.

    Returns:
        list[str]: A list of text chunks. Returns an empty list if the input
        text is empty or contains no words.
    """
    words = text.split()
    chunks = []

    if not words:
        return []

    for i in range(0, len(words), chunk_size - overlap):
        chunk = " ".join(words[i : i + chunk_size])
        chunks.append(chunk)
        if i + chunk_size >= len(words):
            break

    return chunks


class SlidingWindowChunker:
    """Chunker that splits text into overlapping word-count windows."""

    def __init__(self, chunk_size: int = 600, overlap: int = 100) -> None:
        self.chunk_size = chunk_size
        self.overlap = overlap

    def chunk(self, text: str) -> list[Chunk]:
        """Split ``text`` into overlapping chunks.

        Args:
            text: The input text to split.

        Returns:
            Chunks with generated ids and the sliding-window text.
        """
        return [
            Chunk(id=str(uuid.uuid4()), text=chunk_text)
            for chunk_text in sliding_window_chunk(text, self.chunk_size, self.overlap)
        ]

    @property
    def version(self) -> str:
        """Version tag for ingestion snapshot comparison."""
        return "sliding-window-v1"

    @property
    def config(self) -> dict[str, Any]:
        """Chunker tunables for ingestion snapshots."""
        return {"chunk_size": self.chunk_size, "overlap": self.overlap}
