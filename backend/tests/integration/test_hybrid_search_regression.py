"""Tier B assertions 2 and 3: hybrid search projection and distance semantics.

Assertion 2 is the regression guard for the 5.6 defect: an empty
``metadata_fields`` list used to render a bare comma in the outer
SELECT, producing a syntax error that the broad ``except`` swallowed
into an empty result list.

Assertion 3 pins the distance contract: keyword-only hits report
``distance is None``, and identical vectors preserve ``distance == 0.0``.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from app.domain.embeddings import get_vector_store
from tests.integration.conftest import EMBEDDING_DIM, hash_embedding

DISTANCE_THRESHOLD = 1.3


@pytest.mark.asyncio
async def test_empty_metadata_fields_still_returns_rows(
    tierb_seed: dict[str, Any],
):
    """hybrid_search(metadata_fields=[]) returns rows against real Postgres."""
    store = get_vector_store(table_name="policy_chunks", id_field="id")
    results = await store.hybrid_search(
        query="water damage",
        embedding=hash_embedding("water damage"),
        n_results=5,
        threshold=DISTANCE_THRESHOLD,
        text_field="text",
        metadata_fields=[],
    )
    assert len(results) >= 1


@pytest.mark.asyncio
async def test_keyword_only_hit_reports_null_distance(
    tierb_seed: dict[str, Any],
):
    """A hit found only by full-text search has no vector distance."""
    store = get_vector_store(table_name="policy_chunks", id_field="id")
    # A constant vector is ~40 units from every stored unit-norm embedding,
    # so the vector stage contributes nothing and only the keyword stage
    # can match.
    far_embedding = [1.0] * EMBEDDING_DIM
    results = await store.hybrid_search(
        query="water damage pipe burst",
        embedding=far_embedding,
        n_results=5,
        threshold=DISTANCE_THRESHOLD,
        text_field="text",
        metadata_fields=["section", "chunk_id"],
    )
    assert results
    for row in results:
        assert row["distance"] is None
        assert row["keyword_score"] is not None
        assert row["keyword_score"] > 0.0


@pytest.mark.asyncio
async def test_identical_vector_keeps_zero_distance(
    tierb_seed: dict[str, Any],
):
    """Searching with a document's own embedding yields distance 0.0."""
    store = get_vector_store(table_name="claims", id_field="id")
    document_id = str(uuid.uuid4())
    text_content = "Claim CLM-9999: Water Damage - Pipe burst causing kitchen flooding."
    embedding = hash_embedding(text_content)
    await store.upsert(
        documents=[
            {
                "id": document_id,
                "text": text_content,
                "embedding": embedding,
                "metadata": {
                    "claim_id": "CLM-9999",
                    "policy_number": "POL-9999",
                    "claim_type": "Water Damage",
                    "status": "Submitted",
                    "amount": 100.0,
                    "owner_id": str(tierb_seed["users"]["a"]),
                    "description": "Pipe burst causing kitchen flooding.",
                },
            }
        ]
    )
    results = await store.hybrid_search(
        query="pipe burst kitchen flooding",
        embedding=embedding,
        n_results=5,
        threshold=DISTANCE_THRESHOLD,
        text_field="text",
        metadata_fields=["claim_id"],
    )
    matches = [row for row in results if row["id"] == document_id]
    assert matches
    assert matches[0]["distance"] == 0.0
