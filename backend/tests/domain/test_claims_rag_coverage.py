"""Coverage tests for app.domain.claims.retriever uncovered paths.

Moved from tests/test_claims_rag_coverage.py (ragkit plan 06):
the binding moved to the domain adapter, so the patch targets
the ragkit ``HybridRetriever`` the adapter wraps.
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
