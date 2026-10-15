"""Latency and outcome telemetry for the agent's tools.

The tool functions and the registry that assembles them are owned by the agent
layer, so instrumentation is applied where a handler enters the registry
(:meth:`ToolRegistry.register`). That single hook covers every tool the registry
ever holds without editing a tool or the registry, and it runs before the agent
is built, which is when the tool declarations are generated.

The wrapper is deliberately transparent: it forwards every argument, returns the
handler's value untouched, and re-raises its exceptions. Per-invocation cost is
one ``perf_counter`` pair and one bounded append, which is why this can be left
on in production.
"""

from __future__ import annotations

import functools
import inspect
import logging
import time
from collections.abc import Callable
from typing import Any, get_type_hints

from app.observability.metrics import build_tool_call, get_metrics
from app.observability.tracing import trace_tool

logger = logging.getLogger(__name__)

# Marker attribute that makes installation idempotent without module-level
# mutable state: a second install must not wrap an already wrapped handler.
_INSTALL_MARKER = "_omnicare_instrumented"


def _with_original_contract(
    wrapper: Callable[..., Any],
    handler: Callable[..., Any],
) -> Callable[..., Any]:
    """Pin the wrapper's signature and annotations back to the original handler.

    The tracing span ``instrument_tool`` applies innermost returns a callable
    carrying its own run-control keyword (``config``), and ``functools.wraps``
    copies that keyword into the latency wrapper built on top of it. The agent
    generates each tool's declaration from this wrapper's signature, so without
    this the declaration would advertise a ``config`` argument no tool actually
    takes. The signature is therefore pinned to the original handler's, dropping
    whatever run-control keywords the span injected.

    The annotations are stored as resolved type objects for the same reason the
    signature is pinned: they feed the tool declaration too. The tool modules use
    ``from __future__ import annotations``, so the strings ``functools.wraps``
    copies are evaluated against the function's own ``__globals__`` -- the tool
    module for the handler, but this module for the wrapper. A tool-local type
    would therefore fail to resolve on the wrapper and break the declaration, so
    they are resolved against the handler here instead. A resolution failure
    leaves the raw strings in place, which is the behaviour ``functools.wraps``
    already provides.

    Args:
        wrapper: The wrapper function to pin the contract onto.
        handler: The original tool function, before any span was applied.

    Returns:
        Callable[..., Any]: The same wrapper, carrying the handler's signature
        and resolved annotations.
    """
    wrapper.__signature__ = inspect.signature(handler)
    try:
        resolved = get_type_hints(handler)
    except Exception:
        return wrapper
    if resolved:
        wrapper.__annotations__ = resolved
    return wrapper


def instrument_tool(name: str, handler: Callable[..., Any]) -> Callable[..., Any]:
    """Wrap ``handler`` so one tool-call event is recorded per invocation.

    Args:
        name: Tool name recorded on the event. Falls back to the handler's own
            name when empty.
        handler: The tool function to wrap. Both async and sync callables are
            accepted because the registry permits either.

    Returns:
        Callable[..., Any]: A wrapper carrying the handler's name, docstring,
        signature, and resolved annotations, so the agent's tool declaration is
        built from the original function exactly as before.
    """
    tool_name = name or getattr(handler, "__name__", "tool")

    original = handler
    # The tracing span is applied innermost, before the latency wrapper, for
    # two reasons: the span nests under the trace of the turn that invoked the
    # tool through the run context, and the outer wrappers still resolve the
    # original handler's annotations -- which is what the agent builds the
    # tool declaration from. Tracing off makes this a no-op.
    handler = trace_tool(tool_name)(handler)

    def _record(started: float, success: bool, error: BaseException | None) -> None:
        try:
            get_metrics().record_tool_call(
                build_tool_call(
                    name=tool_name,
                    latency_seconds=time.perf_counter() - started,
                    success=success,
                    error="" if error is None else repr(error),
                )
            )
        except Exception:
            logger.debug("Tool telemetry dropped for '%s'", tool_name, exc_info=True)

    if inspect.iscoroutinefunction(handler):

        @functools.wraps(handler)
        async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
            started = time.perf_counter()
            try:
                result = await handler(*args, **kwargs)
            except Exception as exc:
                _record(started, False, exc)
                raise
            _record(started, True, None)
            return result

        return _with_original_contract(async_wrapper, original)

    @functools.wraps(handler)
    def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
        started = time.perf_counter()
        try:
            result = handler(*args, **kwargs)
        except Exception as exc:
            _record(started, False, exc)
            raise
        _record(started, True, None)
        return result

    return _with_original_contract(sync_wrapper, original)


def install_tool_instrumentation() -> None:
    """Instrument every handler the agent's tool registry accepts.

    ``ToolRegistry.register`` is the only point where a handler enters the
    registry, so patching it there instruments tools the agent layer owns
    without editing them. The patch is applied to the class rather than an
    instance because the agent module constructs the registry itself, and it is
    installed once: re-running the application lifespan must not wrap an already
    wrapped handler.
    """
    from app.agent.registry import ToolRegistry  # noqa: PLC0415

    register = ToolRegistry.register
    if getattr(register, _INSTALL_MARKER, False):
        return

    @functools.wraps(register)
    def instrumented_register(self: ToolRegistry, spec: Any) -> None:
        spec.handler = instrument_tool(spec.name, spec.handler)
        register(self, spec)

    setattr(instrumented_register, _INSTALL_MARKER, True)
    ToolRegistry.register = instrumented_register  # type: ignore[method-assign]
