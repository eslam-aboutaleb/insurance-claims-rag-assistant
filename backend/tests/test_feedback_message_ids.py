"""
Regression tests: the id a just-persisted answer is rated against.

Reproduces the user-facing bug where ``POST /api/v1/feedback`` refused a rating
on a freshly answered/streamed turn with ``403 FORBIDDEN`` ("This message is not
available to you"), showing "We could not save your rating." in the UI. The cause
was an id mismatch: the frontend rated the id it generated locally
(``createMessageId``), while the backend independently generated a different id
when it persisted the turn, so no row matched the client's guess.

The fix makes the backend the authority: ``save_conversation_turn`` returns the
ids it assigned, ``/chat`` and ``/chat/stream`` echo them, and the feedback
endpoint accepts exactly those. These tests drive that end to end against the
same Docker Postgres the rest of the suite uses, and pin that the ownership
check which refuses a foreign or unknown message id is left intact.

Every test here fails without the fix: before it the chat and streaming responses
carried no message id at all, so there was no backend id to rate.
"""

from __future__ import annotations

import json
import uuid
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

_SHORT_ANSWER = "Sudden pipe-burst water damage is covered up to $25,000."


# --- Fixtures --------------------------------------------------------------


@pytest.fixture
def no_rate_limit():
    """Stand down the request rate limiter for a single test.

    The limiter is keyed on the test client's address, so a suite that fires many
    chat or stream requests within a minute could otherwise trip a 429 that has
    nothing to do with what these tests assert. Mirrors ``test_idempotency.py``.
    """
    from app.main import app

    previous = app.state.limiter.enabled
    app.state.limiter.enabled = False
    try:
        yield
    finally:
        app.state.limiter.enabled = previous


# --- Helpers ---------------------------------------------------------------


def unique_username() -> str:
    """A username that cannot collide with any other test or a re-run."""
    return f"fb_reg_{uuid.uuid4().hex[:12]}"


def _signup(client, username: str) -> dict[str, str]:
    """Create a user and return its bearer-token headers.

    Args:
        client: The test client.
        username: The username to register.

    Returns:
        An ``Authorization`` header dict for the new user's token.
    """
    response = client.post(
        "/api/v1/auth/signup",
        json={"username": username, "password": "TestPass123!"},
    )
    assert "access_token" in response.json(), response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def _chat_as(test_client, headers: dict[str, str], session_id: str) -> dict[str, Any]:
    """Run one non-streaming chat turn with a scripted agent and return its JSON.

    Args:
        test_client: The test client.
        headers: The calling user's auth headers.
        session_id: The session id the turn persists under.

    Returns:
        The decoded ``/chat`` response body.
    """
    with patch("app.api.v1.chat.run_agent", new_callable=AsyncMock) as mock_run:
        mock_run.return_value = {
            "response": _SHORT_ANSWER,
            "sources": ["Section 1: Home Water Damage Coverage"],
            "tool_calls": [],
            "session_id": session_id,
        }
        response = test_client.post(
            "/api/v1/chat",
            json={"message": "Is a burst pipe covered?"},
            headers=headers,
        )
    assert response.status_code == 200, response.text
    return response.json()


def _stream_terminal(test_client, headers: dict[str, str], session_id: str) -> dict[str, Any]:
    """Run one streaming turn with a scripted agent and return its terminal frame.

    Args:
        test_client: The test client.
        headers: The calling user's auth headers.
        session_id: The session id the turn persists under.

    Returns:
        The decoded terminal ``response_complete`` frame of the stream.
    """

    async def scripted_run(user_id, message, result):  # noqa: ANN001, ANN202
        result.session_id = session_id
        result.final_text_parts = [_SHORT_ANSWER]
        yield f'data: {{"type": "text_delta", "content": "{_SHORT_ANSWER}"}}\n\n'

    with patch("app.api.v1.chat_stream.run_agent_stream") as mock_run_agent_stream:
        mock_run_agent_stream.side_effect = scripted_run
        with test_client.stream(
            "POST",
            "/api/v1/chat/stream",
            json={"message": "Is a burst pipe covered?"},
            headers=headers,
        ) as response:
            assert response.status_code == 200
            body = "".join(response.iter_text())

    terminal: dict[str, Any] = {}
    for line in body.splitlines():
        if not line.startswith("data: "):
            continue
        payload = json.loads(line.removeprefix("data: ").strip())
        if isinstance(payload, dict) and payload.get("type") == "response_complete":
            terminal = payload
    assert terminal, "stream produced no response_complete frame"
    return terminal


# --- The regression: rating a just-persisted answer ------------------------


def test_rating_the_answer_a_chat_call_just_persisted_succeeds(test_client, no_rate_limit):
    """A non-streaming chat turn reports the id, and the owner can rate it."""
    owner = _signup(test_client, unique_username())
    session_id = f"reg-{uuid.uuid4().hex}"

    body = _chat_as(test_client, owner, session_id)

    # Before the fix the response carried no message id at all.
    message_id = body["message_id"]
    assert message_id
    # It is the backend's id, a real UUID -- not a client-side guess.
    uuid.UUID(message_id)

    rated = test_client.post(
        "/api/v1/feedback",
        json={"message_id": message_id, "rating": "up"},
        headers=owner,
    )
    assert rated.status_code == 201, rated.text
    assert rated.json()["message_id"] == message_id
    assert rated.json()["rating"] == "up"


def test_rating_the_answer_a_stream_just_persisted_succeeds(test_client, no_rate_limit):
    """A streamed turn's terminal frame reports the id, and the owner can rate it."""
    owner = _signup(test_client, unique_username())
    intruder = _signup(test_client, unique_username())
    session_id = f"reg-{uuid.uuid4().hex}"

    terminal = _stream_terminal(test_client, owner, session_id)

    message_id = terminal["message_id"]
    assert message_id
    uuid.UUID(message_id)

    rated = test_client.post(
        "/api/v1/feedback",
        json={"message_id": message_id, "rating": "up"},
        headers=owner,
    )
    assert rated.status_code == 201, rated.text
    assert rated.json()["message_id"] == message_id

    # A different user still cannot rate the owner's answer.
    forbidden = test_client.post(
        "/api/v1/feedback",
        json={"message_id": message_id, "rating": "down"},
        headers=intruder,
    )
    assert forbidden.status_code == 403
    assert forbidden.json()["error"]["code"] == "FORBIDDEN"


def test_chat_and_stream_both_report_distinct_user_and_assistant_ids(test_client, no_rate_limit):
    """Both endpoints expose a distinct, valid id for the user and assistant rows."""
    owner = _signup(test_client, unique_username())

    chat_body = _chat_as(test_client, owner, f"reg-{uuid.uuid4().hex}")
    assert chat_body["message_id"] and chat_body["user_message_id"]
    assert chat_body["message_id"] != chat_body["user_message_id"]
    uuid.UUID(chat_body["user_message_id"])

    terminal = _stream_terminal(test_client, owner, f"reg-{uuid.uuid4().hex}")
    assert terminal["message_id"] and terminal["user_message_id"]
    assert terminal["message_id"] != terminal["user_message_id"]
    uuid.UUID(terminal["user_message_id"])


# --- The authorization check is not weakened -------------------------------


def test_feedback_still_refuses_foreign_and_unknown_message_ids(test_client, no_rate_limit):
    """The ownership check still 403s a foreign id, an unknown id, and a local guess."""
    owner = _signup(test_client, unique_username())
    intruder = _signup(test_client, unique_username())
    session_id = f"reg-{uuid.uuid4().hex}"

    message_id = _chat_as(test_client, owner, session_id)["message_id"]
    assert message_id

    # A different user cannot rate the owner's answer.
    forbidden = test_client.post(
        "/api/v1/feedback",
        json={"message_id": message_id, "rating": "down"},
        headers=intruder,
    )
    assert forbidden.status_code == 403
    assert forbidden.json()["error"]["code"] == "FORBIDDEN"

    # An id that matches no row at all is forbidden.
    unknown = test_client.post(
        "/api/v1/feedback",
        json={"message_id": str(uuid.uuid4()), "rating": "up"},
        headers=owner,
    )
    assert unknown.status_code == 403
    assert unknown.json()["error"]["code"] == "FORBIDDEN"

    # The exact value the frontend used to send -- a locally generated guess that
    # is not the persisted id -- is just such an unknown id and must still 403.
    local_guess = str(uuid.uuid4())
    rejected = test_client.post(
        "/api/v1/feedback",
        json={"message_id": local_guess, "rating": "up"},
        headers=owner,
    )
    assert rejected.status_code == 403
    assert rejected.json()["error"]["code"] == "FORBIDDEN"
