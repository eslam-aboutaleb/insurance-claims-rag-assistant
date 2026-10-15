"""
Streaming chat endpoint.

POST /api/v1/chat/stream - Stream ADK SSE events for real-time chat responses.

This endpoint returns a ``StreamingResponse`` that emits Server-Sent Events
(SSE) formatted strings produced by the Google ADK agent runner. The client
is responsible for parsing the SSE stream and rendering partial responses
as they arrive.

SSE event contract
------------------

Every frame is a single ``data:`` line holding a JSON object with a ``type``
field, built from the stream models in ``app.schemas.models`` rather than
interpolated strings:

  - ``text_delta``        ``{"type": "text_delta", "content": "..."}``
    One fragment of the assistant's answer.
  - ``tool_call``         ``{"type": "tool_call", "tool": "...", "arguments": {...}}``
    The agent invoked a tool.
  - ``tool_result``       ``{"type": "tool_result", "tool": "...", "result": {...}}``
    The tool returned a value.
  - ``tool_error``        ``{"type": "tool_error", "tool": "...", "error": "..."}``
    The tool reported its own failure.
  - ``error``             ``{"type": "error", "message": "...", "code": "stream_failed"}``
    The model stream failed. It is never the terminal frame.

Every stream terminates with ``response_complete``, a failed one included,
because the client relies on that frame to stop waiting and to render the
sidebar entry:

  - ``response_complete`` ``{"type": "response_complete", "sources": [...],
    "status": "ok", "message_id": "...", "user_message_id": "..."}``

``status`` is ``"ok"`` for a turn the agent finished and ``"error"`` for one
that failed, which is how a client tells a truncated answer from a complete
one: the ``error`` frame says why the stream stopped and the
``response_complete`` that always follows says whether it stopped.
``sources`` lists the citations collected while the turn ran and is empty on
the error path.

``message_id`` and ``user_message_id`` are the backend-assigned ids of the
persisted assistant answer and user message. The turn is persisted *before*
this frame is emitted, so the terminal frame can carry the ids the feedback
endpoint will authorise against: the client adopts ``message_id`` for the answer
it just streamed instead of rating a locally-generated id that matches no row.
Both are ``null`` when the turn could not be persisted.

The two travel together by construction: an ``error`` frame is always
followed by ``response_complete`` carrying ``"status": "error"``, and
``"status": "ok"`` is sent exactly when no ``error`` frame was sent.
"""

import json
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import StreamingResponse

from app.agent.agent import _StreamResult, run_agent_stream, sse_frame
from app.agent.conversation_store import SavedMessageIds, save_conversation_turn
from app.security.auth import get_current_user
from app.core.rate_limiter import limiter
from app.schemas.models import (
    ChatRequest,
    StreamError,
    StreamResponseComplete,
    StreamTextDelta,
)

logger = logging.getLogger(__name__)

_STREAM_OK = "ok"
_STREAM_FAILED = "error"


def _reports_failure(frame: str) -> bool:
    """Report whether an already-mapped frame is the ``error`` event.

    ``_transform_adk_event`` is a pure mapping, so a payload it rejects is
    reported to the client through the ``error`` frame it returns rather than
    by raising. Reading the frame back is what keeps the terminal ``status``
    honest about why the turn stopped.

    Args:
        frame: The SSE frame produced by ``_transform_adk_event``.

    Returns:
        bool: True when the frame carries the ``error`` discriminator.
    """
    try:
        payload = json.loads(frame.removeprefix("data: ").strip())
    except json.JSONDecodeError:
        return False
    return isinstance(payload, dict) and payload.get("type") == "error"


def _transform_adk_event(event_json: str, sources: list[Any] | None = None) -> str | None:
    """Transform raw ADK event into frontend-friendly SSE event.

    Converts ADK streaming events into a stable frontend contract so the
    client does not need to understand ADK internals. Tool activity is
    already emitted as typed events by ``run_agent_stream``; this only maps
    the ADK text and final-response envelopes.

    Every branch returns a frame whose payload is JSON: events the contract
    maps are re-serialized from a model, and events it only forwards are
    round-tripped through ``json`` so the client never receives a payload
    ``JSON.parse`` would reject.

    Args:
        event_json: Raw JSON string of an ADK event, possibly prefixed
            with ``data: `` from the SSE formatter in ``run_agent_stream``.
        sources: Accumulated source citations collected during streaming,
            or ``None`` if not yet available.

    Returns:
        str | None: A formatted SSE ``data:`` line, or ``None`` if the
            event should be dropped.
    """
    raw = event_json.strip()
    if raw.startswith("data: "):
        raw = raw[6:].strip()

    try:
        event = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        # Forwarding a payload we cannot parse would hand the client a parse
        # error it cannot act on. Emitting a typed error frame instead keeps
        # the failure visible while the stream still terminates normally.
        return sse_frame(StreamError(message="Malformed event received", code="stream_failed"))

    if not isinstance(event, dict):
        return sse_frame(StreamError(message="Malformed event received", code="stream_failed"))

    event_type = event.get("type", "")

    # ``is_final_response`` is a method on ADK's Event, so it is not expected in
    # the serialized payload ``run_agent_stream`` forwards -- but it is cheap to
    # honour and the tests pin the behaviour, so a payload that does carry it
    # still produces exactly one terminal frame here. Should ADK ever start
    # serializing it, this and the ``finally`` block would both emit
    # ``response_complete``; the guard against that belongs wherever the frames
    # are counted, not in a branch that silently changes shape.
    if event.get("is_final_response"):
        return sse_frame(
            StreamResponseComplete(sources=sources or [], status=_STREAM_OK),
        )

    # ADK reports text inside ``content.parts`` and may omit ``content``
    # entirely. Anything that is not an object there has no parts to read, so
    # the event is forwarded instead of being turned into a stream failure.
    content = event.get("content")
    parts = content.get("parts", []) if isinstance(content, dict) else []
    if event_type == "text_delta" or parts:
        text_deltas = [p.get("text", "") for p in parts if isinstance(p, dict) and p.get("text")]
        if text_deltas:
            return sse_frame(StreamTextDelta(content="".join(text_deltas)))

    return f"data: {json.dumps(event)}\n\n"


router = APIRouter()


@router.post(
    "/chat/stream",
    summary="Stream Chat Interaction",
    description="Stream ADK SSE events for real-time chat responses.",
    responses={
        status.HTTP_200_OK: {
            "description": "SSE stream of agent response events.",
        },
        status.HTTP_422_UNPROCESSABLE_ENTITY: {
            "description": "Validation error: missing or invalid message format.",
        },
        status.HTTP_500_INTERNAL_SERVER_ERROR: {
            "description": "Internal server error during streaming.",
        },
    },
)
@limiter.limit("20/minute")
async def chat_stream(
    payload: ChatRequest,
    request: Request,
    current_user_id: str = Depends(get_current_user),
) -> StreamingResponse:
    """Stream a chat interaction response as Server-Sent Events.

    Validates the request payload, then delegates to the ADK agent runner
    in streaming mode. Each ADK event is mapped onto a typed event envelope
    and yielded as an SSE ``data:`` chunk. After the stream completes, the
    conversation turn is persisted to the database before the stream closes.

    Args:
        payload: Validated chat request containing the user message.
        request: The incoming FastAPI request (used for rate limiting).
        current_user_id: The authenticated user's UUID (injected by dependency).

    Returns:
        StreamingResponse: An SSE stream with media type
            ``text/event-stream``.

    Raises:
        HTTPException: 422 if the message is empty or whitespace-only.
        HTTPException: 500 if the streaming generator raises an unexpected error.
    """
    message = payload.message

    if not message.strip():
        raise HTTPException(status_code=422, detail="Message cannot be empty")

    async def event_generator():
        stream_result = _StreamResult()
        stream_failed = False
        try:
            async for chunk in run_agent_stream(
                user_id=current_user_id,
                message=message,
                result=stream_result,
            ):
                if await request.is_disconnected():
                    logger.info("Client disconnected during streaming")
                    return

                transformed = _transform_adk_event(chunk, stream_result.sources)
                if transformed is not None:
                    # A mapping failure surfaces as an ``error`` frame, so the
                    # turn is flagged failed too: the terminal ``status`` must
                    # never call a broken stream complete.
                    if _reports_failure(transformed):
                        stream_failed = True
                    yield transformed
        except ValueError as err:
            # Suppress OpenTelemetry context token mismatch that can occur
            # when the ADK generator is closed across async contexts: it is a
            # teardown artifact rather than a model failure, so it is not
            # logged as an exception. The turn still stopped short, so the
            # client is told that as well.
            if "was created in a different Context" in str(err):
                logger.debug("Suppressed OpenTelemetry context mismatch during streaming: %s", err)
            else:
                logger.exception("ValueError during streaming: %s", err)
            yield sse_frame(StreamError(message="Streaming failed", code="stream_failed"))
            stream_failed = True
        except Exception as err:
            logger.exception("Error during streaming: %s", err)
            yield sse_frame(StreamError(message="Streaming failed", code="stream_failed"))
            stream_failed = True
        finally:
            # Persist the turn first, then emit response_complete, so the
            # terminal frame can carry the backend-assigned message ids. The
            # id a client rates must match the row the database wrote -- a
            # client-generated id matches nothing and the feedback endpoint
            # refuses it with a 403 -- so the ids have to exist before the
            # frame that reports them. The turn is awaited here (rather than
            # fired off with asyncio.create_task) so the stream only closes once
            # the turn is durably saved; a detached task could otherwise commit
            # after the response finished and race with the next request or a
            # test's database reset. Every failure path still reaches this block
            # (a failed stream included) and _persist_streamed_turn swallows its
            # own errors, so the terminal frame is always emitted and reports
            # null ids rather than raising.
            saved_ids = SavedMessageIds()
            if stream_result.session_id:
                saved_ids = await _persist_streamed_turn(
                    user_id=current_user_id,
                    session_id=stream_result.session_id,
                    message=message,
                    result=stream_result,
                )

            yield sse_frame(
                StreamResponseComplete(
                    sources=stream_result.sources,
                    status=_STREAM_FAILED if stream_failed else _STREAM_OK,
                    message_id=saved_ids.assistant_message_id,
                    user_message_id=saved_ids.user_message_id,
                )
            )

    return StreamingResponse(event_generator(), media_type="text/event-stream")


async def _persist_streamed_turn(
    user_id: str,
    session_id: str,
    message: str,
    result: _StreamResult,
) -> SavedMessageIds:
    """Persist a streamed conversation turn and report its message ids.

    This function runs when the SSE stream finishes, before the terminal
    frame is emitted. It catches and logs any errors without affecting the
    streaming response, so a persistence failure surfaces as an empty
    :class:`SavedMessageIds` (both ids ``None``) rather than a raised error.

    Args:
        user_id: The authenticated user's ID.
        session_id: The ADK session ID.
        message: The user's original message.
        result: The populated _StreamResult from run_agent_stream.

    Returns:
        SavedMessageIds: The ids the backend assigned to the persisted user and
            assistant messages, or an empty instance when the save failed.
    """
    try:
        response_text = "\n".join(result.final_text_parts) if result.final_text_parts else ""
        saved = await save_conversation_turn(
            user_id=user_id,
            session_id=session_id,
            message=message,
            response_text=response_text,
            sources=result.sources,
            tool_calls=result.tool_calls,
        )
    except Exception as exc:
        logger.error("Failed to persist streamed conversation turn: %s", exc, exc_info=True)
        return SavedMessageIds()

    # save_conversation_turn returns SavedMessageIds in production; coerce
    # anything else (for example a test double) to the empty instance so the
    # terminal frame can read .assistant_message_id / .user_message_id safely.
    return saved if isinstance(saved, SavedMessageIds) else SavedMessageIds()
