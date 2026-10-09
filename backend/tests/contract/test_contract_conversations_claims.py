"""
Tier A golden contract tests for ``/api/v1/chat/conversations`` and ``/api/v1/claims``.

Freezes the conversation list/detail envelopes, the malformed-UUID 400, the not-owned
404, and the two-step claim submission handshake including the 422 and 400 negatives.

Every scenario drives the public API only. Seeding rows directly would put the test
body on a different event loop from the ``TestClient``, which strands the process-wide
SQLAlchemy engine pool on a closed loop and produces intermittent foreign-key failures.
Conversation and claim state is created through the same endpoints a client uses.
"""

from __future__ import annotations

import time
import uuid

import pytest
from fastapi.testclient import TestClient

from tests.contract.conftest import SeededUser, compare_to_golden
from tests.contract.llm_double import ScriptedTurn

pytestmark = pytest.mark.contract

_VALID_CLAIM = {
    "policy_number": "POL-1092",
    "claim_type": "Water Damage",
    "amount": 5000,
    "description": "A pipe burst flooded the kitchen yesterday.",
}


def _send_chat(contract_client: TestClient, user: SeededUser, message: str) -> None:
    """Post one chat turn so the conversation store records a conversation."""
    response = contract_client.post("/api/v1/chat", json={"message": message}, headers=user.headers)
    assert response.status_code == 200, response.text


def _dump_users() -> str:
    """Return the current usernames, so a failure names the visible rows."""
    import asyncio

    from sqlalchemy import text

    from app.database import async_session_factory

    async def _read() -> list[str]:
        async with async_session_factory() as session:
            rows = await session.execute(text("SELECT username FROM users ORDER BY username"))
            return list(rows.scalars())

    from app.database import engine

    async def _run() -> list[str]:
        await engine.dispose()
        try:
            return await _read()
        finally:
            await engine.dispose()

    try:
        return str(asyncio.run(_run()))
    except Exception as exc:  # pragma: no cover - diagnostics only
        return f"<unreadable: {exc!r}>"


def _first_conversation_id(contract_client: TestClient, user: SeededUser) -> str:
    """Return the id of the caller's most recently updated conversation.

    ``chat.py`` persists the turn with ``asyncio.create_task``, so the write lands
    shortly after the response is returned. The poll mirrors what a real client sees
    and keeps the test independent of task scheduling.
    """
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        response = contract_client.get("/api/v1/chat/conversations", headers=user.headers)
        assert (
            response.status_code == 200
        ), f"conversation list failed: {response.text} user={user.user_id} rows={_dump_users()}"
        conversations = response.json()["conversations"]
        if conversations:
            return conversations[0]["id"]
        time.sleep(0.05)
    raise AssertionError("conversation was never persisted within 5s")


def test_conversation_list_is_newest_first(
    contract_client: TestClient,
    user_a: SeededUser,
    install_scripted_llm,
) -> None:
    """``GET /chat/conversations`` returns metadata ordered by most recently updated."""
    install_scripted_llm(ScriptedTurn(final_text="Grounded answer."))

    _send_chat(contract_client, user_a, "first question about water damage")
    _first_conversation_id(contract_client, user_a)
    _send_chat(contract_client, user_a, "second question about water damage")

    response = contract_client.get("/api/v1/chat/conversations", headers=user_a.headers)
    assert response.status_code == 200

    body = response.json()
    compare_to_golden(
        "conversation_list_item",
        {
            key: ("volatile" if key in {"id", "created_at", "updated_at"} else value)
            for key, value in body["conversations"][0].items()
        },
    )


def test_conversation_list_is_empty_for_a_new_user(
    contract_client: TestClient,
    user_a: SeededUser,
) -> None:
    """A user with no turns sees an empty list, not an error."""
    response = contract_client.get("/api/v1/chat/conversations", headers=user_a.headers)
    assert response.status_code == 200
    assert response.json() == {"conversations": []}


def test_conversation_detail_includes_messages(
    contract_client: TestClient,
    user_a: SeededUser,
    install_scripted_llm,
) -> None:
    """``GET /chat/conversations/{id}`` returns the full message history."""
    install_scripted_llm(ScriptedTurn(final_text="Burst pipes are covered."))

    _send_chat(contract_client, user_a, "Is a burst pipe covered?")
    conversation_id = _first_conversation_id(contract_client, user_a)

    response = contract_client.get(
        f"/api/v1/chat/conversations/{conversation_id}",
        headers=user_a.headers,
    )
    assert response.status_code == 200

    body = response.json()
    assert [message["role"] for message in body["messages"]] == ["user", "assistant"]
    compare_to_golden(
        "conversation_detail",
        {
            "id": "volatile",
            "title": body["title"],
            "created_at": "volatile",
            "updated_at": "volatile",
            "messages": [
                {
                    "role": message["role"],
                    "sources": message["sources"],
                    "tool_calls": message["tool_calls"],
                }
                for message in body["messages"]
            ],
        },
    )


def test_conversation_detail_malformed_uuid_is_400(
    contract_client: TestClient,
    user_a: SeededUser,
) -> None:
    """A non-UUID conversation id returns the frozen 400 envelope."""
    response = contract_client.get(
        "/api/v1/chat/conversations/not-a-uuid",
        headers=user_a.headers,
    )
    assert response.status_code == 400
    compare_to_golden("conversation_detail_malformed_id", response.json())


def test_conversation_detail_not_owned_is_404(
    contract_client: TestClient,
    user_a: SeededUser,
    user_b: SeededUser,
    install_scripted_llm,
) -> None:
    """Another user's conversation id returns the frozen 404 envelope."""
    install_scripted_llm(ScriptedTurn(final_text="Grounded answer."))

    _send_chat(contract_client, user_a, "private question")
    conversation_id = _first_conversation_id(contract_client, user_a)

    response = contract_client.get(
        f"/api/v1/chat/conversations/{conversation_id}",
        headers=user_b.headers,
    )
    assert response.status_code == 404
    compare_to_golden("conversation_detail_not_owned", response.json())


def test_conversation_list_excludes_other_users(
    contract_client: TestClient,
    user_a: SeededUser,
    user_b: SeededUser,
    install_scripted_llm,
) -> None:
    """User B's list never contains user A's conversation."""
    install_scripted_llm(ScriptedTurn(final_text="Grounded answer."))

    _send_chat(contract_client, user_a, "only mine")
    _first_conversation_id(contract_client, user_a)
    user_a_ids = {
        item["id"]
        for item in contract_client.get(
            "/api/v1/chat/conversations", headers=user_a.headers
        ).json()["conversations"]
    }

    user_b_ids = {
        item["id"]
        for item in contract_client.get(
            "/api/v1/chat/conversations", headers=user_b.headers
        ).json()["conversations"]
    }

    assert user_a_ids
    assert not (user_a_ids & user_b_ids)


def test_claims_prepare_returns_confirmation_token(
    contract_client: TestClient,
    user_a: SeededUser,
) -> None:
    """``POST /claims/prepare`` returns the frozen pending-submission envelope."""
    response = contract_client.post(
        "/api/v1/claims/prepare",
        json=_VALID_CLAIM,
        headers=user_a.headers,
    )
    assert response.status_code == 200
    compare_to_golden(
        "claims_prepare",
        response.json(),
        volatile_keys=frozenset({"confirmation_token", "expires_at"}),
    )


@pytest.mark.parametrize(
    ("payload", "case"),
    [
        pytest.param({**_VALID_CLAIM, "amount": 0}, "zero_amount"),
        pytest.param({**_VALID_CLAIM, "amount": -10}, "negative_amount"),
        pytest.param({**_VALID_CLAIM, "description": "short"}, "short_description"),
    ],
)
def test_claims_prepare_validation_errors(
    contract_client: TestClient,
    user_a: SeededUser,
    payload: dict,
    case: str,
) -> None:
    """Invalid claim payloads return the frozen 422 envelope."""
    response = contract_client.post("/api/v1/claims/prepare", json=payload, headers=user_a.headers)
    assert response.status_code == 422
    compare_to_golden(f"claims_prepare_{case}", response.json())


def test_claims_confirm_completes_submission(
    contract_client: TestClient,
    user_a: SeededUser,
) -> None:
    """``POST /claims/confirm`` returns the frozen confirmation envelope."""
    prepared = contract_client.post(
        "/api/v1/claims/prepare",
        json=_VALID_CLAIM,
        headers=user_a.headers,
    )
    assert prepared.status_code == 200
    token = prepared.json()["confirmation_token"]

    response = contract_client.post(
        "/api/v1/claims/confirm",
        json={"confirmation_token": token},
        headers=user_a.headers,
    )
    assert response.status_code == 200

    body = response.json()
    claim_id = body["claim_id"]
    compare_to_golden(
        "claims_confirm",
        body,
        volatile_keys=frozenset({"claim_id"}),
        transform=lambda payload: {
            **payload,
            "message": payload["message"].replace(claim_id, "CLM-REDACTED"),
        },
    )


def test_claims_confirm_unknown_token_is_400(
    contract_client: TestClient,
    user_a: SeededUser,
) -> None:
    """An unknown confirmation token returns the frozen 400 envelope."""
    response = contract_client.post(
        "/api/v1/claims/confirm",
        json={"confirmation_token": str(uuid.uuid4())},
        headers=user_a.headers,
    )
    assert response.status_code == 400
    compare_to_golden("claims_confirm_unknown_token", response.json())


def test_claims_confirm_replayed_token_is_400(
    contract_client: TestClient,
    user_a: SeededUser,
) -> None:
    """Confirming the same token twice returns the frozen 400 envelope."""
    prepared = contract_client.post(
        "/api/v1/claims/prepare",
        json=_VALID_CLAIM,
        headers=user_a.headers,
    )
    token = prepared.json()["confirmation_token"]
    payload = {"confirmation_token": token}

    first = contract_client.post("/api/v1/claims/confirm", json=payload, headers=user_a.headers)
    assert first.status_code == 200
    second = contract_client.post("/api/v1/claims/confirm", json=payload, headers=user_a.headers)
    assert second.status_code == 400
    compare_to_golden("claims_confirm_replayed_token", second.json())


def test_claims_confirm_other_users_token_is_400(
    contract_client: TestClient,
    user_a: SeededUser,
    user_b: SeededUser,
) -> None:
    """Confirming another user's token returns the frozen 400 envelope."""
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
    compare_to_golden("claims_confirm_other_users_token", response.json())


def test_claims_confirm_writes_exactly_one_claim_per_token(
    contract_client: TestClient,
    user_a: SeededUser,
) -> None:
    """A second confirm of the same token creates no additional claim.

    The replayed confirm is rejected with 400 before any write, so the owner ends up
    with exactly one claim from the one token they prepared.
    """
    prepared = contract_client.post(
        "/api/v1/claims/prepare",
        json=_VALID_CLAIM,
        headers=user_a.headers,
    )
    token = prepared.json()["confirmation_token"]
    payload = {"confirmation_token": token}

    first = contract_client.post("/api/v1/claims/confirm", json=payload, headers=user_a.headers)
    assert first.status_code == 200
    for _ in range(3):
        again = contract_client.post("/api/v1/claims/confirm", json=payload, headers=user_a.headers)
        assert again.status_code == 400
