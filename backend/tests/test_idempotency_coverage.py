"""Coverage tests for app.core.idempotency uncovered paths."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class TestIdempotency:
    def test_idempotent_endpoint_body_mismatch_returns_409(self, test_client, mock_current_user):
        from app.core.idempotency import clear_cache, store_response

        clear_cache()
        key = "00000000-0000-0000-0000-000000000001:test-key-bm"
        store_response(key, {"response": "cached", "request_hash": "hash1"}, "hash1")

        response = test_client.post(
            "/api/v1/chat",
            json={"message": "body-mismatch-test-xyz"},
            headers={**mock_current_user, "Idempotency-Key": "test-key-bm"},
        )
        assert response.status_code == 409

    def test_idempotent_endpoint_no_key_calls_directly(self, test_client, mock_current_user):
        from app.main import app

        app.state.limiter.enabled = False
        try:
            with patch("app.api.v1.chat.run_agent", new_callable=AsyncMock) as mock_run:
                mock_run.return_value = {
                    "response": "ok",
                    "sources": [],
                    "tool_calls": [],
                    "session_id": "s1",
                }
                response = test_client.post(
                    "/api/v1/chat",
                    json={"message": "no idempotency key"},
                    headers=mock_current_user,
                )
            assert response.status_code == 200
        finally:
            app.state.limiter.enabled = True

    @pytest.mark.asyncio
    async def test_idempotency_else_branch_direct(self):
        from app.core.idempotency import clear_cache, idempotent_endpoint

        clear_cache()
        call_count = 0

        @idempotent_endpoint()
        async def mock_endpoint(
            payload=None, response=None, current_user_id=None, idempotency_key=None
        ):
            nonlocal call_count
            call_count += 1
            return {"result": "ok"}

        result = await mock_endpoint(payload=None, response=None, current_user_id=None)
        assert result == {"result": "ok"}
        assert call_count == 1

    @pytest.mark.asyncio
    async def test_idempotency_store_response_dict_without_model_dump(self):
        from app.core.idempotency import clear_cache, idempotent_endpoint

        clear_cache()
        call_count = 0
        mock_response = MagicMock()
        mock_response.headers = {}

        @idempotent_endpoint()
        async def mock_endpoint(
            payload=None, response=None, current_user_id=None, idempotency_key=None
        ):
            nonlocal call_count
            call_count += 1
            return {"result": "ok"}

        payload = {"message": "test"}
        result = await mock_endpoint(
            payload=payload,
            response=mock_response,
            current_user_id="user-1",
            idempotency_key="key-1",
        )
        assert result == {"result": "ok"}
        assert call_count == 1
        assert mock_response.headers["X-Idempotency-Key"] == "key-1"

    @pytest.mark.asyncio
    async def test_idempotency_store_response_model_with_model_dump(self):
        from app.core.idempotency import clear_cache, idempotent_endpoint

        clear_cache()
        call_count = 0
        mock_response = MagicMock()
        mock_response.headers = {}

        class MockModel:
            def model_dump(self):
                return {"result": "ok"}

        @idempotent_endpoint()
        async def mock_endpoint(
            payload=None, response=None, current_user_id=None, idempotency_key=None
        ):
            nonlocal call_count
            call_count += 1
            return MockModel()

        payload = {"message": "test"}
        await mock_endpoint(
            payload=payload,
            response=mock_response,
            current_user_id="user-1",
            idempotency_key="key-2",
        )
        assert call_count == 1
        assert mock_response.headers["X-Idempotency-Key"] == "key-2"
