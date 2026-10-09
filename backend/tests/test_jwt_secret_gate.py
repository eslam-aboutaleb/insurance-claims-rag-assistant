"""Tests for the production JWT secret gate.

A weak or placeholder signing key is an authentication bypass: anyone who knows it can
mint a token for any subject and act as any user. These tests pin the gate that prevents
a deployment from starting with one.
"""

import pytest
from pydantic import ValidationError

from app.config import Settings


def _production_settings(**overrides) -> Settings:
    return Settings(environment="production", **overrides)


def test_generated_secret_is_accepted_in_production() -> None:
    """A random 32+ character secret passes."""
    settings = _production_settings(jwt_secret_key="k" * 48)  # noqa: S106 - validator input
    assert settings.jwt_secret_key == "k" * 48


@pytest.mark.parametrize(
    "placeholder",
    [
        "change-me",
        "CHANGE-ME",
        "your-secret-key-here",
        "your_secret_key_here",
        "changeme",
        "secret",
        "password",
    ],
)
def test_every_shipped_placeholder_is_rejected_in_production(placeholder: str) -> None:
    """Each placeholder from ``.env.example`` must be refused in production.

    Previously only ``change-me`` was checked while ``.env.example`` shipped
    ``your-secret-key-here``, so copying the example file and flipping ENVIRONMENT to
    production started the service with a publicly known HMAC key.
    """
    with pytest.raises(ValidationError):
        _production_settings(jwt_secret_key=placeholder)


def test_short_secret_is_rejected_in_production() -> None:
    """A secret too short to sign with is refused even if it is not a known placeholder."""
    with pytest.raises(ValidationError, match="at least 32 characters"):
        _production_settings(jwt_secret_key="a" * 16)


def test_development_allows_the_placeholder() -> None:
    """Development stays frictionless; only production is gated."""
    placeholder = "your-secret-key-here"
    settings = Settings(environment="development", jwt_secret_key=placeholder)
    assert settings.jwt_secret_key == placeholder


def test_env_example_placeholder_is_covered_by_the_validator() -> None:
    """The placeholder shipped in ``.env.example`` is one the validator rejects.

    Guards against the two drifting apart again, which is how the bypass arose.
    """
    from pathlib import Path

    # Locate .env.example by walking up from this test file. The repo layout
    # differs between the host (backend/tests/... -> repo root) and the Docker
    # image (/app/tests/... -> /app), so a fixed parents[2] is fragile.
    example = None
    for candidate in Path(__file__).resolve().parents:
        candidate = candidate / ".env.example"
        if candidate.is_file():
            example = candidate
            break
    assert example, ".env.example not found by walking up from this test file"
    placeholder = None
    for raw in example.read_text(encoding="utf-8").splitlines():
        if raw.strip().startswith("JWT_SECRET_KEY="):
            placeholder = raw.partition("=")[2].strip()
            break

    assert placeholder, "JWT_SECRET_KEY must be present in .env.example"

    with pytest.raises(ValidationError):
        _production_settings(jwt_secret_key=placeholder)
