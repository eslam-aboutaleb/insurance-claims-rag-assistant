"""
API endpoint tests for the RAG evaluation subsystem.

The dataset, evaluator, harness, and runner tests moved to ``libs/ragit/tests``; the HTTP
endpoints stay here because they exercise the OmniCare API surface and its authentication.

  - GET  /api/v1/rag/dataset
  - POST /api/v1/rag/evaluate

The evaluation endpoint is admin-only, and its authorization reads the role
from the database rather than from the token, so the POST tests drive a real
promoted admin account. The dataset endpoint stays open to any authenticated
user and keeps using the mocked one.
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
    limiter's in-memory storage is a process-wide singleton, so every request
    from this suite shares the single ``ip:testclient`` bucket; without this
    fixture the suite's own requests exhaust the 5/minute budget and later
    tests fail with 429.
    """
    from app.main import app

    app.state.limiter.enabled = False
    yield
    app.state.limiter.enabled = True


@pytest.fixture
def admin_user(test_client):
    """A real account promoted to admin, as the evaluation route now requires.

    The evaluation endpoint is admin-only and its authorization reads the
    role from the database rather than from the token, so the tests that
    drive it need an account whose role was granted rather than a mocked
    user id. Signup only mints a policyholder, so the row is promoted the
    way an operator would.
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


def test_api_list_eval_dataset(test_client, mock_current_user):
    response = test_client.get("/api/v1/rag/dataset", headers=mock_current_user)
    assert response.status_code == 200
    data = response.json()
    assert isinstance(data, list)
    assert len(data) >= 10
    assert "query" in data[0]
    assert "expected_sections" in data[0]

    # Filtered
    filtered_resp = test_client.get(
        "/api/v1/rag/dataset?category=coverage", headers=mock_current_user
    )
    assert filtered_resp.status_code == 200
    for item in filtered_resp.json():
        assert item["category"] == "coverage"

    # Invalid category
    bad_resp = test_client.get(
        "/api/v1/rag/dataset?category=invalid_category", headers=mock_current_user
    )
    assert bad_resp.status_code == 422


def test_api_evaluate_rag_endpoint(test_client, admin_user):
    mock_hybrid_results = [
        {
            "document": "Water damage caused by sudden pipe bursts is covered up to $25,000.",
            "metadata": {
                "section": "Section 1: Home Water Damage Coverage",
                "source": "sample_policy.md",
            },
            "distance": 0.2,
            "_rrf_score": 0.95,
        }
    ]

    with patch("app.domain.evaluation.retrieve_hybrid", new_callable=AsyncMock) as mock_retrieve:
        mock_retrieve.return_value = mock_hybrid_results

        payload = {
            "mode": "end-to-end",
            "judge": "heuristic",
            "category": "coverage",
            "difficulty": "easy",
        }
        response = test_client.post(
            "/api/v1/rag/evaluate",
            json=payload,
            headers=admin_user,
        )
        assert response.status_code == 200
        data = response.json()
        assert "retrieval_metrics" in data
        assert "answer_metrics" in data
        assert "summary" in data
        assert data["duration_seconds"] >= 0


def test_api_evaluate_rag_endpoint_retrieval_only(test_client, admin_user):
    """The retrieval-only mode omits answer metrics from the response."""
    mock_hybrid_results = [
        {
            "document": "Water damage caused by sudden pipe bursts is covered up to $25,000.",
            "metadata": {
                "section": "Section 1: Home Water Damage Coverage",
                "source": "sample_policy.md",
            },
            "distance": 0.2,
            "_rrf_score": 0.95,
        }
    ]

    with patch("app.domain.evaluation.retrieve_hybrid", new_callable=AsyncMock) as mock_retrieve:
        mock_retrieve.return_value = mock_hybrid_results

        payload = {
            "mode": "retrieval-only",
            "judge": "heuristic",
            "n_results": 5,
            "category": "coverage",
            "difficulty": "easy",
        }
        response = test_client.post(
            "/api/v1/rag/evaluate",
            json=payload,
            headers=admin_user,
        )
        assert response.status_code == 200
        data = response.json()
        assert data["config"]["mode"] == "retrieval-only"
        assert data["retrieval_metrics"]["total_queries"] > 0


def test_api_evaluate_rag_endpoint_invalid_category(test_client, admin_user):
    """An unknown category is rejected with a 422 listing valid values."""
    payload = {
        "mode": "retrieval-only",
        "judge": "heuristic",
        "category": "not-a-category",
    }
    response = test_client.post(
        "/api/v1/rag/evaluate",
        json=payload,
        headers=admin_user,
    )
    assert response.status_code == 422
    detail = response.json()["error"]["message"]
    assert "Invalid category" in detail


def test_api_evaluate_rag_endpoint_config_round_trip(test_client, admin_user):
    """The request's mode/judge/filters are echoed back in the config."""
    mock_hybrid_results = [
        {
            "document": "Water damage caused by sudden pipe bursts is covered up to $25,000.",
            "metadata": {
                "section": "Section 1: Home Water Damage Coverage",
                "source": "sample_policy.md",
            },
            "distance": 0.2,
            "_rrf_score": 0.95,
        }
    ]

    with patch("app.domain.evaluation.retrieve_hybrid", new_callable=AsyncMock) as mock_retrieve:
        mock_retrieve.return_value = mock_hybrid_results

        payload = {
            "mode": "retrieval-only",
            "judge": "heuristic",
            "n_results": 3,
            "category": "limits",
            "difficulty": "medium",
        }
        response = test_client.post(
            "/api/v1/rag/evaluate",
            json=payload,
            headers=admin_user,
        )
        assert response.status_code == 200
        config = response.json()["config"]
        assert config == {
            "mode": "retrieval-only",
            "judge": "heuristic",
            "n_results": 3,
            "category": "limits",
            "difficulty": "medium",
        }
