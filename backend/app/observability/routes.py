"""Detailed health and telemetry endpoint.

This is a separate route rather than an extension of ``GET /api/v1/health``: that
response shape is pinned by the frozen contract suite, and a liveness probe must
not start failing because a telemetry sink is degraded. Everything reported here
is best-effort -- a sink that is unavailable is reported as degraded, never as a
reason to take the service out of rotation.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import UTC, datetime

from fastapi import APIRouter, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from app.database import async_session_factory, engine
from app.models.embedding_job import EmbeddingJob
from app.observability.metrics import DEFAULT_WINDOW_SECONDS, get_metrics

logger = logging.getLogger(__name__)

# Matches the timeout the liveness probe uses, so both probes give up equally
# fast on a wedged database.
_DB_PING_TIMEOUT_SECONDS = 1.0

# Querying through the ORM keeps the embedding-job schema the single source of
# truth, so a column rename in the model shows up here at import time rather than
# as a runtime SQL error on a route only an operator hits.
_DEPTH_BY_STATUS = select(EmbeddingJob.status, func.count()).group_by(EmbeddingJob.status)
_OLDEST_PENDING = select(func.min(EmbeddingJob.created_at)).where(EmbeddingJob.status == "pending")


class DatabaseMetrics(BaseModel):
    """Database reachability for this probe.

    Attributes:
        connected: Whether the probe could run ``SELECT 1``.
        latency_ms: Round-trip time of the probe in milliseconds.
    """

    connected: bool = Field(description="Whether the database answered the probe.")
    latency_ms: float = Field(default=0.0, ge=0, description="Probe round-trip time.")


class LlmMetrics(BaseModel):
    """Model-call volume and spend over the reporting window.

    Attributes:
        window_seconds: Length of the window these totals cover.
        call_count: Completed model calls, successful and failed.
        error_count: Model calls that raised.
        prompt_tokens: Input tokens billed across the window.
        completion_tokens: Output tokens generated across the window.
        total_cost_usd: Dollar cost across the window.
        avg_latency_ms: Mean call duration in milliseconds.
        calls_by_model: Call counts keyed by model identifier.
    """

    window_seconds: float = Field(description="Window these totals were measured over.")
    call_count: int = Field(description="Model calls recorded in the window.")
    error_count: int = Field(description="Model calls that raised in the window.")
    prompt_tokens: int = Field(description="Input tokens billed in the window.")
    completion_tokens: int = Field(description="Output tokens generated in the window.")
    total_cost_usd: float = Field(default=0.0, description="Dollar cost over the window.")
    avg_latency_ms: float = Field(default=0.0, ge=0, description="Mean call duration.")
    calls_by_model: dict[str, int] = Field(
        default_factory=dict, description="Call counts keyed by model identifier."
    )


class ToolMetrics(BaseModel):
    """Invocation counts for a single agent tool.

    Attributes:
        name: Registered tool name.
        call_count: Invocations in the window.
        error_count: Invocations that raised in the window.
        avg_latency_ms: Mean invocation duration in milliseconds.
    """

    name: str = Field(description="Registered tool name.")
    call_count: int = Field(description="Invocations in the window.")
    error_count: int = Field(description="Invocations that raised in the window.")
    avg_latency_ms: float = Field(default=0.0, ge=0, description="Mean invocation duration.")


class ToolSummary(BaseModel):
    """Per-tool latency and outcome totals for the reporting window.

    Attributes:
        window_seconds: Length of the window these totals cover.
        total_calls: Tool invocations across every tool.
        error_count: Tool invocations that raised.
        by_tool: One entry per tool, ordered by tool name.
    """

    window_seconds: float = Field(description="Window these totals were measured over.")
    total_calls: int = Field(description="Tool invocations in the window.")
    error_count: int = Field(description="Tool invocations that raised in the window.")
    by_tool: list[ToolMetrics] = Field(default_factory=list, description="Per-tool breakdown.")


class OutboxMetrics(BaseModel):
    """Embedding outbox health.

    Attributes:
        depth_by_status: Job counts keyed by job status.
        oldest_pending_age_seconds: Age of the oldest pending job, or ``None``
            when the queue has nothing pending.
    """

    depth_by_status: dict[str, int] = Field(default_factory=dict, description="Jobs by status.")
    oldest_pending_age_seconds: float | None = Field(
        default=None, ge=0, description="Age of the oldest pending job in seconds."
    )


class RateLimitMetrics(BaseModel):
    """Requests the rate limiter refused over the reporting window.

    Rejections are the visible symptom of either abuse or a limit set too tight,
    so they belong beside the spend and latency figures an operator reads when
    the service looks unhealthy.

    Attributes:
        window_seconds: Length of the window this count covers.
        rejections: Refused requests in the window.
        by_route: Rejection counts keyed by request path.
    """

    window_seconds: float = Field(description="Window this count was measured over.")
    rejections: int = Field(description="Requests refused by the rate limiter.")
    by_route: dict[str, int] = Field(
        default_factory=dict, description="Rejections keyed by request path."
    )


class DetailedHealthResponse(BaseModel):
    """Response body for ``GET /api/v1/health/detailed``.

    Attributes:
        status: ``"healthy"`` when the database and the outbox are both
            readable, ``"degraded"`` when only the database is, and
            ``"unhealthy"`` when neither is.
        database: Database probe result.
        llm: Model-call volume and spend.
        tools: Tool latency and outcome totals.
        outbox: Embedding outbox depth and backlog age.
        rate_limits: Requests the rate limiter refused.
    """

    status: str = Field(description="Aggregate status of the reported dependencies.")
    database: DatabaseMetrics = Field(description="Database probe result.")
    llm: LlmMetrics = Field(description="Model-call volume and spend.")
    tools: ToolSummary = Field(description="Tool latency and outcome totals.")
    outbox: OutboxMetrics = Field(description="Embedding outbox depth and backlog age.")
    rate_limits: RateLimitMetrics = Field(description="Rate-limit rejections.")


async def _ping_database() -> DatabaseMetrics:
    """Probe the database and measure how long it took.

    Returns:
        DatabaseMetrics: The probe result, with ``connected`` false when the
        probe raised or timed out.
    """
    started = time.perf_counter()

    async def _probe() -> None:
        async with engine.connect() as connection:
            await connection.execute(select(1))

    try:
        await asyncio.wait_for(_probe(), timeout=_DB_PING_TIMEOUT_SECONDS)
    except Exception as exc:
        logger.warning("Detailed health check: database probe failed: %s", exc)
        return DatabaseMetrics(connected=False)
    latency_ms = round((time.perf_counter() - started) * 1000, 3)
    return DatabaseMetrics(connected=True, latency_ms=latency_ms)


def _age_seconds(timestamp: datetime | None) -> float | None:
    """Turn a stored timestamp into an age in seconds.

    Args:
        timestamp: The value read back from PostgreSQL, or ``None``.

    Returns:
        float | None: Seconds elapsed, or ``None`` when there was no row.
    """
    if timestamp is None:
        return None
    if timestamp.tzinfo is None:
        # The column is timezone-aware, so a naive value means the driver handed
        # back UTC without an offset; treating it as local would skew the age.
        timestamp = timestamp.replace(tzinfo=UTC)
    return max((datetime.now(UTC) - timestamp).total_seconds(), 0.0)


async def _read_outbox() -> OutboxMetrics | None:
    """Read the embedding outbox depth per status and the oldest pending age.

    Returns:
        OutboxMetrics | None: The queue depth and backlog age, or ``None`` when
        the query failed. ``None`` is distinct from an empty depth: a failed read
        is reported as degraded, an empty queue is not.
    """
    try:
        async with async_session_factory() as session:
            depth_rows = (await session.execute(_DEPTH_BY_STATUS)).all()
            oldest_created_at = (await session.execute(_OLDEST_PENDING)).scalar_one_or_none()
    except Exception as exc:
        logger.warning("Detailed health check: outbox read failed: %s", exc)
        return None

    depth = {str(job_status): int(count) for job_status, count in depth_rows}
    return OutboxMetrics(
        depth_by_status=depth, oldest_pending_age_seconds=_age_seconds(oldest_created_at)
    )


router = APIRouter(prefix="/api/v1", tags=["Health"])


@router.get(
    "/health/detailed",
    response_model=DetailedHealthResponse,
    status_code=status.HTTP_200_OK,
    summary="Detailed Health and Telemetry",
    description=(
        "Returns database reachability plus operational telemetry: model-call "
        "count and spend over the last hour, tool latency and outcomes, and the "
        "embedding outbox depth with the age of its oldest pending job."
    ),
    responses={
        status.HTTP_200_OK: {
            "description": (
                "Telemetry was collected. A non-'healthy' status still returns 200; "
                "read the status field for which dependency is degraded."
            ),
            "model": DetailedHealthResponse,
        },
        status.HTTP_503_SERVICE_UNAVAILABLE: {
            "description": "The database is unreachable, so no telemetry was collected.",
            "model": DetailedHealthResponse,
        },
    },
)
async def detailed_health_check(response: Response) -> DetailedHealthResponse:
    """Collect operational telemetry for the running service.

    The metrics store is in-process and the database probe is bounded by a
    timeout, so dependency problems are reported in the body rather than raised;
    only an unreachable database produces a non-200 status.

    Args:
        response: FastAPI response object, used to set 503 when the database is
            unreachable.

    Returns:
        DetailedHealthResponse: Database probe result, LLM call count and cost
        over the window, tool latency and outcome counts, and outbox depth with
        the oldest pending job age.
    """
    snapshot = get_metrics().snapshot(DEFAULT_WINDOW_SECONDS)
    llm = LlmMetrics.model_validate(snapshot["llm"])
    tools = ToolSummary.model_validate(snapshot["tools"])
    rate_limits = RateLimitMetrics.model_validate(snapshot["rate_limits"])

    database = await _ping_database()
    if not database.connected:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return DetailedHealthResponse(
            status="unhealthy",
            database=database,
            llm=llm,
            tools=tools,
            outbox=OutboxMetrics(),
            rate_limits=rate_limits,
        )

    outbox = await _read_outbox()
    return DetailedHealthResponse(
        # The metrics store and the probe are in-process and cannot fail here, so
        # only the outbox read can leave this endpoint with a partial answer.
        status="degraded" if outbox is None else "healthy",
        database=database,
        llm=llm,
        tools=tools,
        outbox=outbox or OutboxMetrics(),
        rate_limits=rate_limits,
    )
