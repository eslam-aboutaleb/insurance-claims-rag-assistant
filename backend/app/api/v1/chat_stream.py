"""
Streaming chat endpoint.

POST /api/v1/chat/stream - Stream ADK SSE events for real-time chat responses.

This endpoint returns a ``StreamingResponse`` that emits Server-Sent Events
(SSE) formatted strings produced by the Google ADK agent runner. The client
is responsible for parsing the SSE stream and rendering partial responses
as they arrive.
"""

import json
import logging

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import StreamingResponse

from app.agent.agent import _StreamResult, run_agent_stream
from app.agent.conversation_store import save_conversation_turn
from app.auth import get_current_user
from app.rate_limiter import limiter
from app.schemas.models import ChatRequest

logger = logging.getLogger(__name__)


def _transform_adk_event(event_json: str, sources: list[str] | None = None) -> str | None:
    """Transform raw ADK event into frontend-friendly SSE event.

    Converts ADK streaming events into a stable frontend contract so the
    client does not need to understand ADK internals.

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
        return f"data: {raw}\n\n"

    event_type = event.get("type", "")

    if event.get("is_final_response"):
        sources_payload = json.dumps(sources or [])
        return f'data: {{"type": "response_complete", "sources": {sources_payload}}}\n\n'

    if event_type == "text_delta" or (
        event.get("content") and event.get("content", {}).get("parts")
    ):
        parts = event.get("content", {}).get("parts", [])
        text_deltas = [p.get("text", "") for p in parts if p.get("text")]
        if text_deltas:
            content_str = json.dumps("".join(text_deltas))
            return f'data: {{"type": "text_delta", "content": {content_str}}}\n\n'

    return f"data: {raw}\n\n"


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
    in streaming mode. Each ADK event is serialized to JSON and yielded as
    an SSE ``data:`` chunk. After the stream completes, the conversation
    turn is persisted to the database before the stream closes.

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
                    yield transformed
        except ValueError as err:
            # Suppress OpenTelemetry context token mismatch that can occur
            # when the ADK generator is closed across async contexts.
            if "was created in a different Context" in str(err):
                logger.debug("Suppressed OpenTelemetry context mismatch during streaming: %s", err)
            else:
                logger.exception("ValueError during streaming: %s", err)
                yield "data: {'error': 'Streaming failed'}\n\n"
        except Exception as err:
            logger.exception("Error during streaming: %s", err)
            yield "data: {'error': 'Streaming failed'}\n\n"
        finally:
            # Always emit response_complete and persist the conversation turn,
            # even when the LLM stream fails. This guarantees the frontend
            # receives a terminal event and the conversation appears in the
            # sidebar regardless of downstream errors. The turn is awaited
            # here (rather than fired off with asyncio.create_task) so the
            # stream only closes once the turn is durably saved -- a detached
            # task could otherwise commit after the response finished and
            # race with the next request or a test's database reset.
            sources_payload = json.dumps(stream_result.sources)
            yield (f'data: {{"type": "response_complete", "sources": {sources_payload}}}\n\n')

            if stream_result.session_id:
                await _persist_streamed_turn(
                    user_id=current_user_id,
                    session_id=stream_result.session_id,
                    message=message,
                    result=stream_result,
                )

    return StreamingResponse(event_generator(), media_type="text/event-stream")


async def _persist_streamed_turn(
    user_id: str,
    session_id: str,
    message: str,
    result: _StreamResult,
) -> None:
    """Persist a streamed conversation turn to the database.

    This function runs when the SSE stream finishes. It catches and logs
    any errors without affecting the streaming response.

    Args:
        user_id: The authenticated user's ID.
        session_id: The ADK session ID.
        message: The user's original message.
        result: The populated _StreamResult from run_agent_stream.
    """
    try:
        response_text = "\n".join(result.final_text_parts) if result.final_text_parts else ""
        await save_conversation_turn(
            user_id=user_id,
            session_id=session_id,
            message=message,
            response_text=response_text,
            sources=result.sources,
            tool_calls=result.tool_calls,
        )
    except Exception as exc:
        logger.error("Failed to persist streamed conversation turn: %s", exc, exc_info=True)
