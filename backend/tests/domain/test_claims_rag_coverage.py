"""Coverage tests for app.domain.claims.retriever uncovered paths.

Moved from tests/test_claims_rag_coverage.py: the binding moved to the domain adapter, so the
patch targets the ragit ``HybridRetriever`` the adapter wraps.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class TestClaimsRag:
    @pytest.mark.asyncio
    async def test_retrieve_claims_hybrid_exception_returns_empty(self):
        from app.domain.claims.retriever import retrieve_claims_hybrid

        with patch("app.domain.claims.retriever.HybridRetriever") as mock_retriever_cls:
            mock_store = MagicMock()
            mock_store.retrieve = AsyncMock(side_effect=Exception("search failed"))
            mock_retriever_cls.return_value = mock_store
            result = await retrieve_claims_hybrid(
                query="test",
                user_id=uuid.UUID("00000000-0000-0000-0000-000000000001"),
            )
        assert result == []


class TestSearchClaimsEnvelope:
    @pytest.mark.asyncio
    async def test_retriever_failure_keeps_the_tool_envelope_homogeneous(self):
        """A failing retrieval must not smuggle a fake row past the tool's contract.

        The adapter swallows the failure and returns nothing, so the tool answers
        through its own envelope — never through a result the LLM could read as a
        claim that exists.
        """
        from app.agent.context import current_user_id
        from app.domain.claims.tools import search_claims

        current_user_id.set(uuid.UUID("00000000-0000-0000-0000-000000000001"))

        with patch("app.domain.claims.retriever.HybridRetriever") as mock_retriever_cls:
            mock_store = MagicMock()
            mock_store.retrieve = AsyncMock(side_effect=Exception("search failed"))
            mock_retriever_cls.return_value = mock_store

            envelope = await search_claims(query="kitchen flooding")

        assert envelope["results"] == []
        assert envelope["count"] == 0
        assert envelope["error"] is None
        assert envelope["message"] == "No relevant claims found matching your search."
