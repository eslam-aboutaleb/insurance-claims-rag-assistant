"""Coverage tests for app.core.rate_limiter uncovered paths."""

from __future__ import annotations

import uuid
from unittest.mock import MagicMock, patch


class TestRateLimiter:
    def test_rate_limiter_cookie_token_fallback(self):
        from app.core.rate_limiter import _user_or_ip_key_func

        request = MagicMock()
        request.headers.get.return_value = None
        request.cookies.get.return_value = "valid-jwt-token"
        with patch(
            "app.security.auth._decode_token",
            return_value=uuid.UUID("00000000-0000-0000-0000-000000000001"),
        ):
            key = _user_or_ip_key_func(request)
        assert key == "user:00000000-0000-0000-0000-000000000001"

    def test_rate_limiter_no_token_falls_back_to_ip(self):
        from app.core.rate_limiter import _user_or_ip_key_func

        request = MagicMock()
        request.headers.get.return_value = None
        request.cookies.get.return_value = None
        with patch("app.core.rate_limiter.get_remote_address", return_value="127.0.0.1"):
            key = _user_or_ip_key_func(request)
        assert key == "ip:127.0.0.1"
