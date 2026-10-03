"""Session provider protocol and standalone session factory.

ragkit talks to databases through the :class:`SessionProvider`
protocol: any callable that returns an async session context
manager works structurally. The OmniCare application keeps its own
engine and session factory in ``app/database.py`` (including its
NullPool test behavior); :func:`create_session_factory` is the
standalone helper for projects that use ragkit without the host
application.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine


@runtime_checkable
class SessionProvider(Protocol):
    """Callable that yields an async session as a context manager."""

    def __call__(self) -> AsyncSession: ...


def create_session_factory(database_url: str) -> SessionProvider:
    """Build a session factory bound to a new async engine.

    Args:
        database_url: SQLAlchemy async database URL (for example
            ``postgresql+asyncpg://user:pass@host/db``).

    Returns:
        A session factory. Each call yields an independent
        ``AsyncSession`` with ``expire_on_commit=False``. The caller
        owns the engine's lifecycle; ragkit creates no module-level
        singletons.
    """
    engine = create_async_engine(database_url)
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
