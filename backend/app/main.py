"""
FastAPI application entry point for OmniCare Financial Backend.

Configures the app with dynamic settings, CORS from environment variables,
lifespan events (database migrations and RAG ingestion on startup), and mounts
the v1 API router.
"""

import asyncio
import logging
import subprocess
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware

from app.agent.agent import configure_llm
from app.api.v1.router import router as v1_router
from app.config import get_settings
from app.middleware import RequestSizeLimitMiddleware
from app.domain.policies.ingestion import ingest_policy
from app.rate_limiter import limiter
from app.schemas.models import ErrorDetail, ErrorResponse

# Load centralized settings
settings = get_settings()

# Configure logging using centralized settings
logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


async def _run_alembic_migrations() -> None:
    """Apply pending Alembic migrations during application startup.

    Runs ``alembic upgrade head`` in a subprocess so that the migration
    environment can safely import application settings and models without
    conflicting with the running Uvicorn event loop.

    Raises:
        RuntimeError: If migrations fail. This causes the application to
            fail fast rather than running with a mismatched schema.
    """
    backend_dir = Path(__file__).resolve().parents[1]
    try:
        # Run in a worker thread: subprocess.run blocks for up to
        # the timeout, and this runs inside the async lifespan.
        result = await asyncio.to_thread(  # noqa: S603
            subprocess.run,
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            cwd=backend_dir,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        if result.returncode == 0:
            logger.info("Database migrations applied successfully.")
            if result.stdout.strip():
                logger.debug("Alembic output: %s", result.stdout.strip())
        else:
            error_msg = (
                "Alembic migration failed with exit code %s: %s",
                result.returncode,
                result.stderr.strip(),
            )
            logger.error(*error_msg)
            raise RuntimeError(f"Database migration failed: {result.stderr.strip()}")
    except Exception as e:
        logger.error("Failed to run database migrations: %s", e)
        raise RuntimeError(f"Failed to run database migrations: {e}") from e


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Application lifespan handler.
    On startup:
      - Applies pending database migrations via Alembic.
      - Configures LiteLLM environment variables.
      - Ingests the policy document into pgvector store (idempotent).
    On shutdown:
      - Gracefully terminates running sessions and resources.
    """
    logger.info(
        "Starting %s (Environment: %s, Version: %s)...",
        settings.app_name,
        settings.environment,
        settings.app_version,
    )

    await _run_alembic_migrations()

    # Configure LiteLLM environment variables from centralized settings
    try:
        configure_llm()
        logger.info("LiteLLM configuration applied")
    except Exception as e:
        logger.warning(f"LiteLLM configuration skipped: {e}")

    # Ingest policy documents into vector store on startup
    if settings.ingest_on_startup:
        try:
            count = await ingest_policy()
            logger.info(f"Policy ingestion complete: {count} chunks indexed")
        except Exception as e:
            logger.error(f"Policy ingestion failed: {e}")
            # Server continues running so claim status/submission endpoints remain available
    else:
        logger.info(
            "Startup ingestion disabled (INGEST_ON_STARTUP=false); "
            "serving the policy index already present in the database."
        )

    yield  # Server is running and receiving traffic

    logger.info(f"Shutting down {settings.app_name}...")
    from app.database import engine  # noqa: PLC0415

    await engine.dispose()


# Initialize the FastAPI application
app = FastAPI(
    title=settings.app_name,
    description=(
        "Production AI customer assistant API for OmniCare Financial. "
        "Supports grounded policy coverage Q&A (RAG), "
        "claim status lookup, and new claim submission."
    ),
    version=settings.app_version,
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
)


# Types that may appear in a Pydantic error ``ctx`` and are safe to pass through.
# Anything else is rendered with ``repr`` so an unexpected object cannot expand into a
# response body that dumps its attributes.
_JSON_SAFE_ERROR_TYPES = (str, int, float, bool, type(None))
_JSON_CONVERTED_ERROR_TYPES = (Decimal, UUID, datetime, date)


def _jsonify_validation_value(value: Any) -> Any:  # noqa: PLR0911
    """Convert one value from ``exc.errors()`` into a JSON-serialisable primitive.

    Each branch maps one input shape to its JSON form. The table is explicit so an
    unexpected type degrades to ``repr`` rather than being expanded attribute by
    attribute, which is what ``jsonable_encoder`` would do.
    """
    if isinstance(value, (bytes, bytearray)):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, _JSON_SAFE_ERROR_TYPES):
        return value
    if isinstance(value, Decimal):
        # Keep integral values as int so the wire format is unchanged for whole-number
        # constraints (0 stays 0, not 0.0); fractional constraints become float.
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, (UUID, datetime, date)):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _jsonify_validation_value(item) for key, item in value.items()}
    return (
        [_jsonify_validation_value(item) for item in value]
        if isinstance(value, (list, tuple, set))
        else repr(value)
    )


def _sanitize_error_detail(value: Any) -> Any:
    """Coerce a Pydantic error payload into JSON-serialisable primitives.

    ``exc.errors()`` carries raw constraint values inside ``ctx``, and those are not
    limited to JSON types: a ``gt`` constraint on a ``Decimal`` field puts
    ``Decimal("0")`` in ``ctx``, and a UUID constraint puts a ``UUID``. Embedding them
    verbatim makes ``JSONResponse`` raise ``TypeError``, which turns a validation failure
    into an opaque 500.

    This uses an explicit conversion table rather than ``jsonable_encoder`` on purpose.
    The encoder expands unknown objects through ``dict()`` then ``vars()``, so a custom
    validator's exception would be serialised attribute by attribute into the response
    body, and it still raises when neither succeeds -- reintroducing the very failure
    this exists to prevent.
    """
    return _jsonify_validation_value(value)


@app.exception_handler(RequestValidationError)
async def request_validation_exception_handler(
    request: Request,
    exc: RequestValidationError,
) -> JSONResponse:
    """
    Convert FastAPI/Pydantic RequestValidationError into a standardized
    ErrorResponse envelope with HTTP 422 Unprocessable Content.

    This ensures the actual response schema matches the OpenAPI declaration
    in the /chat endpoint (responses[422] = ErrorResponse).
    """
    logger.warning(
        "Validation error on %s %s: %s",
        request.method,
        request.url.path,
        _sanitize_error_detail(exc.errors()),
    )

    error = ErrorDetail(
        code="VALIDATION_ERROR",
        message="Request validation failed. See details for field-level errors.",
        details=_sanitize_error_detail(exc.errors()),
    )
    return JSONResponse(
        status_code=422,
        content=ErrorResponse(error=error).model_dump(),
    )


# Configure CORS dynamically from validated settings
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Register rate limit exception handler BEFORE middleware so it can catch
# RateLimitExceeded raised by SlowAPIMiddleware.
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# Register SlowAPIMiddleware so @limiter.limit(...) decorators actually enforce limits.
app.add_middleware(SlowAPIMiddleware)

app.add_middleware(RequestSizeLimitMiddleware, max_upload_size=5 * 1024 * 1024)

# Mount API v1 router
app.include_router(v1_router)


@app.exception_handler(HTTPException)
async def http_exception_handler(
    request: Request,
    exc: HTTPException,
) -> JSONResponse:
    """
    Convert all HTTPExceptions into a standardized ErrorResponse envelope.

    This ensures 401, 403, 404, 422, 500, etc. all return the same
    {"error": {"code": "...", "message": "...", "details": ...}} shape.
    """
    logger.warning(
        "HTTP %s on %s %s: %s",
        exc.status_code,
        request.method,
        request.url.path,
        exc.detail,
    )
    # Map status codes to error codes
    code_map = {
        400: "BAD_REQUEST",
        401: "UNAUTHORIZED",
        403: "FORBIDDEN",
        404: "NOT_FOUND",
        409: "CONFLICT",
        422: "VALIDATION_ERROR",
        429: "RATE_LIMIT_EXCEEDED",
        500: "INTERNAL_ERROR",
        503: "SERVICE_UNAVAILABLE",
    }
    error = ErrorDetail(
        code=code_map.get(exc.status_code, "HTTP_ERROR"),
        message=exc.detail if isinstance(exc.detail, str) else str(exc.detail),
        details=None,
    )
    return JSONResponse(
        status_code=exc.status_code,
        content=ErrorResponse(error=error).model_dump(),
    )
