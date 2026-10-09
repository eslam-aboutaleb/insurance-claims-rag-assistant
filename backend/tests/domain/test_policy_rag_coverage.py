"""Coverage tests for app.domain.policies.tools uncovered paths.

Moved from tests/test_policy_rag_coverage.py (ragit plan 06).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest


class TestPolicyRagTool:
    @pytest.mark.asyncio
    async def test_query_policy_returns_empty_on_no_context(self):
        from app.domain.policies.tools import query_policy

        with patch(
            "app.domain.policies.tools.retrieve_hybrid", new_callable=AsyncMock
        ) as mock_retrieve:
            mock_retrieve.return_value = []
            result = await query_policy(query="coverage")
            assert result.get("chunks_found") == 0
