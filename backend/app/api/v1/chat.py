"""
Chat endpoint.

POST /api/v1/chat - Routes customer inquiries through the OmniCare AI agent.

Idempotency:
  Clients MAY send an ``Idempotency-Key`` header (opaque string, max 128 chars).
  If present, the server caches the first successful response for that key and
  returns it verbatim for all subsequent requests with the same key within the
  TTL window (default 5 min). This allows frontend clients to safely retry on
  network errors without triggering duplicate LLM calls.

  If the header is omitted, a deterministic key is derived from
  ``sha256(user_id + ":" + message)`` so that byte-identical retries of the
  same message from the same user are automatically deduplicated. Users can
  still ask the same question multiple times in a conversation by varying
  their message text slightly.

  Cached responses are indicated by the ``X-Idempotent-Replayed: true`` header.
"""

import logging
from typing import Any

import litellm
from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    Header,
    HTTPException,
    Request,
    Response,
    status,
)

from app.agent.agent import reset_user_session, run_agent
from app.agent.conversation_store import save_conversation_turn
from app.auth import get_current_user
from app.config import Settings, get_settings
from app.idempotency import idempotent_endpoint
from app.rate_limiter import limiter
from app.schemas.models import ChatRequest, ChatResponse, ErrorResponse

logger = logging.getLogger(__name__)

router = APIRouter()

# Resolve 422 status constant across Starlette/FastAPI versions without deprecation warnings
STATUS_422 = getattr(status, "HTTP_422_UNPROCESSABLE_CONTENT", 422)


@router.post(
    "/chat",
    response_model=ChatResponse,
    status_code=status.HTTP_200_OK,
    summary="Process Chat Interaction",
    description=(
        "Accepts a customer message, routes it through the OmniCare agent "
        "with RAG and tools, and returns the response. "
        "Supports idempotent retries via the optional ``Idempotency-Key`` header."
    ),
    responses={
        status.HTTP_200_OK: {
            "description": (
                "Message processed successfully. Returns the agent response, "
                "policy citations, and tool calls. "
                "Replayed responses include ``X-Idempotent-Replayed: true`` header."
            ),
            "model": ChatResponse,
        },
        STATUS_422: {
            "description": "Validation error: missing or invalid message format.",
            "model": ErrorResponse,
        },
        status.HTTP_500_INTERNAL_SERVER_ERROR: {
            "description": (
                "Internal server error occurred during agent reasoning or tool execution."
            ),
            "model": ErrorResponse,
        },
    },
)
@limiter.limit("20/minute")
@idempotent_endpoint()
async def chat(  # noqa: PLR0913, PLR0917
    payload: ChatRequest,
    request: Request,
    response: Response,
    background_tasks: BackgroundTasks,
    current_user_id: str = Depends(get_current_user),
    current_settings: Settings = Depends(get_settings),
    idempotency_key: str | None = Header(
        default=None,
        alias="Idempotency-Key",
        max_length=128,
        description=(
            "Optional client-supplied idempotency key (UUID or opaque string, max 128 chars). "
            "If provided, identical requests within the TTL window return a cached response. "
            "If omitted, the request is processed normally every time."
        ),
    ),
) -> ChatResponse:
    """Process a user chat message through the OmniCare AI agent.

    Executes:
    1. Idempotency check -- return cached response if explicit Idempotency-Key was supplied.
    2. Session lookup or initialization for ``current_user_id``.
    3. Model invocation via Google ADK & LiteLLM routing.
    4. Tool execution (Policy RAG, Claim Status, Claim Preparation) as needed.
    5. Synthesis of grounded response with citations.
    6. Cache the response under the idempotency key for retry safety (if supplied).

    Returns:
        ChatResponse: Structured payload with agent answer, source citations,
        and tool call traces. Replayed (cached) responses are indicated by
        the ``X-Idempotent-Replayed: true`` header.
    """
    effective_user_id = current_user_id

    try:
        try:
            result: dict[str, Any] = await run_agent(
                user_id=effective_user_id,
                message=payload.message,
            )
        except litellm.BadRequestError as exc:
            message_text = str(exc)
            if "tool_call_id" in message_text or "tool_calls" in message_text:
                logger.warning(
                    "Detected bad tool-call history for user '%s'; "
                    "resetting session and retrying once.",
                    effective_user_id,
                )
                await reset_user_session(effective_user_id)
                result = await run_agent(
                    user_id=effective_user_id,
                    message=payload.message,
                )
            else:
                raise
    except HTTPException:
        # Re-raise explicit HTTP exceptions without double-wrapping
        raise
    except Exception as e:
        logger.exception(
            "Unhandled error during chat processing for user '%s': %s",
            current_user_id,
            e,
        )
        if await request.is_disconnected():
            logger.info("Client disconnected during chat processing for user '%s'", current_user_id)
            raise HTTPException(
                status_code=499,
                detail="Client disconnected",
            ) from None
        # Always return a generic, user-friendly error message.
        # Internal details are logged server-side only.
        error_detail = (
            "An unexpected error occurred while processing your request. Please try again later."
        )
        # NOTE: Errors are intentionally NOT cached -- the client should retry on failure.
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=error_detail,
        ) from e

    chat_response = ChatResponse(
        response=result["response"],
        sources=result.get("sources", []),
        tool_calls=result.get("tool_calls", []),
    )

    # Persist the conversation turn after the response is sent. BackgroundTasks
    # are part of the response lifecycle: they run once the response has been
    # delivered, so persistence can never race with the next request (or a
    # test's database reset) the way a detached asyncio.create_task can.
    background_tasks.add_task(
        save_conversation_turn,
        user_id=effective_user_id,
        session_id=result.get("session_id", ""),
        message=payload.message,
        response_text=result["response"],
        sources=result.get("sources", []),
        tool_calls=result.get("tool_calls", []),
    )

    return chat_response


@router.post(
    "/chat/reset",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Reset Chat Session",
    description=(
        "Clears the authenticated user's conversation session "
        "so the next message starts a fresh context."
    ),
)
@limiter.limit("10/minute")
async def reset_chat(
    request: Request,
    response: Response,
    current_user_id: str = Depends(get_current_user),
) -> Response:
    """Reset the conversation session for the authenticated user.

    This clears the ADK InMemorySessionService session so the next message
    from this user starts with a fresh conversation context.
    """
    await reset_user_session(current_user_id)
    logger.info("Reset chat session for user '%s'.", current_user_id)
    response.status_code = status.HTTP_204_NO_CONTENT
    return response
