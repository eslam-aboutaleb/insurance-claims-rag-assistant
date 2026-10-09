"""Coverage tests for app.auth uncovered paths."""

from __future__ import annotations

import uuid
import jwt
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from app.auth import (
    SECRET_KEY,
    _InvalidTokenError,
    _decode_token,
    get_current_user,
)


class TestAuthModule:
    def test_decode_token_malformed_subject_raises_invalid_token(self):
        token = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ0ZXN0In0.bad"
        with pytest.raises(_InvalidTokenError):
            _decode_token(token)

    def test_decode_token_malformed_subject_directly(self):
        payload = {"sub": "not-a-uuid", "exp": 9999999999}
        token = jwt.encode(payload, SECRET_KEY, algorithm="HS256")
        with pytest.raises(_InvalidTokenError, match="malformed-subject"):
            _decode_token(token)

    def test_get_current_user_clears_cookie_for_nonexistent_user(self, test_client):
        with pytest.raises(_InvalidTokenError):
            _decode_token("invalid-token")

        response = test_client.get(
            "/api/v1/auth/me",
            headers={"Authorization": "Bearer invalid-token"},
        )
        assert response.status_code == 401

    def test_get_current_user_clears_cookie_when_user_missing(self, test_client):
        nonexistent_uuid = str(uuid.uuid4())
        payload = {"sub": nonexistent_uuid, "exp": 9999999999}
        token = jwt.encode(payload, SECRET_KEY, algorithm="HS256")

        response = test_client.get(
            "/api/v1/auth/me",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_get_current_user_account_not_found_clears_cookie(self):
        nonexistent_uuid = str(uuid.uuid4())
        payload = {"sub": nonexistent_uuid, "exp": 9999999999}
        token = jwt.encode(payload, SECRET_KEY, algorithm="HS256")

        mock_session = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_session.execute = AsyncMock(return_value=mock_result)

        mock_response = MagicMock()
        mock_credentials = MagicMock()
        mock_credentials.credentials = token

        with patch("app.auth.clear_session_cookie") as mock_clear:
            try:
                await get_current_user(
                    request=MagicMock(),
                    credentials=mock_credentials,
                    session=mock_session,
                    response=mock_response,
                )
            except HTTPException as exc:
                assert exc.status_code == 401
                assert "Account not found" in exc.detail
                mock_clear.assert_called_once_with(mock_response)
            else:
                pytest.fail("Expected HTTPException")

    @pytest.mark.asyncio
    async def test_get_current_user_returns_user_id_when_found(self):
        from app.models import User

        user_uuid = uuid.uuid4()
        payload = {"sub": str(user_uuid), "exp": 9999999999}
        token = jwt.encode(payload, SECRET_KEY, algorithm="HS256")

        mock_user = MagicMock(spec=User)
        mock_user.id = user_uuid

        mock_session = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = mock_user
        mock_session.execute = AsyncMock(return_value=mock_result)

        mock_credentials = MagicMock()
        mock_credentials.credentials = token

        result = await get_current_user(
            request=MagicMock(),
            credentials=mock_credentials,
            session=mock_session,
            response=MagicMock(),
        )
        assert result == str(user_uuid)
