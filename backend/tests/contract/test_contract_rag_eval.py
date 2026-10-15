"""
Tier A golden contract tests for ``/api/v1/rag/evaluate`` and ``/api/v1/rag/dataset``.

Only the response *key sets* and the dataset envelope are frozen here. Retrieval metric
values are deliberately not frozen in Tier A: adding ``id`` to ``hybrid_search`` results
legitimately changes those numbers, so the values live in the Tier B baseline instead.

``POST /rag/evaluate`` is admin-only: the harness scores the shared retrieval layer
rather than the caller's own data, so the submission tests drive a promoted admin
account instead of the default policyholder. The frozen envelopes are unchanged --
an admin caller receives exactly the response body the policyholder used to, and no
golden was re-recorded.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
from fastapi.testclient import TestClient

from tests.contract.conftest import SeededUser, compare_to_golden, signup

pytestmark = pytest.mark.contract

_RETRIEVAL_METRIC_KEYS = {"recall_at_k", "precision_at_k", "mrr", "latency_ms"}
_ANSWER_METRIC_KEYS = {"faithfulness", "relevance", "completeness", "conciseness"}


@pytest.fixture(scope="function")
def admin_a(contract_client: TestClient) -> SeededUser:
    """A signed-up account promoted to admin, built from the shared signup helper.

    ``signup`` only mints a policyholder and no public route assigns a role, so the
    row is promoted the way an operator would. The evaluation route reads the role
    from the database rather than from the token, so a promoted account is the only
    honest way to drive it.
    """
    from app.core.database import async_session_factory, engine
    from app.models.user import User

    username = f"contract_admin_{uuid.uuid4().hex[:8]}"
    seeded = signup(contract_client, username, "TestPass123!")

    async def _promote() -> None:
        await engine.dispose()
        try:
            async with async_session_factory() as session:
                user = await session.get(User, uuid.UUID(seeded.user_id))
                assert user is not None
                user.role = "admin"
                await session.commit()
        finally:
            await engine.dispose()

    asyncio.run(_promote())
    return seeded


def test_dataset_item_shape_is_frozen(contract_client: TestClient, user_a: SeededUser) -> None:
    """``GET /rag/dataset`` returns the frozen per-sample envelope."""
    response = contract_client.get(
        "/api/v1/rag/dataset",
        params={"category": "coverage"},
        headers=user_a.headers,
    )
    assert response.status_code == 200
    samples = response.json()
    assert samples, "the coverage category must not be empty"
    compare_to_golden("rag_dataset_sample", samples[0])


def test_dataset_invalid_category_is_422(contract_client: TestClient, user_a: SeededUser) -> None:
    """An unknown category returns the frozen 422 envelope."""
    response = contract_client.get(
        "/api/v1/rag/dataset",
        params={"category": "not-a-category"},
        headers=user_a.headers,
    )
    assert response.status_code == 422
    compare_to_golden("rag_dataset_invalid_category", response.json())


def test_dataset_requires_authentication(contract_client: TestClient) -> None:
    """The dataset endpoint is protected."""
    response = contract_client.get("/api/v1/rag/dataset")
    assert response.status_code == 401


def test_evaluate_envelope_keys_are_frozen(
    contract_client: TestClient,
    admin_a: SeededUser,
) -> None:
    """``POST /rag/evaluate`` returns the frozen top-level key set."""
    response = contract_client.post(
        "/api/v1/rag/evaluate",
        json={"mode": "retrieval-only", "judge": "heuristic", "n_results": 5},
        headers=admin_a.headers,
    )
    assert response.status_code == 200
    body = response.json()

    assert set(body) == {
        "timestamp",
        "config",
        "retrieval_metrics",
        "answer_metrics",
        "per_sample_results",
        "summary",
        "duration_seconds",
    }
    compare_to_golden(
        "rag_evaluate_envelope",
        {
            "top_level_keys": list(body),
            "config": body["config"],
            "retrieval_metric_keys": sorted(body["retrieval_metrics"]),
            "answer_metric_keys": sorted(body["answer_metrics"]),
            "summary_keys": sorted(body["summary"]) if isinstance(body["summary"], dict) else None,
            "per_sample_item_keys": sorted(body["per_sample_results"][0])
            if body["per_sample_results"]
            else None,
        },
        volatile_keys=frozenset({"timestamp", "duration_seconds"}),
    )


def test_evaluate_invalid_category_is_422(contract_client: TestClient, admin_a: SeededUser) -> None:
    """An unknown category returns the frozen 422 envelope."""
    response = contract_client.post(
        "/api/v1/rag/evaluate",
        json={"mode": "retrieval-only", "category": "not-a-category"},
        headers=admin_a.headers,
    )
    assert response.status_code == 422
    compare_to_golden("rag_evaluate_invalid_category", response.json())


def test_evaluate_refuses_a_policyholder(contract_client: TestClient, user_a: SeededUser) -> None:
    """A policyholder is refused by the evaluation harness with the 403 envelope."""
    response = contract_client.post(
        "/api/v1/rag/evaluate",
        json={"mode": "retrieval-only", "judge": "heuristic"},
        headers=user_a.headers,
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "FORBIDDEN"


def test_evaluate_requires_authentication(contract_client: TestClient) -> None:
    """The evaluation endpoint is protected."""
    response = contract_client.post("/api/v1/rag/evaluate", json={"mode": "retrieval-only"})
    assert response.status_code == 401
