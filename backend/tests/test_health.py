"""
Unit and integration tests for the /api/v1/health endpoint.

Tests:
- test_health_returns_200: Verifies GET /api/v1/health returns status 200 with {'status': 'healthy'}
- test_health_response_schema: Validates the exact JSON response schema
- test_health_method_post_not_allowed: Verifies POST method is rejected with HTTP 405 Method Not Allowed
"""

from app.schemas.models import HealthResponse


def test_health_returns_200(test_client):
    """
    Test that GET /api/v1/health returns HTTP 200 with expected body:
    {"status": "healthy"}.
    """
    response = test_client.get("/api/v1/health")

    assert (
        response.status_code == 200
    ), f"Expected status code 200, got {response.status_code}. Response: {response.text}"
    data = response.json()
    assert data == {
        "status": "healthy"
    }, f"Expected response body {{'status': 'healthy'}}, got: {data}"


def test_health_response_schema(test_client):
    """
    Test that GET /api/v1/health conforms to the exact HealthResponse schema.
    Verifies keys, value types, and Pydantic model validation.
    """
    response = test_client.get("/api/v1/health")

    assert response.status_code == 200
    assert response.headers.get("content-type", "").startswith("application/json")

    data = response.json()

    # Exact key set assertion - no extra or missing fields
    assert set(data.keys()) == {
        "status"
    }, f"Expected exact keys {{'status'}}, got: {set(data.keys())}"
    assert isinstance(
        data["status"], str
    ), f"Expected 'status' to be a string, got {type(data['status'])}"
    assert data["status"] == "healthy"

    # Validate against Pydantic model
    validated = HealthResponse(**data)
    assert validated.status == "healthy"


def test_health_method_post_not_allowed(test_client):
    """
    Test that POST /api/v1/health returns HTTP 405 Method Not Allowed.
    The health endpoint must only accept GET requests.
    """
    response = test_client.post("/api/v1/health", json={"dummy": "data"})

    assert (
        response.status_code == 405
    ), f"Expected status code 405 Method Not Allowed for POST, got {response.status_code}"


def test_health_unhealthy_returns_503(test_client, monkeypatch):
    """
    Test that GET /api/v1/health returns HTTP 503 Service Unavailable
    when the underlying datastore is inaccessible.
    """
    from unittest.mock import AsyncMock

    def mock_wait_for_side_effect(coro, *args, **kwargs):
        coro.close()
        raise Exception("DB Down")

    mock_wait_for = AsyncMock(side_effect=mock_wait_for_side_effect)
    monkeypatch.setattr("app.api.v1.health.asyncio.wait_for", mock_wait_for)

    response = test_client.get("/api/v1/health")

    assert response.status_code == 503
    data = response.json()
    assert data["status"] == "unhealthy"
