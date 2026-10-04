"""Tier B assertion 4: owner scoping and ACL filters on claim search."""

from __future__ import annotations

from typing import Any

import pytest

from app.domain.claims.retriever import retrieve_claims_hybrid
from app.domain.embeddings import get_vector_store
from tests.integration.conftest import hash_embedding

DISTANCE_THRESHOLD = 1.3


@pytest.mark.asyncio
async def test_owner_scoping_hides_other_users_claims(
    tierb_seed: dict[str, Any],
):
    """User B's claim search returns nothing for user A's claim text."""
    user_a = tierb_seed["users"]["a"]
    user_b = tierb_seed["users"]["b"]

    results_b = await retrieve_claims_hybrid(query="pipe burst kitchen flooding", user_id=user_b)
    assert results_b == []

    results_a = await retrieve_claims_hybrid(query="pipe burst kitchen flooding", user_id=user_a)
    claim_ids = [row["metadata"]["claim_id"] for row in results_a]
    assert claim_ids == ["CLM-8821"]


@pytest.mark.asyncio
async def test_acl_filter_returns_only_own_claim(
    tierb_seed: dict[str, Any],
):
    """The owner_id filter scopes both search stages to one user."""
    store = get_vector_store(table_name="claims", id_field="id")
    user_b = tierb_seed["users"]["b"]
    results = await store.hybrid_search(
        query="fire damage apartment",
        embedding=hash_embedding("fire damage apartment"),
        n_results=5,
        threshold=DISTANCE_THRESHOLD,
        text_field="text",
        metadata_fields=["claim_id", "owner_id"],
        owner_id=str(user_b),
    )
    assert [row["metadata"]["claim_id"] for row in results] == ["CLM-7700"]


@pytest.mark.asyncio
async def test_claim_search_returns_identifiers(
    tierb_seed: dict[str, Any],
):
    """Claim results carry the document id and keyword score."""
    user_a = tierb_seed["users"]["a"]
    results = await retrieve_claims_hybrid(query="burglary stolen electronics", user_id=user_a)
    assert results
    row = results[0]
    assert row["id"]
    assert row["metadata"]["claim_id"] == "CLM-9014"
    assert row["keyword_score"] is not None
