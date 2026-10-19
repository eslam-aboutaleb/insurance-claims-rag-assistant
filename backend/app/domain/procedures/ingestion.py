"""Procedure playbook ingestion over the ragnor pipeline.

"Skills" here means domain procedure playbooks retrieved as documents through a
second index — not Claude-style on-disk skill bundles (ADK 1.3 has no native
loader for those), and not the OpenAI Skills API (the app drives models through
``LiteLlm`` on ADK's chat-completions path rather than the Responses API, so a
``skill_reference`` is not consumable today). That choice matters because the
system prompt is already dense and every added rule costs tokens on every turn:
with the playbooks in the procedures index, a compliance update is an edit to a
markdown file with no deploy and no prompt change.

Ingestion reuses ragnor's :class:`ragnor.ingestion.IngestionPipeline` unchanged.
One pipeline instance is shared across the playbooks in the directory and run
once per file, each run bound to a
:class:`~app.domain.procedures.store.ProcedureVersionStore` for that one file,
so the per-source snapshot comparison, version retirement, and chunk deletion
all stay per-playbook. Nothing here forks or reimplements the pipeline.

The startup gate is read from ``OMNICARE_INGEST_PROCEDURES`` rather than from
``app.core.config.Settings``: the settings module is owned by another track, and a
flag read inside this package is the non-invasive way to make boot cost
optional. Ingestion is idempotent — an unchanged corpus is detected by snapshot
comparison and skips embedding entirely — so the default is enabled.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

from sqlalchemy import text as sa_text

from app.core.config import get_settings
from app.core.database import async_session_factory
from app.domain.procedures.store import ProcedureVersionStore
from ragnor.chunking import MarkdownSectionChunker, SlidingWindowChunker
from ragnor.ingestion import FileDocumentSource, IngestionPipeline

logger = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_PROCEDURES_DIR",
    "PROCEDURES_INGESTION_ENV",
    "ingest_procedures",
    "procedures_ingestion_enabled",
]

DEFAULT_PROCEDURES_DIR = Path(__file__).resolve().parents[2] / "data" / "procedures"
"""Default location of the procedure playbook corpus."""

PROCEDURES_INGESTION_ENV = "OMNICARE_INGEST_PROCEDURES"
"""Environment variable gating procedure ingestion at startup."""

_DEFAULT_PROCEDURES_INGESTION = True

_TRUTHY = frozenset({"1", "true", "yes", "on"})


def procedures_ingestion_enabled() -> bool:
    """Return whether procedure ingestion should run at startup.

    The gate is an environment variable read here rather than a field on
    ``Settings``: the settings module is owned by another track. Ingestion is
    idempotent (an unchanged corpus skips embedding), so the default is on.

    Returns:
        ``True`` unless the variable is explicitly set to a falsy value.
    """
    raw = os.environ.get(PROCEDURES_INGESTION_ENV)
    if raw is None or not raw.strip():
        return _DEFAULT_PROCEDURES_INGESTION
    return raw.strip().lower() in _TRUTHY


def _build_pipeline() -> IngestionPipeline:
    """Build the shared pipeline bound to the procedure chunk table.

    Returns:
        An :class:`IngestionPipeline` writing into ``procedure_chunks``.
    """
    from app.domain.embeddings import EmbeddingFactory, get_vector_store  # noqa: PLC0415

    return IngestionPipeline(
        store=get_vector_store(
            table_name="procedure_chunks",
            id_field="id",
        ),
        embedder=EmbeddingFactory.get_embedding_function(),
        # The pipeline overwrites each chunk's ``source`` metadata with the
        # document it actually came from, so the chunker's own source name is
        # never what ends up stored.
        chunker=MarkdownSectionChunker(SlidingWindowChunker()),
        session_factory=async_session_factory,
        settings=get_settings(),
        version_id_key="procedure_version_id",
    )


async def ingest_procedures(procedures_dir: str | None = None) -> int:
    """Ingest the procedure playbook corpus into the vector store.

    Delegates each playbook to the ragnor
    :class:`~ragnor.ingestion.IngestionPipeline`: it loads the file, hashes its
    content, compares the ingestion snapshot against the active version for
    that file, and — only when the source changed — retires the old version,
    deletes its chunks, chunks and embeds the new content, and upserts the
    chunks, all in one transaction serialized by a PostgreSQL advisory lock
    keyed by the file path.

    Args:
        procedures_dir: Directory holding the playbook markdown files. If
            ``None``, uses ``DEFAULT_PROCEDURES_DIR``.

    Returns:
        The number of chunks the procedures index holds for this corpus after
        the run, or ``0`` when the run was skipped. The count is read from the
        index rather than summed per playbook, because ragnor's skip path
        reports the store-wide chunk count for an unchanged source: summing
        that would inflate the number on every warm boot.
    """
    if not procedures_ingestion_enabled():
        logger.info(
            "Procedure ingestion disabled (%s=false); serving the procedures "
            "index already present in the database.",
            PROCEDURES_INGESTION_ENV,
        )
        return 0

    directory = Path(procedures_dir) if procedures_dir else DEFAULT_PROCEDURES_DIR
    if not directory.is_dir():
        logger.warning("Procedures directory not found at '%s'; skipping ingestion.", directory)
        return 0

    playbooks = sorted(directory.glob("*.md"))
    if not playbooks:
        logger.warning("No procedure playbooks found in '%s'; skipping ingestion.", directory)
        return 0

    pipeline = _build_pipeline()
    for playbook in playbooks:
        # One store per file: the pipeline's find_active/close_active are
        # per-source, so a shared store would retire one playbook's version
        # when another changed.
        store = ProcedureVersionStore(playbook.name)
        await pipeline.run(
            FileDocumentSource(str(playbook)),
            store,
            lock_key=str(playbook),
        )

    total = await _corpus_chunk_count()
    logger.info("Procedure ingestion complete: %d chunks indexed.", total)
    return total


async def _corpus_chunk_count() -> int:
    """Return the number of chunks the procedures index holds."""
    async with async_session_factory() as session:
        return (
            await session.execute(sa_text("SELECT count(*) FROM procedure_chunks"))
        ).scalar_one()


if __name__ == "__main__":
    asyncio.run(ingest_procedures())
