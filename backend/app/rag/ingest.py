"""
Deprecated shim for policy ingestion.

The OmniCare policy ingestion binding moved to
:mod:`app.domain.policies.ingestion` (ragkit
plan 06). This module keeps the historical
import path — and the historical patch points
(``get_settings``, ``get_vector_store``,
``FileDocumentSource``, ``IngestionPipeline``,
``EmbeddingFactory``) — working until plan 07
removes the shims.

.. deprecated::
    Use :mod:`app.domain.policies.ingestion` instead.
"""

from __future__ import annotations

import asyncio
import logging

from app.config import get_settings
from app.database import async_session_factory
from app.domain.policies.ingestion import (
    POLICY_CHUNKER_SNAPSHOT_VERSION,
    PolicyVersionStore,
    _HistoricalSnapshotChunker,
    chunk_policy_document,
)
from app.domain.policies.retriever import POLICY_RETRIEVER_SPEC
from app.rag.embedding import EmbeddingFactory
from app.rag.vector_store import get_vector_store
from ragkit.chunking import (
    MarkdownSectionChunker,
    SlidingWindowChunker,
    sliding_window_chunk,
)
from ragkit.ingestion import FileDocumentSource, IngestionPipeline

logger = logging.getLogger(__name__)

__all__ = [
    "POLICY_CHUNKER_SNAPSHOT_VERSION",
    "PolicyVersionStore",
    "chunk_policy_document",
    "ingest_policy",
    "sliding_window_chunk",
]


async def ingest_policy(policy_path: str | None = None) -> int:
    """Ingest the policy document into the vector store.

    .. deprecated::
        Use :func:`app.domain.policies.ingestion.ingest_policy`
        instead. This shim remains until plan 07 removes it.

    Delegates to the ragkit :class:`IngestionPipeline`:
    loads the policy file, hashes its content, compares
    the ingestion snapshot against the active version,
    and -- when the source changed -- retires the old
    version, deletes its chunks, chunks and embeds the
    new content, validates every embedding, and upserts
    the chunks with their policy and version ownership,
    all in one transaction serialized by a PostgreSQL
    advisory lock keyed by the source path.

    Args:
        policy_path: Path to the policy document. If None,
            uses ``settings.policy_file_path``.

    Returns:
        The number of chunks ingested, or the stored chunk
        count when the source was unchanged.
    """
    settings = get_settings()
    policy_path = policy_path or settings.policy_file_path

    if not policy_path:
        logger.warning("No policy_path configured; skipping ingestion.")
        return 0

    source = FileDocumentSource(policy_path)
    source_name = policy_path.rsplit("/", maxsplit=1)[-1]
    chunker = _HistoricalSnapshotChunker(
        MarkdownSectionChunker(SlidingWindowChunker(), source_name=source_name),
        snapshot_version=POLICY_CHUNKER_SNAPSHOT_VERSION,
    )
    pipeline = IngestionPipeline(
        store=get_vector_store(
            table_name=POLICY_RETRIEVER_SPEC.table_name,
            id_field=POLICY_RETRIEVER_SPEC.id_field,
        ),
        embedder=EmbeddingFactory.get_embedding_function(),
        chunker=chunker,
        session_factory=async_session_factory,
        settings=settings,
        version_id_key="policy_version_id",
        source_id_key="policy_id",
    )
    return await pipeline.run(source, PolicyVersionStore(), lock_key=policy_path)


if __name__ == "__main__":
    asyncio.run(ingest_policy())
