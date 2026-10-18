"""
Application configuration using pydantic-settings (Pydantic v2).
Loads environment variables from .env file with strict typing and sensible defaults.
"""

import json
import os
import secrets
import socket
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """
    Application settings loaded from environment variables and .env file.
    All properties are type-safe and validated upon initialization.
    """

    # Application Metadata
    app_name: str = Field(
        default="OmniCare Financial API",
        description="Public API application title",
    )
    app_version: str = Field(
        default="1.0.0",
        description="Semantic application release version",
    )
    environment: Literal["development", "staging", "production", "test"] = Field(
        default="development",
        description="Runtime environment tier",
    )

    # Logging Configuration
    log_level: str = Field(
        default="INFO",
        description="Standard logging verbosity level",
    )

    # LLM & Agent Configuration
    openai_api_key: str = Field(
        default="",
        description="OpenAI API key used by LiteLLM for LLM inferences",
    )
    openai_api_base: str = Field(
        default="",
        description="Optional OpenAI API base URL override for LiteLLM routing",
    )
    llm_model: str = Field(
        default="openai/gpt-4o-mini",
        description="Provider-prefixed model identifier routed via LiteLLM",
    )

    # Embedding Model Configuration
    embedding_model: str = Field(
        default="text-embedding-3-small",
        description="OpenAI embedding model name used by the embedding function",
    )
    embedding_provider: str = Field(
        default="litellm",
        description=(
            "Embedding provider selected through the ragnor embedding "
            "registry; 'litellm' is the default."
        ),
    )
    embedding_drain_interval_seconds: float = Field(
        default=5.0,
        gt=0.1,
        description=("Idle sleep of the embedding drainer between empty passes."),
    )
    embedding_drain_batch_size: int = Field(
        default=10,
        gt=0,
        description="Number of embedding jobs the drainer claims per pass.",
    )
    job_stale_after_seconds: float = Field(
        default=300.0,
        gt=0,
        description=(
            "Seconds after which a job stuck in 'processing' is considered "
            "abandoned by a dead worker and reclaimed to 'pending'."
        ),
    )
    notification_drain_interval_seconds: float = Field(
        default=5.0,
        gt=0.1,
        description=("Idle sleep of the notification drainer between empty passes."),
    )
    notification_drain_batch_size: int = Field(
        default=10,
        gt=0,
        description="Number of notification jobs the drainer delivers per pass.",
    )
    worker_id: str = Field(
        default_factory=lambda: f"{socket.gethostname()}-{os.getpid()}",
        description=(
            "Identity this process stamps onto embedding jobs it claims. "
            "Defaults to a per-process host-pid value so concurrent drainers "
            "are distinguishable in locked_by."
        ),
    )
    ingest_on_startup: bool = Field(
        default=True,
        description=(
            "Whether to index the policy documents during application startup. "
            "Defaults to True so a fresh deployment is immediately queryable. Set to "
            "False to start the API against a database that is already indexed, "
            "which avoids paying the ingestion cost on every boot and on every test "
            "run."
        ),
    )

    # RAG Configuration
    rag_distance_threshold: float = Field(
        default=1.3,
        description="Maximum L2 distance for RAG vector search (lower = stricter matching)",
    )

    # Vector Store Configuration
    vector_store_provider: str = Field(
        default="pgvector",
        description="Vector store provider to use (pgvector, chroma, etc.)",
    )

    # Mock Data Store Paths
    policy_file_path: str = Field(
        default=str(Path(__file__).resolve().parents[1] / "data" / "sample_policy.md"),
        description="Filesystem path to master policy markdown document",
    )

    # Server Network Binding
    host: str = Field(
        default="0.0.0.0",  # noqa: S104 -- intentional; container/service deployments bind all interfaces
        description="Host interface to bind the Uvicorn server",
    )
    port: int = Field(
        default=8000,
        ge=1,
        le=65535,
        description="Network port to bind the Uvicorn server (1-65535)",
    )

    # CORS Allowed Origins
    cors_origins: list[str] = Field(
        default_factory=lambda: [
            "http://localhost:3000",
            "http://localhost:3002",
            "http://127.0.0.1:3000",
        ],
        description="Allowed CORS origin URLs for browser frontend integration",
    )

    # Authentication
    jwt_secret_key: str = Field(
        default_factory=lambda: secrets.token_urlsafe(32),
        description="Secret key for signing JWT tokens. Auto-generated in development if not set.",
    )

    # Database
    database_url: str = Field(
        description="SQLAlchemy async PostgreSQL database URL",
    )

    # Observability Configuration
    observability_enabled: bool = Field(
        default=True,
        description=(
            "Master switch for the observability hooks: the request-id correlation "
            "middleware, structured log records, per-tool latency instrumentation, and "
            "the telemetry reported by GET /api/v1/health/detailed. Disabling it removes "
            "the hooks themselves rather than only silencing their output, so a "
            "deployment that has chosen a different observability stack pays nothing."
        ),
    )
    llm_cost_tracking_enabled: bool = Field(
        default=True,
        description=(
            "Whether the LiteLLM callback that records tokens, dollar cost, and latency "
            "for every model call is registered on startup. The per-call figures are the "
            "unit economics of the product: without them there is no way to see what a "
            "conversation costs. Disable only when an external tracer already collects "
            "the same data."
        ),
    )
    # LangSmith tracing.
    # Off by default for two reasons: it ships prompts and responses to a
    # third-party service, and it needs a key to do anything at all. With it on
    # and no key configured, tracing stays off and startup says so, so a
    # Deployment that means to trace cannot silently run untraced.
    langsmith_tracing_enabled: bool = Field(
        default=False,
        description=(
            "Whether agent turns are traced to LangSmith: one trace per turn with the "
            "model calls and tool calls it made nested under it, viewable at "
            "smith.langchain.com. Off by default; requires LANGSMITH_API_KEY to take "
            "effect, and is gated by OBSERVABILITY_ENABLED like every other hook."
        ),
    )
    langsmith_api_key: str = Field(
        default="",
        description=(
            "LangSmith API key (starts with 'lsv2_'). Created under Settings > API Keys "
            "in the LangSmith app. Blank leaves tracing off even when "
            "LANGSMITH_TRACING_ENABLED=true."
        ),
    )
    langsmith_project: str = Field(
        default="omnicare-financial",
        description=(
            "LangSmith project (the traces' grouping inside the workspace) that runs "
            "are reported to. Created on demand by LangSmith on the first traced run."
        ),
    )
    langsmith_endpoint: str = Field(
        default="",
        description=(
            "LangSmith API base URL. Blank uses the LangSmith cloud "
            "(https://api.smith.langchain.com); set it to the URL of a self-hosted or "
            "region-specific deployment."
        ),
    )
    langsmith_workspace_id: str = Field(
        default="",
        description=(
            "Optional LangSmith workspace (organization) id for keys scoped to one "
            "workspace of a multi-workspace organization. Blank works for the common "
            "single-workspace case."
        ),
    )
    log_format: Literal["json", "text"] = Field(
        default="json",
        description=(
            "Log record encoding. 'json' writes one JSON object per line with the "
            "correlation id as a field, which is what a log aggregator expects and what "
            "makes a single request traceable across services; 'text' keeps the original "
            "single-line layout for reading logs by eye during development."
        ),
    )

    # Distributed State
    # Redis is the shared store that makes two process-local caches -- the idempotency
    # cache and the rate-limit counters -- correct when more than one replica serves
    # traffic. Every setting below falls back to the original in-process behaviour when
    # Redis is not configured, so local development and the test suite are unaffected.
    redis_url: str = Field(
        default="",
        description=(
            "Redis connection URI used by the distributed stores, for example "
            "``redis://redis:6379/0``. An empty value means no Redis is configured and "
            "every store below silently keeps its in-process behaviour; set it together "
            "with IDEMPOTENCY_STORAGE and/or RATE_LIMIT_STORAGE to make those stores "
            "global across replicas."
        ),
    )
    redis_connect_timeout_seconds: float = Field(
        default=1.0,
        gt=0,
        description=(
            "Seconds to wait for a Redis TCP connection before giving up. Kept short "
            "because a Redis that is not answering is treated as 'unavailable' and the "
            "store falls back to in-process state, so a long timeout would only stall "
            "requests."
        ),
    )
    redis_socket_timeout_seconds: float = Field(
        default=2.0,
        gt=0,
        description=(
            "Seconds to wait for a Redis reply before the command is abandoned and the "
            "store falls back to in-process state."
        ),
    )
    idempotency_storage: Literal["memory", "redis"] = Field(
        default="memory",
        description=(
            "Where successful chat responses are cached for replay. 'memory' keeps the "
            "cache in the process (correct for uvicorn --workers 1, which is the default "
            "here); 'redis' shares one cache across every replica so a retry replays "
            "regardless of which instance receives it. 'redis' without a configured "
            "REDIS_URL, and any Redis outage, degrade to 'memory' rather than failing "
            "the request."
        ),
    )
    rate_limit_storage: Literal["memory", "redis"] = Field(
        default="memory",
        description=(
            "Where SlowAPI keeps rate-limit counters. 'memory' counts per process, so N "
            "replicas allow N x the configured quota; 'redis' makes the 20/minute chat "
            "limit global. 'redis' without a configured REDIS_URL, and any Redis outage, "
            "degrade to 'memory' -- the limiter fails open so a Redis problem cannot take "
            "the chat endpoints down with it."
        ),
    )

    # Session Storage
    session_storage: Literal["memory", "database"] = Field(
        default="memory",
        description=(
            "Where the ADK agent keeps conversation sessions. 'memory' keeps them in the "
            "process and loses them on restart; 'database' stores them in the "
            "``agent_sessions`` and ``agent_session_events`` tables of the same PostgreSQL "
            "database, so any replica can continue any conversation and a restart no "
            "longer forgets an in-flight discussion. The default is 'memory' because a "
            "single-process deployment gains nothing from the database round-trips. "
            "Switching a running deployment from 'memory' to 'database' abandons the "
            "sessions the process was holding: a one-time, acceptable loss, since the "
            "turns themselves are already persisted in ``conversation_messages``."
        ),
    )
    session_history_turns: int = Field(
        default=20,
        gt=0,
        description=(
            "Number of most-recent persisted conversation turns replayed into the model's "
            "history when the ADK session starts empty (a fresh session, a new replica, "
            "or the first turn after the storage swap). Older turns are covered by the "
            "compaction summary instead of being replayed in full."
        ),
    )
    session_history_compaction_turns: int = Field(
        default=60,
        gt=0,
        description=(
            "Number of older turns folded into the single compaction summary that precedes "
            "the replayed history, so a long conversation stays inside the model's context "
            "window without discarding that it happened."
        ),
    )

    # Agent prompt and sampling
    # The prompt the agent runs with lives with the agent's behaviour in
    # ``app.agent.prompts``; these settings override it, and tune sampling, for a
    # deployment without a code change. Both sampling knobs default to unset so
    # the model provider's own defaults apply unless asked otherwise.
    system_prompt: str = Field(
        default="",
        description=(
            "System instruction the agent runs with. Empty runs the reviewed prompt "
            "from app.agent.prompts, which is the version that belongs to the agent; "
            "set this to override it for a deployment."
        ),
    )
    llm_temperature: float | None = Field(
        default=None,
        description=(
            "Sampling temperature for the agent's model calls. Unset (the default) "
            "leaves the provider's own default in place."
        ),
    )
    llm_max_tokens: int | None = Field(
        default=None,
        gt=0,
        description=(
            "Maximum tokens per agent model call. Unset (the default) leaves the "
            "provider's own default in place."
        ),
    )

    @property
    def redis_enabled(self) -> bool:
        """Whether a Redis URI is configured.

        Shortcut for callers that should not re-parse the URI, and the single guard that
        keeps a store from trying to connect when no Redis was deployed at all.
        """
        return bool(self.redis_url.strip())

    @field_validator("cors_origins", mode="before")
    @classmethod
    def assemble_cors_origins(cls, value: Any) -> list[str]:
        """Supports comma-separated strings, JSON arrays, or existing lists."""
        if isinstance(value, str):
            # Check for comma-delimited string
            value = value.strip()
            if value.startswith("[") and value.endswith("]"):
                try:
                    return json.loads(value)
                except json.JSONDecodeError:
                    pass
            return [item.strip() for item in value.split(",") if item.strip()]
        if isinstance(value, list | tuple):
            return [str(item).strip() for item in value if str(item).strip()]
        return []

    @field_validator("log_level")
    @classmethod
    def validate_log_level(cls, value: str) -> str:
        """Validates that log level is an accepted standard logging level."""
        valid_levels = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        upper_val = value.strip().upper()
        if upper_val not in valid_levels:
            raise ValueError(
                f"Invalid log_level '{value}'. Must be one of: {', '.join(sorted(valid_levels))}"
            )
        return upper_val

    @field_validator("jwt_secret_key")
    @classmethod
    def validate_jwt_secret_key(cls, value: str, info: Any) -> str:
        """Reject a placeholder or weak JWT secret in production.

        This is an authentication-bypass gate, so it is deliberately stricter than a
        single sentinel check. It rejects:

        * every placeholder value shipped in ``.env.example``, so copying that file and
          setting ``ENVIRONMENT=production`` cannot start the service with a publicly
          known HMAC key;
        * any secret shorter than 32 characters, which is too weak to sign tokens with.
        """
        environment = info.data.get("environment", "development")
        if environment != "production":
            return value

        normalised = value.strip().lower()
        placeholders = {
            "change-me",
            "your-secret-key-here",
            "your_secret_key_here",
            "changeme",
            "secret",
            "password",
            "string",
        }
        if normalised in placeholders or "change-me" in normalised:
            raise ValueError(
                "JWT_SECRET_KEY is still a placeholder. Generate one with "
                '`python -c "import secrets; print(secrets.token_urlsafe(32))"` '
                "before deploying to production."
            )
        if len(value) < 32:
            raise ValueError(
                f"JWT_SECRET_KEY must be at least 32 characters in production; got {len(value)}."
            )
        return value

    @field_validator("cors_origins")
    @classmethod
    def forbid_wildcard_origin(cls, value: list[str]) -> list[str]:
        """Reject wildcard CORS origins when credentials are allowed."""
        if "*" in value:
            raise ValueError(
                "Wildcard origin '*' is not allowed when allow_credentials=True. "
                "Specify explicit origins instead."
            )
        return value

    model_config = SettingsConfigDict(
        # Resolve .env relative to this file's project root so settings load
        # consistently regardless of the current working directory.
        env_file=str(Path(__file__).resolve().parents[2] / ".env"),
        env_file_encoding="utf-8",
        # Unknown env vars are silently ignored so deployments can pass
        # extra infrastructure variables without breaking startup.
        extra="ignore",
        case_sensitive=False,
    )


@lru_cache
def get_settings() -> Settings:
    """
    Returns a cached singleton instance of Settings.
    Supports dependency injection in FastAPI routers via Depends(get_settings).
    """
    return Settings()


# Singleton settings instance for backward compatibility with direct imports
settings = get_settings()
