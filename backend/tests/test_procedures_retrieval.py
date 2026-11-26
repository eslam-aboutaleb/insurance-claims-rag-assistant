"""Domain-level procedure ingestion and retrieval tests.

The procedures index is a second index next to the policy one, and it is
idempotent for the same reason the policy index is: the ingestion snapshot
carries the chunker version, so an unchanged corpus compares equal against the
active version and a second run creates no new ``procedure_versions`` row and
ingests no new chunks.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import text

from app.core.database import async_session_factory

_PROCEDURE_TABLES = ("procedure_chunks", "procedure_versions")

_COVERAGE_SOURCE = "coverage-review.md"
_TRIAGE_SOURCE = "claim-triage.md"


@pytest_asyncio.fixture
async def clean_procedure_tables():
    """Delete procedure rows before and after the test (dedicated test DB only)."""

    async def _clear() -> None:
        async with async_session_factory() as session:
            for table in _PROCEDURE_TABLES:
                # Table names come from the fixed tuple above, never from test input.
                await session.execute(text(f'DELETE FROM "{table}"'))  # noqa: S608
            await session.commit()

    await _clear()
    yield
    await _clear()


async def _count(table: str) -> int:
    """Return the row count of one procedure table."""
    async with async_session_factory() as session:
        # Table names come from the fixed tuple above, never from test input.
        return (
            await session.execute(text(f'SELECT count(*) FROM "{table}"'))  # noqa: S608
        ).scalar_one()


async def _active_versions() -> dict[str, str]:
    """Return the active playbook versions keyed by source file."""
    async with async_session_factory() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT source_file, version FROM procedure_versions "
                    "WHERE effective_to IS NULL"
                )
            )
        ).all()
    return dict(rows)


@pytest.mark.asyncio
async def test_both_playbooks_ingest_and_retrieve(clean_procedure_tables):
    """Both playbooks land in the index and retrieve only their own chunks."""
    from app.domain.procedures.ingestion import ingest_procedures
    from app.domain.procedures.retrieval import retrieve_procedures

    total = await ingest_procedures()
    assert total > 0

    assert await _active_versions() == {
        _COVERAGE_SOURCE: "current",
        _TRIAGE_SOURCE: "current",
    }
    assert await _count("procedure_chunks") == total

    coverage = await retrieve_procedures(
        "coverage determination covered peril appraisal threshold",
        n_results=16,
        source_file=_COVERAGE_SOURCE,
    )
    triage = await retrieve_procedures(
        "intake triage classify request missing fields escalation",
        n_results=16,
        source_file=_TRIAGE_SOURCE,
    )

    assert coverage, "coverage-review.md produced no retrievable chunks"
    assert triage, "claim-triage.md produced no retrievable chunks"
    assert all(row["metadata"]["source"] == _COVERAGE_SOURCE for row in coverage)
    assert all(row["metadata"]["source"] == _TRIAGE_SOURCE for row in triage)


@pytest.mark.asyncio
async def test_reingesting_unchanged_corpus_creates_no_new_version(clean_procedure_tables):
    """Second run creates no new version rows (the snapshot comparison)."""
    from app.domain.procedures.ingestion import ingest_procedures

    first = await ingest_procedures()
    assert first > 0

    versions_after_first = await _count("procedure_versions")
    chunks_after_first = await _count("procedure_chunks")
    stored_chunker_version = await _stored_chunker_version()

    second = await ingest_procedures()

    assert second == first
    assert await _count("procedure_versions") == versions_after_first
    assert await _count("procedure_chunks") == chunks_after_first

    # No historical snapshot version is pinned here, so the recorded version is
    # the chunker's own. A pinned or drifted value would make the snapshot
    # compare unequal and silently re-ingest on every boot.
    assert stored_chunker_version == await _chunker_reported_version()


async def _stored_chunker_version() -> str | None:
    """Return the chunker version recorded with the active coverage version."""
    async with async_session_factory() as session:
        return (
            await session.execute(
                text(
                    "SELECT chunker_version FROM procedure_versions "
                    "WHERE source_file = :source AND effective_to IS NULL"
                ),
                {"source": _COVERAGE_SOURCE},
            )
        ).scalar_one_or_none()


async def _chunker_reported_version() -> str:
    """Return the version the section-aware chunker reports for itself."""
    from ragnor.chunking import MarkdownSectionChunker, SlidingWindowChunker  # noqa: PLC0415

    return MarkdownSectionChunker(SlidingWindowChunker()).version


@pytest.mark.asyncio
async def test_get_procedure_returns_coverage_playbook_without_triage_content(
    clean_procedure_tables,
):
    """A coverage question loads the coverage playbook and nothing else."""
    from app.domain.procedures.ingestion import ingest_procedures
    from app.domain.procedures.tools import get_procedure

    await ingest_procedures()

    result = await get_procedure("coverage-review")

    assert result["error"] is None
    assert result["name"] == "coverage-review"
    assert result["version"] == "current"
    assert result["chunk_count"] > 0
    # Sections come back in document order, so the playbook reads top to bottom
    # rather than in retrieval-rank order.
    assert result["procedure"].startswith("## Coverage Review")
    assert "COVERED" in result["procedure"]
    assert "appraisal" in result["procedure"]
    assert result["sources"]
    assert {source["source"] for source in result["sources"]} == {_COVERAGE_SOURCE}

    # The triage playbook's body must not bleed into the coverage answer.
    assert "Intake Triage" not in result["procedure"]
    assert "Classify the request" not in result["procedure"]
    assert _TRIAGE_SOURCE not in {source["source"] for source in result["sources"]}

    active = await _active_versions()
    assert result["version"] == active[_COVERAGE_SOURCE]


@pytest.mark.asyncio
async def test_get_procedure_returns_triage_playbook_without_coverage_body(
    clean_procedure_tables,
):
    """The triage name loads the triage playbook and nothing else."""
    from app.domain.procedures.ingestion import ingest_procedures
    from app.domain.procedures.tools import get_procedure

    await ingest_procedures()

    result = await get_procedure("claim-triage")

    assert result["error"] is None
    assert result["name"] == "claim-triage"
    assert result["chunk_count"] > 0
    assert result["procedure"].startswith("## Claim Triage")
    assert "escalation" in result["procedure"].lower()
    assert {source["source"] for source in result["sources"]} == {_TRIAGE_SOURCE}
    assert "Coverage Review — Coverage Determination Procedure" not in result["procedure"]


@pytest.mark.asyncio
async def test_get_procedure_accepts_alternate_spellings(clean_procedure_tables):
    """A name the model is likely to improvise still resolves to the playbook."""
    from app.domain.procedures.ingestion import ingest_procedures
    from app.domain.procedures.tools import get_procedure

    await ingest_procedures()

    for requested, expected in (
        ("Coverage Review", "coverage-review"),
        ("coverage", "coverage-review"),
        ("coverage determination", "coverage-review"),
        ("triage", "claim-triage"),
        ("claim intake", "claim-triage"),
    ):
        result = await get_procedure(requested)
        assert result["error"] is None, requested
        assert result["name"] == expected, requested
        assert result["chunk_count"] > 0, requested


@pytest.mark.asyncio
async def test_get_procedure_unknown_name_returns_structured_error(clean_procedure_tables):
    """An unknown name is a structured error, not another playbook's body."""
    from app.domain.procedures.ingestion import ingest_procedures
    from app.domain.procedures.tools import get_procedure

    await ingest_procedures()

    result = await get_procedure("fraud-screening")

    assert result["procedure"] is None
    assert result["version"] is None
    assert result["chunk_count"] == 0
    assert result["sources"] == []
    assert result["error"]

    # Non-leaking: no index internals, no SQL, and no other playbook's body.
    assert "procedure_chunks" not in result["error"]
    assert "SELECT" not in result["error"]
    assert "Traceback" not in result["error"]
    assert "Intake Triage" not in str(result)
    assert "Coverage Review" not in str(result)
    # The envelope still names what is available so the model can recover.
    assert "coverage-review" in result["error"]
    assert "claim-triage" in result["error"]


@pytest.mark.asyncio
async def test_get_procedure_requires_a_non_empty_name(clean_procedure_tables):
    """An empty name is rejected without touching the index."""
    from app.domain.procedures.ingestion import ingest_procedures
    from app.domain.procedures.tools import get_procedure

    await ingest_procedures()

    result = await get_procedure("   ")

    assert result["procedure"] is None
    assert result["chunk_count"] == 0
    assert result["error"]


@pytest.mark.asyncio
async def test_get_procedure_reports_an_unindexed_playbook(clean_procedure_tables):
    """An empty index is reported, not answered from another playbook."""
    from app.domain.procedures.ingestion import ingest_procedures
    from app.domain.procedures.tools import get_procedure

    await ingest_procedures()
    async with async_session_factory() as session:
        for table in _PROCEDURE_TABLES:
            await session.execute(text(f'DELETE FROM "{table}"'))  # noqa: S608
        await session.commit()

    result = await get_procedure("coverage-review")

    assert result["procedure"] is None
    assert result["chunk_count"] == 0
    assert result["version"] is None
    assert result["error"]
    assert "coverage-review" in result["error"]


@pytest.mark.asyncio
async def test_ingestion_gate_disables_the_startup_task(clean_procedure_tables, monkeypatch):
    """The startup gate is an environment variable read inside the package."""
    from app.domain.procedures.ingestion import ingest_procedures

    monkeypatch.setenv("OMNICARE_INGEST_PROCEDURES", "false")

    assert await ingest_procedures() == 0
    assert await _count("procedure_versions") == 0
    assert await _count("procedure_chunks") == 0
