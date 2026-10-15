"""Tests for the tracing API: the neutral surface callers import.

The observability layer's contract has two halves, and both are covered here:
the neutral API in ``app.observability.tracing`` that the rest of the
application imports (setup, turn and tool spans, reported status, and the
vendor-independence of every module outside the package), and the LangSmith
implementation behind it (the exported environment, the LiteLLM callback, and
the metadata injection that nests a model call under its turn).
"""

from __future__ import annotations

import functools
import inspect
import os
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from unittest.mock import patch

import litellm
import pytest

from app.agent.agent import run_agent, run_agent_stream
from app.core.config import settings
from app.observability.langsmith import (
    _TURN_IDENTITY,
    _summarize_stream_frames,
    configure_langsmith,
    install_llm_trace_context,
    langsmith_tracing_active,
    register_langsmith_callback,
    trace_fields_for,
)
from app.observability.metrics import get_metrics, reset_metrics
from app.observability.request_context import reset_request_id, set_request_id
from app.observability.tracing import (
    TracingStatus,
    setup_tracing,
    trace_tool,
    trace_turn,
    tracing_status,
)

# The SDK reads its destination from these; a test that exports one must not
# let it reach the next test.
_LANGSMITH_ENV = (
    "LANGSMITH_TRACING",
    "LANGSMITH_API_KEY",
    "LANGSMITH_PROJECT",
    "LANGSMITH_ENDPOINT",
    "LANGSMITH_BASE_URL",
    "LANGSMITH_WORKSPACE_ID",
)

# The identity of the turn's run, fixed so a test can assert against it.
_TURN_RUN_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
_TURN_TRACE_ID = uuid.UUID("22222222-2222-2222-2222-222222222222")
_TURN_DOTTED_ORDER = "20260115T111111111111Z33333333-3333-3333-3333-333333333333"


@pytest.fixture(autouse=True)
def _isolate_tracing_env(monkeypatch):
    """Keep a test's exported LangSmith environment out of the next test."""
    for name in _LANGSMITH_ENV:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def _isolate_turn_context():
    """Leave no turn identity bound between tests, whichever way a test ends."""
    token = _TURN_IDENTITY.set({})
    yield
    _TURN_IDENTITY.reset(token)


@pytest.fixture(autouse=True)
def _reset_metrics_for_tracing_tests():
    """Run each test against an empty telemetry store and leave it empty."""
    reset_metrics()
    yield
    reset_metrics()


@pytest.fixture
def tracing_enabled(monkeypatch):
    """Turn LangSmith tracing on without exporting any LangSmith environment.

    The settings side alone is enough to exercise the decorators and the
    injection seam. The environment is deliberately left unset, which also
    keeps the SDK from posting to the real LangSmith service: the decorator
    runs, the spans post nothing, and the test stays hermetic.
    """
    monkeypatch.setattr(settings, "langsmith_tracing_enabled", True)
    monkeypatch.setattr(settings, "langsmith_api_key", "lsv2_test_key")
    monkeypatch.setattr(settings, "langsmith_project", "omnicare-test")


@pytest.fixture
def patched_llm_client():
    """Patch ADK's LiteLLM client for a test, then put the original back.

    The patch is class-level and installed once per process; restoring here
    keeps a test from leaking it into every other test in the session.

    Yields:
        type: The patched ``LiteLLMClient`` class.
    """
    from google.adk.models.lite_llm import LiteLLMClient

    originals = {name: getattr(LiteLLMClient, name) for name in ("acompletion", "completion")}
    install_llm_trace_context()
    yield LiteLLMClient
    for name, original in originals.items():
        setattr(LiteLLMClient, name, original)


@pytest.fixture
def captured_completion(monkeypatch, patched_llm_client) -> dict[str, Any]:
    """Capture the keyword arguments a patched completion forwards to LiteLLM.

    Yields:
        dict[str, Any]: The keywords the completion under test received.
    """
    captured: dict[str, Any] = {}

    async def fake_acompletion(**kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return {"id": "stub"}

    monkeypatch.setattr("google.adk.models.lite_llm.acompletion", fake_acompletion)
    return captured


def _turn_run_tree() -> Any:
    """Return a stable run tree for the turn in progress.

    A real ``RunTree`` rather than a stand-in: the SDK's context lookup keys on
    the instance, so a duck-typed object would leave the seam finding no turn.

    Returns:
        RunTree: The run of the turn in progress.
    """
    from langsmith import RunTree

    return RunTree(
        run_type="chain",
        name="turn",
        id=_TURN_RUN_ID,
        trace_id=_TURN_TRACE_ID,
        dotted_order=_TURN_DOTTED_ORDER,
    )


def test_tracing_is_off_by_default() -> None:
    """Tracing to a third party is opt-in, so a checkout without a key is inert."""
    assert settings.langsmith_tracing_enabled is False
    assert langsmith_tracing_active() is False


def test_enabled_without_a_key_stays_off(monkeypatch) -> None:
    """A deployment that enabled tracing without a key runs untraced, loudly.

    The reason is logged at startup (see app.main); what matters here is that
    the settings flag alone never activates anything.
    """
    monkeypatch.setattr(settings, "langsmith_tracing_enabled", True)
    monkeypatch.setattr(settings, "langsmith_api_key", "")

    assert langsmith_tracing_active() is False
    assert configure_langsmith() is False
    assert "LANGSMITH_TRACING" not in os.environ


def test_configure_reports_off_when_tracing_is_disabled() -> None:
    """A disabled deployment exports nothing, whatever else is configured."""
    assert configure_langsmith() is False
    assert "LANGSMITH_TRACING" not in os.environ


def test_configure_exports_the_environment_the_sdk_and_litellm_read(tracing_enabled) -> None:
    """The exported variables are the contract both writers of a trace honour.

    ``LANGSMITH_TRACING`` is the SDK's own switch: without it the decorator
    runs but posts nothing, so exporting it is what makes tracing live.
    """
    assert configure_langsmith() is True

    assert os.environ["LANGSMITH_TRACING"] == "true"
    assert os.environ["LANGSMITH_API_KEY"] == "lsv2_test_key"
    assert os.environ["LANGSMITH_PROJECT"] == "omnicare-test"
    # The cloud default is left to the SDK rather than pinned here.
    assert "LANGSMITH_ENDPOINT" not in os.environ
    assert "LANGSMITH_BASE_URL" not in os.environ


def test_configure_exports_a_regional_endpoint_under_both_spellings(
    tracing_enabled, monkeypatch
) -> None:
    """The SDK and LiteLLM name the base URL differently, so both are exported."""
    monkeypatch.setattr(settings, "langsmith_endpoint", "https://eu.api.smith.langchain.com")
    monkeypatch.setattr(settings, "langsmith_workspace_id", "ws-42")

    assert configure_langsmith() is True

    assert os.environ["LANGSMITH_ENDPOINT"] == "https://eu.api.smith.langchain.com"
    assert os.environ["LANGSMITH_BASE_URL"] == "https://eu.api.smith.langchain.com"
    assert os.environ["LANGSMITH_WORKSPACE_ID"] == "ws-42"


def test_callback_registers_once_with_settings_credentials(tracing_enabled) -> None:
    """Repeated startups must not stack callbacks, or every trace is doubled.

    The application lifespan runs once per process, but the test suite re-runs
    it for every client it builds.
    """
    from litellm.integrations.langsmith import LangsmithLogger

    with patch.object(litellm, "callbacks", []):
        first = register_langsmith_callback()
        second = register_langsmith_callback()

        assert isinstance(first, LangsmithLogger)
        assert first is second
        assert litellm.callbacks == [first]
        # The credentials come from settings, not from the ambient environment,
        # so a stray LANGSMITH_* variable cannot re-point the traces.
        assert first.default_credentials["LANGSMITH_API_KEY"] == "lsv2_test_key"
        assert first.default_credentials["LANGSMITH_PROJECT"] == "omnicare-test"


def test_trace_fields_nest_a_model_call_under_its_turn() -> None:
    """The fields are exactly what LiteLLM's LangSmith callback reads back."""
    fields = trace_fields_for(_turn_run_tree(), {"session_id": "user-1"})

    assert uuid.UUID(fields["id"])
    assert fields["trace_id"] == str(_TURN_TRACE_ID)
    assert fields["parent_run_id"] == str(_TURN_RUN_ID)
    # A dotted order that extends the parent's is what groups the model call
    # into the turn's trace rather than leaving it a trace of its own.
    assert fields["dotted_order"].startswith(f"{_TURN_DOTTED_ORDER}.")
    assert fields["dotted_order"].split(".")[-1].endswith(fields["id"])
    assert fields["session_id"] == "user-1"


def test_trace_fields_are_empty_outside_a_turn() -> None:
    """A model call with no turn in progress keeps its own top-level trace."""
    assert trace_fields_for(None, {}) == {}


def test_trace_fields_omit_the_thread_when_the_user_is_unknown() -> None:
    """No identity means no thread key, rather than a thread named ''."""
    assert "session_id" not in trace_fields_for(_turn_run_tree(), {})


@pytest.mark.asyncio
async def test_model_call_outside_a_turn_carries_no_trace_fields(
    captured_completion, patched_llm_client
) -> None:
    """Startup ingestion and the drain workers run outside any turn."""
    await patched_llm_client().acompletion(model="openai/gpt-4o-mini", messages=[], tools=None)

    assert "metadata" not in captured_completion


@pytest.mark.asyncio
async def test_model_call_inside_a_turn_joins_the_turn_trace(
    captured_completion, patched_llm_client
) -> None:
    """The model call is stitched to the turn through the completion metadata.

    LiteLLM's callback does not read the SDK's run context, so this patch --
    not LiteLLM -- is what makes the trace a tree.
    """
    from langsmith import tracing_context

    with tracing_context(parent=_turn_run_tree()):
        await patched_llm_client().acompletion(model="openai/gpt-4o-mini", messages=[], tools=None)

    metadata = captured_completion["metadata"]
    assert metadata["parent_run_id"] == str(_TURN_RUN_ID)
    assert metadata["trace_id"] == str(_TURN_TRACE_ID)
    assert metadata["dotted_order"].startswith(f"{_TURN_DOTTED_ORDER}.")


@pytest.mark.asyncio
async def test_model_call_of_a_turn_carries_the_thread_key(
    captured_completion, patched_llm_client, tracing_enabled
) -> None:
    """A user's turns share a thread, so they read as one conversation."""
    from langsmith import tracing_context

    token = _TURN_IDENTITY.set({"user_id": "user-1", "session_id": "user-1"})
    try:
        with tracing_context(parent=_turn_run_tree()):
            await patched_llm_client().acompletion(
                model="openai/gpt-4o-mini", messages=[], tools=None
            )
    finally:
        _TURN_IDENTITY.reset(token)

    assert captured_completion["metadata"]["session_id"] == "user-1"


def test_turn_decorator_leaves_the_function_untouched_when_tracing_is_off() -> None:
    """Tracing off is the function itself: no wrapper, no overhead, no change."""

    async def turn(user_id: str, message: str) -> str:
        """Answer the message."""
        return "answer"

    assert trace_turn("omnicare_agent_turn")(turn) is turn


def test_agent_entry_points_are_undecorated_when_tracing_is_off() -> None:
    """The import-time decoration saw tracing off, so the routes got the raw
    functions -- the SSE wrapper's own keyword never appears in a signature."""
    assert "langsmith_extra" not in inspect.signature(run_agent).parameters
    assert "langsmith_extra" not in inspect.signature(run_agent_stream).parameters


@pytest.mark.asyncio
async def test_turn_decorator_builds_the_span_with_the_turn_identity(
    tracing_enabled, monkeypatch
) -> None:
    """The span carries the turn's identity as metadata, and the identity is
    read from the entry point's own arguments so the evaluation harness's calls
    are traced the same way a request's are."""
    spans: dict[str, Any] = {}

    def fake_traceable(**span_kwargs: Any) -> Any:
        def decorate(func: Any) -> Any:
            @functools.wraps(func)
            async def wrapper(*args: Any, **kwargs: Any) -> Any:
                spans["span_kwargs"] = span_kwargs
                spans["extra"] = kwargs.pop("langsmith_extra", None)
                return await func(*args, **kwargs)

            return wrapper

        return decorate

    monkeypatch.setattr("langsmith.traceable", fake_traceable)

    @trace_turn("omnicare_agent_turn")
    async def turn(user_id: str, message: str, **extra: Any) -> str:
        return f"{user_id}:{message}"

    token = set_request_id("req-1")
    try:
        assert await turn(user_id="user-1", message="hello") == "user-1:hello"
    finally:
        reset_request_id(token)

    assert spans["span_kwargs"]["run_type"] == "chain"
    assert spans["span_kwargs"]["name"] == "omnicare_agent_turn"
    assert spans["span_kwargs"]["project_name"] == "omnicare-test"
    assert spans["span_kwargs"]["metadata"]["model"] == settings.llm_model
    # The entry point's own keyword must not reach it: the SDK owns that one.
    assert spans["extra"] == {
        "metadata": {"user_id": "user-1", "session_id": "user-1", "request_id": "req-1"}
    }


@pytest.mark.asyncio
async def test_turn_decorator_binds_the_turn_identity_for_the_duration(tracing_enabled) -> None:
    """The identity stays bound while the turn runs and is released after it."""
    seen: dict[str, Any] = {}

    @trace_turn("omnicare_agent_turn")
    async def turn(user_id: str, message: str) -> str:
        seen["during"] = dict(_TURN_IDENTITY.get())
        return "answer"

    await turn(user_id="user-1", message="hello")

    assert seen["during"] == {"user_id": "user-1", "session_id": "user-1"}
    assert _TURN_IDENTITY.get() == {}


@pytest.mark.asyncio
async def test_turn_decorator_passes_frames_through_when_tracing_is_on(tracing_enabled) -> None:
    """A streamed turn's frames still reach the client, traced but unchanged."""
    frames = [
        'data: {"type": "text_delta", "content": "Hi"}\n\n',
        'data: {"type": "tool_call", "tool": "query_policy", "arguments": {}}\n\n',
    ]

    @trace_turn("omnicare_agent_turn_stream")
    async def turn(user_id: str, message: str) -> AsyncIterator[str]:
        for frame in frames:
            yield frame

    assert [chunk async for chunk in turn(user_id="user-1", message="hi")] == frames


@pytest.mark.asyncio
async def test_turn_decorator_reduces_a_streamed_turn_to_its_shape(
    tracing_enabled, monkeypatch
) -> None:
    """The streamed turn's trace holds the turn, not the frames copied out."""
    captured: dict[str, Any] = {}

    def fake_traceable(**span_kwargs: Any) -> Any:
        captured["reduce_fn"] = span_kwargs.get("reduce_fn")

        def decorate(func: Any) -> Any:
            @functools.wraps(func)
            async def wrapper(*args: Any, **kwargs: Any) -> Any:
                kwargs.pop("langsmith_extra", None)
                async for chunk in func(*args, **kwargs):
                    yield chunk

            return wrapper

        return decorate

    monkeypatch.setattr("langsmith.traceable", fake_traceable)

    @trace_turn("omnicare_agent_turn_stream")
    async def turn(user_id: str, message: str) -> AsyncIterator[str]:
        yield ""

    # The decoration happens at definition time; the call only confirms the
    # decorated generator still runs.
    assert [chunk async for chunk in turn(user_id="user-1", message="hi")] == [""]
    assert captured["reduce_fn"] is not None


def test_stream_frames_are_reduced_to_the_turn_shape() -> None:
    """Event count, the tools that ran, and the answer the client received."""
    frames = [
        'data: {"type": "tool_call", "tool": "query_policy", "arguments": {}}\n\n',
        'data: {"type": "tool_result", "tool": "query_policy", "result": {}}\n\n',
        'data: {"type": "text_delta", "content": "Your policy "}\n\n',
        'data: {"type": "text_delta", "content": "covers floods."}\n\n',
        "data: not json at all\n\n",
    ]

    summary = _summarize_stream_frames(frames)

    assert summary == {
        "event_count": len(frames),
        "tools": ["query_policy"],
        "response": "Your policy covers floods.",
    }


def test_tool_span_is_a_no_op_when_tracing_is_off() -> None:
    """A tool keeps its own wrapper and gains nothing when tracing is off."""

    async def query_policy(query: str) -> dict[str, Any]:
        """Search the policy index."""
        return {"chunks": [query]}

    assert trace_tool("query_policy")(query_policy) is query_policy


@pytest.mark.asyncio
async def test_tool_span_wraps_the_handler_when_tracing_is_on(tracing_enabled) -> None:
    """A traced tool gets a tool span, still recognisably itself."""

    async def query_policy(query: str) -> dict[str, Any]:
        """Search the policy index."""
        return {"chunks": [query]}

    wrapped = trace_tool("query_policy")(query_policy)

    assert wrapped is not query_policy
    assert wrapped.__name__ == "query_policy"
    assert await wrapped(query="flood") == {"chunks": ["flood"]}


@pytest.mark.asyncio
async def test_instrumented_tool_keeps_its_contract_with_a_span(tracing_enabled) -> None:
    """The span wraps inside the latency wrapper, so the declaration the agent
    builds from the tool is unchanged: same signature, same annotations, and
    the same metrics the observability tests already pin."""
    from app.observability.tool_instrumentation import instrument_tool

    async def query_policy(query: str, limit: int = 2) -> dict[str, Any]:
        """Search the policy index."""
        return {"chunks": [query] * limit}

    wrapped = instrument_tool("query_policy", query_policy)

    assert inspect.signature(wrapped) == inspect.signature(query_policy)
    assert await wrapped(query="flood") == {"chunks": ["flood", "flood"]}

    tools = get_metrics().snapshot()["tools"]
    assert tools["total_calls"] == 1
    assert tools["error_count"] == 0


def test_detailed_health_reports_the_live_tracing_configuration(test_client, monkeypatch) -> None:
    """An operator reads the telemetry endpoint to confirm traces are going
    somewhere, without shelling into the container."""
    monkeypatch.setattr(settings, "langsmith_tracing_enabled", True)
    monkeypatch.setattr(settings, "langsmith_api_key", "lsv2_test_key")
    monkeypatch.setattr(settings, "langsmith_project", "omnicare-test")
    monkeypatch.setattr(settings, "langsmith_endpoint", "https://eu.api.smith.langchain.com")

    response = test_client.get("/api/v1/health/detailed")

    assert response.json()["tracing"] == {
        "enabled": True,
        "backend": "langsmith",
        "project": "omnicare-test",
        "endpoint": "https://eu.api.smith.langchain.com",
    }


def test_setup_tracing_reports_off_when_tracing_is_disabled(monkeypatch) -> None:
    """A deployment that has not turned tracing on gets nothing installed:
    no environment exported, no callback registered, no client patched."""
    from google.adk.models.lite_llm import LiteLLMClient

    monkeypatch.setattr(settings, "langsmith_tracing_enabled", False)

    assert setup_tracing() is False
    assert "LANGSMITH_TRACING" not in os.environ
    assert not getattr(LiteLLMClient.acompletion, "_omnicare_langsmith_trace_context", False)


def test_setup_tracing_brings_tracing_up_when_configured(tracing_enabled) -> None:
    """The lifespan's one call is the whole of startup: environment, model-call
    observer, and the seam that nests a model call under its turn."""
    from litellm.integrations.langsmith import LangsmithLogger

    from google.adk.models.lite_llm import LiteLLMClient

    originals = {name: getattr(LiteLLMClient, name) for name in ("acompletion", "completion")}
    with patch.object(litellm, "callbacks", []):
        try:
            assert setup_tracing() is True

            assert os.environ["LANGSMITH_TRACING"] == "true"
            assert isinstance(litellm.callbacks[0], LangsmithLogger)
            assert getattr(LiteLLMClient.acompletion, "_omnicare_langsmith_trace_context", False)
        finally:
            for name, original in originals.items():
                setattr(LiteLLMClient, name, original)


def test_tracing_status_names_the_backend_even_when_tracing_is_off() -> None:
    """The configured-but-inactive case is the one an operator must see, so the
    backend is reported whether or not tracing came up."""
    assert tracing_status() == TracingStatus(
        enabled=False,
        backend="langsmith",
        project=settings.langsmith_project,
        endpoint=settings.langsmith_endpoint.strip() or "https://api.smith.langchain.com",
    )


def test_no_module_outside_the_observability_layer_names_the_vendor() -> None:
    """The abstraction's guard: the application is uncoupled from the tracing
    vendor, so only the observability package -- and the settings module that
    documents the vendor's variables -- may name it.

    A future change that reaches past ``app.observability.tracing`` fails here
    rather than in review.
    """
    app_dir = Path(__file__).resolve().parents[1] / "app"
    offenders = [
        path.relative_to(app_dir.parent).as_posix()
        for path in sorted(app_dir.rglob("*.py"))
        if "__pycache__" not in path.parts
        and path.parent != app_dir / "observability"
        and path.name != "config.py"
        and "langsmith" in path.read_text(encoding="utf-8").lower()
    ]

    assert offenders == []
