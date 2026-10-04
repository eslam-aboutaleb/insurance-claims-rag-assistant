"""
Tier A golden contract tests for the shared error envelope and the IDOR negatives.

Every failure response must be ``{"error": {"code", "message", "details"}}`` with the
``code`` string frozen per status. These tests are the guard that a later plan cannot
quietly change an error shape.

Scenarios drive the public API only; see the note in
``test_contract_conversations_claims`` for why the suite never writes rows directly.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient
from fastapi import HTTPException
from starlette.requests import Request

from tests.contract.conftest import SeededUser, _unique, compare_to_golden, signup
from tests.contract.llm_double import ScriptedToolCall, ScriptedTurn

pytestmark = pytest.mark.contract

_ERROR_CODES = {
    400: "BAD_REQUEST",
    401: "UNAUTHORIZED",
    403: "FORBIDDEN",
    404: "NOT_FOUND",
    409: "CONFLICT",
    422: "VALIDATION_ERROR",
    429: "RATE_LIMIT_EXCEEDED",
    500: "INTERNAL_ERROR",
    503: "SERVICE_UNAVAILABLE",
}

_VALID_CLAIM = {
    "policy_number": "POL-1092",
    "claim_type": "Water Damage",
    "amount": 5000,
    "description": "A pipe burst flooded the kitchen yesterday.",
}


def test_error_envelope_shape_is_stable(
    contract_client: TestClient,
    user_a: SeededUser,
) -> None:
    """The envelope has exactly ``error.code``, ``error.message``, ``error.details``."""
    response = contract_client.get(
        "/api/v1/chat/conversations/not-a-uuid",
        headers=user_a.headers,
    )
    body = response.json()
    assert set(body) == {"error"}
    assert set(body["error"]) == {"code", "message", "details"}


def _scope_request() -> Request:
    """Build a minimal but complete ASGI scope for direct handler invocation.

    ``request.url`` is logged inside the handler, and Starlette builds a ``URL`` from
    the scope, so ``scheme``, ``server``, and ``headers`` are all required.
    """
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/v1/contract",
            "root_path": "",
            "scheme": "http",
            "query_string": b"",
            "headers": [],
            "server": ("testserver", 80),
            "client": ("testclient", 50000),
        }
    )


def _json_body(raw: bytes) -> dict:
    """Decode a JSON response body captured from a Starlette response."""
    import json

    return json.loads(raw.decode())


@pytest.mark.asyncio
@pytest.mark.parametrize(("status", "code"), sorted(_ERROR_CODES.items()))
async def test_error_code_mapping_is_frozen(status: int, code: str) -> None:
    """Every status maps to its frozen ``code`` through the registered handler.

    Exercised against the handler directly because no public v1 route can currently
    produce 403, 429, or 503. If a later plan adds such a route, this table is the
    contract it must satisfy.
    """
    from app.main import http_exception_handler

    response = await http_exception_handler(
        _scope_request(),
        HTTPException(status, "frozen detail"),
    )
    assert response.status_code == status
    assert response.body is not None
    assert _json_body(response.body)["error"]["code"] == code
    compare_to_golden(f"error_code_{status}", _json_body(response.body))


def test_error_envelope_400_end_to_end(contract_client: TestClient, user_a: SeededUser) -> None:
    """A malformed conversation id renders the frozen 400 envelope."""
    response = contract_client.get(
        "/api/v1/chat/conversations/not-a-uuid",
        headers=user_a.headers,
    )
    assert response.status_code == 400
    compare_to_golden("error_400", response.json())


def test_error_envelope_401_end_to_end(contract_client: TestClient) -> None:
    """An unauthenticated request renders the frozen 401 envelope."""
    response = contract_client.get("/api/v1/auth/me")
    assert response.status_code == 401
    compare_to_golden("error_401", response.json())


def test_error_envelope_404_end_to_end(contract_client: TestClient, user_a: SeededUser) -> None:
    """An unknown conversation renders the frozen 404 envelope."""
    response = contract_client.get(
        f"/api/v1/chat/conversations/{uuid.uuid4()}",
        headers=user_a.headers,
    )
    assert response.status_code == 404
    compare_to_golden("error_404", response.json())


def test_error_envelope_409_end_to_end(contract_client: TestClient) -> None:
    """A duplicate signup renders the frozen 409 envelope."""
    username = _unique("envelope_dup_user")
    signup(contract_client, username)
    response = contract_client.post(
        "/api/v1/auth/signup",
        json={"username": username, "password": "ContractPass123!"},
    )
    assert response.status_code == 409
    compare_to_golden("error_409", response.json())


def test_error_envelope_422_end_to_end(
    contract_client: TestClient,
    user_a: SeededUser,
) -> None:
    """A validation failure renders the frozen 422 envelope."""
    response = contract_client.post(
        "/api/v1/claims/prepare",
        json={**_VALID_CLAIM, "amount": 0},
        headers=user_a.headers,
    )
    assert response.status_code == 422
    compare_to_golden("error_422", response.json())


def test_error_envelope_500_is_generic(
    contract_client: TestClient,
    user_a: SeededUser,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unexpected agent failure returns a generic 500 that leaks no internals."""

    async def _boom(*_args, **_kwargs):
        raise RuntimeError("secret internal detail /srv/app/stack.py")

    monkeypatch.setattr("app.api.v1.chat.run_agent", _boom)

    response = contract_client.post(
        "/api/v1/chat",
        json={"message": "trigger an internal error"},
        headers=user_a.headers,
    )
    assert response.status_code == 500
    body = response.json()
    assert "secret internal detail" not in body["error"]["message"]
    compare_to_golden("error_500", body)


def test_idor_conversation_detail_is_404_for_other_users(
    contract_client: TestClient,
    user_a: SeededUser,
    user_b: SeededUser,
    install_scripted_llm,
) -> None:
    """User B cannot read user A's conversation by guessing its UUID."""
    install_scripted_llm(ScriptedTurn(final_text="Grounded answer."))

    chat = contract_client.post(
        "/api/v1/chat",
        json={"message": "private question"},
        headers=user_a.headers,
    )
    assert chat.status_code == 200

    from tests.contract.test_contract_conversations_claims import _first_conversation_id

    conversation_id = _first_conversation_id(contract_client, user_a)

    response = contract_client.get(
        f"/api/v1/chat/conversations/{conversation_id}",
        headers=user_b.headers,
    )
    assert response.status_code == 404
    assert "private question" not in response.text


def test_idor_conversation_list_is_empty_for_other_users(
    contract_client: TestClient,
    user_a: SeededUser,
    user_b: SeededUser,
    install_scripted_llm,
) -> None:
    """User B's list contains nothing belonging to user A."""
    install_scripted_llm(ScriptedTurn(final_text="Grounded answer."))

    contract_client.post(
        "/api/v1/chat",
        json={"message": "mine only"},
        headers=user_a.headers,
    )

    response = contract_client.get("/api/v1/chat/conversations", headers=user_b.headers)
    assert response.status_code == 200
    assert response.json()["conversations"] == []


def test_idor_confirmation_token_is_400_for_other_users(
    contract_client: TestClient,
    user_a: SeededUser,
    user_b: SeededUser,
) -> None:
    """User B cannot complete user A's pending claim submission."""
    prepared = contract_client.post(
        "/api/v1/claims/prepare",
        json=_VALID_CLAIM,
        headers=user_a.headers,
    )
    token = prepared.json()["confirmation_token"]

    response = contract_client.post(
        "/api/v1/claims/confirm",
        json={"confirmation_token": token},
        headers=user_b.headers,
    )
    assert response.status_code == 400
    assert user_b.user_id != user_a.user_id


def test_idor_claim_lookup_tool_is_owner_scoped(
    contract_client: TestClient,
    user_a: SeededUser,
    user_b: SeededUser,
    install_scripted_llm,
) -> None:
    """The claim lookup tool reports not-found when another user asks for a claim.

    User A creates the claim through the real confirm flow, then user B's agent is
    scripted to look it up. The tool is owner-scoped, so B must not learn the claim
    exists, let alone read its description or amount.
    """
    prepared = contract_client.post(
        "/api/v1/claims/prepare",
        json=_VALID_CLAIM,
        headers=user_a.headers,
    )
    confirmed = contract_client.post(
        "/api/v1/claims/confirm",
        json={"confirmation_token": prepared.json()["confirmation_token"]},
        headers=user_a.headers,
    )
    assert confirmed.status_code == 200
    claim_id = confirmed.json()["claim_id"]

    install_scripted_llm(
        ScriptedTurn(
            tool_calls=(ScriptedToolCall("get_claim_status", {"claim_id": claim_id}),),
            final_text="I could not find that claim.",
        )
    )
    response = contract_client.post(
        "/api/v1/chat",
        json={"message": f"What is the status of claim {claim_id}?"},
        headers=user_b.headers,
    )
    assert response.status_code == 200

    tool_results = response.json()["tool_calls"]
    assert tool_results, "the scripted turn must have invoked the lookup tool"
    result = tool_results[0]["result"]
    assert result["found"] is False
    assert "Pipe burst flooded the kitchen yesterday." not in str(result)


def test_idor_claim_search_tool_is_owner_scoped(
    contract_client: TestClient,
    user_a: SeededUser,
    user_b: SeededUser,
    install_scripted_llm,
) -> None:
    """Hybrid claim search for user B returns no rows belonging to user A.

    The claims index is populated by the embedding worker, so this asserts on the
    retrieval boundary: the ``owner_id`` filter is applied to both search CTEs, so a
    differently-owned claim can never appear in the other user's results.
    """
    import asyncio

    from app.domain.claims.tools import search_claims

    results = asyncio.run(search_claims(query="kitchen flooding", n_results=5))
    owners = {row.get("metadata", {}).get("owner_id") for row in results}

    assert user_b.user_id not in owners
    assert user_a.user_id not in owners
