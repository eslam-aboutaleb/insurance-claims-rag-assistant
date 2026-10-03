"""Session provider protocol and standalone session factory.

Pins two things: :func:`ragkit.db.session.create_session_factory`
builds a working session factory, and the OmniCare application's
own ``async_session_factory`` satisfies the
:class:`SessionProvider` protocol structurally (the app keeps its
own engine, including its NullPool test behavior).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from sqlalchemy import text

from ragkit.db.session import SessionProvider, create_session_factory


def test_create_session_factory_returns_session_provider():
    factory = create_session_factory("sqlite+aiosqlite:///:memory:")
    assert callable(factory)
    assert isinstance(factory, SessionProvider)


@pytest.mark.asyncio
async def test_create_session_factory_yields_working_session():
    factory = create_session_factory("sqlite+aiosqlite:///:memory:")
    async with factory() as session:
        result = await session.execute(text("SELECT 1"))
        assert result.scalar_one() == 1


def test_app_session_factory_satisfies_session_provider_protocol():
    """The OmniCare app's async_session_factory satisfies the Protocol.

    The app keeps its own engine (including NullPool test behavior);
    this structural check pins that its session factory is a drop-in
    :class:`SessionProvider` for ragkit stores.
    """
    backend_dir = Path(__file__).resolve().parents[3] / "backend"
    if str(backend_dir) not in sys.path:
        sys.path.insert(0, str(backend_dir))
    try:
        from app.database import async_session_factory
    except Exception:
        pytest.skip("OmniCare backend could not be imported (settings/engine)")
    assert isinstance(async_session_factory, SessionProvider)
