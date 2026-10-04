"""Tier B assertion 6: embedding validation rejection.

Every ingestion and search path validates dimension and finiteness
explicitly, so a 3072-dim embedding against a 1536-dim table raises
instead of failing at insert time or silently returning no results.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from ragkit.validation import validate_embedding as _validate_embedding
from app.domain.embeddings import get_vector_store


@pytest.mark.asyncio
async def test_wrong_dimension_embeddings_are_rejected(
    tierb_seed: dict[str, Any],
):
    """A 3072-dim embedding is rejected on all three paths."""
    store = get_vector_store(table_name="policy_chunks", id_field="id")

    with pytest.raises(ValueError, match="dimensions"):
        _validate_embedding([0.0] * 3072, expected_dim=1536)

    with pytest.raises(ValueError, match="dimensions"):
        await store.upsert(
            documents=[
                {
                    "id": str(uuid.uuid4()),
                    "text": "bad dimension",
                    "embedding": [0.0] * 3072,
                    "metadata": {},
                }
            ]
        )

    with pytest.raises(ValueError, match="dimensions"):
        await store.hybrid_search(
            query="water damage",
            embedding=[0.0] * 3072,
            n_results=5,
            threshold=1.3,
        )


@pytest.mark.asyncio
async def test_non_finite_embeddings_are_rejected(
    tierb_seed: dict[str, Any],
):
    """NaN and infinity never reach the database."""
    with pytest.raises(ValueError, match="not finite"):
        _validate_embedding([float("nan")] + [0.0] * 1535, expected_dim=1536)
    with pytest.raises(ValueError, match="not finite"):
        _validate_embedding([float("inf")] + [0.0] * 1535, expected_dim=1536)
    with pytest.raises(TypeError, match="not a number"):
        _validate_embedding(["nan"] + [0.0] * 1535, expected_dim=1536)
