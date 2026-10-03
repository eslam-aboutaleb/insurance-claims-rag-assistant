"""Coverage tests for app.api.v1.rag_eval uncovered paths."""

from __future__ import annotations

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


class TestRagEvalAPI:
    def test_evaluate_rag_invalid_category_returns_422(self, test_client, mock_current_user):
        response = test_client.post(
            "/api/v1/rag/evaluate",
            json={"mode": "retrieval-only", "judge": "heuristic", "category": "invalid"},
            headers=mock_current_user,
        )
        assert response.status_code == 422

    def test_evaluate_rag_invalid_difficulty_returns_422(self, test_client, mock_current_user):
        response = test_client.post(
            "/api/v1/rag/evaluate",
            json={"mode": "retrieval-only", "judge": "heuristic", "difficulty": "invalid"},
            headers=mock_current_user,
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

    def test_evaluate_rag_unexpected_exception_returns_500(self, test_client, mock_current_user):
        with patch("app.api.v1.rag_eval.run_evaluation", new_callable=AsyncMock) as mock_run:
            mock_run.side_effect = Exception("unexpected failure")
            response = test_client.post(
                "/api/v1/rag/evaluate",
                json={"mode": "retrieval-only", "judge": "heuristic"},
                headers=mock_current_user,
            )
        assert response.status_code == 500
