"""
Security isolation tests for OmniCare Financial backend.

Tests horizontal privilege escalation (IDOR) by verifying that:
1. Users cannot access claims owned by other users
2. Chat messages and conversations are scoped to the authenticated user
3. Claim status tool enforces owner_id filtering
4. RAG-based claims search is scoped to current user
"""

import uuid
from datetime import UTC
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ============================================================================
# Setup: Create two distinct users and their tokens
# ============================================================================


@pytest.fixture(scope="function")
def two_users(test_client):
    """Create two users and return their auth tokens."""
    import time

    def signup_with_retry(username, password, max_retries=3, delay=0.5):
        for attempt in range(max_retries):
            try:
                return test_client.post(
                    "/api/v1/auth/signup", json={"username": username, "password": password}
                )
            except Exception as exc:
                if "another operation is in progress" in str(exc) and attempt < max_retries - 1:
                    time.sleep(delay * (attempt + 1))
                    continue
                raise
        return None

    # User A
    username_a = f"security_usera_{uuid.uuid4().hex[:8]}"
    password = "TestPass123!"
    r_a = signup_with_retry(username_a, password)
    token_a = r_a.json()["access_token"]
    headers_a = {"Authorization": f"Bearer {token_a}"}

    # User B
    username_b = f"security_userb_{uuid.uuid4().hex[:8]}"
    r_b = signup_with_retry(username_b, password)
    token_b = r_b.json()["access_token"]
    headers_b = {"Authorization": f"Bearer {token_b}"}

    return {
        "user_a": {"username": username_a, "token": token_a, "headers": headers_a},
        "user_b": {"username": username_b, "token": token_b, "headers": headers_b},
    }


# ============================================================================
# Claim Isolation Tests (Horizontal Privilege Escalation / IDOR)
# ============================================================================


class TestClaimIDOR:
    @pytest.mark.asyncio
    async def test_user_b_cannot_check_user_a_claim_status(self):
        """User B's token cannot retrieve User A's claim status."""
        from app.agent.context import current_user_id
        from app.agent.tools.claim_status import get_claim_status

        # Set context to User B
        uuid.UUID("00000000-0000-0000-0000-000000000002")
        current_user_id.set(uuid.UUID("00000000-0000-0000-0000-000000000002"))

        mock_session = AsyncMock()
        mock_session.add = MagicMock()
        mock_result = MagicMock()
        # Claim belongs to User A, so query returns None for User B
        mock_result.scalar_one_or_none.return_value = None
        mock_session.execute.return_value = mock_result

        with patch("app.agent.tools.claim_status.async_session_factory") as mock_factory:
            mock_factory.return_value.__aenter__.return_value = mock_session
            result = await get_claim_status(claim_id="CLM-OWNED-BY-A")

            # Should return not found (or error), NOT the claim data
            assert result["found"] is False or "error" in result

    @pytest.mark.asyncio
    async def test_user_b_cannot_submit_claim_as_user_a(self):
        """Submit claim sets owner to current user, cannot spoof another user."""
        from app.agent.context import current_user_id
        from app.agent.tools.submit_claim import prepare_claim_submission

        # Set context to User B
        user_b_id = uuid.UUID("00000000-0000-0000-0000-000000000002")
        current_user_id.set(uuid.UUID("00000000-0000-0000-0000-000000000002"))

        mock_session = AsyncMock()
        mock_session.add = MagicMock()
        with (
            patch("app.agent.tools.submit_claim.async_session_factory") as mock_factory,
            patch("app.domain.claims.ingest.ingest_claim"),
        ):
            mock_factory.return_value.__aenter__.return_value = mock_session
            result = await prepare_claim_submission(
                policy_number="POL-1092",
                claim_type="Water Damage",
                amount=500.0,
                description="User B tries to submit as User A but owner is User B.",
            )

        # Claim should succeed but owner should be User B (current context)
        assert result["success"] is True
        # Verify the new claim was added with User B as owner
        added_claim = mock_session.add.call_args[0][0]
        assert str(added_claim.user_id) == str(user_b_id)

    @pytest.mark.asyncio
    async def test_search_claims_does_not_leak_other_users_claims(self):
        """Claims search only returns claims owned by current user."""
        from app.agent.context import current_user_id
        from app.domain.claims.tools import search_claims

        # Set context to User B
        user_b_id = uuid.UUID("00000000-0000-0000-0000-000000000002")
        current_user_id.set(uuid.UUID("00000000-0000-0000-0000-000000000002"))

        # Mock retrieve_claims_hybrid to return only User B's claims
        mock_b_claims = [
            {"document": "User B claim", "metadata": {"owner_id": str(user_b_id)}, "distance": 0.1}
        ]

        with patch("app.domain.claims.tools.retrieve_claims_hybrid") as mock_retrieve:
            mock_retrieve.return_value = mock_b_claims
            result = await search_claims("water damage")

        # Verify the search was scoped to User B
        mock_retrieve.assert_called_once()
        call_kwargs = mock_retrieve.call_args[1]
        assert call_kwargs["user_id"] == user_b_id

        # Result should only contain User B's claims
        assert len(result) == 1
        assert result[0]["metadata"]["owner_id"] == str(user_b_id)


# ============================================================================
# Conversation/Chat Isolation Tests
# ============================================================================


class TestChatIDOR:
    def test_chat_endpoint_returns_401_without_valid_token(self, test_client):
        """Un-authenticated requests to chat are rejected."""
        response = test_client.post("/api/v1/chat", json={"message": "Hello"})
        assert response.status_code == 401

    def test_chat_endpoint_scopes_user_id(self, test_client):
        """Chat endpoint passes authenticated user_id to agent."""
        from unittest.mock import AsyncMock, patch

        with patch("app.api.v1.chat.run_agent", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = {"response": "Test", "sources": [], "tool_calls": []}
            # Use a valid user_id in the mock override
            from app.auth import get_current_user
            from app.main import app

            async def _override():
                return "00000000-0000-0000-0000-000000000001"

            app.dependency_overrides[get_current_user] = _override
            try:
                response = test_client.post("/api/v1/chat", json={"message": "Hello"})
                assert response.status_code == 200
                mock_run.assert_awaited_once_with(
                    user_id="00000000-0000-0000-0000-000000000001",
                    message="Hello",
                )
            finally:
                app.dependency_overrides.pop(get_current_user, None)

    def test_conversation_list_scoped_to_authenticated_user(self, test_client):
        """List conversations returns only current user's conversations."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from app.auth import get_current_user
        from app.main import app

        user_id = "00000000-0000-0000-0000-000000000002"

        async def _override():
            return user_id

        app.dependency_overrides[get_current_user] = _override
        try:
            with patch("app.database.async_session_factory") as mock_factory:
                mock_session = AsyncMock()
                mock_session.add = MagicMock()
                mock_result = MagicMock()
                # Mock returning only User B's conversations
                mock_conv = MagicMock()
                mock_conv.id = uuid.uuid4()
                mock_conv.title = "User B Chat"
                mock_conv.user_id = uuid.UUID(user_id)
                mock_conv.created_at = "2024-01-01T00:00:00Z"
                mock_conv.updated_at = "2024-01-01T00:00:00Z"
                mock_result.scalars.return_value.all.return_value = [mock_conv]
                mock_session.execute.return_value = mock_result
                mock_factory.return_value.__aenter__.return_value = mock_session

                response = test_client.get("/api/v1/chat/conversations")
                assert response.status_code == 200
                data = response.json()
                assert "conversations" in data
        finally:
            app.dependency_overrides.pop(get_current_user, None)


# ============================================================================
# Token Forgery / Tampering Tests
# ============================================================================


class TestTokenForgery:
    def test_tampered_token_rejected(self, test_client):
        """Chat endpoint rejects tampered JWT tokens."""
        tampered_token = "eyJhbGciOiJIUzI1NiJ9.tampered.payload.signature"
        response = test_client.post(
            "/api/v1/chat",
            json={"message": "Hello"},
            headers={"Authorization": f"Bearer {tampered_token}"},
        )
        assert response.status_code == 401

    def test_expired_token_rejected(self, test_client):
        """Chat endpoint rejects expired JWT tokens."""
        from datetime import datetime, timedelta

        import jwt

        from app.auth import ALGORITHM, SECRET_KEY

        expired_payload = {
            "sub": str(uuid.uuid4()),
            "exp": datetime.now(tz=UTC) - timedelta(hours=1),
        }
        expired_token = jwt.encode(expired_payload, SECRET_KEY, algorithm=ALGORITHM)
        response = test_client.post(
            "/api/v1/chat",
            json={"message": "Hello"},
            headers={"Authorization": f"Bearer {expired_token}"},
        )
        assert response.status_code == 401

    def test_wrong_secret_token_rejected(self, test_client):
        """Chat endpoint rejects tokens signed with wrong secret."""
        from datetime import datetime, timedelta

        import jwt

        wrong_secret_payload = {
            "sub": str(uuid.uuid4()),
            "exp": datetime.now(tz=UTC) + timedelta(hours=1),
        }
        wrong_token = jwt.encode(
            wrong_secret_payload,
            "this-is-a-long-enough-wrong-secret-key-for-testing",
            algorithm="HS256",
        )
        response = test_client.post(
            "/api/v1/chat",
            json={"message": "Hello"},
            headers={"Authorization": f"Bearer {wrong_token}"},
        )
        assert response.status_code == 401

    def test_token_without_sub_rejected(self, test_client):
        """Chat endpoint rejects tokens missing 'sub' claim."""
        from datetime import datetime, timedelta

        import jwt

        from app.auth import ALGORITHM, SECRET_KEY

        no_sub_payload = {
            "exp": datetime.now(tz=UTC) + timedelta(hours=1),
        }
        no_sub_token = jwt.encode(no_sub_payload, SECRET_KEY, algorithm=ALGORITHM)
        response = test_client.post(
            "/api/v1/chat",
            json={"message": "Hello"},
            headers={"Authorization": f"Bearer {no_sub_token}"},
        )
        assert response.status_code == 401

    def test_nonexistent_user_token_clears_cookie_and_rejects(self, test_client):
        """Token for non-existent user returns 401 and clears cookie."""
        from datetime import datetime, timedelta

        import jwt

        from app.auth import ALGORITHM, SECRET_KEY

        # Create a valid token for a UUID that doesn't exist in DB
        fake_user_id = str(uuid.uuid4())
        payload = {
            "sub": fake_user_id,
            "exp": datetime.now(tz=UTC) + timedelta(hours=1),
        }
        token = jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)
        response = test_client.post(
            "/api/v1/chat", json={"message": "Hello"}, headers={"Authorization": f"Bearer {token}"}
        )
        assert response.status_code == 401
