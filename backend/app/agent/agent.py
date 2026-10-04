"""
Core ADK Agent definition and runner for the OmniCare Assistant.

Configures the Google ADK LlmAgent with LiteLLM for model routing,
registers all tools, and provides the async run_agent() and
run_agent_stream() functions for the FastAPI endpoints to call.

The agent is intentionally framework-agnostic: it does not import or
touch the database directly. Persistence is handled by the API layer via
``app.agent.conversation_store``.
"""

from __future__ import annotations

import logging
import os
import uuid
from collections.abc import AsyncGenerator
from typing import Any

from google.adk.agents import LlmAgent
from google.adk.agents.run_config import RunConfig, StreamingMode
from google.adk.models.lite_llm import LiteLlm
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

from app.agent.context import current_user_id
from app.agent.prompts import SYSTEM_INSTRUCTION
from app.agent.registry import ToolRegistry, build_default_registry
from app.config import Settings, settings

logger = logging.getLogger(__name__)


# --- Configuration -------------------------------------------------------


def configure_llm() -> None:
    """
    Configure LiteLLM environment variables from centralized settings.

    This is called explicitly during application startup (lifespan)
    rather than at module import time to avoid side-effects and ensure
    configuration is applied in a controlled, testable manner.
    """
    if settings.openai_api_key:
        os.environ["OPENAI_API_KEY"] = settings.openai_api_key
        logger.info("OpenAI API key configured for LiteLLM routing")
    if settings.openai_api_base:
        os.environ["OPENAI_API_BASE"] = settings.openai_api_base


# --- Agent & Runtime -----------------------------------------------------

# The ADK agent, session service, and runner are built lazily by
# get_runner() on first use rather than at import time, so importing
# this module has no side effects. The module-level attributes stay
# patchable: tests replace app.agent.agent.runner /
# app.agent.agent.session_service exactly as they did with the
# import-time construction.
omnicare_agent: LlmAgent | None = None
session_service: InMemorySessionService | None = None
runner: Runner | None = None


def create_agent(tool_registry: ToolRegistry, settings: Settings) -> LlmAgent:
    """Create the ADK agent from a tool registry.

    Args:
        tool_registry: Registry supplying the agent's tools.
        settings: Application settings; ``llm_model`` selects the
            LiteLLM model.

    Returns:
        A configured :class:`LlmAgent` with the registry's tools.
    """
    return LlmAgent(
        name="omnicare_assistant",
        model=LiteLlm(model=settings.llm_model),
        instruction=SYSTEM_INSTRUCTION,
        description=(
            "OmniCare Financial customer service assistant that handles "
            "policy questions, claim lookups, and claim submissions."
        ),
        tools=tool_registry.build_tool_list(),
    )


def get_runner() -> Runner:
    """Lazily build and cache the ADK runner.

    The agent, session service, and runner are created on the first
    call and cached in the module-level ``omnicare_agent``,
    ``session_service``, and ``runner`` attributes. A patched
    ``session_service`` or ``runner`` (see the agent tests) is
    preserved: only ``None`` attributes are built.

    Returns:
        The cached :class:`Runner` for the OmniCare agent.
    """
    global omnicare_agent, session_service, runner
    if session_service is None:
        session_service = InMemorySessionService()
    if runner is None:
        omnicare_agent = create_agent(build_default_registry(), settings)
        runner = Runner(
            agent=omnicare_agent,
            app_name="omnicare_financial",
            session_service=session_service,
        )
    return runner


def _session_service() -> InMemorySessionService:
    """Return the session service, building it on first use."""
    global session_service
    if session_service is None:
        session_service = InMemorySessionService()
    return session_service


# Track active sessions per user, bounded to MAX_SESSIONS to prevent
# unbounded memory growth in long-running processes. When the limit
# is reached, the oldest session is evicted (FIFO).
_MAX_SESSIONS = 500
_user_sessions: dict[str, str] = {}


class _StreamResult:
    """Mutable container for collecting streaming agent results.

    Passed into ``run_agent_stream`` so the caller can access the assembled
    response after the generator is exhausted.

    Attributes:
        session_id: The ADK session identifier for this conversation.
        final_text_parts: Accumulated text parts from the final assistant response.
        sources: Citation sources collected from tool results (policy sections,
            claim references).
        tool_calls: Trace of tools invoked with arguments and execution results.
    """

    def __init__(self) -> None:
        self.session_id: str = ""
        self.final_text_parts: list[str] = []
        self.sources: list[str] = []
        self.tool_calls: list[dict] = []


# --- Session Management --------------------------------------------------


async def _ensure_session(user_id: str) -> str:
    """Get or create a session for a given user, evicting the oldest if at capacity.

    Uses a FIFO eviction strategy when the maximum session count is reached.
    Evicted sessions are also removed from the ADK InMemorySessionService to
    prevent unbounded memory growth over long-running server uptime.

    Args:
        user_id: The authenticated user's unique identifier.

    Returns:
        str: The ADK session identifier associated with this user.
    """
    if user_id not in _user_sessions:
        # Evict oldest entry when at capacity (FIFO -- dict preserves insertion order).
        if len(_user_sessions) >= _MAX_SESSIONS:
            oldest_user = next(iter(_user_sessions))
            evicted_session_id = _user_sessions.pop(oldest_user)
            logger.info(
                "Session capacity reached (%d). Evicted session '%s' for user '%s'.",
                _MAX_SESSIONS,
                evicted_session_id,
                oldest_user,
            )
            # Clean up the session object from the InMemorySessionService to
            # prevent unbounded memory growth over long-running server uptime.
            try:
                await _session_service().delete_session(
                    app_name="omnicare_financial",
                    session_id=evicted_session_id,
                )
                logger.debug(
                    "Deleted evicted session '%s' from session service.",
                    evicted_session_id,
                )
            except Exception:
                logger.warning(
                    "Failed to delete evicted session '%s' from session service.",
                    evicted_session_id,
                    exc_info=True,
                )

        session_id = f"session_{uuid.uuid4().hex[:12]}"
        await _session_service().create_session(
            app_name="omnicare_financial",
            user_id=user_id,
            session_id=session_id,
        )
        _user_sessions[user_id] = session_id
        logger.debug("Created session '%s' for user '%s'.", session_id, user_id)

    return _user_sessions[user_id]


async def reset_user_session(user_id: str) -> None:
    """Clear the session for a user (e.g., for 'New Chat')."""
    session_id = _user_sessions.pop(user_id, None)
    if session_id:
        try:
            await _session_service().delete_session(
                app_name="omnicare_financial",
                session_id=session_id,
            )
            logger.debug("Deleted session '%s' for user '%s'.", session_id, user_id)
        except Exception:
            logger.warning(
                "Failed to delete session '%s' for user '%s'.",
                session_id,
                user_id,
                exc_info=True,
            )


# --- Agent Execution -----------------------------------------------------


async def run_agent(user_id: str, message: str) -> dict[str, Any]:
    """
    Run the OmniCare agent with a user message and collect the response.

    This is the synchronous entry point used by the non-streaming chat
    endpoint. It runs the agent to completion, collects all tool calls,
    sources, and the final text response, and returns them as a dict.

    Args:
        user_id: Unique user identifier for session management.
        message: The user's chat message.

    Returns:
        Dict with keys:
            - response (str): The agent's synthesized text response.
            - sources (list[str]): Policy section citations and claim references.
            - tool_calls (list[dict]): Trace of tools invoked with arguments
              and execution results.
            - session_id (str): The ADK session identifier for this conversation.
    """
    session_id = await _ensure_session(user_id)

    # SECURE: Bind the authenticated user_id to the async context so tools
    # can access it safely without it being passed through user input.
    current_user_id.set(uuid.UUID(user_id))

    # Wrap user message in ADK Content format.
    user_content = types.Content(
        role="user",
        parts=[types.Part(text=message)],
    )

    # Collect response data.
    final_text_parts: list[str] = []
    sources: list[str] = []
    tool_calls: list[dict] = []

    # Resolve the lazily built (and patchable) runner.
    runner = get_runner()

    # Run the agent and iterate through events. The ADK runner yields a stream
    # of events representing the agent's reasoning, tool invocations, and
    # final response. We collect all relevant data into local lists so the
    # full response can be returned as a single dict.
    async for event in runner.run_async(
        user_id=user_id,
        session_id=session_id,
        new_message=user_content,
    ):
        # Collect function call events (tool invocations initiated by the agent).
        function_calls = event.get_function_calls()
        if function_calls:
            for fc in function_calls:
                tool_call_info = {
                    "name": fc.name if hasattr(fc, "name") else str(fc),
                    "arguments": fc.args if hasattr(fc, "args") else {},
                }
                tool_calls.append(tool_call_info)

        # Collect function response events (results returned by tool execution).
        function_responses = event.get_function_responses()
        if function_responses:
            for fr in function_responses:
                result = fr.response if hasattr(fr, "response") else {}
                # Extract sources from RAG tool results.
                if isinstance(result, dict) and "sources" in result:
                    for src in result["sources"]:
                        if isinstance(src, dict):
                            section = src.get("section", "")
                            source_file = src.get("source", "")
                            sources.append(f"{section} ({source_file})")
                        elif isinstance(src, str):
                            sources.append(src)

                # Extract claim citations from claim tool results.
                if isinstance(result, dict) and "citation" in result:
                    sources.append(result["citation"])

                # Attach result to the most recent matching tool call so the
                # frontend can correlate tool invocations with their outcomes.
                for tc in reversed(tool_calls):
                    if tc.get("name") == (fr.name if hasattr(fr, "name") else ""):
                        tc["result"] = result
                        break

        # Collect final text response parts from the assistant's synthesis.
        if event.is_final_response() and event.content and event.content.parts:
            for part in event.content.parts:
                if hasattr(part, "text") and part.text:
                    final_text_parts.append(part.text)

    response_text = (
        "\n".join(final_text_parts)
        if final_text_parts
        else "I'm sorry, I couldn't generate a response. Please try again."
    )

    return {
        "response": response_text,
        "sources": sources,
        "tool_calls": tool_calls,
        "session_id": session_id,
    }


async def run_agent_stream(
    user_id: str,
    message: str,
    result: _StreamResult | None = None,
) -> AsyncGenerator[str, None]:
    """
    Run the OmniCare agent and stream results as Server-Sent Events (SSE).

    This is the streaming entry point used by the SSE chat endpoint. It
    runs the agent with ``StreamingMode.SSE`` and yields each ADK event
    as a formatted SSE data chunk. The caller is responsible for forwarding
    these chunks to the client.

    Args:
        user_id: Unique user identifier for session management.
        message: The user's chat message.
        result: Optional ``_StreamResult`` instance to populate with the
            assembled response after streaming completes. The caller must
            consume the full generator before accessing this object.

    Yields:
        str: SSE-formatted event strings (``data: <JSON>\\n\\n``).

    Note:
        The final assembled response text is computed and stored in ``result``
        (if provided) after the stream completes. The backend endpoint or
        frontend consumer must extract the final response from the final
        ``is_final_response`` event.
    """
    session_id = await _ensure_session(user_id)

    # SECURE: Bind the authenticated user_id to the async context so tools
    # can access it safely without it being passed through user input.
    current_user_id.set(uuid.UUID(user_id))

    # Wrap user message in ADK Content format.
    user_content = types.Content(
        role="user",
        parts=[types.Part(text=message)],
    )

    final_text_parts: list[str] = []
    sources: list[str] = []
    tool_calls: list[dict] = []

    # Resolve the lazily built (and patchable) runner.
    runner = get_runner()

    async for event in runner.run_async(
        user_id=user_id,
        session_id=session_id,
        new_message=user_content,
        run_config=RunConfig(streaming_mode=StreamingMode.SSE),
    ):
        # Collect final text response parts from the assistant's synthesis.
        if event.is_final_response() and event.content and event.content.parts:
            for part in event.content.parts:
                if hasattr(part, "text") and part.text:
                    final_text_parts.append(part.text)

        # Collect function call events (tool invocations initiated by the agent).
        function_calls = event.get_function_calls()
        if function_calls:
            for fc in function_calls:
                tool_call_info = {
                    "name": fc.name if hasattr(fc, "name") else str(fc),
                    "arguments": fc.args if hasattr(fc, "args") else {},
                }
                tool_calls.append(tool_call_info)

        # Collect function response events (results returned by tool execution).
        function_responses = event.get_function_responses()
        if function_responses:
            for fr in function_responses:
                result_data = fr.response if hasattr(fr, "response") else {}
                if isinstance(result_data, dict) and "sources" in result_data:
                    for src in result_data["sources"]:
                        if isinstance(src, dict):
                            section = src.get("section", "")
                            source_file = src.get("source", "")
                            sources.append(f"{section} ({source_file})")
                        elif isinstance(src, str):
                            sources.append(src)

                if isinstance(result_data, dict) and "citation" in result_data:
                    sources.append(result_data["citation"])

                for tc in reversed(tool_calls):
                    if tc.get("name") == (fr.name if hasattr(fr, "name") else ""):
                        tc["result"] = result_data
                        break

        # Yield ADK's built-in formatted SSE JSON string to the client.
        sse_event = event.model_dump_json(exclude_none=True, by_alias=True)
        yield f"data: {sse_event}\n\n"

    # Populate the result container if provided.
    if result is not None:
        result.session_id = session_id
        result.final_text_parts = final_text_parts
        result.sources = sources
        result.tool_calls = tool_calls
