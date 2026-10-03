"""PostgreSQL advisory lock helper for ingestion.

Extracted from ``ingest_policy`` in
``backend/app/rag/ingest.py`` (ragkit plan 04).
The lock serializes ingestion of the same source
across concurrent backend instances, so they cannot
race and duplicate the DELETE + INSERT of chunks.

Ordering contract (preserved verbatim from the
original code):

* the lock is taken on the session the pipeline
  transacts on;
* on failure the transaction is rolled back *before*
  the lock is released -- a failed statement leaves
  the session in an aborted transaction, and any
  further command on it raises
  ``InFailedSQLTransactionError``, which would replace
  the real ingestion failure with a confusing error
  about the advisory lock;
* a lock-cleanup failure must never mask the outcome:
  ending the transaction closes the connection, which
  releases a session-level advisory lock anyway, so
  the cleanup failure is only logged.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from sqlalchemy import text as sa_text
from sqlalchemy.exc import SQLAlchemyError

logger = logging.getLogger(__name__)


@asynccontextmanager
async def advisory_lock(session: Any, key: str) -> AsyncIterator[None]:
    """Hold a PostgreSQL advisory lock keyed by ``key``.

    The lock is session-level and taken with
    ``pg_advisory_lock(hashtext(:key))``, so concurrent
    ingestion of the same source -- across instances or
    tasks -- is serialized on the database.

    Args:
        session: SQLAlchemy async session the ingestion
            transaction runs on. The caller owns the
            session and its commit.
        key: Lock key (the source path). Hashed with
            ``hashtext`` before locking.

    Yields:
        Nothing. The lock is held for the duration of the
        ``async with`` block.

    Raises:
        Exception: Whatever the block raises, after rolling
            the session back and releasing the lock.
    """
    await session.execute(
        sa_text("SELECT pg_advisory_lock(hashtext(:source))"),
        {"source": key},
    )
    try:
        yield
    except Exception:
        # Roll back before the lock is released. A failed
        # statement leaves the session in an aborted
        # transaction, and any further command on it raises
        # InFailedSQLTransactionError -- which would replace
        # the real ingestion failure with a confusing error
        # about the advisory lock.
        await session.rollback()
        raise
    finally:
        try:
            await session.execute(
                sa_text("SELECT pg_advisory_unlock(hashtext(:source))"),
                {"source": key},
            )
        except SQLAlchemyError:
            # Never let lock cleanup mask the outcome. Ending
            # the transaction closes the connection, which
            # releases a session-level advisory lock anyway.
            logger.warning(
                "Could not explicitly release the advisory lock for '%s'; "
                "releasing it by closing the session instead.",
                key,
            )
