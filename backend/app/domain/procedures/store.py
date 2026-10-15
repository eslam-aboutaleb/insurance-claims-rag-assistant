"""Procedure playbook version storage for the ragit ingestion pipeline.

The ``procedure_versions`` table is application-side storage: ragit's
:class:`ragit.ingestion.IngestionPipeline` orchestrates
load/hash/compare/retire/chunk/embed/upsert and talks to a
:class:`ragit.ingestion.VersionStore` protocol, which
:class:`ProcedureVersionStore` implements here.

One store instance is bound to one playbook file. The pipeline is deliberately
per-source — its ``find_active`` returns "the active version" for the source it
was handed — so binding the store to a single ``source_file`` is what lets one
directory of playbooks share a single pipeline without their versions
colliding.

Unlike the policy adapter there is no historical snapshot version to pin: this
table is new, so the chunker's own reported version can be recorded as-is and an
unchanged corpus still compares equal on the next run.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, text as sa_text

from app.core.database import async_session_factory
from app.models.policies.procedure_version import ProcedureVersion
from ragit.ingestion import IngestionSnapshot, VersionRecord

logger = logging.getLogger(__name__)

__all__ = [
    "ProcedureVersionStore",
    "get_active_version_record",
]


async def _find_active_version(session: Any, source_file: str) -> ProcedureVersion | None:
    """Return the active version row for ``source_file``, or ``None``."""
    return (
        await session.execute(
            select(ProcedureVersion)
            .where(
                ProcedureVersion.source_file == source_file,
                ProcedureVersion.effective_to.is_(None),
            )
            .order_by(ProcedureVersion.effective_from.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


class ProcedureVersionStore:
    """``VersionStore`` implementation against the ``ProcedureVersion`` model.

    Every method joins the pipeline's transaction: the session is the
    pipeline's, so version retirement, chunk deletion, and version insertion
    commit together with the chunk upsert.

    Args:
        source_file: Playbook file name this store versions, e.g.
            ``coverage-review.md``.
    """

    def __init__(self, source_file: str) -> None:
        self.source_file = source_file

    async def _find_active(self, session: Any, source_file: str) -> ProcedureVersion | None:
        return await _find_active_version(session, source_file)

    async def find_active(self, session: Any) -> VersionRecord | None:
        """Return the active version for this playbook, or ``None``.

        Args:
            session: Session the read runs on.

        Returns:
            The active :class:`VersionRecord`, or ``None`` when this
            playbook has never been ingested.
        """
        active_version = await self._find_active(session, self.source_file)
        if active_version is None:
            return None
        return VersionRecord(
            version_id=str(active_version.version_id),
            source_id=None,
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
        """Retire the active version of this playbook.

        Args:
            session: Session the retirement runs on.
        """
        active_version = await self._find_active(session, self.source_file)
        if active_version is not None:
            active_version.effective_to = datetime.now(UTC)
            await session.flush()

    async def create_version(self, session: Any, snapshot: IngestionSnapshot) -> VersionRecord:
        """Insert a new version row carrying ``snapshot``.

        Args:
            session: Session the insert runs on.
            snapshot: The snapshot to record.

        Returns:
            The created :class:`VersionRecord`.
        """
        version = ProcedureVersion(
            source_file=self.source_file,
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
            source_id=None,
            snapshot=snapshot,
        )

    async def delete_chunks(self, session: Any, version_id: str) -> None:
        """Delete the chunks belonging to ``version_id``.

        Args:
            session: Session the delete runs on.
            version_id: Owning version id.
        """
        await session.execute(
            sa_text("DELETE FROM procedure_chunks WHERE procedure_version_id = :vid"),
            {"vid": version_id},
        )


async def get_active_version_record(source_file: str) -> dict[str, Any] | None:
    """Return the active version provenance for one playbook.

    Opened outside the ingestion pipeline so the ``get_procedure`` tool can
    report which version of a playbook it just returned without running a
    pipeline.

    Args:
        source_file: Playbook file name, e.g. ``coverage-review.md``.

    Returns:
        A dict with ``version``, ``source_hash``, ``chunker_version``, and
        ``effective_from`` keys, or ``None`` when the playbook has no active
        version.
    """
    async with async_session_factory() as session:
        active_version = await _find_active_version(session, source_file)
        if active_version is None:
            return None
        return {
            "version": active_version.version,
            "source_hash": active_version.source_hash,
            "chunker_version": active_version.chunker_version,
            "effective_from": active_version.effective_from,
        }
