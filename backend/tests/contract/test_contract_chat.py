"""
Tier A golden contract tests for ``/api/v1/chat`` and ``/api/v1/chat/stream``.

Freezes the agent response shape, the tool-call trace, idempotent replay behaviour, the
409 body-mismatch rule, the SSE event ordering, and the guarantee that a terminal
``response_complete`` event is emitted even when the model stream fails.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app.agent.conversation_store import SavedMessageIds
from tests.contract.conftest import SeededUser, compare_to_golden
from tests.contract.llm_double import ScriptedToolCall, ScriptedTurn

pytestmark = pytest.mark.contract

_GROUNDED_TURN = ScriptedTurn(
    tool_calls=(ScriptedToolCall("query_policy", {"query": "water damage coverage"}),),
    final_text="Burst pipes are covered under Home Water Damage Coverage.",
)

_PLAIN_TURN = ScriptedTurn(final_text="Ask me anything about your policy.")


def _sse_events(payload: str) -> list[dict]:
    """Parse an SSE payload into a list of decoded ``data:`` payloads."""
    events: list[dict] = []
    for raw_block in payload.split("\n\n"):
        block = raw_block.strip()
        if not block:
            continue
        data = block.removeprefix("data:").strip()
        try:
            events.append(json.loads(data))
        except json.JSONDecodeError:
            events.append({"raw": data})
    return events


def test_chat_returns_response_sources_and_tool_calls(
    contract_client: TestClient,
    user_a: SeededUser,
    install_scripted_llm,
) -> None:
    """``POST /chat`` returns the frozen envelope including the tool-call trace."""
    install_scripted_llm(_GROUNDED_TURN)
    response = contract_client.post(
        "/api/v1/chat",
        json={"message": "Is a burst pipe covered?"},
        headers=user_a.headers,
    )
    assert response.status_code == 200
    compare_to_golden("chat_response", response.json())


def test_chat_without_tool_calls(
    contract_client: TestClient, user_a: SeededUser, install_scripted_llm
) -> None:
    """A turn with no tool calls returns an empty tool-call trace."""
    install_scripted_llm(_PLAIN_TURN)
    response = contract_client.post(
        "/api/v1/chat",
        json={"message": "Hello"},
        headers=user_a.headers,
    )
    assert response.status_code == 200
    compare_to_golden("chat_response_no_tools", response.json())


def test_chat_accepts_empty_message(
    contract_client: TestClient,
    user_a: SeededUser,
    install_scripted_llm,
) -> None:
    """``POST /chat`` accepts an empty message and answers it.

    ``ChatRequest.message`` has no length constraint, so the empty message reaches the
    agent. ``POST /chat/stream`` rejects the same payload with 422 via an explicit
    guard in the endpoint. That asymmetry is real behaviour and is frozen here so a
    future plan that closes it does so deliberately.
    """
    install_scripted_llm(_PLAIN_TURN)
    response = contract_client.post(
        "/api/v1/chat",
        json={"message": ""},
        headers=user_a.headers,
    )
    assert response.status_code == 200
    compare_to_golden("chat_response_empty_message", response.json())


def test_chat_requires_authentication(contract_client: TestClient) -> None:
    """``POST /chat`` without credentials returns the frozen 401 envelope."""
    response = contract_client.post("/api/v1/chat", json={"message": "hello"})
    assert response.status_code == 401
    compare_to_golden("chat_unauthenticated", response.json())


def test_chat_replays_cached_response(
    contract_client: TestClient,
    user_a: SeededUser,
    install_scripted_llm,
) -> None:
    """A repeated Idempotency-Key returns the cached body with a replay header."""
    double = install_scripted_llm(_GROUNDED_TURN)
    payload = {"message": "Is a burst pipe covered?"}
    headers = {**user_a.headers, "Idempotency-Key": "contract-replay-key"}

    first = contract_client.post("/api/v1/chat", json=payload, headers=headers)
    assert first.status_code == 200
    assert first.headers.get("X-Idempotency-Key") == "contract-replay-key"
    assert "X-Idempotent-Replayed" not in first.headers

    second = contract_client.post("/api/v1/chat", json=payload, headers=headers)
    assert second.status_code == 200
    assert second.headers.get("X-Idempotent-Replayed") == "true"
    assert second.json() == first.json()

    assert len(double.calls) == 2, "the replayed request must not reach the model twice"


def test_chat_idempotency_key_reuse_with_different_body_is_409(
    contract_client: TestClient,
    user_a: SeededUser,
    install_scripted_llm,
) -> None:
    """Reusing a key with a different body returns the frozen 409 envelope."""
    install_scripted_llm(_PLAIN_TURN)
    headers = {**user_a.headers, "Idempotency-Key": "contract-conflict-key"}
    first = contract_client.post("/api/v1/chat", json={"message": "first"}, headers=headers)
    assert first.status_code == 200

    second = contract_client.post("/api/v1/chat", json={"message": "second"}, headers=headers)
    assert second.status_code == 409
    compare_to_golden("chat_idempotency_conflict", second.json())


def test_chat_derives_key_when_header_absent(
    contract_client: TestClient,
    user_a: SeededUser,
    install_scripted_llm,
) -> None:
    """Without a header, an identical repeat is deduplicated by the derived key."""
    double = install_scripted_llm(_PLAIN_TURN)
    payload = {"message": "derived key probe"}

    first = contract_client.post("/api/v1/chat", json=payload, headers=user_a.headers)
    assert first.status_code == 200
    second = contract_client.post("/api/v1/chat", json=payload, headers=user_a.headers)

    assert second.status_code == 200
    assert second.headers.get("X-Idempotent-Replayed") == "true"
    assert len(double.calls) == 1


def test_chat_reset_returns_204(contract_client: TestClient, user_a: SeededUser) -> None:
    """``POST /chat/reset`` returns 204 with an empty body."""
    response = contract_client.post("/api/v1/chat/reset", headers=user_a.headers)
    assert response.status_code == 204
    assert response.content == b""


def test_chat_stream_emits_text_delta_then_response_complete(
    contract_client: TestClient,
    user_a: SeededUser,
    install_scripted_llm,
) -> None:
    """The SSE stream ends with ``response_complete`` carrying the sources."""
    install_scripted_llm(_GROUNDED_TURN)
    with contract_client.stream(
        "POST",
        "/api/v1/chat/stream",
        json={"message": "Is a burst pipe covered?"},
        headers=user_a.headers,
    ) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        body = "".join(response.iter_text())

    events = _sse_events(body)
    types = [event.get("type") for event in events]

    assert types[-1] == "response_complete", f"terminal event missing, got {types}"
    assert "text_delta" in types
    compare_to_golden(
        "chat_stream_terminal",
        events[-1],
        volatile_keys=frozenset({"sources"}),
    )


def test_chat_stream_emits_terminal_event_when_model_stream_fails(
    contract_client: TestClient,
    user_a: SeededUser,
    install_scripted_llm,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A model stream that raises still produces a terminal ``response_complete``.

    The frontend depends on this: without it the sidebar entry and the render both hang.
    """

    async def _boom(*_args, **_kwargs):
        raise RuntimeError("scripted model failure")
        yield  # pragma: no cover - generator marker

    install_scripted_llm(_PLAIN_TURN)
    monkeypatch.setattr("app.api.v1.chat_stream.run_agent_stream", _boom)

    with contract_client.stream(
        "POST",
        "/api/v1/chat/stream",
        json={"message": "trigger a failure"},
        headers=user_a.headers,
    ) as response:
        body = "".join(response.iter_text())

    events = _sse_events(body)
    types = [event.get("type") for event in events]

    # The failure is announced by its own frame, and the terminal frame says the
    # turn stopped: together they let a client tell a truncated answer from a
    # complete one instead of silently rendering an empty response.
    assert "error" in types, f"no error frame emitted, got {types}"
    assert types[-1] == "response_complete"
    assert types.index("error") < len(types) - 1
    assert events[-1]["status"] == "error"
    assert events[-1]["sources"] == []


def test_chat_stream_empty_message_is_422(contract_client: TestClient, user_a: SeededUser) -> None:
    """An empty message is rejected before the stream opens."""
    response = contract_client.post(
        "/api/v1/chat/stream",
        json={"message": "   "},
        headers=user_a.headers,
    )
    assert response.status_code == 422
    compare_to_golden("chat_stream_empty_message", response.json())


@pytest.fixture(autouse=True)
def _silence_conversation_persistence(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep contract scenarios on the HTTP surface rather than on side-effect writes.

    The chat endpoints await the save inside the request, so a real write would
    land inside the request that a golden compares. Replacing it with the
    no-save ``SavedMessageIds`` keeps both the return type the production code
    already coerces and the ids absent from the frozen envelope, so the goldens
    can pin the new ``message_id`` fields as null rather than drifting on them.
    """

    async def _noop(*_args, **_kwargs):
        return SavedMessageIds()

    for module in ("app.api.v1.chat", "app.api.v1.chat_stream"):
        monkeypatch.setattr(f"{module}.save_conversation_turn", _noop)
