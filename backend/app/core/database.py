"""
Database session factory and engine configuration for the OmniCare backend.

This module centralizes SQLAlchemy async engine setup and provides the
``get_db`` FastAPI dependency used by every endpoint that requires a database
session. It also exposes ``create_all_tables`` for startup-time schema
initialization (used in development and Docker build contexts).

Design decisions:
  - ``expire_on_commit=False`` prevents SQLAlchemy from expiring ORM instances
    after each commit, which avoids extra SELECT queries in read-heavy flows.
  - ``create_all_tables`` imports models inside the function body rather than
    at module scope. This ensures the declarative base metadata is fully
    populated before ``create_all`` runs, while also avoiding circular import
    issues during module initialization.
"""

import os
from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import settings

# Database URL resolved from centralized Pydantic settings.
DATABASE_URL = settings.database_url

# The async engine is a process-wide singleton. ``echo=False`` suppresses
# SQL logging; enable it temporarily when debugging query performance.
#
# ``OMNICARE_TEST_NULLPOOL`` selects ``NullPool``, which opens a fresh connection
# for every checkout and closes it on return. The test suite runs each request on a
# different event loop (the ``TestClient`` portal loop, then the fixture reset's own
# ``asyncio.run`` loop). A pooling engine can hand a live connection to a loop other
# than the one that created it, which asyncpg rejects with "got Future attached to a
# different loop" and which surfaces as an intermittent 401 or a missing row rather
# than as an obvious error. NullPool makes that impossible.
#
# Honoured only when ENVIRONMENT=test. NullPool is correct for tests and pathological
# under load, so a stray value in a deployment environment must not select it.
_NULL_POOL = os.environ.get("OMNICARE_TEST_NULLPOOL") == "1" and settings.environment == "test"

engine = create_async_engine(
    DATABASE_URL,
    echo=False,
    poolclass=NullPool if _NULL_POOL else None,
)

# Session factory bound to the engine. Each ``async_session_factory()`` call
# produces an independent ``AsyncSession`` that must be closed after use.
async_session_factory = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency that yields an async database session.

    The session is automatically closed after the request completes, even if
    the endpoint raises an exception. This is the canonical way to inject a
    database session into route handlers.

    Yields:
        AsyncSession: A scoped SQLAlchemy async session for the duration of
        the request.
    """
    async with async_session_factory() as session:
        try:
            yield session
        finally:
            await session.close()


async def create_all_tables():
    """Create all tables defined by SQLAlchemy ORM models.

    Importing ``app.models`` inside the function body imports every model
    module, which is what guarantees the declarative base metadata is fully
    populated before ``create_all`` runs. Without that ordering, ``create_all``
    would silently create no tables because the model subclasses would not yet
    be registered.

    Intended for development and CI setup. In production, use Alembic
    migrations instead.
    """
    import app.models  # noqa: F401, PLC0415
    from app.models.base import Base  # noqa: PLC0415

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
