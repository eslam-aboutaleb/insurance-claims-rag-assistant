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
            "Embedding provider selected through the ragkit embedding "
            "registry; 'litellm' is the historical behavior."
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
        default=str(Path(__file__).parent / "data" / "sample_policy.md"),
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
