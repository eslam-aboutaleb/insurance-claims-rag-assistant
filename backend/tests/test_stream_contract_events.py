"""Contract tests for the Server-Sent Events emitted by ``/api/v1/chat/stream``.

These tests freeze what the client is allowed to rely on: every frame is valid
JSON, every frame carries a typed ``type``, tool activity is streamed with the
same shape it is persisted with, and a failed turn still terminates with a
``response_complete`` whose ``status`` says so.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable, Iterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.agent.agent import _StreamResult, _as_object, run_agent_stream, sse_frame
from app.schemas.models import (
    StreamError,
    StreamResponseComplete,
    StreamTextDelta,
    StreamToolCall,
    StreamToolError,
    StreamToolResult,
)
from tests.contract.llm_double import ScriptedLlm, ScriptedToolCall, ScriptedTurn

_POLICY_TURN = ScriptedTurn(
    tool_calls=(ScriptedToolCall("query_policy", {"query": "water damage coverage"}),),
    final_text="Burst pipes are covered under Home Water Damage Coverage.",
)


def _sse_events(payload: str) -> list[dict[str, Any]]:
    """Parse an SSE body into the decoded JSON payload of every ``data:`` frame.

    Args:
        payload: The raw ``text/event-stream`` response body.

    Returns:
        list[dict[str, Any]]: One decoded payload per frame, in order.
    """
    events: list[dict[str, Any]] = []
    for raw_block in payload.split("\n\n"):
        block = raw_block.strip()
        if not block:
            continue
        data = block.removeprefix("data:").strip()
        events.append(json.loads(data))
    return events


def _payloads(frames: list[str]) -> list[dict[str, Any]]:
    """Decode SSE frames into their JSON objects."""
    return [json.loads(frame.removeprefix("data:").strip()) for frame in frames]


_PRISTINE_AGENT_MODEL: dict[str, Any] = {"model": None}
"""The agent's real model, captured once on first use.

The agent is a process-wide singleton that several suites swap for a scripted
double. Restoring to *this* value rather than to whatever is installed when a
suite happens to run is what stops a leaked double from becoming the next
suite's baseline. Held in a dict so the lazy capture needs no ``global``.
"""


def _pristine_agent_model(agent_module: Any) -> Any:
    """Return the agent's real model, capturing it on first use."""
    if _PRISTINE_AGENT_MODEL["model"] is None:
        _PRISTINE_AGENT_MODEL["model"] = agent_module.omnicare_agent.model
    return _PRISTINE_AGENT_MODEL["model"]


@pytest.fixture
def scripted_llm() -> Iterator[Callable[[ScriptedTurn], ScriptedLlm]]:
    """Install a scripted model into the real agent and restore the original.

    Yields:
        Callable[[ScriptedTurn], ScriptedLlm]: Installer for one scripted turn.
    """
    from app.agent import agent as agent_module

    agent_module.get_runner()  # the agent is built lazily on first use
    original = _pristine_agent_model(agent_module)

    def _install(turn: ScriptedTurn) -> ScriptedLlm:
        double = ScriptedLlm(turn=turn)
        agent_module.omnicare_agent.model = double
        return double

    yield _install

    # Restore to the pristine model, not to whatever happens to be installed
    # right now. Capturing the "original" per test chains: if an earlier test
    # leaves a scripted double behind, this test would capture that double as
    # its baseline and every later test in the process would silently run
    # against the wrong script, which shows up as a tool trace that does not
    # match the turn under test.
    agent_module.omnicare_agent.model = original


async def _collect_stream(turn: ScriptedTurn) -> tuple[list[str], _StreamResult]:
    """Run one scripted turn through ``run_agent_stream``.

    Args:
        turn: The scripted model behaviour for this turn.

    Returns:
        tuple[list[str], _StreamResult]: The SSE frames yielded by the agent
        and the result container they were collected into.
    """
    double = ScriptedLlm(turn=turn)
    with patch("app.agent.agent.omnicare_agent.model", double):
        result = _StreamResult()
        frames = [
            frame
            async for frame in run_agent_stream(
                user_id=str(uuid.uuid4()),
                message="Is a burst pipe covered?",
                result=result,
            )
        ]
    return frames, result


# --- The event envelope ---------------------------------------------------


def test_malformed_event_is_replaced_by_a_typed_error_frame():
    """A payload that is not JSON must never be forwarded verbatim.

    Forwarding it hands the client a ``JSON.parse`` failure it cannot act on
    while the stream still claims to have completed.
    """
    from app.api.v1.chat_stream import _transform_adk_event

    frame = _transform_adk_event("not-valid-json")

    assert frame is not None and frame.startswith("data: ")
    payload = json.loads(frame.removeprefix("data:").strip())
    assert payload == {
        "type": "error",
        "message": "Malformed event received",
        "code": "stream_failed",
    }
    assert StreamError.model_validate(payload).code == "stream_failed"


def test_text_delta_frame_shape_is_unchanged():
    """``text_delta`` keeps the exact envelope existing consumers parse."""
    from app.api.v1.chat_stream import _transform_adk_event

    frame = _transform_adk_event(
        'data: {"type": "content", "content": {"parts": [{"text": "Hello"}]}}\n\n'
    )

    assert frame is not None
    assert json.loads(frame.removeprefix("data:").strip()) == {
        "type": "text_delta",
        "content": "Hello",
    }


def test_final_response_frame_carries_status_ok():
    """The terminal frame reports ``status: "ok"`` for a turn that finished."""
    from app.api.v1.chat_stream import _transform_adk_event

    frame = _transform_adk_event(json.dumps({"is_final_response": True}), sources=["clause"])

    assert frame is not None
    payload = json.loads(frame.removeprefix("data:").strip())
    # The transform is called with no persisted ids, so the additive id fields
    # serialize as null: a client rating this message must fall back to the id
    # the endpoint's own persistence step reported, which this frame omits.
    assert payload == {
        "type": "response_complete",
        "sources": ["clause"],
        "status": "ok",
        "message_id": None,
        "user_message_id": None,
    }
    assert StreamResponseComplete.model_validate(payload).status == "ok"


def test_tool_payloads_validate_against_their_schemas():
    """The typed schemas accept exactly what the stream emits."""
    assert (
        StreamToolCall.model_validate(
            {"type": "tool_call", "tool": "query_policy", "arguments": {"query": "coverage"}}
        ).tool
        == "query_policy"
    )
    assert StreamToolResult.model_validate(
        {"type": "tool_result", "tool": "query_policy", "result": {"chunks_found": 0}}
    ).result == {"chunks_found": 0}
    assert (
        StreamToolError.model_validate(
            {"type": "tool_error", "tool": "get_claim_status", "error": "claim unavailable"}
        ).error
        == "claim unavailable"
    )


def test_non_object_tool_payloads_are_wrapped():
    """A tool returning a scalar still produces a well-formed envelope."""
    assert _as_object({"a": 1}) == {"a": 1}
    assert _as_object("scalar") == {"value": "scalar"}


# --- Tool events streamed from the agent ----------------------------------


@pytest.mark.asyncio
async def test_streamed_tool_events_match_the_persisted_tool_calls(scripted_llm):
    """The streamed tool trace and the persisted one cannot drift.

    Both are built from the same collection, so the live UI and the
    conversation history describe the same tool activity.
    """
    scripted_llm(_POLICY_TURN)
    frames, result = await _collect_stream(_POLICY_TURN)

    payloads = _payloads(frames)
    tool_calls = [event for event in payloads if event.get("type") == "tool_call"]
    tool_results = [event for event in payloads if event.get("type") == "tool_result"]

    assert tool_calls == [
        {
            "type": "tool_call",
            "tool": "query_policy",
            "arguments": {"query": "water damage coverage"},
        }
    ]
    assert StreamToolCall.model_validate(tool_calls[0]).tool == "query_policy"

    assert len(tool_results) == 1
    assert tool_results[0]["type"] == "tool_result"
    assert tool_results[0]["tool"] == "query_policy"
    assert tool_results[0]["result"] == result.tool_calls[0]["result"]
    assert StreamToolResult.model_validate(tool_results[0]).tool == "query_policy"

    assert result.tool_calls[0]["name"] == tool_calls[0]["tool"]
    assert result.tool_calls[0]["arguments"] == tool_calls[0]["arguments"]


@pytest.mark.asyncio
async def test_tool_call_is_streamed_before_the_text_delta():
    """Tool activity has to reach the client before the answer it produced."""
    frames, _ = await _collect_stream(_POLICY_TURN)

    payloads = _payloads(frames)
    types = [event.get("type") for event in payloads]

    assert "tool_call" in types
    assert "tool_result" in types
    # The final ADK text frame carries no ``type``; it is the last payload.
    assert types.index("tool_call") < len(payloads) - 1
    assert types.index("tool_result") < len(payloads) - 1


@pytest.mark.asyncio
async def test_tool_error_is_streamed_when_the_tool_reports_a_failure():
    """A tool that fails reaches the model as an error payload, not a result.

    Streamed as its own event it stays visible in the UI instead of being
    indistinguishable from a successful return.
    """

    class _ToolFailureEvent:
        """A minimal stand-in for an ADK event carrying a failed tool call."""

        def get_function_calls(self):
            return []

        def get_function_responses(self):
            response = MagicMock()
            response.name = "get_claim_status"
            response.response = {"error": "claim lookup unavailable"}
            return [response]

        def is_final_response(self):
            return False

        content = None

        def model_dump_json(self, **_kwargs):
            return json.dumps({"reason": "tool failure"})

    async def _run_async(**_kwargs):
        yield _ToolFailureEvent()

    with (
        patch("app.agent.agent.get_runner") as mock_get_runner,
        patch("app.agent.agent.session_service") as mock_session_service,
    ):
        mock_get_runner.return_value = MagicMock(run_async=_run_async)
        mock_session_service.create_session = AsyncMock()
        mock_session_service.delete_session = AsyncMock()

        frames = [
            frame
            async for frame in run_agent_stream(
                user_id=str(uuid.uuid4()),
                message="what is happening with my claim?",
            )
        ]

    payloads = _payloads(frames)
    assert [event for event in payloads if event.get("type") == "tool_result"] == []
    assert [event for event in payloads if event.get("type") == "tool_error"] == [
        {
            "type": "tool_error",
            "tool": "get_claim_status",
            "error": "claim lookup unavailable",
        }
    ]


# --- HTTP surface ---------------------------------------------------------


def test_stream_endpoint_emits_tool_events_and_a_typed_terminal_frame(
    test_client, mock_current_user, scripted_llm
):
    """Every frame on the wire is valid JSON and the stream ends with a status."""
    scripted_llm(_POLICY_TURN)

    response = test_client.post(
        "/api/v1/chat/stream",
        json={"message": "Is a burst pipe covered?"},
        headers=mock_current_user,
    )
    assert response.status_code == 200

    events = _sse_events(response.text)
    assert events, "the stream produced no events"
    types = [event.get("type") for event in events]

    assert "tool_call" in types
    assert "tool_result" in types
    assert "text_delta" in types
    assert types[-1] == "response_complete"
    assert types.index("tool_result") < types.index("text_delta")

    terminal = events[-1]
    assert terminal["status"] == "ok"
    assert isinstance(terminal["sources"], list)
    assert StreamResponseComplete.model_validate(terminal).status == "ok"


def test_stream_endpoint_emits_json_error_frame_then_failed_terminal(
    test_client, mock_current_user
):
    """A failed turn emits an ``error`` frame and still ends the stream.

    The ``error`` frame must be parseable JSON and be followed immediately by
    the terminal ``response_complete`` with ``status == "error"``, so the
    client can render the failure instead of a silently truncated answer.
    """

    async def failing_run(user_id, message, result):
        result.session_id = "session-contract-error"
        yield 'data: {"type": "text_delta", "content": "Partial answer"}\n\n'
        raise RuntimeError("upstream model blew up in a very specific way")

    with patch("app.api.v1.chat_stream.run_agent_stream") as mock_run_agent_stream:
        mock_run_agent_stream.side_effect = failing_run

        response = test_client.post(
            "/api/v1/chat/stream",
            json={"message": "trigger a failure"},
            headers=mock_current_user,
        )

    assert response.status_code == 200

    events = _sse_events(response.text)
    error_events = [event for event in events if event.get("type") == "error"]
    assert len(error_events) == 1

    error_event = error_events[0]
    assert error_event["code"] == "stream_failed"
    assert error_event["message"] == "Streaming failed"
    assert StreamError.model_validate(error_event).code == "stream_failed"
    # Internal detail is logged server-side only, never streamed.
    assert "upstream model blew up" not in response.text

    assert events[-1]["type"] == "response_complete"
    assert events[-1]["status"] == "error"
    assert events[-1]["sources"] == []
    assert events.index(error_event) == len(events) - 2


def test_stream_endpoint_value_error_still_terminates_with_a_failed_status(
    test_client, mock_current_user
):
    """The ValueError path fails the turn the same way any other error does."""

    async def failing_run(user_id, message, result):
        result.session_id = "session-contract-value-error"
        raise ValueError("bad payload shape")
        yield  # pragma: no cover - unreachable, keeps this an async generator

    with patch("app.api.v1.chat_stream.run_agent_stream") as mock_run_agent_stream:
        mock_run_agent_stream.side_effect = failing_run

        response = test_client.post(
            "/api/v1/chat/stream",
            json={"message": "trigger a failure"},
            headers=mock_current_user,
        )

    assert response.status_code == 200

    events = _sse_events(response.text)
    assert events[-1]["type"] == "response_complete"
    assert events[-1]["status"] == "error"
    assert any(event.get("type") == "error" for event in events)


def test_stream_endpoint_malformed_event_frame_is_valid_json(test_client, mock_current_user):
    """A frame the endpoint cannot parse is replaced, not forwarded verbatim."""

    async def run(*args, **kwargs):
        yield "data: not-json-at-all\n\n"

    with patch("app.api.v1.chat_stream.run_agent_stream") as mock_run_agent_stream:
        mock_run_agent_stream.return_value = run()

        response = test_client.post(
            "/api/v1/chat/stream",
            json={"message": "malformed"},
            headers=mock_current_user,
        )

    assert response.status_code == 200
    events = _sse_events(response.text)
    assert events[0]["type"] == "error"
    assert events[0]["code"] == "stream_failed"
    assert events[-1]["type"] == "response_complete"


def test_stream_endpoint_malformed_frame_marks_the_terminal_event_failed(
    test_client, mock_current_user
):
    """A rejected frame fails the turn, so ``status`` never calls it complete.

    The malformed payload is reported by ``_transform_adk_event``, which only
    returns the frame, so the generator has to read the failure back out of
    it. Without that the client would still receive ``status: "ok"`` after an
    event the stream refused to render.
    """

    async def run(*args, **kwargs):
        yield "data: not-json-at-all\n\n"

    with patch("app.api.v1.chat_stream.run_agent_stream") as mock_run_agent_stream:
        mock_run_agent_stream.return_value = run()

        response = test_client.post(
            "/api/v1/chat/stream",
            json={"message": "malformed"},
            headers=mock_current_user,
        )

    assert response.status_code == 200
    events = _sse_events(response.text)
    assert [event["type"] for event in events] == ["error", "response_complete"]
    assert events[-1]["status"] == "error"


def test_transform_adk_event_serializes_non_string_citations():
    """A citation the agent collected must not take the stream down.

    The terminal frame is the last thing a client sees, so a shape the model
    refuses would replace the answer with a validation error.
    """
    from app.api.v1.chat_stream import _transform_adk_event

    citation = {"section": "Section 1", "source": "policy.md"}
    frame = _transform_adk_event(json.dumps({"is_final_response": True}), sources=[citation])

    assert frame is not None
    payload = json.loads(frame.removeprefix("data:").strip())
    assert payload["type"] == "response_complete"
    assert payload["status"] == "ok"
    assert payload["sources"] == [citation]
    assert StreamResponseComplete.model_validate(payload).sources == [citation]


def test_every_stream_event_serializes_as_valid_json():
    """Each frame the contract defines decodes without a client parse error."""
    events = (
        StreamTextDelta(content="partial answer"),
        StreamToolCall(tool="query_policy", arguments={"query": "coverage"}),
        StreamToolResult(tool="query_policy", result={"chunks_found": 2}),
        StreamToolError(tool="get_claim_status", error="claim unavailable"),
        StreamError(message="Streaming failed", code="stream_failed"),
        StreamResponseComplete(sources=["Section 1 (policy.md)"], status="ok"),
        StreamResponseComplete(status="error"),
    )

    payloads = [json.loads(sse_frame(event).removeprefix("data:").strip()) for event in events]

    assert [payload["type"] for payload in payloads] == [
        "text_delta",
        "tool_call",
        "tool_result",
        "tool_error",
        "error",
        "response_complete",
        "response_complete",
    ]
    assert all(isinstance(payload, dict) for payload in payloads)
