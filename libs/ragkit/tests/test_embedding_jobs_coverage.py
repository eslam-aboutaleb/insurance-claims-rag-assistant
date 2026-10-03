"""Coverage tests for the ragkit job outbox enqueue path.

Moved from ``backend/tests/test_embedding_jobs_coverage.py``
(ragkit extraction plan 05), including the outbox
invariant test: a job enqueued with a caller session
is not committed by the enqueue call.
"""

from __future__ import annotations

import pytest

from ragkit.jobs.outbox import enqueue_job
from tests.job_doubles import FakeJobStore, RecordingSession


class TestEnqueueJob:
    @pytest.mark.asyncio
    async def test_enqueue_with_caller_session_does_not_commit(self):
        """Passing a session must only flush, never commit."""
        store = FakeJobStore()
        session = RecordingSession()
        payload = {"claim_id": "CLM-9999", "description": "Test"}

        await enqueue_job(store, payload, session=session)

        assert session.commits == 0, "enqueue committed a caller-owned session"
        assert session.flushes == 1
        assert store.enqueued_payloads == [payload]

    @pytest.mark.asyncio
    async def test_enqueue_without_session_commits(self):
        """With no session the enqueue is self-contained and durable."""
        store = FakeJobStore()
        payload = {"claim_id": "CLM-9999", "description": "Test"}

        await enqueue_job(store, payload)

        assert store.enqueued_payloads == [payload]
        assert store.committed_payloads == [payload]

    @pytest.mark.asyncio
    async def test_enqueue_passes_the_session_through_to_the_store(self):
        """The caller's session reaches the store's enqueue verbatim."""
        store = FakeJobStore()
        session = RecordingSession()

        await enqueue_job(store, {"claim_id": "CLM-1"}, session=session)

        assert session.flushes == 1
        assert session.commits == 0
