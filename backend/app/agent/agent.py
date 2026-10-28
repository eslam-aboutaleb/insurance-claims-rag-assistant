"""
OmniCare Financial - Customer Claims Assistant

Main ADK agent definition and orchestration logic using LiteLLM.

Features:
- LiteLlm-based model routing for OpenAI-compatible endpoints
- ADK runner for agent lifecycle and session management
- Session management with reuse and automatic cleanup
- Self-contained tool integration (Policy RAG, Claim Status, Claim Preparation)
- Gemini-style function call + result response synthesis
- Idempotency handled at the route decorator level (see app.core.idempotency)

Architecture Note:
    This module is intentionally framework-agnostic regarding the web server;
    it only exports async functions consumed by the API routes.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections import OrderedDict
from collections.abc import AsyncGenerator
from typing import Any

from google.adk.agents import LlmAgent
from google.adk.agents.callback_context import CallbackContext
from google.adk.agents.run_config import RunConfig, StreamingMode
from google.adk.events import Event
from google.adk.models.llm_request import LlmRequest
from google.adk.runners import Runner
from google.adk.sessions import BaseSessionService, InMemorySessionService, Session, State
from google.adk.sessions.base_session_service import ListSessionsResponse
from google.genai import types
from sqlalchemy import (
    BigInteger,
    Column,
    DateTime,
    Float,
    MetaData,
    String,
    Table,
    delete,
    func,
    insert,
    select,
    update,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import IntegrityError

from app.agent.context import current_user_id
from app.agent.models import LlmFactory
from app.agent.prompts import SYSTEM_INSTRUCTION
from app.agent.registry import build_default_registry
from app.core.config import Settings, settings
from app.observability.tracing import trace_turn
from app.schemas.models import StreamToolCall, StreamToolError, StreamToolResult

logger = logging.getLogger(__name__)

# The App name used when identifying sessions in the ADK session store.
APP_NAME = "omnicare_financial"

# Maximum number of user sessions kept in-process. Beyond this the oldest is
# evicted (FIFO) to bound memory over long-running server uptime.
_MAX_SESSIONS = 500

# ADK's Session/Event objects only need to survive a JSON round-trip to be
# replayed in a later request on another replica.
_SESSION_MODEL_CONFIG = {"exclude_none": True}


# ── Shared session storage ────────────────────────────────────────────────────
#
# In-memory sessions live and die with the process, which is why they are not the
# default for a deployment: a restart forgets the conversation in flight, and a
# second replica cannot continue it. The database-backed service below keeps the
# same ADK contract (``BaseSessionService``) so the runner, ``_ensure_session``
# and the SSE stream are unchanged by the swap -- the choice is made when the
# service is built, from ``SESSION_STORAGE``.

_session_metadata = MetaData()

# One row per active session. ``app_name`` / ``user_id`` are part of the primary
# key because that is the tuple ADK addresses a session by, so a session id may
# be reused across applications without colliding.
_agent_sessions = Table(
    "agent_sessions",
    _session_metadata,
    Column("app_name", String(128), primary_key=True),
    Column("user_id", String(128), primary_key=True),
    Column("id", String(128), primary_key=True),
    Column("state", JSONB, nullable=False, server_default="{}"),
    Column("created_at", DateTime(timezone=True), server_default=func.now()),
    Column("updated_at", DateTime(timezone=True), server_default=func.now()),
)

# Append-only event log. ``seq`` is the ordering authority: ``Event.timestamp``
# has microsecond resolution but is assigned by the producer, so two events can
# tie, and a replica that appends out of order must not reorder the history.
_agent_session_events = Table(
    "agent_session_events",
    _session_metadata,
    Column("seq", BigInteger, primary_key=True, autoincrement=True),
    Column("app_name", String(128), nullable=False, index=True),
    Column("user_id", String(128), nullable=False),
    Column("session_id", String(128), nullable=False, index=True),
    Column("invocation_id", String(256)),
    Column("author", String(256)),
    Column("timestamp", Float, nullable=False),
    Column("event", JSONB, nullable=False),
)


class DatabaseSessionService(BaseSessionService):
    """ADK session service that keeps sessions in the application's PostgreSQL.

    ADK ships a database session service of its own, but it drives a
    *synchronous* SQLAlchemy engine and this deployment's driver is async-only,
    so it cannot be pointed at the application database. This implementation
    keeps the same contract against the application's async engine and creates
    its two tables, ``agent_sessions`` and ``agent_session_events``, on startup.
    Both are owned here rather than by Alembic, exactly as ADK's own service
    creates its tables itself, so the two can never be out of step.

    Only the fixed contract ``Runner`` needs is implemented: create, get, list,
    delete, and append. Two ADK behaviours are simplified on purpose:

    * One state blob per session instead of separate app-, user- and
      session-scoped rows: the assistant keeps no state that is meant to outlive
      its session, so splitting it would only add write traffic. Keys with the
      ``temp:`` prefix are still dropped, because ADK marks those as
      invocation-scoped.
    * An append whose in-memory session has fallen behind a concurrent replica
      is logged rather than refused. ADK's database service raises in that
      situation, which in a multi-replica deployment turns one interleaved turn
      into a failed request for the user, while the append itself is still
      correct.

    Args:
        engine: The application's async engine.

    Returns:
        None
    """

    # Only the tail of the history is replayed: a long conversation must not be
    # pushed at the model whole, and a request never needs more than the recent
    # events to answer it.
    _MAX_REPLAYED_EVENTS = 200

    def __init__(self, engine: Any) -> None:
        self._engine = engine

    async def create_tables(self) -> None:
        """Create the session tables if the database does not have them yet."""
        async with self._engine.begin() as connection:
            await connection.run_sync(_session_metadata.create_all)

    def _session_key(self, app_name: str, user_id: str, session_id: str) -> dict[str, Any]:
        return {"app_name": app_name, "user_id": user_id, "id": session_id}

    async def create_session(
        self,
        *,
        app_name: str,
        user_id: str,
        state: dict[str, Any] | None = None,
        session_id: str | None = None,
    ) -> Session:
        """Create a session and return it.

        A session id that already exists returns the stored session instead of
        failing, so two replicas resolving a user's active session at the same
        moment both converge on the same row.

        Args:
            app_name: Application the session belongs to.
            user_id: Owning user.
            state: Optional initial session state.
            session_id: Optional caller-supplied id; generated when absent.

        Returns:
            Session: The created session.
        """
        session_id = session_id or f"session_{uuid.uuid4().hex[:12]}"
        initial_state = {
            key: value
            for key, value in (state or {}).items()
            if not key.startswith(State.TEMP_PREFIX)
        }
        try:
            await self._insert_session(app_name, user_id, session_id, initial_state)
        except IntegrityError:
            # Somebody else created this row first; adopt theirs.
            logger.debug("Session '%s' already exists; adopting the stored session.", session_id)
            existing = await self.get_session(
                app_name=app_name, user_id=user_id, session_id=session_id
            )
            if existing is not None:
                return existing
        return Session(
            app_name=app_name,
            user_id=user_id,
            id=session_id,
            state=initial_state,
            last_update_time=time.time(),
        )

    async def _insert_session(
        self, app_name: str, user_id: str, session_id: str, state: dict[str, Any]
    ) -> None:
        async with self._engine.begin() as connection:
            await connection.execute(
                insert(_agent_sessions).values(
                    app_name=app_name,
                    user_id=user_id,
                    id=session_id,
                    state=state,
                )
            )

    async def get_session(
        self,
        *,
        app_name: str,
        user_id: str,
        session_id: str,
        config: Any = None,
    ) -> Session | None:
        """Load a session with its events, newest last.

        The history is capped at ``_MAX_REPLAYED_EVENTS`` unless the caller
        narrows it further: a turn needs the recent turns, not every event the
        session ever recorded, and an unbounded read would push a whole
        conversation -- tool payloads included -- at the model.

        Args:
            app_name: Application the session belongs to.
            user_id: Owning user.
            session_id: The session to load.
            config: Optional ADK ``GetRequestConfig``; ``num_recent_events``
                overrides how much history is replayed.

        Returns:
            Session | None: The session, or None when the id is unknown.
        """
        limit = getattr(config, "num_recent_events", None) or self._MAX_REPLAYED_EVENTS
        async with self._engine.connect() as connection:
            row = (
                (
                    await connection.execute(
                        select(_agent_sessions).where(
                            _agent_sessions.c.app_name == app_name,
                            _agent_sessions.c.user_id == user_id,
                            _agent_sessions.c.id == session_id,
                        )
                    )
                )
                .mappings()
                .first()
            )
            if row is None:
                return None
            event_query = (
                select(_agent_session_events.c.event)
                .where(
                    _agent_session_events.c.app_name == app_name,
                    _agent_session_events.c.user_id == user_id,
                    _agent_session_events.c.session_id == session_id,
                )
                .order_by(_agent_session_events.c.seq.desc())
                .limit(limit)
            )
            rows = (await connection.execute(event_query)).scalars().all()
            updated_at = row["updated_at"]

        events = [Event.model_validate(payload) for payload in reversed(rows)]
        return Session(
            app_name=app_name,
            user_id=user_id,
            id=session_id,
            state=dict(row["state"] or {}),
            events=events,
            last_update_time=_timestamp(updated_at),
        )

    async def list_sessions(self, *, app_name: str, user_id: str) -> ListSessionsResponse:
        """List a user's sessions, most recently updated first.

        Args:
            app_name: Application the sessions belong to.
            user_id: Owning user.

        Returns:
            ListSessionsResponse: The sessions, without their events, which is
            what ADK asks this method for.
        """
        async with self._engine.connect() as connection:
            rows = (
                (
                    await connection.execute(
                        select(_agent_sessions.c.id, _agent_sessions.c.updated_at)
                        .where(
                            _agent_sessions.c.app_name == app_name,
                            _agent_sessions.c.user_id == user_id,
                        )
                        .order_by(_agent_sessions.c.updated_at.desc())
                    )
                )
                .mappings()
                .all()
            )
        return ListSessionsResponse(
            sessions=[
                Session(
                    app_name=app_name,
                    user_id=user_id,
                    id=row["id"],
                    state={},
                    last_update_time=_timestamp(row["updated_at"]),
                )
                for row in rows
            ]
        )

    async def delete_session(self, *, app_name: str, user_id: str, session_id: str) -> None:
        """Delete a session and its events.

        Args:
            app_name: Application the session belongs to.
            user_id: Owning user.
            session_id: The session to remove.

        Returns:
            None
        """
        key = self._session_key(app_name, user_id, session_id)
        async with self._engine.begin() as connection:
            await connection.execute(
                delete(_agent_session_events).where(
                    _agent_session_events.c.app_name == key["app_name"],
                    _agent_session_events.c.user_id == key["user_id"],
                    _agent_session_events.c.session_id == key["id"],
                )
            )
            await connection.execute(
                delete(_agent_sessions).where(
                    _agent_sessions.c.app_name == key["app_name"],
                    _agent_sessions.c.user_id == key["user_id"],
                    _agent_sessions.c.id == key["id"],
                )
            )
            logger.info("Deleted session '%s' for user '%s'.", session_id, user_id)

    async def append_event(self, session: Session, event: Event) -> Event:
        """Persist one event and keep the in-memory session in step.

        Args:
            session: The session the event belongs to. Its ``last_update_time``
                is updated to the value the database recorded.
            event: The event to store. Partial (streaming) events are skipped,
                matching ADK's own database service, because only completed
                events belong in a history.

        Returns:
            Event: The event that was stored.
        """
        if event.partial:
            return await super().append_event(session=session, event=event)

        persisted_state = dict(session.state)
        if event.actions and event.actions.state_delta:
            # ``temp:`` keys are scoped to a single invocation and must not
            # survive it.
            persisted_state.update(
                {
                    key: value
                    for key, value in event.actions.state_delta.items()
                    if not key.startswith(State.TEMP_PREFIX)
                }
            )

        payload = event.model_dump(mode="json", **_SESSION_MODEL_CONFIG)
        async with self._engine.begin() as connection:
            stored_at = await self._store_event(connection, session, event, payload)
        if stored_at is not None:
            session.last_update_time = stored_at
        await super().append_event(session=session, event=event)
        return event

    async def _store_event(
        self,
        connection: Any,
        session: Session,
        event: Event,
        payload: dict[str, Any],
    ) -> float | None:
        """Write the event row and refresh the session's update time.

        Args:
            connection: The open transaction to write in.
            session: Owner of the event.
            event: The event being stored.
            payload: The event serialized as JSON.

        Returns:
            float | None: The timestamp the database recorded for the session,
            or None if the session row vanished.
        """
        state_delta = bool(event.actions and event.actions.state_delta)
        persisted_state = dict(session.state)
        if state_delta:
            persisted_state.update(
                {
                    key: value
                    for key, value in event.actions.state_delta.items()
                    if not key.startswith(State.TEMP_PREFIX)
                }
            )
        await connection.execute(
            insert(_agent_session_events).values(
                app_name=session.app_name,
                user_id=session.user_id,
                session_id=session.id,
                invocation_id=event.invocation_id,
                author=event.author,
                timestamp=event.timestamp,
                event=payload,
            )
        )
        session_filter = (
            _agent_sessions.c.app_name == session.app_name,
            _agent_sessions.c.user_id == session.user_id,
            _agent_sessions.c.id == session.id,
        )
        updates: dict[str, Any] = {"updated_at": func.now()}
        if state_delta:
            updates["state"] = persisted_state
        await connection.execute(update(_agent_sessions).where(*session_filter).values(**updates))
        stored = (
            await connection.execute(select(_agent_sessions.c.updated_at).where(*session_filter))
        ).first()
        if stored is None or stored[0] is None:  # pragma: no cover - defensive
            logger.warning("Session '%s' vanished while an event was being stored.", session.id)
            return None
        return _timestamp(stored[0])


def _timestamp(value: Any) -> float:
    """Return a POSIX timestamp for a datetime the driver handed back."""
    return value.timestamp() if hasattr(value, "timestamp") else float(value)


# Module-level configuration
# NOTE: ``settings`` is imported from app.core.config so there is exactly one settings
# object in the process; everything that reads ``settings.llm_model`` or
# ``settings.session_storage`` sees the same values the rest of the app does.

# The single agent instance. ``get_runner()`` replaces this when it builds the
# runner, so tests (and operators reading the logs) can see which model is live.
omnicare_agent: LlmAgent | None = None

# The active session service, selected by SESSION_STORAGE when the runner is
# first built. Kept as a module global (rather than read per request) so
# ``tests/contract/conftest.py`` and the coverage tests can force a service of
# their own the way they already force a model.
session_service: BaseSessionService | None = None

# The ADK runner bound to the current agent and session service.
runner: Runner | None = None

# OrderedDict provides O(1) FIFO eviction for session capacity enforcement
_user_sessions: OrderedDict[str, str] = OrderedDict()


def create_agent(registry: Any, agent_settings: Settings) -> Any:
    """Create the ADK agent with per-agent model configuration.

    Args:
        registry: Tool registry providing the OmniCare function tools.
        agent_settings: Runtime settings (model choice, prompt, temperature,
            max tokens).

    Returns:
        LlmAgent: Configured agent instance.
    """
    agent = LlmAgent(
        name="omnicare_assistant",
        model=LlmFactory.create_model(agent_settings),
        # The reviewed prompt lives in the prompts module; the setting overrides
        # it for a deployment without a code change.
        instruction=agent_settings.system_prompt or SYSTEM_INSTRUCTION,
        tools=registry.build_tool_list(),
        # [SUB-TASK] History rehydration from conversation_messages. Delete this
        # argument and the marked section below to drop that sub-task; nothing
        # else in this module depends on it.
        before_model_callback=rehydrate_conversation_history,
    )
    return agent


def get_runner() -> Runner:
    """Return the shared ADK Runner, building the agent on first call.

    The session service is chosen from ``SESSION_STORAGE`` the first time the
    runner (or ``_ensure_session``) needs one, and a shared store is the only
    thing that lets a second replica continue a conversation the first one
    started.

    Returns:
        Runner: The ADK runner bound to the current agent and session service.
    """
    global omnicare_agent, session_service, runner  # noqa: PLW0603
    if session_service is None:
        session_service = _build_session_service()
    if runner is None:
        omnicare_agent = create_agent(build_default_registry(), settings)
        runner = Runner(
            agent=omnicare_agent,
            app_name=APP_NAME,
            session_service=session_service,
        )
    return runner


def _build_session_service() -> BaseSessionService:
    """Build the session service the settings ask for.

    Returns:
        BaseSessionService: A database-backed service when the deployment
        selects it and the database can be reached, and an in-process service
        otherwise. The fallback logs the reason and stays in memory: losing
        stored history on a restart is a better outcome than refusing to serve.
    """
    if settings.session_storage == "database":
        from app.core.database import engine  # noqa: PLC0415

        service = DatabaseSessionService(engine)
        logger.info(
            "ADK sessions are stored in the '%s' and '%s' tables, shared across replicas.",
            _agent_sessions.name,
            _agent_session_events.name,
        )
        return service
    return InMemorySessionService()


async def warm_session_service() -> None:
    """Build the shared session service ahead of the first request.

    Called from the application lifespan when ``SESSION_STORAGE=database``.
    Creating the tables at startup makes the first request against an empty
    database succeed, instead of the runner discovering a missing table at the
    worst possible moment. If the database cannot be reached the process keeps
    its in-memory service, because a deployment that cannot talk to its own
    database has bigger problems than conversation continuity.
    """
    global session_service  # noqa: PLW0603
    if settings.session_storage != "database":
        return
    try:
        service = _build_session_service()
        await service.create_tables()
    except Exception as e:
        logger.warning(
            "Database session storage unavailable (%s); this process keeps in-memory "
            "sessions, which are lost on restart.",
            e,
        )
        return
    session_service = service
    logger.info("ADK session tables are ready.")


def _session_service() -> BaseSessionService:
    """Return the active session service, building one if it has not been built.

    Returns:
        BaseSessionService: The ADK session service used by this process.
    """
    global session_service  # noqa: PLW0603
    if session_service is None:
        session_service = _build_session_service()
    return session_service


def _session_store_is_shared() -> bool:
    """Whether sessions live in a store other replicas can read.

    Returns:
        bool: True only when the active service is the database-backed one, so
        a service installed by a test continues to short-circuit the shared
        lookup path.
    """
    return isinstance(session_service, DatabaseSessionService)


async def _latest_shared_session(user_id: str) -> str | None:
    """Return the most recently updated stored session for a user, or None."""
    service = session_service
    if service is None:
        return None
    try:
        response = await service.list_sessions(app_name=APP_NAME, user_id=user_id)
    except Exception:
        logger.warning("Could not read the stored session for user '%s'.", user_id, exc_info=True)
        return None
    if response.sessions:
        return str(response.sessions[0].id)
    return None


async def _ensure_session(user_id: str) -> str:
    """Get or create the active session for a user.

    With an in-process store the active session is the one this process created,
    kept in ``_user_sessions`` and evicted FIFO once ``_MAX_SESSIONS`` users are
    held. With a shared store the per-process mapping cannot be the truth: a
    request may land on any replica, so the session is read from the database on
    every call. That is what makes conversation continuity work across replicas,
    and what makes a reset visible immediately everywhere.

    Args:
        user_id: The authenticated user identifier.

    Returns:
        str: The ADK session id to use for this request.
    """
    if _session_store_is_shared():
        stored_id = await _latest_shared_session(user_id)
        if stored_id is not None:
            _user_sessions[user_id] = stored_id
            return stored_id

    if user_id not in _user_sessions:
        # Evict oldest entry when at capacity (FIFO -- OrderedDict preserves
        # insertion order, so the first key is always the oldest entry).
        if len(_user_sessions) >= _MAX_SESSIONS:
            oldest_user, evicted_session_id = _user_sessions.popitem(last=False)
            logger.info(
                "Session capacity reached (%d). Evicted session '%s' for user '%s'.",
                _MAX_SESSIONS,
                evicted_session_id,
                oldest_user,
            )
            # Clean up the session object from the session service to prevent
            # unbounded memory growth over long-running server uptime. The
            # in-memory service holds sessions for the whole process lifetime,
            # so deleting is what actually frees them. A shared store keeps its
            # own retention instead: dropping the row would throw away a
            # conversation another replica may still be reading.
            if not _session_store_is_shared():
                try:
                    await _session_service().delete_session(
                        app_name=APP_NAME,
                        user_id=oldest_user,
                        session_id=evicted_session_id,
                    )
                    logger.debug(
                        "Cleaned up evicted session '%s' for user '%s'.",
                        evicted_session_id,
                        oldest_user,
                    )
                except Exception as e:
                    logger.warning(
                        "Failed to clean up evicted session '%s' for user '%s': %s",
                        evicted_session_id,
                        oldest_user,
                        e,
                    )

        # Create a new session for this user.
        try:
            session = await _session_service().create_session(
                app_name=APP_NAME,
                user_id=user_id,
            )
            _user_sessions[user_id] = session.id
            logger.debug("Created new session '%s' for user '%s'.", session.id, user_id)
        except Exception as e:
            logger.error(
                "Failed to create session for user '%s': %s. Reusing existing session ID.",
                user_id,
                e,
            )
            _user_sessions[user_id] = f"session_{uuid.uuid4().hex[:12]}"

    return _user_sessions[user_id]


async def reset_user_session(user_id: str) -> None:
    """Clear the session for a user (e.g., for 'New Chat').

    With a shared store the session is read from the database before it is
    dropped, so a reset issued through one replica deletes the stored session
    every replica is using, and the next message starts a fresh one.

    Args:
        user_id: The user identifier to clear the session for.

    Returns:
        None
    """
    session_id = _user_sessions.pop(user_id, None)
    if session_id is None and _session_store_is_shared():
        session_id = await _latest_shared_session(user_id)
    if session_id:
        try:
            await _session_service().delete_session(
                app_name=APP_NAME,
                user_id=user_id,
                session_id=session_id,
            )
            logger.debug("Deleted session '%s' for user '%s'.", session_id, user_id)
        except Exception as e:
            logger.warning(
                "Failed to delete session for user '%s': %s",
                user_id,
                e,
            )


async def delete_user_session(user_id: str, session_id: str) -> None:
    """Delete one specific session belonging to a user.

    Unlike ``reset_user_session``, which clears whichever session is currently
    active, this targets a single stored session by id. A deleted conversation
    takes its agent session with it, so the next message cannot resume -- and
    cite from -- a transcript the user has already deleted.

    Args:
        user_id: The user identifier that owns the session.
        session_id: The stored session id to delete.

    Returns:
        None
    """
    if _user_sessions.get(user_id) == session_id:
        _user_sessions.pop(user_id, None)
    try:
        await _session_service().delete_session(
            app_name=APP_NAME,
            user_id=user_id,
            session_id=session_id,
        )
        logger.debug("Deleted session '%s' for user '%s'.", session_id, user_id)
    except Exception as e:
        logger.warning(
            "Failed to delete session '%s' for user '%s': %s",
            session_id,
            user_id,
            e,
        )


def _format_sources(sources: list[str | dict[str, Any]]) -> list[str]:
    """Normalize tool sources into display-ready strings.

    Policy tool returns dicts with 'source'/'section'; claim tools may return
    plain strings for multi-source claims. Handles both.

    Args:
        sources: Raw source entries from a tool response.

    Returns:
        list[str]: Formatted source strings.
    """
    formatted = []
    for item in sources:
        if isinstance(item, dict):
            source = item.get("source") or item.get("document", "")
            section = item.get("section", "")
            formatted.append(f"{section} ({source})" if section else source)
        else:
            formatted.append(item)  # already a string
    return formatted


def _extract_text(event: Event) -> str:
    """Extract concatenated text from the finalized response event.

    Args:
        event: The finalized ADK Event.

    Returns:
        str: Extracted text, or empty string if no text found.
    """
    if not event.content:
        return ""
    parts = getattr(event.content, "parts", None)
    if not parts:
        return ""
    texts = [part.text for part in parts if hasattr(part, "text") and part.text]
    return "".join(texts).strip()


def _bind_user_id(user_id: str) -> None:
    """Bind the user id to the current async context for the tools to read.

    The database layer scopes every tool query through ``current_user_id``, so
    the agent -- which is also driven outside a request, by the evaluation
    harness -- sets it from the identity it was given rather than relying on
    the request having done it. An id that is not a UUID is left unbound and
    reported, because failing loudly would cost the caller their turn while an
    unbound context fails at the query, where it is actionable.

    Args:
        user_id: The user identifier driving this run.

    Returns:
        None.
    """
    try:
        current_user_id.set(uuid.UUID(user_id))
    except ValueError:
        logger.warning("User id '%s' is not a UUID; tool queries will not be scoped.", user_id)


# The span that represents one turn, whatever service ends up recording it.
# Tracing is opt-in in the observability layer; with it off the decorator is
# the function itself, so the agent is unchanged by observability that is not
# deployed.
@trace_turn("omnicare_agent_turn")
async def run_agent(user_id: str, message: str) -> dict[str, Any]:
    """Execute the ADK agent for a single user turn.

    Args:
        user_id: The authenticated user identifier (ADK user_id).
        message: The user's natural-language message.

    Returns:
        dict: {
            "response": str,      # synthesized agent text
            "sources": list[str], # document citations
            "tool_calls": list,   # tool invocation traces
            "session_id": str     # ADK session id used
        }
    """
    runner = get_runner()

    # Ensure session exists (this is the idempotency/reuse layer).
    session_id = await _ensure_session(user_id)

    # Tools read the authenticated user through the context variable, which the
    # auth dependency sets for HTTP requests; a direct call has not come through
    # it, so it is bound here too.
    _bind_user_id(user_id)

    logger.debug("Running agent for user '%s' (session '%s').", user_id, session_id)

    # Execute the agent via ADK Runner
    final_text = ""
    sources: list[str] = []
    tool_calls: list[dict[str, Any]] = []

    async for event in runner.run_async(
        user_id=user_id,
        session_id=session_id,
        new_message=_new_user_content(message),
    ):
        # Collect tool call information for telemetry/debugging
        if event.get_function_calls():
            for fc in event.get_function_calls():
                tool_calls.append({"name": fc.name, "arguments": fc.args})

        # Collect function results (sources and citations)
        if event.get_function_responses():
            for fr in event.get_function_responses():
                if not isinstance(fr.response, dict):
                    continue
                sources_raw = fr.response.get("sources", [])
                if sources_raw:
                    sources.extend(_format_sources(sources_raw))
                # Claims tools may return 'citations' — accept both
                citation = fr.response.get("citation")
                if citation:
                    sources.append(citation)

                # Attach the result to the tool call it answers, newest first:
                # the last unmatched call is the one that produced it. Without
                # it the trace says a tool ran but not what came back.
                tool_name = fr.name if hasattr(fr, "name") else ""
                for tc in reversed(tool_calls):
                    if tc.get("name") == tool_name:
                        tc["result"] = fr.response
                        break

        # Final response
        if event.is_final_response():
            final_text = _extract_text(event)

    if not final_text:
        logger.error(
            "No final response extracted for user '%s' (session '%s').", user_id, session_id
        )

    return {
        "response": final_text,
        "sources": sources,
        "tool_calls": tool_calls,
        "session_id": session_id,
    }


def _new_user_content(message: str) -> types.Content:
    """Build the user turn ADK expects.

    Args:
        message: The raw user message.

    Returns:
        types.Content: A single-part user content for the runner.
    """
    return types.Content(role="user", parts=[types.Part.from_text(text=message)])


def sse_frame(payload: Any) -> str:
    """Encode one event as a Server-Sent Events frame.

    Args:
        payload: The event model to serialize.

    Returns:
        str: A complete ``data:`` frame, terminated by the blank line that
        tells the browser's ``EventSource`` the event is complete.
    """
    return f"data: {payload.model_dump_json()}\n\n"


def _as_object(value: Any) -> dict[str, Any]:
    """Normalize a tool payload into an object the SSE envelope can carry.

    A tool may answer with a structured payload or with a bare scalar, and an
    SSE payload has to be an object, so a scalar is wrapped in one rather than
    dropped.

    Args:
        value: The tool's return value.

    Returns:
        dict: The payload unchanged when it is already an object, and
        ``{"value": ...}`` otherwise.
    """
    return value if isinstance(value, dict) else {"value": value}


def _tool_failure_message(result: Any) -> str | None:
    """Extract a tool-reported failure from a function response payload.

    ADK surfaces a tool that could not run (bad arguments, a rejection, an
    exception it was able to capture) as a function response whose body carries
    an ``error`` key instead of a result.

    Args:
        result: The raw function response payload.

    Returns:
        str | None: The error text, or ``None`` when the call succeeded.
    """
    if isinstance(result, dict):
        error = result.get("error")
        if error:
            return str(error)
    return None


# ── Server-Sent Events (SSE) streaming agent execution ────────────────────────


class _StreamResult:
    """Terminal outcome of a streamed agent run.

    Attributes:
        session_id: The ADK session the turn ran in.
        final_text_parts: The assistant's answer, one fragment per final event.
        sources: Document citations collected while the turn ran, in order.
        tool_calls: Tool invocations made during the turn. An entry gains a
            ``result`` key once the matching function response arrives, which is
            what keeps the live UI and the persisted history describing the same
            tool activity.
    """

    def __init__(self) -> None:
        self.session_id: str | None = None
        self.final_text_parts: list[str] = []
        self.sources: list[str] = []
        self.tool_calls: list[dict[str, Any]] = []

    @property
    def text(self) -> str:
        """The full assistant text produced by this run."""
        return "".join(self.final_text_parts)


@trace_turn("omnicare_agent_turn_stream")
async def run_agent_stream(
    user_id: str,
    message: str,
    result: _StreamResult | None = None,
) -> AsyncGenerator[str, None]:
    """Run the OmniCare agent and stream results as Server-Sent Events (SSE).

    This is the streaming entry point used by the SSE chat endpoint. It runs
    the same agent and the same session as :func:`run_agent`; the difference is
    what comes back. Tool activity is emitted as typed events the client can
    render directly, and the model's own events are forwarded verbatim so the
    route can map them into the streaming contract.

    Args:
        user_id: The authenticated user identifier (ADK user_id).
        message: The user's natural-language message.
        result: Optional container the caller supplies, populated with the
            session id, the answer fragments, the citations and the tool trace
            so the route can persist the turn once the stream closes.

    Yields:
        str: Formatted SSE ``data:`` frames.

    Returns:
        None. The final ``response_complete`` event is yielded by the route.
    """
    session_id = await _ensure_session(user_id)

    # The tools scope their database reads through the context variable, which
    # the auth dependency sets for HTTP requests. The agent is also driven
    # directly -- by the evaluation harness and by these tests -- so the value
    # is set here as well rather than assumed to have come from the request.
    _bind_user_id(user_id)

    user_content = types.Content(
        role="user",
        parts=[types.Part(text=message)],
    )
    final_text_parts: list[str] = []
    sources: list[str] = []
    tool_calls: list[dict[str, Any]] = []

    runner = get_runner()

    async for event in runner.run_async(
        user_id=user_id,
        session_id=session_id,
        new_message=user_content,
        run_config=RunConfig(streaming_mode=StreamingMode.SSE),
    ):
        # The final response carries the answer text; fragmentation is kept so
        # the caller can reconstruct the turn exactly as it was streamed.
        if event.is_final_response() and event.content and event.content.parts:
            for part in event.content.parts:
                if hasattr(part, "text") and part.text:
                    final_text_parts.append(part.text)

        function_calls = event.get_function_calls()
        if function_calls:
            for fc in function_calls:
                tool_name = fc.name if hasattr(fc, "name") else str(fc)
                tool_arguments = fc.args if hasattr(fc, "args") else {}
                tool_calls.append({"name": tool_name, "arguments": tool_arguments})
                yield sse_frame(
                    StreamToolCall(tool=tool_name, arguments=_as_object(tool_arguments))
                )

        function_responses = event.get_function_responses()
        if function_responses:
            for fr in function_responses:
                result_data = fr.response if hasattr(fr, "response") else {}
                tool_name = fr.name if hasattr(fr, "name") else ""

                if isinstance(result_data, dict):
                    for src in result_data.get("sources", []):
                        if isinstance(src, dict):
                            section = src.get("section", "")
                            source_file = src.get("source", "")
                            sources.append(f"{section} ({source_file})")
                        elif isinstance(src, str):
                            sources.append(src)

                    # Claim tools return a single citation alongside their
                    # result, which belongs in the same list.
                    if "citation" in result_data:
                        sources.append(result_data["citation"])

                # Attach the result to the tool call it answers, newest first:
                # the last unmatched call is the one that produced it.
                for tc in reversed(tool_calls):
                    if tc.get("name") == tool_name:
                        tc["result"] = result_data
                        break

                tool_error = _tool_failure_message(result_data)
                if tool_error is not None:
                    yield sse_frame(StreamToolError(tool=tool_name, error=tool_error))
                else:
                    yield sse_frame(
                        StreamToolResult(
                            tool=tool_name,
                            result=_as_object(result_data),
                        )
                    )

        sse_event = event.model_dump_json(exclude_none=True, by_alias=True)
        yield f"data: {sse_event}\n\n"

    if result is not None:
        result.session_id = session_id
        result.final_text_parts = final_text_parts
        result.sources = sources
        result.tool_calls = tool_calls


# [SUB-TASK] Conversation history rehydration.
#
# This section is independent of the shared session store and can be deleted in
# full -- along with the ``before_model_callback=`` argument in ``create_agent``
# -- without touching anything else in this module. It exists because
# ``conversation_messages`` was written on every turn but never read: a fresh
# ADK session (a restart, a new replica, or the first turn after switching the
# storage) started from nothing and the user had to re-explain the situation.
# This callback replays the recent turns into the model's history and folds the
# older ones into a summary, so the persisted store is read as well as written.


async def rehydrate_conversation_history(
    callback_context: CallbackContext,
    llm_request: LlmRequest,
) -> None:
    """Replay persisted turns into a session that has no history of its own.

    Returning None hands the turn to the model unchanged; returning an
    ``LlmResponse`` would skip the model entirely, which this callback never
    does -- it only widens what the model reads.

    Args:
        callback_context: ADK callback context, used to reach the user.
        llm_request: The request about to be sent, whose ``contents`` are
            extended in place when there is history worth replaying.

    Returns:
        None. The model always runs; only its inputs change.
    """
    try:
        # ADK builds the request from the session's events plus the new user
        # message, so a single content means the session is empty and the user
        # is relying on the database to remember the conversation.
        if len(llm_request.contents) > 1:
            return
        user_id = _callback_user_id(callback_context)
        if not user_id:
            return
        history = await _load_recent_turns(user_id)
        if not history:
            return
        llm_request.contents = _merge_same_role_contents(history + list(llm_request.contents))
    except Exception:
        # A history problem must never cost the user their message.
        logger.warning("Could not rehydrate conversation history.", exc_info=True)


def _callback_user_id(callback_context: CallbackContext) -> str | None:
    """Return the user the current invocation belongs to, if ADK exposes it."""
    context = getattr(callback_context, "_invocation_context", None)
    return getattr(context, "user_id", None)


async def _load_recent_turns(user_id: str) -> list[types.Content]:
    """Load the recent turns for a user, oldest first, with a compaction summary.

    Only the last ``session_history_turns`` turns are replayed in full. The
    turns older than that are represented by one summary content, because a
    long conversation has to fit the model's context window but the fact that
    it happened must not vanish from the history.

    Args:
        user_id: The user whose turns to read.

    Returns:
        list[types.Content]: Contents to prepend, oldest first. Empty when the
        user has no persisted turns.
    """
    from app.core.database import async_session_factory  # noqa: PLC0415
    from app.models.conversations.conversation import Conversation  # noqa: PLC0415
    from app.models.conversations.conversation_message import ConversationMessage  # noqa: PLC0415

    try:
        user_uuid = uuid.UUID(str(user_id))
    except ValueError:
        # Session user ids that are not UUIDs cannot be matched to a user.
        return []

    async with async_session_factory() as session:
        result = await session.execute(
            select(
                ConversationMessage.role, ConversationMessage.content, ConversationMessage.timestamp
            )
            .join(Conversation, ConversationMessage.conversation_id == Conversation.id)
            .where(Conversation.user_id == user_uuid)
            .order_by(ConversationMessage.timestamp.desc())
            .limit(settings.session_history_compaction_turns)
        )
        rows = list(result.all())

    if not rows:
        return []

    recent = list(reversed(rows[: settings.session_history_turns]))
    compacted = list(reversed(rows[settings.session_history_turns :]))
    contents: list[types.Content] = []
    if compacted:
        contents.append(_compaction_summary_content(compacted))
    contents.extend(_turn_contents(recent))
    return contents


def _turn_contents(rows: list[Any]) -> list[types.Content]:
    """Convert persisted turns into model/user contents.

    Args:
        rows: ``(role, content, timestamp)`` tuples, oldest first.

    Returns:
        list[types.Content]: One content per non-empty turn.
    """
    contents: list[types.Content] = []
    for role, content, _timestamp in rows:
        text = (content or "").strip()
        if not text:
            continue
        is_user = str(role).lower() == "user"
        contents.append(
            types.Content(
                role="user" if is_user else "model",
                parts=[types.Part.from_text(text=text)],
            )
        )
    return contents


def _compaction_summary_content(rows: list[Any]) -> types.Content:
    """Summarize the turns that are too old to replay.

    The summary is deliberately deterministic: it reports how much was folded
    away and quotes the user's earliest questions, which is the part of an old
    turn that still shapes what a follow-up question means.

    Args:
        rows: ``(role, content, timestamp)`` tuples, oldest first.

    Returns:
        types.Content: A user-role content holding the summary.
    """
    user_texts = [str(content).strip() for role, content, _ in rows if str(role).lower() == "user"]
    excerpts = [f"'{text[:80]}'" for text in user_texts[:3]]
    spans = f"{rows[0][2]:%Y-%m-%d %H:%M} to {rows[-1][2]:%Y-%m-%d %H:%M}"
    summary = (
        f"Summary of earlier conversation: {len(rows)} older turns from {spans} were "
        "compacted into this summary to preserve context. The user's earliest questions"
        f" were {', '.join(excerpts) if excerpts else 'not recorded'}."
    )
    return types.Content(role="user", parts=[types.Part.from_text(text=summary)])


def _merge_same_role_contents(contents: list[types.Content]) -> list[types.Content]:
    """Merge runs of contents that share a role into single contents.

    The model API rejects a conversation where one side speaks twice in a row,
    and a compaction summary directly followed by a user turn would do exactly
    that.

    Args:
        contents: Conversation contents in send order.

    Returns:
        list[types.Content]: The same contents with neighbouring same-role
        contents combined.
    """
    merged: list[types.Content] = []
    for content in contents:
        if merged and merged[-1].role == content.role:
            merged[-1].parts.extend(content.parts or [])
            continue
        merged.append(types.Content(role=content.role, parts=list(content.parts or [])))
    return merged
