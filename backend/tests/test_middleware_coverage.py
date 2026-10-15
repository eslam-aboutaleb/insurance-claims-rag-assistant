"""Coverage tests for app.core.middleware uncovered paths."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock


class TestMiddleware:
    def test_request_size_limit_content_length_value_error(self):
        from app.core.middleware import RequestSizeLimitMiddleware

        middleware = RequestSizeLimitMiddleware(MagicMock(), max_upload_size=1024)
        request = MagicMock()
        request.headers.get.return_value = "not-a-number"
        request.body = AsyncMock(return_value=b"")

        async def call_next(req):
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            return mock_resp

        response = asyncio.run(middleware.dispatch(request, call_next))
        assert response.status_code == 200

    def test_request_size_limit_no_content_length_exceeds(self):
        from app.core.middleware import RequestSizeLimitMiddleware

        middleware = RequestSizeLimitMiddleware(MagicMock(), max_upload_size=10)
        request = MagicMock()
        request.headers.get.return_value = None
        request.body = AsyncMock(return_value=b"a" * 20)
        response = asyncio.run(middleware.dispatch(request, MagicMock()))
        assert response.status_code == 413

    def test_request_size_limit_exceeded_returns_413(self):
        from app.core.middleware import RequestSizeLimitMiddleware

        middleware = RequestSizeLimitMiddleware(MagicMock(), max_upload_size=10)
        request = MagicMock()
        request.headers.get.return_value = "999"
        request.body = AsyncMock(return_value=b"small")
        response = asyncio.run(middleware.dispatch(request, MagicMock()))
        assert response.status_code == 413
        data = json.loads(response.body)
        assert data["error"]["code"] == "PAYLOAD_TOO_LARGE"
