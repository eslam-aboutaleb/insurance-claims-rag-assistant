"""Coverage tests for app.core.config uncovered paths."""

from __future__ import annotations

import pytest


class TestConfig:
    def test_cors_origins_comma_separated_string(self):
        from app.core.config import Settings

        settings = Settings(cors_origins="http://a.com, http://b.com")
        assert "http://a.com" in settings.cors_origins
        assert "http://b.com" in settings.cors_origins

    def test_cors_origins_json_array_string(self):
        from app.core.config import Settings

        settings = Settings(cors_origins='["http://a.com", "http://b.com"]')
        assert "http://a.com" in settings.cors_origins
        assert "http://b.com" in settings.cors_origins

    def test_cors_origins_wildcard_rejected(self):
        from app.core.config import Settings

        with pytest.raises(ValueError, match="Wildcard origin"):
            Settings(cors_origins=["*"])

    def test_cors_origins_list_tuple_accepted(self):
        from app.core.config import Settings

        settings = Settings(cors_origins=["http://a.com", "http://b.com"])
        assert "http://a.com" in settings.cors_origins

    def test_cors_origins_invalid_json_falls_back_to_comma_split(self):
        from app.core.config import Settings

        settings = Settings(cors_origins='[not-valid-json, "http://a.com"]')
        assert any("http://a.com" in item for item in settings.cors_origins)

    def test_cors_origins_non_list_returns_empty(self):
        from app.core.config import Settings

        settings = Settings(cors_origins=123)
        assert settings.cors_origins == []
