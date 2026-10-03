"""
Tests for app.domain.policies.tools module.

Moved from tests/test_policy_rag.py (ragkit plan 06):
the tool moved to the domain adapter, so the patches
target ``app.domain.policies.tools``.
"""

from unittest.mock import AsyncMock, patch

import pytest

from app.domain.policies.tools import query_policy


@pytest.mark.asyncio
async def test_query_policy_returns_empty_when_no_results():
    with patch(
        "app.domain.policies.tools.retrieve_hybrid", new_callable=AsyncMock
    ) as mock_retrieve:
        mock_retrieve.return_value = []

        result = await query_policy("some random query that returns nothing")

    assert result["chunks_found"] == 0
    assert result["sources"] == []
    assert "No relevant policy information found" in result["answer_context"]


@pytest.mark.asyncio
async def test_query_policy_returns_results_with_metadata():
    mock_results = [
        {
            "document": "Water damage is covered up to $25,000.",
            "metadata": {
                "section": "Water Damage",
                "source": "policy.md",
                "chunk_index": 0,
                "sub_chunk_index": 0,
            },
            "distance": 0.5,
            "_rrf_score": 0.8,
        },
        {
            "document": "Electronics are covered up to $10,000.",
            "metadata": {
                "section": "Personal Property",
                "source": "policy.md",
                "chunk_index": 1,
                "sub_chunk_index": 0,
            },
            "distance": 0.3,
            "_rrf_score": 0.9,
        },
    ]

    with patch(
        "app.domain.policies.tools.retrieve_hybrid", new_callable=AsyncMock
    ) as mock_retrieve:
        mock_retrieve.return_value = mock_results

        result = await query_policy("water damage coverage")

    assert result["chunks_found"] == 2
    assert len(result["sources"]) == 2
    assert "Water Damage" in result["answer_context"]
    assert "Personal Property" in result["answer_context"]
    assert result["sources"][0]["section"] == "Water Damage"
    assert result["sources"][1]["section"] == "Personal Property"


@pytest.mark.asyncio
async def test_query_policy_deduplicates_sections():
    """The sources list deduplicates by section (frozen sources format)."""
    mock_results = [
        {
            "document": "Water damage coverage part 1.",
            "metadata": {"section": "Water Damage", "source": "policy.md", "chunk_index": 0},
            "distance": 0.5,
            "_rrf_score": 0.8,
        },
        {
            "document": "Water damage coverage part 2.",
            "metadata": {"section": "Water Damage", "source": "policy.md", "chunk_index": 1},
            "distance": 0.4,
            "_rrf_score": 0.9,
        },
    ]

    with patch(
        "app.domain.policies.tools.retrieve_hybrid", new_callable=AsyncMock
    ) as mock_retrieve:
        mock_retrieve.return_value = mock_results

        result = await query_policy("water damage")

    assert result["chunks_found"] == 2
    assert len(result["sources"]) == 1
    assert result["sources"][0]["section"] == "Water Damage"
    assert "part 1" in result["answer_context"]
    assert "part 2" in result["answer_context"]


@pytest.mark.asyncio
async def test_query_policy_handles_missing_metadata():
    mock_results = [
        {
            "document": "Some policy text.",
            "metadata": {},
            "distance": 0.5,
            "_rrf_score": 0.8,
        },
    ]

    with patch(
        "app.domain.policies.tools.retrieve_hybrid", new_callable=AsyncMock
    ) as mock_retrieve:
        mock_retrieve.return_value = mock_results

        result = await query_policy("test")

    assert result["chunks_found"] == 1
    assert result["sources"][0]["section"] == "General Policy"
    assert result["sources"][0]["source"] == "sample_policy.md"
