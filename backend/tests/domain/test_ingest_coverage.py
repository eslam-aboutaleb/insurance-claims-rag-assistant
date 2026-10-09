"""Coverage tests for app.domain.policies.ingestion uncovered paths.

The chunking tests moved to ``libs/ragit/tests/test_chunking.py``
together with the chunking implementation (ragit plan 02); the
ingestion-path tests below point at the domain adapter
(ragit plan 06).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class TestIngest:
    @pytest.mark.asyncio
    async def test_ingest_policy_no_path_configured(self):
        from app.config import Settings
        from app.domain.policies.ingestion import ingest_policy

        with patch("app.domain.policies.ingestion.get_settings") as mock_get_settings:
            mock_settings = Settings(policy_file_path="")
            mock_get_settings.return_value = mock_settings
            count = await ingest_policy()
        assert count == 0

    @pytest.mark.asyncio
    async def test_ingest_policy_file_not_found_returns_zero(self):
        from app.domain.policies.ingestion import ingest_policy

        with patch("app.domain.policies.ingestion.get_settings") as mock_get_settings:
            mock_settings = MagicMock()
            mock_settings.policy_file_path = "/nonexistent/policy.md"
            mock_get_settings.return_value = mock_settings
            count = await ingest_policy()
        assert count == 0

    def test_ingest_main_block(self):
        with patch(
            "app.domain.policies.ingestion.ingest_policy", new_callable=AsyncMock
        ) as mock_ingest:
            mock_ingest.return_value = 0
            import app.domain.policies.ingestion as ingest_module

            original_name = ingest_module.__name__
            try:
                ingest_module.__name__ = "__main__"
                import asyncio

                asyncio.run(ingest_module.ingest_policy())
            finally:
                ingest_module.__name__ = original_name
