"""
Unit and integration tests for the /api/v1/chat endpoint.

All LLM and agent execution is mocked to ensure fast, deterministic,
and offline execution without external API dependencies.

Tests:
- test_chat_valid_request: Mock the agent runner, POST valid payload, verify response schema
  has response, sources, tool_calls
- test_chat_missing_message: POST without message returns HTTP 422 Unprocessable Entity
- test_chat_empty_message: POST with empty message string succeeds and returns valid response
- test_chat_internal_error_500: Unhandled exception raises HTTP 500
- test_chat_production_error_sanitization: Production environment sanitizes error details
"""

from unittest.mock import AsyncMock, patch

import pytest

from app.config import settings
from app.idempotency import clear_cache
from app.schemas.models import ChatResponse


@pytest.fixture(autouse=True)
def reset_idempotency_cache():
    """Clear the idempotency cache before every chat test to prevent cross-test pollution."""
    clear_cache()
    yield
    clear_cache()


def test_chat_valid_request(test_client, mock_current_user):
    """
    Test sending a valid chat request with a message.
    Mocks run_agent at 'app.api.v1.chat.run_agent' and verifies that the
    response contains 'response', 'sources', and 'tool_calls' matching the schema.
    """
    mock_agent_result = {
        "response": "Water damage caused by sudden pipe bursts is covered up to $25,000.",
        "sources": ["sample_policy.md#Section 1: Home Water Damage Coverage"],
        "tool_calls": [
            {
                "name": "query_policy",
                "arguments": {"query": "water damage coverage"},
                "result": {"chunks_found": 1},
            }
        ],
    }

    with patch("app.api.v1.chat.run_agent", new_callable=AsyncMock) as mock_run:
        mock_run.return_value = mock_agent_result

        payload = {
            "message": "What is covered under water damage?",
        }
        response = test_client.post(
            "/api/v1/chat",
            json=payload,
            headers=mock_current_user,
        )

        assert (
            response.status_code == 200
        ), f"Expected status code 200, got {response.status_code}. Details: {response.text}"

        data = response.json()

        # Verify required schema fields
        assert "response" in data, "Response missing 'response' field"
        assert "sources" in data, "Response missing 'sources' field"
        assert "tool_calls" in data, "Response missing 'tool_calls' field"

        # Verify content
        assert data["response"] == mock_agent_result["response"]
        assert data["sources"] == mock_agent_result["sources"]
        assert len(data["tool_calls"]) == 1
        assert data["tool_calls"][0]["name"] == "query_policy"

        # Verify Pydantic schema validation
        validated = ChatResponse(**data)
        assert validated.response == mock_agent_result["response"]

        # Verify mock was called with the authenticated user_id
        mock_run.assert_awaited_once_with(
            user_id="00000000-0000-0000-0000-000000000001",
            message="What is covered under water damage?",
        )


def test_chat_missing_message(test_client, mock_current_user):
    """
    Test that a POST request omitting the required 'message' field
    is rejected with HTTP 422 Unprocessable Entity using a standardized
    ErrorResponse envelope.
    """
    payload = {}
    response = test_client.post(
        "/api/v1/chat",
        json=payload,
        headers=mock_current_user,
    )

    assert (
        response.status_code == 422
    ), f"Expected 422 Unprocessable Entity when message is omitted, got {response.status_code}"
    data = response.json()

    # Standardized error envelope: {"error": {"code", "message", "details"}}
    assert "error" in data, "Expected 'error' key in 422 response envelope"
    error = data["error"]
    assert "code" in error, "Expected 'code' field in error object"
    assert "message" in error, "Expected 'message' field in error object"
    assert "details" in error, "Expected 'details' field in error object"

    # Confirm the validation error mentions message
    error_fields = [err["loc"][-1] for err in error["details"] if "loc" in err]
    assert "message" in error_fields, f"'message' not found in error fields: {error_fields}"


def test_chat_empty_message(test_client, mock_current_user):
    """
    Test that POST with an empty message string succeeds (HTTP 200).
    The schema allows empty strings for message, and the agent handles it gracefully.
    """
    mock_agent_result = {
        "response": "Hello! How can I assist you with OmniCare insurance today?",
        "sources": [],
        "tool_calls": [],
    }

    with patch("app.api.v1.chat.run_agent", new_callable=AsyncMock) as mock_run:
        mock_run.return_value = mock_agent_result

        payload = {
            "message": "",
        }
        response = test_client.post(
            "/api/v1/chat",
            json=payload,
            headers=mock_current_user,
        )

        assert (
            response.status_code == 200
        ), f"Expected status code 200 for empty message string, got {response.status_code}"

        data = response.json()
        assert "response" in data
        assert "sources" in data
        assert "tool_calls" in data
        assert data["response"] == mock_agent_result["response"]
        assert data["sources"] == []
        assert data["tool_calls"] == []

        mock_run.assert_awaited_once_with(
            user_id="00000000-0000-0000-0000-000000000001", message=""
        )


def test_chat_internal_error_500(test_client, mock_current_user):
    """
    Test that an unhandled exception in the agent runner raises HTTP 500 Internal Server Error.
    """
    with patch("app.api.v1.chat.run_agent", new_callable=AsyncMock) as mock_run:
        mock_run.side_effect = RuntimeError("Simulated internal LLM service crash")

        payload = {
            "message": "Will this crash?",
        }
        response = test_client.post(
            "/api/v1/chat",
            json=payload,
            headers=mock_current_user,
        )

        assert response.status_code == 500
        data = response.json()
        assert "error" in data
        assert "An unexpected error occurred" in data["error"]["message"]


def test_chat_production_error_sanitization(test_client, mock_current_user, monkeypatch):
    """
    Test that when environment is 'production', internal exceptions are sanitized
    to prevent sensitive detail leakage to clients.
    """
    monkeypatch.setattr(settings, "environment", "production")

    with patch("app.api.v1.chat.run_agent", new_callable=AsyncMock) as mock_run:
        mock_run.side_effect = ValueError("Database connection secret password leaked in exception")

        payload = {
            "message": "Trigger failure",
        }
        response = test_client.post(
            "/api/v1/chat",
            json=payload,
            headers=mock_current_user,
        )

        assert response.status_code == 500
        data = response.json()
        assert "secret password" not in data["error"]["message"]
        assert "An unexpected error occurred" in data["error"]["message"]
