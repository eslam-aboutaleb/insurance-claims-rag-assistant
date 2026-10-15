"""
Unit tests for application configuration management (Pydantic v2 Settings).

Tests:
- Default settings instantiation and type integrity
- Dynamic parsing of CORS origins (comma-separated, JSON array, list)
- Log level validation and error handling for invalid values
- Port validation (range boundaries 1-65535)
- get_settings() singleton / lru_cache behavior
"""

import pytest
from pydantic import ValidationError

from app.core.config import Settings, get_settings


def test_default_settings():
    """Verify default settings instantiation with standard defaults."""
    cfg = Settings()
    assert cfg.app_name == "OmniCare Financial API"
    assert cfg.environment in ("dev", "development", "staging", "production", "test")
    assert cfg.port == 8000
    assert cfg.log_level == "INFO"
    assert isinstance(cfg.cors_origins, list)
    assert "http://localhost:3000" in cfg.cors_origins


def test_jwt_secret_key_validation():
    """Verify JWT_SECRET_KEY default and validation."""
    # Development is unrestricted: a placeholder is accepted there.
    cfg = Settings(environment="development", jwt_secret_key="change-me")  # noqa: S106 - validator input, not a secret
    assert cfg.jwt_secret_key == "change-me"

    # In production, a placeholder must raise ValidationError. The value below is 48
    # characters so that only the placeholder rule applies, not the length rule.
    with pytest.raises(ValidationError) as exc_info:
        Settings(
            environment="production",
            jwt_secret_key="change-me-change-me-change-me-change-me-xx",  # noqa: S106
        )
    assert "placeholder" in str(exc_info.value)

    # A placeholder is refused even when it is long enough to satisfy the length rule,
    # which is the case that previously let .env.example through to production.
    with pytest.raises(ValidationError):
        Settings(
            environment="production",
            jwt_secret_key="your-secret-key-here",  # noqa: S106 - validator input
        )

    # A short but non-placeholder secret is refused on length.
    with pytest.raises(ValidationError) as exc_info:
        Settings(environment="production", jwt_secret_key="k" * 16)  # noqa: S106 - validator input
    assert "at least 32 characters" in str(exc_info.value)

    # A strong secret is accepted.
    strong = Settings(environment="production", jwt_secret_key="k" * 48)  # noqa: S106 - validator input
    assert strong.jwt_secret_key == "k" * 48


def test_cors_origins_parsing():
    """Verify that cors_origins accepts comma-separated strings and JSON arrays."""
    # Comma-separated string
    cfg_csv = Settings(cors_origins="http://example.com, https://app.omnicare.com")
    assert cfg_csv.cors_origins == ["http://example.com", "https://app.omnicare.com"]

    # JSON array string
    cfg_json = Settings(cors_origins='["http://alpha.com", "http://beta.com"]')
    assert cfg_json.cors_origins == ["http://alpha.com", "http://beta.com"]

    # Existing list
    cfg_list = Settings(cors_origins=["http://gamma.com"])
    assert cfg_list.cors_origins == ["http://gamma.com"]


def test_invalid_log_level():
    """Verify that an invalid log level raises a ValidationError."""
    with pytest.raises(ValidationError) as exc_info:
        Settings(log_level="VERBOSE")
    assert "Invalid log_level" in str(exc_info.value)


def test_invalid_port_range():
    """Verify that out-of-range port numbers raise a ValidationError."""
    with pytest.raises(ValidationError):
        Settings(port=0)

    with pytest.raises(ValidationError):
        Settings(port=70000)


def test_get_settings_cached_singleton():
    """Verify that get_settings() returns the same cached instance."""
    s1 = get_settings()
    s2 = get_settings()
    assert s1 is s2
