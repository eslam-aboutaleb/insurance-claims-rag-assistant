"""Owner-scoping tests for the claims retrieval adapter.

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

from app.agent.context import current_user_id
from app.domain.claims.retriever import ClaimsRetriever
from app.domain.claims.tools import search_claims


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


@pytest.mark.asyncio
async def test_search_claims_envelope_never_exposes_the_owner_uuid():
    """The search tool drops ``owner_id`` before a retrieval row reaches the model.

    Scoping happens on the retrieval side; the tool projection is the second
    guard, so the caller's raw UUID and the internal ranking signals never
    enter the LLM-facing envelope.
    """
    user = uuid.UUID("00000000-0000-0000-0000-000000000002")
    current_user_id.set(user)

    raw_row = {
        "document": "Claim CLM-9: Water damage",
        "metadata": {
            "claim_id": "CLM-9",
            "policy_number": "POL-9",
            "owner_id": str(user),
        },
        "distance": 0.2,
        "_rrf_score": 0.75,
    }

    with patch(
        "app.domain.claims.tools.retrieve_claims_hybrid", new_callable=AsyncMock
    ) as mock_retrieve:
        mock_retrieve.return_value = [raw_row]
        envelope = await search_claims("water damage")

    assert envelope["count"] == 1
    assert str(user) not in str(envelope)
    projected = envelope["results"][0]
    assert set(projected["metadata"]) == {"claim_id", "policy_number"}
    assert projected["citation"] == "Claim CLM-9 in the OmniCare claims system (policy POL-9)."
