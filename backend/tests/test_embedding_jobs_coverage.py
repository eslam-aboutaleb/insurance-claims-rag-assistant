"""Coverage tests for the OmniCare embedding-job enqueue path.

Moved from ``backend/tests/test_embedding_jobs_coverage.py``
(ragkit plan 05): the enqueue now delegates to ragkit's
outbox through the app-side store; these tests pin the
app adapter's session handling and error logging.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, patch

import pytest

from app.domain.claims.outbox import enqueue_embedding_job


class TestEmbeddingJobs:
    @pytest.mark.asyncio
    async def test_enqueue_embedding_job_exception_logged(self):
        from app.domain.claims import outbox as embedding_jobs

        with patch.object(embedding_jobs, "async_session_factory") as mock_factory:
            mock_factory.return_value.__aenter__.side_effect = Exception("DB down")
            with patch.object(embedding_jobs, "logger") as mock_logger:
                await enqueue_embedding_job(
                    claim_uuid=uuid.UUID("00000000-0000-0000-0000-000000000001"),
                    claim_id="CLM-9999",
                    owner_id=uuid.UUID("00000000-0000-0000-0000-000000000001"),
                    claim_type="Water Damage",
                    description="Test",
                    policy_number="POL-1092",
                    claim_status="Submitted",
                )
                mock_logger.exception.assert_called()

    @pytest.mark.asyncio
    async def test_enqueue_embedding_job_success(self):
        from app.domain.claims import outbox as embedding_jobs

        with patch.object(embedding_jobs, "async_session_factory") as mock_factory:
            mock_session = AsyncMock()
            mock_factory.return_value.__aenter__.return_value = mock_session
            await enqueue_embedding_job(
                claim_uuid=uuid.UUID("00000000-0000-0000-0000-000000000001"),
                claim_id="CLM-9999",
                owner_id=uuid.UUID("00000000-0000-0000-0000-000000000001"),
                claim_type="Water Damage",
                description="Test",
                policy_number="POL-1092",
                claim_status="Submitted",
            )
            mock_session.add.assert_called_once()
            mock_session.commit.assert_called_once()
