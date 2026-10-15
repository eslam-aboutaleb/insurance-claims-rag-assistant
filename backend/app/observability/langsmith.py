"""The LangSmith implementation behind the observability layer's tracing API.

This module is the only one in the application that names the tracing vendor.
Everything outside the observability package imports ``tracing.py`` instead,
which delegates here; nothing here is anyone else's business, and a change of
tracing vendor is a change confined to this module.

What it does: one trace per agent turn, with the calls the turn makes nested
inside. LangSmith gets its picture of a run from two writers that have to agree
on the shape of the trace tree. The ``langsmith`` SDK writes the chain run that
represents one agent turn, and LiteLLM's own LangSmith callback writes the
model calls that turn makes. The two are stitched together in one place: the
metadata LiteLLM attaches to every completion. LiteLLM's callback does not
read the SDK's run context, so without help each model call lands as its own
orphan trace and the turn it belongs to is invisible; reading the current run
tree and injecting its id, trace id, and dotted order into the completion
metadata is what makes the model call a child of the turn
(:func:`install_llm_trace_context`).

The pieces, in the order startup assembles them:

* :func:`configure_langsmith` exports the environment the SDK and LiteLLM
  read, once, before the first traced call.
* :func:`register_langsmith_callback` attaches LiteLLM's callback to the
  process-wide callback list.
* :func:`install_llm_trace_context` patches ADK's ``LiteLLMClient`` -- the
  single seam every model completion passes through -- to inject the trace
  fields.
* :func:`trace_turn` and :func:`trace_tool` decorate the agent's turn and
  tool entry points with the SDK spans.

Everything here follows the same contract as the rest of the observability
package: tracing is opt-in, and a failure anywhere in it is logged and
skipped, because a chat request must never fail because of telemetry.
"""

from __future__ import annotations

import inspect
import json
import logging
import os
import uuid
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from functools import wraps
from typing import Any, TypeVar

import litellm

from app.core.config import settings
from app.observability.request_context import get_request_id

logger = logging.getLogger(__name__)

F = TypeVar("F", bound=Callable[..., Any])

# Marker attribute that keeps the LiteLLM client patch idempotent without
# module-level mutable state, mirroring the tool instrumentation marker.
_TRACE_CONTEXT_MARKER = "_omnicare_langsmith_trace_context"

# Identity of the agent turn in progress. The LiteLLM client patch reads it to
# label the model calls of that turn, and the decorators write it so the label
# exists whether or not a request is in flight -- the evaluation harness drives
# the same entry points outside one.
_TURN_IDENTITY: ContextVar[dict[str, str]] = ContextVar("langsmith_turn_identity", default={})

# LangSmith's own default, named here because the fallback copies LiteLLM's.
DEFAULT_LANGSMITH_ENDPOINT = "https://api.smith.langchain.com"


def langsmith_tracing_active() -> bool:
    """Whether LangSmith tracing should run.

    Returns:
        bool: True when tracing is enabled in settings *and* a key is present.
        A deployment that enabled tracing without a key is reported as off
        rather than half-configured, so startup can say so.
    """
    return settings.langsmith_tracing_enabled and bool(settings.langsmith_api_key.strip())


def tracing_destination() -> dict[str, Any]:
    """Return where traces are reported, and whether that reporting is live.

    The telemetry endpoint relays this so an operator can see whether traces
    are being produced at all and where they land, without reading the
    environment. Keeping the reading here keeps the vendor's spelling of its
    own credentials on the vendor's side of the boundary.

    Returns:
        dict[str, Any]: ``enabled`` (the flag and a key are both in place),
        ``project``, and ``endpoint`` -- the effective destination, with the
        cloud default spelled out rather than left blank.
    """
    return {
        "enabled": langsmith_tracing_active(),
        "project": settings.langsmith_project,
        "endpoint": settings.langsmith_endpoint.strip() or DEFAULT_LANGSMITH_ENDPOINT,
    }


def configure_langsmith() -> bool:
    """Export the LangSmith environment variables the SDK and LiteLLM read.

    Called from the application lifespan before the first traced call, so the
    SDK's default client -- created lazily on first use -- and LiteLLM's
    callback both resolve the configured key, project, and endpoint.

    Returns:
        bool: Whether tracing is active, with the reason logged when it is
        not.
    """
    if not settings.langsmith_tracing_enabled:
        return False
    if not settings.langsmith_api_key.strip():
        logger.warning(
            "LangSmith tracing is enabled but LANGSMITH_API_KEY is empty; tracing stays off."
        )
        return False

    os.environ["LANGSMITH_TRACING"] = "true"
    os.environ["LANGSMITH_API_KEY"] = settings.langsmith_api_key
    os.environ["LANGSMITH_PROJECT"] = settings.langsmith_project
    endpoint = settings.langsmith_endpoint.strip()
    if endpoint:
        # LiteLLM's callback and the SDK name the same variable differently.
        os.environ["LANGSMITH_ENDPOINT"] = endpoint
        os.environ["LANGSMITH_BASE_URL"] = endpoint
    workspace_id = settings.langsmith_workspace_id.strip()
    if workspace_id:
        os.environ["LANGSMITH_WORKSPACE_ID"] = workspace_id
    return True


def register_langsmith_callback() -> Any:
    """Attach LiteLLM's LangSmith callback to the process-wide callback list.

    A constructed instance rather than the ``"langsmith"`` callback name, for
    two reasons: the credentials come from settings rather than from whatever
    the ambient environment happens to hold, and the membership check keeps
    repeated startups -- the test suite re-runs the application lifespan --
    from stacking callbacks that would double every trace.

    Returns:
        LangsmithLogger: The active callback, newly registered or already
        present.
    """
    from litellm.integrations.langsmith import LangsmithLogger

    for callback in litellm.callbacks:
        if isinstance(callback, LangsmithLogger):
            return callback

    callback = LangsmithLogger(
        langsmith_api_key=settings.langsmith_api_key,
        langsmith_project=settings.langsmith_project,
        langsmith_base_url=settings.langsmith_endpoint.strip() or None,
        langsmith_tenant_id=settings.langsmith_workspace_id.strip() or None,
    )
    litellm.callbacks = [*litellm.callbacks, callback]
    logger.info(
        "LangSmith tracing callback registered (project '%s').",
        settings.langsmith_project,
    )
    return callback


def _dotted_order_segment(run_id: str) -> str:
    """Format one LangSmith dotted-order segment for ``run_id``.

    LangSmith writes a segment as a UTC timestamp immediately followed by the
    run id, and joins a run's ancestry with dots; a child's segment has to
    follow the parent's so the trace renders as a tree.

    Args:
        run_id: The run id the segment belongs to.

    Returns:
        str: The formatted segment.
    """
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ") + run_id


def trace_fields_for(run_tree: Any, identity: Mapping[str, str]) -> dict[str, str]:
    """Return the fields a model call needs to join the current turn's trace.

    These are exactly the fields LiteLLM's LangSmith callback reads back out
    of a completion's metadata: the model call's own id, the trace and parent
    run it belongs to, the dotted order that places it inside that trace, and
    the thread id that groups the user's turns.

    Args:
        run_tree: The SDK run for the turn in progress, or ``None`` when the
            call happens outside one -- startup ingestion, the drain workers.
        identity: The turn identity bound by :func:`bind_turn_context`.

    Returns:
        dict[str, str]: The metadata fields to merge into the completion, or
        an empty mapping when there is no turn to join.
    """
    if run_tree is None:
        return {}
    run_id = str(uuid.uuid4())
    fields = {
        "id": run_id,
        "trace_id": str(run_tree.trace_id),
        "parent_run_id": str(run_tree.id),
        "dotted_order": f"{run_tree.dotted_order}.{_dotted_order_segment(run_id)}",
    }
    if session_id := identity.get("session_id"):
        fields["session_id"] = session_id
    return fields


def _current_run_tree() -> Any | None:
    """Return the LangSmith run for the turn in progress, if any.

    The import is deferred so a deployment without LangSmith never pays for
    the SDK, and kept in its own function so tests can stand in for the run
    context without touching contextvars themselves.

    Returns:
        RunTree | None: The active run, or ``None`` outside a traced call.
    """
    from langsmith import get_current_run_tree

    return get_current_run_tree()


def _inject_trace_fields(kwargs: dict[str, Any]) -> None:
    """Merge the current turn's trace fields into a completion's metadata.

    A failure here costs the run its place in the trace tree and nothing
    else, so every exception is swallowed: a broken trace context must not
    break the model call it was labelling.

    Args:
        kwargs: The completion call's keyword arguments, updated in place.
    """
    try:
        fields = trace_fields_for(_current_run_tree(), _TURN_IDENTITY.get())
        if not fields:
            return
        metadata = dict(kwargs.get("metadata") or {})
        metadata.update(fields)
        kwargs["metadata"] = metadata
    except Exception:
        logger.debug("LangSmith trace context not attached to this model call", exc_info=True)


def _patch_completion(client_cls: type, method_name: str) -> None:
    """Wrap one ``LiteLLMClient`` completion method with trace injection.

    Args:
        client_cls: The class owning the method -- ADK's ``LiteLLMClient``.
        method_name: ``"acompletion"`` or ``"completion"``; the pair covers
            the asynchronous and the streaming paths.
    """
    original = getattr(client_cls, method_name)

    if getattr(original, _TRACE_CONTEXT_MARKER, False):
        return

    if inspect.iscoroutinefunction(original):

        @wraps(original)
        async def traced_async(self: Any, *args: Any, **kwargs: Any) -> Any:
            _inject_trace_fields(kwargs)
            return await original(self, *args, **kwargs)

        traced = traced_async
    else:

        @wraps(original)
        def traced_sync(self: Any, *args: Any, **kwargs: Any) -> Any:
            _inject_trace_fields(kwargs)
            return original(self, *args, **kwargs)

        traced = traced_sync

    setattr(traced, _TRACE_CONTEXT_MARKER, True)
    setattr(client_cls, method_name, traced)


def install_llm_trace_context() -> None:
    """Make every ADK model call join the LangSmith trace of its turn.

    ADK's ``LiteLlm`` funnels every completion -- asynchronous for the plain
    chat path, synchronous iteration for the streaming path -- through
    ``LiteLLMClient``, so patching that pair is the one seam that sees them
    all. The patch adds no behaviour of its own: it merges the trace fields
    into metadata the completion already carries and forwards the call
    unchanged, and it is installed once.
    """
    from google.adk.models.lite_llm import LiteLLMClient

    for method_name in ("acompletion", "completion"):
        _patch_completion(LiteLLMClient, method_name)
    logger.info("ADK model calls will nest under the LangSmith trace of their turn.")


def _turn_identity(
    func: Callable[..., Any], args: tuple[Any, ...], kwargs: dict[str, Any]
) -> dict[str, str]:
    """Describe the turn an entry point is starting, for run metadata.

    Read from the entry point's own arguments rather than from context a
    request may already have set, because the evaluation harness calls the
    same entry points outside a request. Missing values are simply absent:
    a trace is still useful without them.

    Args:
        func: The entry point being called.
        args: Its positional arguments.
        kwargs: Its keyword arguments.

    Returns:
        dict[str, str]: ``user_id``, ``session_id``, and the correlation
        ``request_id``, whichever are known.
    """
    identity: dict[str, str] = {}
    try:
        bound = inspect.signature(func).bind_partial(*args, **kwargs)
    except TypeError:
        bound = None
    user_id = bound.arguments.get("user_id") if bound else None
    if user_id:
        identity["user_id"] = str(user_id)
        # One thread per user: the conversation is the unit a reader follows,
        # and a user's turns belong beside each other in LangSmith's view.
        identity["session_id"] = str(user_id)
    if request_id := get_request_id():
        identity["request_id"] = request_id
    return identity


@contextmanager
def bind_turn_context(identity: Mapping[str, str]) -> Iterator[None]:
    """Bind the identity of the turn in progress for its duration.

    Args:
        identity: The turn identity to bind.

    Yields:
        None.
    """
    token = _TURN_IDENTITY.set(dict(identity))
    try:
        yield
    finally:
        _TURN_IDENTITY.reset(token)


def _run_extra(identity: Mapping[str, str]) -> dict[str, Any]:
    """Assemble the per-call LangSmith extras for a turn.

    Args:
        identity: The turn identity bound for this call.

    Returns:
        dict[str, Any]: The ``langsmith_extra`` payload; the identity rides
        along as metadata so a turn can be found by user or correlation id.
    """
    return {"metadata": dict(identity)}


def _build_turn_span(run_name: str, *, streaming: bool) -> Callable[..., Any]:
    """Build the SDK span that represents one agent turn.

    Args:
        run_name: Name the trace is listed under in LangSmith.
        streaming: Whether the decorated entry point is a generator, whose
            yields are reduced to the turn's shape rather than logged whole.

    Returns:
        Callable[..., Any]: The ``traceable``-wrapped callable.
    """
    from langsmith import traceable

    kwargs: dict[str, Any] = {
        "run_type": "chain",
        "name": run_name,
        "project_name": settings.langsmith_project,
        "tags": [settings.app_name, settings.environment],
        "metadata": {"model": settings.llm_model, "component": "adk-agent"},
    }
    if streaming:
        kwargs["reduce_fn"] = _summarize_stream_frames
    return traceable(**kwargs)


def _summarize_stream_frames(frames: list[str]) -> dict[str, Any]:
    """Compact the SSE frames a streamed turn yielded into trace output.

    A streamed turn's natural output is its frames, and a trace that logged
    every one of them would be the stream copied verbatim. The frames are
    re-read into the shape the turn is understood by instead: how many events
    it carried, which tools ran, and the answer the client received.

    Args:
        frames: The SSE frames the decorated generator yielded, in order.

    Returns:
        dict[str, Any]: Event count, the tool names invoked, and the
        assembled answer text.
    """
    summary: dict[str, Any] = {"event_count": len(frames), "tools": [], "response": ""}
    for frame in frames:
        try:
            payload = json.loads(frame.removeprefix("data: ").strip())
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(payload, dict):
            continue
        if payload.get("type") == "tool_call":
            if tool := payload.get("tool"):
                summary["tools"].append(tool)
        elif payload.get("type") == "text_delta":
            summary["response"] += payload.get("content", "")
    summary["response"] = summary["response"].strip()
    return summary


def trace_turn(run_name: str) -> Callable[[F], F]:
    """Wrap an agent-turn entry point in a LangSmith chain run.

    The chain run is the root of one trace: the model calls the turn makes
    and the tool calls it triggers nest under it through the run context, so
    one turn of a conversation is one trace in LangSmith. Tracing off returns
    the function untouched -- a deployment that has not configured tracing
    pays nothing and sees no change in behaviour.

    Args:
        run_name: Name the trace is listed under in LangSmith.

    Returns:
        Callable[[F], F]: The decorator.
    """

    def decorator(func: F) -> F:
        if not langsmith_tracing_active():
            return func
        is_stream = inspect.isasyncgenfunction(func)
        span = _build_turn_span(run_name, streaming=is_stream)

        if is_stream:

            @wraps(func)
            async def stream_wrapper(*args: Any, **kwargs: Any) -> Any:
                identity = _turn_identity(func, args, kwargs)
                with bind_turn_context(identity):
                    async for chunk in span(func)(
                        *args, **kwargs, langsmith_extra=_run_extra(identity)
                    ):
                        yield chunk

            return stream_wrapper  # type: ignore[return-value]

        @wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            identity = _turn_identity(func, args, kwargs)
            with bind_turn_context(identity):
                return await span(func)(*args, **kwargs, langsmith_extra=_run_extra(identity))

        return wrapper  # type: ignore[return-value]

    return decorator


def trace_tool(name: str) -> Callable[[F], F]:
    """Wrap a tool handler in a LangSmith tool span.

    The span nests under the agent turn through the run context, so a trace
    shows which tool ran, with what arguments, and what it returned. Tracing
    off returns the handler unchanged.

    Args:
        name: Tool name the span is recorded under.

    Returns:
        Callable[[F], F]: The decorator.
    """
    if not langsmith_tracing_active():
        return lambda handler: handler

    from langsmith import traceable

    span = traceable(
        run_type="tool",
        name=name,
        project_name=settings.langsmith_project,
    )

    def decorator(handler: F) -> F:
        return span(handler)

    return decorator
