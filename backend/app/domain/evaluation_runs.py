"""Persistence for evaluation runs: one ``eval_runs`` row per completed run.

The eval harness used to be a fire-and-forget endpoint: a run produced a
summary, the summary was printed, and nothing survived it. Without a stored
history there is no way to answer the only question that matters operationally
-- "did the last change make the answers better or worse?" -- because every run
starts from zero memory.

The table lives here rather than in ``app/models`` because this module owns the
only writer: ``app.domain.evaluation``. Keeping it beside the code that writes
it means the row shape and the metric payload cannot drift apart, and it leaves
the shared model registry free of a table no other subsystem reads.

Alembic owns the canonical table (see
``alembic/versions/f1a4b7c2d9e3_create_eval_runs_table.py``), and it is
idempotent. :func:`ensure_eval_runs_table` remains as the safety net for a
database that predates that migration -- it only issues
``CREATE TABLE IF NOT EXISTS``, so once the migration has run it does nothing
and the two paths cannot disagree about the column set.
"""

from __future__ import annotations

import logging
import os
import subprocess  # noqa: S404
import uuid
from datetime import UTC, datetime
from functools import lru_cache
from typing import Any

from sqlalchemy import Column, DateTime, Integer, MetaData, String, Table, func, select
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

EVAL_RUNS_TABLE = "eval_runs"

# A private MetaData rather than the shared ``app.models.base.Base`` one: this
# table is written by exactly one module, so it must not be swept into
# ``Base.metadata.create_all`` and must not appear in a future autogenerate
# diff against migrations that do not describe it.
metadata = MetaData()

eval_runs = Table(
    EVAL_RUNS_TABLE,
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True, default=uuid.uuid4),
    Column("mode", String(32), nullable=False),
    Column("git_sha", String(64), nullable=True),
    Column("config", JSONB, nullable=False),
    Column("metrics", JSONB, nullable=False),
    Column(
        "created_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    ),
    Column("sample_count", Integer, nullable=False, server_default="0"),
)


def persistence_enabled() -> bool:
    """Return True unless persistence is explicitly switched off.

    Persistence is on by default so a run leaves a trace wherever a database is
    reachable. ``OMNICARE_EVAL_PERSIST=0`` turns it off for a hermetic run --
    a test against a shared database, or a one-off local sweep whose numbers
    should not enter the trend.
    """
    return os.environ.get("OMNICARE_EVAL_PERSIST", "1").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }


@lru_cache(maxsize=1)
def resolve_git_sha() -> str | None:
    """Return the commit this process is running, or None when it cannot be told.

    The provenance of a metric is half its value: a trend line whose points
    cannot be tied to a commit is decoration. ``OMNICARE_GIT_SHA`` wins because
    a CI runner knows the SHA without a checkout, and the ``git`` lookup is only
    a fallback for a developer's working tree. Both can fail -- an exported
    source tree has no ``.git`` -- and neither failure is worth aborting a run
    over, so the answer is allowed to be None.
    """
    override = os.environ.get("OMNICARE_GIT_SHA", "").strip()
    if override:
        return override[:64]

    try:
        completed = subprocess.run(  # noqa: S603
            ["git", "rev-parse", "HEAD"],  # noqa: S607
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None

    if completed.returncode != 0:
        return None
    sha = completed.stdout.strip()
    return sha[:64] or None


async def ensure_eval_runs_table() -> None:
    """Create the ``eval_runs`` table if the database does not have it yet.

    Idempotent, so it is safe to call before every write. The Alembic migration
    creates the table for a normally-migrated deployment; this is what keeps the
    write path working on a database that has not reached it, and it is a no-op
    once the table exists so the two cannot disagree about the column set.

    Raises whatever the database raises; the caller decides whether a storage
    failure is fatal.
    """
    from app.database import engine  # noqa: PLC0415

    async with engine.begin() as connection:
        await connection.run_sync(metadata.create_all)


async def record_eval_run(
    mode: str,
    config: dict[str, Any],
    metrics: dict[str, Any],
    git_sha: str | None = None,
) -> uuid.UUID | None:
    """Persist one completed evaluation run.

    Args:
        mode: The mode the run actually executed in.
        config: The effective configuration of the run.
        metrics: The metric payload to store as JSONB.
        git_sha: Commit provenance. Defaults to :func:`resolve_git_sha`.

    Returns:
        The id of the stored row, or None when persistence is disabled.

    Raises:
        Exception: Whatever the database raises. Storage failures are reported
            to the caller rather than swallowed here so a caller that promised
            a durable record can keep that promise; a caller that did not wraps
            this in its own error handling.
    """
    if not persistence_enabled():
        return None

    from app.database import async_session_factory  # noqa: PLC0415

    await ensure_eval_runs_table()

    run_id = uuid.uuid4()
    async with async_session_factory() as session:
        await _insert(session, run_id, mode, config, metrics, git_sha)
        await session.commit()

    logger.debug("Persisted evaluation run %s (%s mode).", run_id, mode)
    return run_id


async def _insert(  # noqa: PLR0913
    session: AsyncSession,
    run_id: uuid.UUID,
    mode: str,
    config: dict[str, Any],
    metrics: dict[str, Any],
    git_sha: str | None,
) -> None:
    """Insert one row, deriving its timestamp from the database clock."""
    await session.execute(
        eval_runs.insert().values(
            id=run_id,
            mode=mode,
            git_sha=git_sha if git_sha is not None else resolve_git_sha(),
            config=_json_safe(config),
            metrics=_json_safe(metrics),
            created_at=datetime.now(tz=UTC),
            sample_count=_sample_count(metrics),
        )
    )


def _sample_count(metrics: dict[str, Any]) -> int:
    """Return how many samples the stored metrics describe.

    Stored as its own column so a trend query can count runs per sample
    without trusting the caller to have put the number in the payload.
    """
    for key in ("samples", "total_queries", "total_evaluated"):
        value = metrics.get(key)
        if isinstance(value, int):
            return value
    return 0


def _json_safe(payload: dict[str, Any]) -> dict[str, Any]:
    """Return a JSON-serialisable copy of ``payload``.

    Enum values and nested dataclass output reach this function straight from
    the runner, and asyncpg refuses a type it does not recognise with an error
    that names the driver rather than the offending key.
    """
    return {
        str(key): value.value if hasattr(value, "value") else value
        for key, value in payload.items()
    }


async def list_eval_runs(limit: int = 20) -> list[dict[str, Any]]:
    """Return the most recent runs, newest first.

    The read side the trend view needs. Not exposed over HTTP yet; it exists so
    the write path and the reader are written together and cannot disagree on
    the payload shape.

    Args:
        limit: Maximum number of rows to return.

    Returns:
        One dict per row with ``id``, ``mode``, ``git_sha``, ``config``,
        ``metrics``, and ``created_at``.
    """
    if not persistence_enabled():
        return []

    from app.database import async_session_factory  # noqa: PLC0415

    await ensure_eval_runs_table()
    statement = (
        select(
            eval_runs.c.id,
            eval_runs.c.mode,
            eval_runs.c.git_sha,
            eval_runs.c.config,
            eval_runs.c.metrics,
            eval_runs.c.sample_count,
            eval_runs.c.created_at,
        )
        .order_by(eval_runs.c.created_at.desc())
        .limit(limit)
    )

    async with async_session_factory() as session:
        rows = (await session.execute(statement)).mappings().all()

    return [
        {
            "id": row["id"],
            "mode": row["mode"],
            "git_sha": row["git_sha"],
            "config": dict(row["config"] or {}),
            "metrics": dict(row["metrics"] or {}),
            "sample_count": row["sample_count"],
            "created_at": row["created_at"],
        }
        for row in rows
    ]


async def delete_eval_runs(git_sha: str | None = None) -> int:
    """Delete stored runs, optionally only those from one commit.

    Exists for the test suite, which shares a database with other processes and
    must not leave rows behind that a later trend query would read as history.

    Args:
        git_sha: When given, delete only rows attributed to this commit.

    Returns:
        Number of rows deleted.
    """
    if not persistence_enabled():
        return 0

    from sqlalchemy import delete  # noqa: PLC0415

    from app.database import async_session_factory  # noqa: PLC0415

    async with async_session_factory() as session:
        statement = delete(eval_runs)
        if git_sha is not None:
            statement = statement.where(eval_runs.c.git_sha == git_sha)
        result = await session.execute(statement)
        await session.commit()
    return int(result.rowcount or 0)


__all__ = [
    "EVAL_RUNS_TABLE",
    "delete_eval_runs",
    "ensure_eval_runs_table",
    "eval_runs",
    "list_eval_runs",
    "persistence_enabled",
    "record_eval_run",
    "resolve_git_sha",
]
