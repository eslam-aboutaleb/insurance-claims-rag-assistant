"""Policy ingestion adapter over ragkit (ragkit plan 06).

The OmniCare policy version tables (``policies``,
``policy_versions``, ``policy_ingestion_meta``) are
application-side storage: ragkit's
:class:`ragkit.ingestion.IngestionPipeline` orchestrates
load/hash/compare/retire/chunk/embed/upsert and talks to a
:class:`ragkit.ingestion.VersionStore` protocol, which
:class:`PolicyVersionStore` implements here against the
OmniCare tables. This module is the permanent home for the
binding; the historical ``app.rag.ingest`` module was
removed in plan 07.

The stored snapshot carries ``chunker_version="v1"`` — the
historical value — even though the chunker itself reports
``section-aware-v1``. The wrapper below pins the snapshot
version so stored snapshots keep comparing equal; a mismatch
would re-ingest every policy on the next run.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, text as sa_text

from app.config import get_settings
from app.database import async_session_factory
from app.domain.policies.retriever import POLICY_RETRIEVER_SPEC
from app.models.policy import Policy
from app.models.policy_version import PolicyVersion
from ragkit.chunking import (
    MarkdownSectionChunker,
    SlidingWindowChunker,
    sliding_window_chunk,
)
from ragkit.chunking.base import Chunker
from ragkit.ingestion import (
    FileDocumentSource,
    IngestionPipeline,
    IngestionSnapshot,
    VersionRecord,
)
from ragkit.types import Chunk

logger = logging.getLogger(__name__)

__all__ = [
    "POLICY_CHUNKER_SNAPSHOT_VERSION",
    "PolicyVersionStore",
    "chunk_policy_document",
    "ingest_policy",
    "sliding_window_chunk",
]

POLICY_CHUNKER_SNAPSHOT_VERSION = "v1"
"""Chunker version recorded in ingestion snapshots.

The :class:`MarkdownSectionChunker` reports
``"section-aware-v1"``, but the snapshots stored by
existing deployments carry ``chunker_version="v1"``.
The adapter wraps the chunker so the snapshot
comparison stays stable and existing policies are not
spuriously re-ingested. This is the ``chunker_version``
trap flagged across plans 02, 04, and 06; this module
is the permanent home for the mapping (ragkit plan 06).
"""


class _HistoricalSnapshotChunker:
    """Chunker adapter that reports the historical snapshot version.

    Delegates chunking to the wrapped chunker but reports
    the snapshot version the version table already stores,
    so ``build_snapshot`` produces a snapshot that compares
    equal to the stored one.
    """

    def __init__(self, chunker: Chunker, snapshot_version: str) -> None:
        self._chunker = chunker
        self._snapshot_version = snapshot_version

    def chunk(self, text: str) -> list[Chunk]:
        return self._chunker.chunk(text)

    @property
    def version(self) -> str:
        return self._snapshot_version

    @property
    def config(self) -> dict[str, Any]:
        return self._chunker.config


class PolicyVersionStore:
    """``VersionStore`` implementation against the Policy/PolicyVersion models.

    Every method joins the pipeline's transaction: the
    session is the pipeline's, so version retirement,
    chunk deletion, and version insertion commit together
    with the chunk upsert.
    """

    _PRODUCT = "omnicare_base"
    _JURISDICTION = "US"

    async def _find_or_create_policy(self, session: Any) -> Policy:
        policy = (
            await session.execute(
                select(Policy).where(
                    Policy.product == self._PRODUCT,
                    Policy.jurisdiction == self._JURISDICTION,
                )
            )
        ).scalar_one_or_none()
        if policy is None:
            policy = Policy(product=self._PRODUCT, jurisdiction=self._JURISDICTION)
            session.add(policy)
            await session.flush()
        return policy

    async def _find_active_version(self, session: Any, policy: Policy) -> PolicyVersion | None:
        return (
            await session.execute(
                select(PolicyVersion)
                .where(
                    PolicyVersion.policy_id == policy.policy_id,
                    PolicyVersion.effective_to.is_(None),
                )
                .order_by(PolicyVersion.effective_from.desc())
                .limit(1)
            )
        ).scalar_one_or_none()

    async def find_active(self, session: Any) -> VersionRecord | None:
        policy = await self._find_or_create_policy(session)
        active_version = await self._find_active_version(session, policy)
        if active_version is None:
            return None
        return VersionRecord(
            version_id=str(active_version.version_id),
            source_id=str(policy.policy_id),
            snapshot=IngestionSnapshot(
                source_hash=active_version.source_hash,
                embedding_model=active_version.embedding_model or "",
                embedding_dim=active_version.embedding_dim or 0,
                chunker_version=active_version.chunker_version or "",
                chunk_size=active_version.chunk_size or 0,
                overlap=active_version.overlap or 0,
                retrieval_schema_version=(active_version.retrieval_schema_version or ""),
            ),
        )

    async def close_active(self, session: Any) -> None:
        policy = await self._find_or_create_policy(session)
        active_version = await self._find_active_version(session, policy)
        if active_version is not None:
            active_version.effective_to = datetime.now(UTC)
            await session.flush()

    async def create_version(self, session: Any, snapshot: IngestionSnapshot) -> VersionRecord:
        policy = await self._find_or_create_policy(session)
        version = PolicyVersion(
            policy_id=policy.policy_id,
            version="current",
            effective_from=datetime.now(UTC),
            source_hash=snapshot.source_hash,
            embedding_model=snapshot.embedding_model,
            embedding_dim=snapshot.embedding_dim,
            chunker_version=snapshot.chunker_version,
            chunk_size=snapshot.chunk_size,
            overlap=snapshot.overlap,
            retrieval_schema_version=snapshot.retrieval_schema_version,
        )
        session.add(version)
        await session.flush()
        return VersionRecord(
            version_id=str(version.version_id),
            source_id=str(policy.policy_id),
            snapshot=snapshot,
        )

    async def delete_chunks(self, session: Any, version_id: str) -> None:
        await session.execute(
            sa_text("DELETE FROM policy_chunks WHERE policy_version_id = :vid"),
            {"vid": version_id},
        )


def chunk_policy_document(filepath: str) -> list[dict[str, Any]]:
    """Parse a Markdown policy file into structured, overlapping chunks.

    .. deprecated::
        Use the ragkit ingestion pipeline with a
        :class:`ragkit.chunking.MarkdownSectionChunker`
        instead. This wrapper remains for one release for
        backward compatibility.

    Args:
        filepath: Path to the Markdown policy file.

    Returns:
        List of chunk dicts with keys: id, text, metadata.
        Metadata includes: chunk_id, section, source,
        chunk_index, sub_chunk_index.
    """
    try:
        with open(filepath, encoding="utf-8") as f:
            content = f.read()
    except FileNotFoundError:
        logger.error("Policy document not found at: %s", filepath)
        return []

    source_filename = filepath.rsplit("/", maxsplit=1)[-1]
    chunker = MarkdownSectionChunker(
        SlidingWindowChunker(),
        source_name=source_filename,
    )
    return [
        {
            "id": chunk.id,
            "text": chunk.text,
            "metadata": chunk.metadata,
        }
        for chunk in chunker.chunk(content)
    ]


async def ingest_policy(policy_path: str | None = None) -> int:
    """Ingest the policy document into the vector store.

    Delegates to the ragkit :class:`IngestionPipeline`:
    loads the policy file, hashes its content, compares
    the ingestion snapshot against the active version,
    and -- when the source changed -- retires the old
    version, deletes its chunks, chunks and embeds the
    new content, validates every embedding, and upserts
    the chunks with their policy and version ownership,
    all in one transaction serialized by a PostgreSQL
    advisory lock keyed by the source path.

    The chunk metadata carries ``policy_id`` and
    ``policy_version_id`` — the upsert column derivation
    reads metadata keys only, so the ownership columns are
    stamped inside the metadata dict by the pipeline.

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

    from app.domain.embeddings import EmbeddingFactory, get_vector_store  # noqa: PLC0415

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
