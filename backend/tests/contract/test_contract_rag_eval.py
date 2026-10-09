"""
Tier A golden contract tests for ``/api/v1/rag/evaluate`` and ``/api/v1/rag/dataset``.

Only the response *key sets* and the dataset envelope are frozen here. Retrieval metric
values are deliberately not frozen in Tier A: adding ``id`` to ``hybrid_search`` results
legitimately changes those numbers, so the values live in the Tier B baseline instead.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tests.contract.conftest import SeededUser, compare_to_golden

pytestmark = pytest.mark.contract

_RETRIEVAL_METRIC_KEYS = {"recall_at_k", "precision_at_k", "mrr", "latency_ms"}
_ANSWER_METRIC_KEYS = {"faithfulness", "relevance", "completeness", "conciseness"}


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
    user_a: SeededUser,
) -> None:
    """``POST /rag/evaluate`` returns the frozen top-level key set."""
    response = contract_client.post(
        "/api/v1/rag/evaluate",
        json={"mode": "retrieval-only", "judge": "heuristic", "n_results": 5},
        headers=user_a.headers,
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


def test_evaluate_invalid_category_is_422(contract_client: TestClient, user_a: SeededUser) -> None:
    """An unknown category returns the frozen 422 envelope."""
    response = contract_client.post(
        "/api/v1/rag/evaluate",
        json={"mode": "retrieval-only", "category": "not-a-category"},
        headers=user_a.headers,
    )
    assert response.status_code == 422
    compare_to_golden("rag_evaluate_invalid_category", response.json())


def test_evaluate_requires_authentication(contract_client: TestClient) -> None:
    """The evaluation endpoint is protected."""
    response = contract_client.post("/api/v1/rag/evaluate", json={"mode": "retrieval-only"})
    assert response.status_code == 401
