"""Coverage tests for app.api.v1.rag_eval uncovered paths.

The evaluation endpoint is admin-only and reads the role from the database, so
the POST tests drive a real promoted admin account; the dataset endpoint stays
open to any authenticated user and keeps the mocked one.
"""

from __future__ import annotations

import asyncio
import uuid
from unittest.mock import AsyncMock, patch

import pytest


@pytest.fixture(autouse=True)
def _disable_rate_limiter():
    """Keep the process-wide SlowAPI limiter out of these tests.

    ``evaluate_rag`` is decorated with ``@limiter.limit("5/minute")`` and the
    limiter's in-memory storage is a process-wide singleton. The fake test
    token does not decode, so every request from this suite shares the single
    ``ip:testclient`` bucket; requests from earlier test files (notably the
    four ``POST /api/v1/rag/evaluate`` calls in ``test_rag_eval.py``) exhaust
    the 5/minute budget, so these tests would see 429 instead of the 422/500
    they exercise.
    """
    from app.main import app

    app.state.limiter.enabled = False
    yield
    app.state.limiter.enabled = True


@pytest.fixture
def admin_user(test_client):
    """A real account promoted to admin, as the evaluation route now requires.

    Signup only mints a policyholder, so the row is promoted the way an
    operator would before its token is used.
    """
    from app.core.database import async_session_factory, engine
    from app.models.user import User

    username = f"eval_admin_{uuid.uuid4().hex[:8]}"
    response = test_client.post(
        "/api/v1/auth/signup", json={"username": username, "password": "TestPass123!"}
    )
    assert response.status_code == 201
    body = response.json()

    async def _promote() -> None:
        await engine.dispose()
        try:
            async with async_session_factory() as session:
                user = await session.get(User, uuid.UUID(body["user_id"]))
                assert user is not None
                user.role = "admin"
                await session.commit()
        finally:
            await engine.dispose()

    asyncio.run(_promote())
    return {"Authorization": f"Bearer {body['access_token']}"}


class TestRagEvalAPI:
    def test_evaluate_rag_invalid_category_returns_422(self, test_client, admin_user):
        response = test_client.post(
            "/api/v1/rag/evaluate",
            json={"mode": "retrieval-only", "judge": "heuristic", "category": "invalid"},
            headers=admin_user,
        )
        assert response.status_code == 422

    def test_evaluate_rag_invalid_difficulty_returns_422(self, test_client, admin_user):
        response = test_client.post(
            "/api/v1/rag/evaluate",
            json={"mode": "retrieval-only", "judge": "heuristic", "difficulty": "invalid"},
            headers=admin_user,
        )
        assert response.status_code == 422

    def test_list_eval_dataset_invalid_category_returns_422(self, test_client, mock_current_user):
        response = test_client.get(
            "/api/v1/rag/dataset?category=invalid", headers=mock_current_user
        )
        assert response.status_code == 422

    def test_list_eval_dataset_invalid_difficulty_returns_422(self, test_client, mock_current_user):
        response = test_client.get(
            "/api/v1/rag/dataset?difficulty=invalid", headers=mock_current_user
        )
        assert response.status_code == 422

    def test_evaluate_rag_unexpected_exception_returns_500(self, test_client, admin_user):
        with patch("app.api.v1.rag_eval.run_evaluation", new_callable=AsyncMock) as mock_run:
            mock_run.side_effect = Exception("unexpected failure")
            response = test_client.post(
                "/api/v1/rag/evaluate",
                json={"mode": "retrieval-only", "judge": "heuristic"},
                headers=admin_user,
            )
        assert response.status_code == 500
