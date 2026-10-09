"""
Tests for app.domain.claims.tools module.

Moved from tests/test_search_claims.py (ragit plan 06):
the tool moved to the domain adapter, so the patches
target ``app.domain.claims.tools``. The ``current_user_id``
ContextVar is resolved at call time inside the tool, so
the unauthorized test patches ``app.agent.context``.
"""

import uuid
from unittest.mock import AsyncMock, patch

import pytest

from app.agent.context import current_user_id
from app.domain.claims.tools import search_claims


@pytest.mark.asyncio
async def test_search_claims_unauthorized():
    with patch("app.agent.context.current_user_id") as mock_ctx:
        mock_ctx.get.side_effect = LookupError

        result = await search_claims("water damage")

    assert result == [{"error": "Unauthorized claim search."}]


@pytest.mark.asyncio
async def test_search_claims_no_results():
    current_user_id.set(uuid.uuid4())

    with patch(
        "app.domain.claims.tools.retrieve_claims_hybrid", new_callable=AsyncMock
    ) as mock_retrieve:
        mock_retrieve.return_value = []

        result = await search_claims("water damage")

    assert result == [{"message": "No relevant claims found matching your search."}]


@pytest.mark.asyncio
async def test_search_claims_error_path():
    current_user_id.set(uuid.uuid4())

    with patch(
        "app.domain.claims.tools.retrieve_claims_hybrid", new_callable=AsyncMock
    ) as mock_retrieve:
        mock_retrieve.side_effect = RuntimeError("Search failed")

        result = await search_claims("water damage")

    assert result == [{"error": "Unable to search claims at this time. Please try again later."}]


@pytest.mark.asyncio
async def test_search_claims_success():
    current_user_id.set(uuid.uuid4())

    mock_results = [
        {
            "document": "Claim CLM-1: Water damage",
            "metadata": {"claim_id": "CLM-1"},
            "distance": 0.5,
        }
    ]

    with patch(
        "app.domain.claims.tools.retrieve_claims_hybrid", new_callable=AsyncMock
    ) as mock_retrieve:
        mock_retrieve.return_value = mock_results

        result = await search_claims("water damage")

    assert result == mock_results
