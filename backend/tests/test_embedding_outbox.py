"""
Tests for the embedding-job outbox invariant.

``embedding_jobs.claim_uuid`` has no foreign key to ``claims``, so an enqueue that
commits on its own session can outlive the claim transaction it was meant to track.
These tests pin the invariant documented in ``app.domain.claims.outbox``: a job is
committed in the same transaction as the claim, or by a caller whose claim is already
durable.
"""

import asyncio
import uuid

from sqlalchemy import func, select

from app.database import async_session_factory
from app.domain.claims.outbox import enqueue_embedding_job


def _register_user() -> uuid.UUID:
    """Create a real user, because ``embedding_jobs.owner_id`` has a foreign key."""
    from app.models.user import User

    user_id = uuid.uuid4()

    async def _write() -> uuid.UUID:
        async with async_session_factory() as session:
            session.add(
                User(
                    id=user_id,
                    username=f"outbox_{user_id.hex[:12]}",
                    password_hash="not-a-real-hash",  # noqa: S106 - fixture row, never verified
                )
            )
            await session.commit()
        return user_id

    return asyncio.run(_write())


def _job_kwargs(claim_uuid: uuid.UUID, owner_id: uuid.UUID) -> dict:
    return {
        "claim_uuid": claim_uuid,
        "claim_id": f"CLM-{claim_uuid.hex[:8].upper()}",
        "owner_id": owner_id,
        "claim_type": "Water Damage",
        "description": "A pipe burst flooded the kitchen yesterday.",
        "policy_number": "POL-1092",
        "claim_status": "Submitted",
    }


def _count_jobs(**where) -> int:
    from app.models.embedding_job import EmbeddingJob

    async def _count() -> int:
        async with async_session_factory() as session:
            stmt = select(func.count()).select_from(EmbeddingJob)
            for column, value in where.items():
                stmt = stmt.where(column(value))
            return await session.scalar(stmt)

    return asyncio.run(_count())


def test_enqueue_with_caller_session_does_not_commit() -> None:
    """Passing a session must only add and flush, never commit."""

    class _RecordingSession:
        def __init__(self) -> None:
            self.commits = 0
            self.flushes = 0
            self.added: list[object] = []

        def add(self, obj) -> None:
            self.added.append(obj)

        async def flush(self) -> None:
            self.flushes += 1

        async def commit(self) -> None:
            self.commits += 1

    session = _RecordingSession()
    asyncio.run(enqueue_embedding_job(session=session, **_job_kwargs(uuid.uuid4(), uuid.uuid4())))  # type: ignore[arg-type]

    assert session.commits == 0, "enqueue committed a caller-owned session"
    assert session.flushes == 1
    assert len(session.added) == 1


def test_rolled_back_claim_leaves_no_orphan_job() -> None:
    """A claim transaction that rolls back must not leave a committed job behind.

    This is the defect the invariant prevents: an enqueue on its own session commits
    the job, the claim transaction then fails, and the worker later upserts an index
    entry for a claim that does not exist.
    """
    claim_uuid = uuid.uuid4()

    async def _stage_then_rollback() -> None:
        async with async_session_factory() as session:
            await enqueue_embedding_job(session=session, **_job_kwargs(claim_uuid, uuid.uuid4()))  # type: ignore[arg-type]
            await session.rollback()

    asyncio.run(_stage_then_rollback())

    assert _count_jobs() == 0


def test_enqueue_without_session_commits_its_own_transaction() -> None:
    """With no session the enqueue is self-contained and durable."""
    owner_id = _register_user()
    claim_uuid = uuid.uuid4()
    asyncio.run(enqueue_embedding_job(**_job_kwargs(claim_uuid, owner_id)))

    assert _count_jobs() == 1


def test_job_claim_uuid_has_no_foreign_key_to_claims() -> None:
    """The invariant is necessary precisely because the database will not enforce it.

    ``owner_id`` is protected by a foreign key, but ``claim_uuid`` points at no table,
    so an orphan job inserts cleanly and only fails later, inside the worker.
    """
    from sqlalchemy import inspect as sa_inspect

    from app.database import engine

    async def _fks() -> dict[str, list[str]]:
        def _sync(connection):
            inspector = sa_inspect(connection)
            return {
                name: [fk["referred_table"] for fk in inspector.get_foreign_keys(name)]
                for name in ["embedding_jobs"]
            }

        async with engine.connect() as connection:
            return await connection.run_sync(_sync)

    foreign_keys = asyncio.run(_fks())["embedding_jobs"]

    assert "claims" not in foreign_keys
    assert "users" in foreign_keys
