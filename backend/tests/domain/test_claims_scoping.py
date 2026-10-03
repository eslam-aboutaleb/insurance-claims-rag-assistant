"""Owner-scoping tests for the claims retrieval adapter (ragkit plan 06 T4a).

The horizontal privilege-escalation guard: ``ClaimsRetriever``
always passes ``owner_id=str(user_id)`` as an equality filter
on both search stages, so a caller can never see another
user's claims. The filter is applied inside the adapter, not
by callers.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.domain.claims.retriever import ClaimsRetriever


@pytest.mark.asyncio
async def test_claims_retrieval_is_always_owner_scoped():
    """User B's retrieval filters on user B's owner_id — never user A's."""
    user_a = uuid.uuid4()
    user_b = uuid.uuid4()

    with patch("app.domain.claims.retriever.HybridRetriever") as mock_retriever_cls:
        mock_instance = MagicMock()
        mock_instance.retrieve = AsyncMock(return_value=[])
        mock_retriever_cls.return_value = mock_instance

        retriever = ClaimsRetriever()
        await retriever.retrieve("water damage claim", user_id=user_b)

    mock_instance.retrieve.assert_awaited_once()
    kwargs = mock_instance.retrieve.await_args.kwargs
    # The guard: the requesting user's owner_id is always the filter.
    assert kwargs["owner_id"] == str(user_b)
    assert kwargs["owner_id"] != str(user_a)


@pytest.mark.asyncio
async def test_claims_retrieval_owner_filter_is_the_requesting_user_only():
    """A different requester produces a different owner_id filter."""
    user_c = uuid.uuid4()

    with patch("app.domain.claims.retriever.HybridRetriever") as mock_retriever_cls:
        mock_instance = MagicMock()
        mock_instance.retrieve = AsyncMock(return_value=[])
        mock_retriever_cls.return_value = mock_instance

        retriever = ClaimsRetriever()
        await retriever.retrieve("fire damage claim", user_id=user_c)

    kwargs = mock_instance.retrieve.await_args.kwargs
    assert kwargs["owner_id"] == str(user_c)
