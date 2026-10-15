"""
Tests for app.core.database module.

Covers get_db dependency and create_all_tables.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core.database import create_all_tables, get_db


@pytest.mark.asyncio
async def test_get_db_yields_session():
    mock_session = AsyncMock()
    mock_session.close = AsyncMock()

    with patch("app.core.database.async_session_factory") as mock_factory:
        mock_factory.return_value.__aenter__.return_value = mock_session

        gen = get_db()
        session = await gen.__anext__()
        assert session == mock_session

        try:
            await gen.__anext__()
        except StopAsyncIteration:
            pass

        mock_session.close.assert_called_once()


@pytest.mark.asyncio
async def test_get_db_closes_on_exception():
    mock_session = AsyncMock()
    mock_session.close = AsyncMock()

    with patch("app.core.database.async_session_factory") as mock_factory:
        mock_factory.return_value.__aenter__.return_value = mock_session

        gen = get_db()
        await gen.__anext__()
        try:
            await gen.__anext__()
        except StopAsyncIteration:
            pass

        mock_session.close.assert_called_once()


@pytest.mark.asyncio
async def test_create_all_tables():
    with patch("app.core.database.engine") as mock_engine:
        mock_conn = AsyncMock()
        mock_engine.begin.return_value.__aenter__.return_value = mock_conn
        mock_conn.run_sync = AsyncMock()

        with patch("app.models.base.Base") as mock_base:
            mock_base.metadata = MagicMock()

            await create_all_tables()

            mock_conn.run_sync.assert_called_once_with(mock_base.metadata.create_all)
