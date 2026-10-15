"""Coverage tests for app.api.v1.claims uncovered paths."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch


class TestClaimsAPI:
    def test_prepare_claim_invalid_user_returns_401(self, test_client, mock_current_user):
        with patch("app.api.v1.claims.uuid.UUID", side_effect=ValueError("bad uuid")):
            response = test_client.post(
                "/api/v1/claims/prepare",
                json={
                    "policy_number": "POL-1092",
                    "claim_type": "Water Damage",
                    "amount": 2500.0,
                    "description": "Frozen pipe burst causing water damage to hardwood floors",
                },
                headers=mock_current_user,
            )
        assert response.status_code == 401

    def test_prepare_claim_sqlalchemy_error_returns_500(self, test_client, mock_current_user):
        from sqlalchemy.exc import SQLAlchemyError

        with patch("app.api.v1.claims.async_session_factory") as mock_factory:
            mock_session = AsyncMock()
            mock_session.add = MagicMock()
            mock_session.commit = AsyncMock(side_effect=SQLAlchemyError("DB error"))
            mock_factory.return_value.__aenter__.return_value = mock_session
            response = test_client.post(
                "/api/v1/claims/prepare",
                json={
                    "policy_number": "POL-1092",
                    "claim_type": "Water Damage",
                    "amount": 2500.0,
                    "description": "Frozen pipe burst causing water damage to hardwood floors",
                },
                headers=mock_current_user,
            )
        assert response.status_code == 500

    def test_confirm_claim_invalid_user_returns_401(self, test_client):
        from app.security.auth import get_current_user
        from app.main import app

        async def _bad_user():
            return "not-a-valid-uuid"

        app.dependency_overrides[get_current_user] = _bad_user
        try:
            response = test_client.post(
                "/api/v1/claims/confirm",
                json={"confirmation_token": str(uuid.uuid4())},
            )
        finally:
            app.dependency_overrides.pop(get_current_user, None)
        assert response.status_code == 401

    def test_confirm_claim_submission_none_returns_400(self, test_client, mock_current_user):
        with patch("app.api.v1.claims.async_session_factory") as mock_factory:
            mock_session = AsyncMock()
            mock_result = MagicMock()
            mock_result.scalar_one_or_none.return_value = None
            mock_session.execute.return_value = mock_result
            mock_factory.return_value.__aenter__.return_value = mock_session
            response = test_client.post(
                "/api/v1/claims/confirm",
                json={"confirmation_token": str(uuid.uuid4())},
                headers=mock_current_user,
            )
        assert response.status_code == 400

    def test_confirm_claim_expired_token_returns_400(self, test_client, mock_current_user):
        token = str(uuid.uuid4())
        user_uuid = uuid.UUID("00000000-0000-0000-0000-000000000001")
        past_expiry = datetime.now(UTC) - timedelta(minutes=5)

        mock_submission = MagicMock()
        mock_submission.confirmation_token = token
        mock_submission.user_id = user_uuid
        mock_submission.status = "pending"
        mock_submission.expires_at = past_expiry
        mock_submission.claim_data = {
            "policy_number": "POL-1092",
            "claim_type": "Water Damage",
            "amount": "2500.00",
            "description": "Frozen pipe burst causing water damage to hardwood floors",
        }

        with patch("app.api.v1.claims.async_session_factory") as mock_factory:
            mock_session = AsyncMock()
            mock_result = MagicMock()
            mock_result.scalar_one_or_none.return_value = mock_submission
            mock_session.execute.return_value = mock_result
            mock_factory.return_value.__aenter__.return_value = mock_session
            response = test_client.post(
                "/api/v1/claims/confirm",
                json={"confirmation_token": token},
                headers=mock_current_user,
            )
        assert response.status_code == 400
        assert "expired" in response.json()["error"]["message"].lower()

    def test_prepare_claim_logs_info_and_returns_token(self, test_client, mock_current_user):
        with patch("app.api.v1.claims.async_session_factory") as mock_factory:
            mock_session = AsyncMock()
            mock_factory.return_value.__aenter__.return_value = mock_session
            response = test_client.post(
                "/api/v1/claims/prepare",
                json={
                    "policy_number": "POL-1092",
                    "claim_type": "Water Damage",
                    "amount": 2500.0,
                    "description": "Frozen pipe burst causing water damage to hardwood floors",
                },
                headers=mock_current_user,
            )
        assert response.status_code == 200
        data = response.json()
        assert "confirmation_token" in data

    def test_confirm_claim_success_returns_200(self, test_client, mock_current_user):
        token = str(uuid.uuid4())
        user_uuid = uuid.UUID("00000000-0000-0000-0000-000000000001")
        future_expiry = datetime.now(UTC) + timedelta(minutes=5)

        mock_submission = MagicMock()
        mock_submission.confirmation_token = token
        mock_submission.user_id = user_uuid
        mock_submission.status = "pending"
        mock_submission.expires_at = future_expiry
        mock_submission.claim_data = {
            "policy_number": "POL-1092",
            "claim_type": "Water Damage",
            "amount": "2500.00",
            "description": "Frozen pipe burst causing water damage to hardwood floors",
        }

        with patch("app.api.v1.claims.async_session_factory") as mock_factory:
            mock_session = AsyncMock()
            mock_result = MagicMock()
            mock_result.scalar_one_or_none.return_value = mock_submission
            mock_session.execute.return_value = mock_result
            mock_factory.return_value.__aenter__.return_value = mock_session
            with patch(
                "app.api.v1.claims.submit_claim_internal", new_callable=AsyncMock
            ) as mock_submit:
                mock_submit.return_value = {
                    "success": True,
                    "confirmation_id": "CLM-NEW",
                    "status": "submitted",
                    "message": "Claim submitted",
                }
                response = test_client.post(
                    "/api/v1/claims/confirm",
                    json={"confirmation_token": token},
                    headers=mock_current_user,
                )
        assert response.status_code == 200
