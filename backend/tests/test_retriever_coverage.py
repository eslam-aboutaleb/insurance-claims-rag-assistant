"""Coverage tests for app.rag.retriever uncovered paths."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest


class TestRetriever:
    @pytest.mark.asyncio
    async def test_retrieve_hybrid_exception_returns_empty(self):
        from app.rag.retriever import retrieve_hybrid

        mock_retriever = AsyncMock()
        mock_retriever.retrieve.side_effect = Exception("search failed")

        with patch("app.rag.retriever.HybridRetriever", return_value=mock_retriever):
            result = await retrieve_hybrid(query="test", n_results=5)
        assert result == []
