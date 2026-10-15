"""Tests for the observability package: correlation ids, telemetry, and metrics.

Covers the three things the track promises:
  - the request id reaches a context variable, every log record, and the response
    headers, whether the caller supplied it or not;
  - the LiteLLM callback records tokens, cost, and latency, and a failing sink
    never breaks the call it was measuring;
  - GET /api/v1/health/detailed reports telemetry without disturbing the pinned
    GET /api/v1/health contract.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, get_type_hints
from unittest.mock import patch

import pytest

from app.config import settings
from app.observability.langsmith import DEFAULT_LANGSMITH_ENDPOINT
from app.observability.metrics import build_llm_call, build_tool_call, reset_metrics
from app.observability.request_context import (
    REQUEST_ID_HEADER,
    generate_request_id,
    get_request_id,
    reset_request_id,
    resolve_request_id,
    set_request_id,
)


@pytest.fixture(autouse=True)
def _isolate_request_id():
    """Leave no correlation id bound between tests, whichever way a test ends."""
    token = set_request_id("")
    yield
    reset_request_id(token)


@pytest.fixture(autouse=True)
def _isolate_metrics():
    """Run each test against an empty telemetry store and leave it empty."""
    reset_metrics()
    yield
    reset_metrics()


def _http_scope(headers: list[tuple[bytes, bytes]] | None = None) -> dict[str, Any]:
    """Build a minimal ASGI HTTP scope for a GET request."""
    return {"type": "http", "method": "GET", "path": "/api/v1/health", "headers": headers or []}


def _json_app(captured: dict[str, str], sent: list[dict[str, Any]]):
    """Return an ASGI app that records the request id and sends a JSON response."""

    async def inner_app(scope: dict[str, Any], receive: Any, send: Any) -> None:
        captured["request_id"] = get_request_id()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"{}"})

    return inner_app


async def _receive() -> dict[str, Any]:
    """Return an empty ASGI request message."""
    return {"type": "http.request", "body": b"", "more_body": False}


async def _collect(message: dict[str, Any]) -> None:
    """Discard a sent ASGI message, asynchronously like a real send."""
    return


class _FakeResponse:
    """Stand-in for a LiteLLM ``ModelResponse``.

    Only the fields the callback reads are present, so the tests exercise this
    package's extraction logic rather than LiteLLM's own model.
    """

    def __init__(self, model: str, usage: dict[str, int]) -> None:
        self._model = model
        self._usage = usage

    def get(self, key: str, default: Any = None) -> Any:
        """Return the stubbed field for ``key``."""
        if key == "usage":
            return self._usage
        if key == "model":
            return self._model
        return default


def _logging_kwargs(
    *,
    model: str = "gpt-4o-mini",
    prompt_tokens: int = 120,
    completion_tokens: int = 30,
    response_cost: float = 0.00123,
    exception: BaseException | None = None,
) -> dict[str, Any]:
    """Build the logging payload LiteLLM passes to a callback hook."""
    return {
        "model": model,
        "standard_logging_object": {
            "model": model,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "response_cost": response_cost,
            "response_time": 0.75,
        },
        "exception": exception,
    }


def _invoke_callback(
    *,
    kwargs: dict[str, Any],
    response_obj: Any,
    success: bool,
) -> None:
    """Run the callback's async hook for a completed call."""
    from app.observability.llm_callback import LlmCostCallback

    callback = LlmCostCallback()
    start_time = datetime.now(UTC) - timedelta(seconds=1)
    end_time = datetime.now(UTC)

    async def invoke() -> None:
        if success:
            await callback.async_log_success_event(kwargs, response_obj, start_time, end_time)
        else:
            await callback.async_log_failure_event(kwargs, response_obj, start_time, end_time)

    asyncio.run(invoke())


class TestRequestContext:
    def test_inbound_id_is_echoed_when_safe(self):
        assert resolve_request_id("upstream-trace-42") == "upstream-trace-42"

    def test_inbound_id_with_control_characters_is_replaced(self):
        # A header that could inject newlines into a JSON log line is dropped
        # rather than echoed back into the log stream.
        forged = resolve_request_id("trace\nforged-entry")

        assert forged != "trace\nforged-entry"
        assert len(forged) == len(generate_request_id())

    def test_missing_id_is_generated(self):
        assert len(resolve_request_id(None)) == len(generate_request_id())
        assert len(resolve_request_id("   ")) == len(generate_request_id())

    def test_over_long_id_is_replaced(self):
        assert len(resolve_request_id("x" * 500)) == len(generate_request_id())

    def test_generated_id_is_an_opaque_token(self):
        assert generate_request_id().isalnum()


class TestRequestIdMiddleware:
    def test_inbound_id_is_bound_and_echoed_on_the_response(self):
        from app.observability.middleware import RequestIdMiddleware

        captured: dict[str, str] = {}
        sent: list[dict[str, Any]] = []
        scope = _http_scope([(b"x-request-id", b"trace-from-caller")])

        async def send(message: dict[str, Any]) -> None:
            sent.append(message)

        asyncio.run(RequestIdMiddleware(_json_app(captured, sent))(scope, _receive, send))

        assert captured["request_id"] == "trace-from-caller"
        assert dict(sent[0]["headers"])[b"x-request-id"] == b"trace-from-caller"

    def test_missing_id_is_generated_and_bound(self):
        from app.observability.middleware import RequestIdMiddleware

        captured: dict[str, str] = {}
        sent: list[dict[str, Any]] = []

        async def send(message: dict[str, Any]) -> None:
            sent.append(message)

        asyncio.run(RequestIdMiddleware(_json_app(captured, sent))(_http_scope(), _receive, send))

        assert captured["request_id"]
        assert len(captured["request_id"]) == len(generate_request_id())
        assert dict(sent[0]["headers"])[b"x-request-id"] == captured["request_id"].encode()

    def test_context_is_restored_after_the_request(self):
        from app.observability.middleware import RequestIdMiddleware

        async def inner_app(scope: dict[str, Any], receive: Any, send: Any) -> None:
            await send({"type": "http.response.start", "status": 200, "headers": []})

        # A request must not leak its id into whatever runs next on this
        # context: the middleware restores the id that was bound before it.
        outer = set_request_id("outer-request")
        try:
            asyncio.run(RequestIdMiddleware(inner_app)(_http_scope(), _receive, _collect))
            assert get_request_id() == "outer-request"
        finally:
            reset_request_id(outer)

    def test_non_http_connections_are_passed_through(self):
        from app.observability.middleware import RequestIdMiddleware

        seen: list[str] = []

        async def inner_app(scope: dict[str, Any], receive: Any, send: Any) -> None:
            seen.append(scope["type"])

        asyncio.run(RequestIdMiddleware(inner_app)({"type": "lifespan"}, _receive, _collect))

        assert seen == ["lifespan"]

    def test_response_header_is_present_on_the_live_endpoint(self, test_client):
        response = test_client.get("/api/v1/health/detailed")

        assert response.status_code == 200
        assert response.headers.get(REQUEST_ID_HEADER)

    def test_caller_supplied_header_is_echoed_by_the_live_endpoint(self, test_client):
        response = test_client.get("/api/v1/health", headers={REQUEST_ID_HEADER: "trace-live"})

        assert response.headers[REQUEST_ID_HEADER] == "trace-live"

    def test_an_upstream_header_is_replaced_not_duplicated(self):
        from app.observability.middleware import RequestIdMiddleware

        async def inner_app(scope: dict[str, Any], receive: Any, send: Any) -> None:
            await send(
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [(b"x-request-id", b"set-by-an-inner-layer")],
                }
            )

        sent: list[dict[str, Any]] = []

        async def send(message: dict[str, Any]) -> None:
            sent.append(message)

        asyncio.run(RequestIdMiddleware(inner_app)(_http_scope(), _receive, send))

        sent_ids = [(name, value) for name, value in sent[0]["headers"] if name == b"x-request-id"]
        assert len(sent_ids) == 1


class TestLogging:
    def test_json_formatter_emits_a_fixed_schema(self):
        from app.observability.logging_config import JsonLogFormatter

        record = logging.LogRecord(
            "app.observability", logging.INFO, __file__, 1, "hello %s", ("world",), None
        )
        payload = json.loads(JsonLogFormatter().format(record))

        assert payload["level"] == "INFO"
        assert payload["logger"] == "app.observability"
        assert payload["message"] == "hello world"
        # A record made outside a request has no bound id and still reports one.
        assert payload["request_id"] == "-"

    def test_json_formatter_carries_the_correlation_id(self):
        from app.observability.logging_config import JsonLogFormatter

        record = logging.LogRecord(
            "app.observability", logging.INFO, __file__, 1, "correlated", None, None
        )
        record.request_id = "trace-log"
        payload = json.loads(JsonLogFormatter().format(record))

        assert payload["request_id"] == "trace-log"

    def test_json_formatter_forwards_structured_extras(self):
        from app.observability.logging_config import JsonLogFormatter

        record = logging.LogRecord(
            "app.observability", logging.INFO, __file__, 1, "llm call", None, None
        )
        record.event = "llm_call"
        record.cost_usd = 0.0042
        payload = json.loads(JsonLogFormatter().format(record))

        assert payload["attributes"] == {"event": "llm_call", "cost_usd": 0.0042}

    def test_text_formatter_keeps_the_original_layout(self):
        from app.observability.logging_config import TextLogFormatter

        formatter = TextLogFormatter(
            "%(asctime)s - %(name)s - %(levelname)s - [%(request_id)s] %(message)s"
        )
        record = logging.LogRecord(
            "app.observability", logging.WARNING, __file__, 1, "degraded", None, None
        )
        line = formatter.format(record)

        assert "app.observability" in line
        assert "WARNING" in line
        assert "[-]" in line
        assert line.endswith("degraded")

    def test_configure_logging_replaces_its_own_handler_without_stacking(self):
        from app.observability.logging_config import ObservabilityLogHandler, configure_logging

        configure_logging("INFO", "json")
        configure_logging("INFO", "json")

        installed = [
            handler
            for handler in logging.getLogger().handlers
            if isinstance(handler, ObservabilityLogHandler)
        ]

        assert len(installed) == 1

    def test_record_factory_stamps_the_current_correlation_id(self):
        from app.observability.logging_config import _record_factory

        token = set_request_id("trace-factory")
        try:
            record = _record_factory(
                "app.observability", logging.INFO, __file__, 1, "message", (), None
            )
        finally:
            reset_request_id(token)

        assert record.request_id == "trace-factory"


class TestLlmCostCallback:
    def test_callback_records_model_tokens_cost_and_latency(self):
        from app.observability.metrics import get_metrics

        token = set_request_id("trace-llm")
        try:
            with patch(
                "app.observability.llm_callback.litellm.completion_cost", return_value=0.0042
            ):
                _invoke_callback(
                    kwargs=_logging_kwargs(),
                    response_obj=_FakeResponse(
                        "gpt-4o-mini", {"prompt_tokens": 0, "completion_tokens": 0}
                    ),
                    success=True,
                )
        finally:
            reset_request_id(token)

        llm = get_metrics().snapshot()["llm"]
        assert llm["call_count"] == 1
        assert llm["error_count"] == 0
        assert llm["prompt_tokens"] == 120
        assert llm["completion_tokens"] == 30
        assert llm["total_cost_usd"] == pytest.approx(0.0042)
        assert llm["calls_by_model"] == {"gpt-4o-mini": 1}
        assert llm["avg_latency_ms"] == pytest.approx(1000.0, rel=0.5)

    def test_recorded_calls_are_tagged_with_the_correlation_id(self):
        token = set_request_id("trace-llm")
        try:
            call = build_llm_call(model="gpt-4o-mini", cost_usd=0.1, latency_seconds=1.0)
        finally:
            reset_request_id(token)

        assert call.request_id == "trace-llm"

    def test_callback_falls_back_to_the_cost_litellm_logged(self):
        from app.observability.metrics import get_metrics

        def _unpriceable(**_kwargs: Any) -> float:
            raise RuntimeError("model is not in the LiteLLM price map")

        with patch("app.observability.llm_callback.litellm.completion_cost", _unpriceable):
            _invoke_callback(
                kwargs=_logging_kwargs(response_cost=0.0009), response_obj=None, success=True
            )

        assert get_metrics().snapshot()["llm"]["total_cost_usd"] == pytest.approx(0.0009)

    def test_callback_records_failures_without_a_response(self):
        from app.observability.metrics import get_metrics

        _invoke_callback(
            kwargs=_logging_kwargs(exception=RuntimeError("rate limited"), response_cost=0.0),
            response_obj=None,
            success=False,
        )

        llm = get_metrics().snapshot()["llm"]
        assert llm["call_count"] == 1
        assert llm["error_count"] == 1
        # LiteLLM stamps the cost it believes failed calls incurred, so a failure
        # is not silently reported as free.
        assert llm["total_cost_usd"] == 0.0

    def test_sink_failure_never_reaches_the_caller(self):
        from app.observability.metrics import get_metrics

        def _broken_sink(_call: Any) -> None:
            raise RuntimeError("telemetry store unavailable")

        with patch("app.observability.llm_callback.get_metrics", _broken_sink):
            with patch("app.observability.llm_callback.litellm.completion_cost", return_value=1.0):
                _invoke_callback(
                    kwargs=_logging_kwargs(),
                    response_obj=_FakeResponse(
                        "gpt-4o-mini", {"prompt_tokens": 1, "completion_tokens": 1}
                    ),
                    success=True,
                )

        assert get_metrics().snapshot()["llm"]["call_count"] == 0

    def test_registration_is_idempotent(self):
        import litellm

        from app.observability.llm_callback import LlmCostCallback, register_llm_cost_callback

        with patch.object(litellm, "callbacks", []):
            register_llm_cost_callback()
            register_llm_cost_callback()

            assert sum(isinstance(cb, LlmCostCallback) for cb in litellm.callbacks) == 1


class TestToolInstrumentation:
    def test_wrapper_forwards_arguments_and_result(self):
        from app.observability.metrics import get_metrics
        from app.observability.tool_instrumentation import instrument_tool

        async def fake_tool(query: str, n_results: int = 5) -> dict[str, Any]:
            """A stub tool."""
            await asyncio.sleep(0)
            return {"query": query, "n_results": n_results}

        wrapped = instrument_tool("query_policy", fake_tool)
        result = asyncio.run(wrapped("water damage", n_results=3))

        assert result == {"query": "water damage", "n_results": 3}
        # The agent builds its tool declaration from the wrapped function, so the
        # name, docstring, signature, and resolved annotations must all survive.
        assert wrapped.__name__ == "fake_tool"
        assert wrapped.__doc__ == "A stub tool."
        assert inspect.signature(wrapped) == inspect.signature(fake_tool)
        assert get_type_hints(wrapped) == get_type_hints(fake_tool)

        tools = get_metrics().snapshot()["tools"]
        assert tools["total_calls"] == 1
        assert tools["by_tool"][0]["name"] == "query_policy"
        assert tools["by_tool"][0]["error_count"] == 0

    def test_wrapper_records_failures_and_reraises(self):
        from app.observability.metrics import get_metrics
        from app.observability.tool_instrumentation import instrument_tool

        async def failing_tool() -> dict[str, Any]:
            """A stub tool that always fails."""
            raise ValueError("policy store unavailable")

        wrapped = instrument_tool("query_policy", failing_tool)

        with pytest.raises(ValueError, match="policy store unavailable"):
            asyncio.run(wrapped())

        assert get_metrics().snapshot()["tools"]["error_count"] == 1

    def test_recorded_calls_are_tagged_with_the_correlation_id(self):
        token = set_request_id("trace-tool")
        try:
            call = build_tool_call(name="query_policy", latency_seconds=0.25, success=True)
        finally:
            reset_request_id(token)

        assert call.request_id == "trace-tool"

    def test_installing_instruments_every_registered_handler(self):
        from app.agent.registry import ToolRegistry, ToolSpec
        from app.observability.metrics import get_metrics
        from app.observability.tool_instrumentation import install_tool_instrumentation

        async def fake_tool(query: str) -> str:
            """A stub tool."""
            return query

        install_tool_instrumentation()
        registry = ToolRegistry()
        registry.register(ToolSpec(name="query_policy", description="stub", handler=fake_tool))

        handlers = registry.build_tool_list()

        assert handlers[0] is not fake_tool
        assert asyncio.run(handlers[0]("water damage")) == "water damage"
        assert get_metrics().snapshot()["tools"]["by_tool"][0]["name"] == "query_policy"


class TestRateLimitTelemetry:
    def test_rejection_is_recorded_with_the_route(self):
        from app.observability.metrics import get_metrics
        from app.rate_limiter import record_rate_limit_rejection

        token = set_request_id("trace-limited")
        try:
            record_rate_limit_rejection("/api/v1/chat")
        finally:
            reset_request_id(token)

        rate_limits = get_metrics().snapshot()["rate_limits"]
        assert rate_limits["rejections"] == 1
        assert rate_limits["by_route"] == {"/api/v1/chat": 1}

    def test_rejections_outside_the_window_are_not_counted(self):
        from app.observability.metrics import LimiterRejection, get_metrics

        # Seeded as an event from long before the window opened, which is what a
        # process that has been up for days reports.
        get_metrics().record_limiter_rejection(
            LimiterRejection(timestamp=0.0, route="/api/v1/chat", request_id="-")
        )

        assert get_metrics().snapshot()["rate_limits"]["rejections"] == 0


class TestDetailedHealth:
    def test_pinned_health_endpoint_shape_is_untouched(self, test_client):
        response = test_client.get("/api/v1/health")

        assert response.status_code == 200
        assert set(response.json()) == {"status"}

    def test_detailed_health_reports_the_expected_sections(self, test_client):
        from app.observability.metrics import get_metrics

        get_metrics().record_llm_call(
            build_llm_call(
                model="gpt-4o-mini",
                prompt_tokens=120,
                completion_tokens=30,
                cost_usd=0.0042,
                latency_seconds=0.9,
                success=True,
            )
        )
        response = test_client.get("/api/v1/health/detailed")

        assert response.status_code == 200
        payload = response.json()
        assert set(payload) == {
            "status",
            "database",
            "llm",
            "tools",
            "outbox",
            "rate_limits",
            "tracing",
        }
        assert payload["status"] == "healthy"
        assert payload["database"]["connected"] is True
        assert payload["database"]["latency_ms"] >= 0
        assert payload["llm"]["call_count"] == 1
        assert payload["llm"]["total_cost_usd"] == pytest.approx(0.0042)
        assert payload["tools"]["total_calls"] == 0
        assert payload["rate_limits"]["rejections"] == 0
        # Tracing is opt-in, and the suite never configures a key: the section
        # reports the deployed configuration rather than the metrics, and names
        # the backend even while off.
        assert payload["tracing"] == {
            "enabled": False,
            "backend": "langsmith",
            "project": settings.langsmith_project,
            "endpoint": settings.langsmith_endpoint.strip() or DEFAULT_LANGSMITH_ENDPOINT,
        }
        assert isinstance(payload["outbox"]["depth_by_status"], dict)
        assert payload["outbox"]["oldest_pending_age_seconds"] is None or (
            payload["outbox"]["oldest_pending_age_seconds"] >= 0
        )

    def test_detailed_health_reports_outbox_depth_and_backlog(self, test_client):
        from app.database import async_session_factory
        from app.models.embedding_job import EmbeddingJob
        from app.models.user import User

        async def _seed() -> None:
            owner_id = uuid.uuid4()
            async with async_session_factory() as session:
                session.add(
                    User(
                        id=owner_id,
                        username=f"observability_{owner_id.hex[:12]}",
                        password_hash="not-a-real-hash",  # noqa: S106 - fixture row
                    )
                )
                await session.commit()
            # A committed owner first: the embedding jobs reference it, and the
            # SQLAlchemy unit of work does not order the two tables' inserts.
            async with async_session_factory() as session:
                for index, job_status in enumerate(("pending", "pending", "completed")):
                    # The oldest pending job is seeded well before the others so
                    # the reported backlog age belongs to that job.
                    created_at = datetime.now(UTC) - timedelta(minutes=5 - index)
                    session.add(
                        EmbeddingJob(
                            id=uuid.uuid4(),
                            claim_id=f"CLM-{index}",
                            claim_uuid=uuid.uuid4(),
                            owner_id=owner_id,
                            claim_type="Water Damage",
                            description="Pipe burst causing kitchen flooding.",
                            policy_number="POL-1092",
                            claim_status="Submitted",
                            status=job_status,
                            status_detail=job_status,
                            created_at=created_at,
                        )
                    )
                await session.commit()

        asyncio.run(_seed())

        response = test_client.get("/api/v1/health/detailed")

        assert response.status_code == 200
        outbox = response.json()["outbox"]
        assert outbox["depth_by_status"] == {"pending": 2, "completed": 1}
        assert outbox["oldest_pending_age_seconds"] >= 4 * 60

    def test_detailed_health_returns_503_when_the_database_is_unreachable(
        self, test_client, monkeypatch
    ):
        class _BrokenConnection:
            async def __aenter__(self) -> _BrokenConnection:
                raise RuntimeError("database unreachable")

            async def __aexit__(self, *_exc_info: Any) -> bool:
                return False

        class _BrokenEngine:
            def connect(self) -> _BrokenConnection:
                return _BrokenConnection()

        monkeypatch.setattr("app.observability.routes.engine", _BrokenEngine())

        response = test_client.get("/api/v1/health/detailed")

        assert response.status_code == 503
        payload = response.json()
        assert payload["status"] == "unhealthy"
        assert payload["database"]["connected"] is False
        assert REQUEST_ID_HEADER in response.headers
