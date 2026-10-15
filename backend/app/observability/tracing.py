"""The observability layer's public tracing surface.

Everything outside this package that wants a turn traced imports from this
module and from nowhere else. ``app.main``, ``app.agent.agent``,
``app.observability.tool_instrumentation``, and ``app.observability.routes``
name this module and nothing vendor-specific, and that is what keeps the
application uncoupled from a tracing vendor: which service the traces go to,
what it is called, and how its credentials are spelled belong to
``app.observability.langsmith``, the implementation this module delegates to.
A change of vendor is therefore a change inside that one module -- this
module's four names stay.

The API is three verbs and one question:

* :func:`setup_tracing` -- what the application lifespan calls to bring
  tracing up, reporting whether it came up.
* :func:`trace_turn` -- one span per agent turn, the root of its trace.
* :func:`trace_tool` -- one span per tool call, nested under its turn.
* :func:`tracing_status` -- what the telemetry endpoint reports.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, TypeVar

from app.observability import langsmith

logger = logging.getLogger(__name__)

F = TypeVar("F", bound=Callable[..., Any])


@dataclass(frozen=True)
class TracingStatus:
    """The tracing configuration a deployment is actually running.

    Attributes:
        enabled: Whether turns are traced right now. The flag and a key are
            both required, so this is False for a deployment that enabled
            tracing without configuring a key -- the state that most needs
            surfacing.
        backend: The vendor providing tracing. Named even when tracing is
            off, because the configured-but-inactive case is exactly what an
            operator reading the telemetry endpoint needs to see.
        project: The project traces are grouped under.
        endpoint: The API base URL traces are posted to, with the vendor's
            cloud default spelled out rather than left blank.
    """

    enabled: bool
    backend: str
    project: str
    endpoint: str


def setup_tracing() -> bool:
    """Bring tracing up, exactly as the application lifespan calls it.

    Ordering is the implementation's to decide and this function's to fix:
    the environment the tracing library and the model router read has to be
    exported before either resolves a client or a credential, so
    configuration comes first and the model-call observer after it.

    Returns:
        bool: Whether tracing is active afterwards. A disabled or unkeyed
        deployment reports ``False`` -- and the implementation logs why, so a
        deployment that meant to trace cannot silently run untraced -- and a
        failure is contained here rather than raised, because telemetry must
        never take the application down with it.
    """
    try:
        destination = langsmith.tracing_destination()
        if not langsmith.configure_langsmith():
            return False
        langsmith.register_langsmith_callback()
        langsmith.install_llm_trace_context()
        logger.info("Tracing enabled (project '%s').", destination["project"])
        return True
    except Exception:
        logger.warning("Tracing setup skipped", exc_info=True)
        return False


def trace_turn(run_name: str) -> Callable[[F], F]:
    """Return the decorator that traces one agent turn.

    The turn's span is the root of its trace: the model calls the turn makes
    and the tool calls it triggers nest under it, so one turn of a
    conversation is one trace the operator can read end to end. Tracing off
    returns the function untouched.

    Args:
        run_name: Name the trace is listed under. Callers pass their own so
            the plain and streaming entry points read apart.

    Returns:
        Callable[[F], F]: The decorator.
    """
    return langsmith.trace_turn(run_name)


def trace_tool(tool_name: str) -> Callable[[F], F]:
    """Return the decorator that traces one tool call.

    The span nests under the turn that invoked the tool, so a trace shows
    which tool ran, with what arguments, and what it returned. Tracing off
    returns the handler unchanged.

    Args:
        tool_name: Tool name the span is recorded under.

    Returns:
        Callable[[F], F]: The decorator.
    """
    return langsmith.trace_tool(tool_name)


def tracing_status() -> TracingStatus:
    """Report the tracing configuration this deployment is running.

    Returns:
        TracingStatus: Whether tracing is live, which backend provides it,
        and where traces are reported to.
    """
    destination = langsmith.tracing_destination()
    return TracingStatus(backend="langsmith", **destination)
