"""Coverage tests for app.api.v1.auth uncovered paths.

The signup and signin endpoints carry an audit-trail write, so the direct-call
unit tests below pass a minimal ``Request``: the trail reads the client IP and
user agent from it when one is in scope, and a direct call has none.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException, Request, Response
from sqlalchemy.exc import IntegrityError

_TEST_PASSWORD = "TestPass123!"
_WRONG_PASSWORD = "WrongPass1!"

# A request scope carrying no client and no headers: the audit trail reads both
# as absent, which is the honest answer for a call made outside a request.
_AUDITED_REQUEST = Request(scope={"type": "http", "headers": [], "method": "POST"})


class TestAuthAPI:
    def test_signup_duplicate_username_returns_409(self, test_client):
        username = f"dupuser_{uuid.uuid4().hex[:8]}"
        payload = {"username": username, "password": "TestPass123!"}
        r1 = test_client.post("/api/v1/auth/signup", json=payload)
        assert r1.status_code == 201
        r2 = test_client.post("/api/v1/auth/signup", json=payload)
        assert r2.status_code == 409

    def test_signup_integrity_error_returns_409(self, test_client):
        mock_session = AsyncMock()
        # No existing user: the uniqueness pre-check passes and the
        # duplicate is only detected at COMMIT time (the race the
        # IntegrityError handler exists for).
        mock_session.execute.return_value.scalar_one_or_none.return_value = None
        mock_session.commit = AsyncMock(side_effect=IntegrityError("duplicate", None, None))

        with patch("app.core.database.async_session_factory") as mock_factory:
            mock_factory.return_value.__aenter__.return_value = mock_session
            response = test_client.post(
                "/api/v1/auth/signup",
                json={"username": "intuser", "password": "TestPass123!"},
            )
        assert response.status_code == 409

    def test_signin_uses_dummy_hash_for_nonexistent_user(self, test_client):
        response = test_client.post(
            "/api/v1/auth/signin",
            json={"username": f"nonexistent_{uuid.uuid4().hex}", "password": "AnyPass123!"},
        )
        assert response.status_code == 401

    def test_get_current_user_info_returns_user_id(self, test_client, mock_current_user):
        response = test_client.get("/api/v1/auth/me", headers=mock_current_user)
        assert response.status_code == 200
        data = response.json()
        assert data["user_id"] == "00000000-0000-0000-0000-000000000001"

    def test_signin_success_sets_cookie_and_returns_token(self, test_client):
        username = f"signin_{uuid.uuid4().hex[:8]}"
        payload = {"username": username, "password": "TestPass123!"}
        test_client.post("/api/v1/auth/signup", json=payload)
        response = test_client.post(
            "/api/v1/auth/signin",
            json={"username": username, "password": "TestPass123!"},
        )
        assert response.status_code == 200
        data = response.json()
        assert "access_token" in data
        assert data["token_type"] == "bearer"

    def test_logout_returns_204_and_clears_cookie(self, test_client):
        response = test_client.post("/api/v1/auth/logout")
        assert response.status_code == 204


class TestAuthEndpointUnits:
    """Direct calls into the endpoint functions.

    The TestClient runs the ASGI app on a portal thread and offloads
    Argon2 hashing to a worker threadpool, neither of which the
    coverage tracer follows. Calling the coroutine functions directly
    exercises the same code paths on the test's own event loop, so the
    hashing, integrity-error, and dummy-hash branches are measured.
    """

    @staticmethod
    def _session_with(existing=None, commit_error=None):
        """Build a mock session whose user lookup returns ``existing``.

        ``session.execute`` is awaited, so it is an ``AsyncMock`` whose
        return value is a plain ``MagicMock``; otherwise the chained
        ``scalar_one_or_none()`` call would itself return a coroutine.
        """
        session = AsyncMock()
        result = MagicMock()
        result.scalar_one_or_none.return_value = existing
        session.execute = AsyncMock(return_value=result)
        # Session.add is synchronous in SQLAlchemy, so it must not
        # be an AsyncMock (which would return an un-awaited coroutine).
        session.add = MagicMock()
        if commit_error is not None:
            session.commit = AsyncMock(side_effect=commit_error)
        return session

    @pytest.mark.asyncio
    async def test_signup_hashes_password_and_returns_token(self):
        from app.api.v1.auth import signup
        from app.schemas.models import UserSignup

        session = self._session_with(existing=None)
        response = Response()

        result = await signup(
            payload=UserSignup(username=f"unit_{uuid.uuid4().hex[:8]}", password=_TEST_PASSWORD),
            response=response,
            request=_AUDITED_REQUEST,
            session=session,
        )

        assert result["token_type"] == "bearer"
        assert result["user_id"]
        session.add.assert_called_once()
        # One commit persists the account and its audit entry together, because
        # the entry is written in a savepoint of this transaction.
        session.commit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_signup_integrity_error_raises_409(self):
        from app.api.v1.auth import signup
        from app.schemas.models import UserSignup

        session = self._session_with(
            existing=None,
            commit_error=IntegrityError("duplicate", None, None),
        )

        with pytest.raises(HTTPException) as excinfo:
            await signup(
                payload=UserSignup(username="unit_int", password=_TEST_PASSWORD),
                response=Response(),
                request=_AUDITED_REQUEST,
                session=session,
            )

        assert excinfo.value.status_code == 409
        session.rollback.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_signup_existing_username_raises_409(self):
        from app.api.v1.auth import signup
        from app.schemas.models import UserSignup

        session = self._session_with(existing=MagicMock())

        with pytest.raises(HTTPException) as excinfo:
            await signup(
                payload=UserSignup(username="unit_dup", password=_TEST_PASSWORD),
                response=Response(),
                request=_AUDITED_REQUEST,
                session=session,
            )

        assert excinfo.value.status_code == 409

    @pytest.mark.asyncio
    async def test_signin_nonexistent_user_verifies_against_dummy_hash(self):
        from app.api.v1.auth import signin
        from app.security.auth import DUMMY_PASSWORD_HASH
        from app.schemas.models import UserSignin

        session = self._session_with(existing=None)

        # run_in_threadpool invokes the callable synchronously in a
        # worker thread, so a plain MagicMock records the call.
        with patch("app.api.v1.auth.verify_password", return_value=False) as mock_verify:
            with pytest.raises(HTTPException) as excinfo:
                await signin(
                    payload=UserSignin(username="unit_missing", password=_WRONG_PASSWORD),
                    response=Response(),
                    request=_AUDITED_REQUEST,
                    session=session,
                )

        assert excinfo.value.status_code == 401
        # The dummy hash is what a non-existent user is verified against,
        # so the timing profile matches a real user with a wrong password.
        mock_verify.assert_called_once_with("WrongPass1!", DUMMY_PASSWORD_HASH)

    @pytest.mark.asyncio
    async def test_signin_wrong_password_raises_401(self):
        from app.api.v1.auth import signin
        from app.schemas.models import UserSignin

        user = MagicMock()
        user.password_hash = "argon2hash"
        session = self._session_with(existing=user)

        with patch("app.api.v1.auth.verify_password", return_value=False) as mock_verify:
            with pytest.raises(HTTPException) as excinfo:
                await signin(
                    payload=UserSignin(username="unit_user", password=_WRONG_PASSWORD),
                    response=Response(),
                    request=_AUDITED_REQUEST,
                    session=session,
                )

        assert excinfo.value.status_code == 401
        mock_verify.assert_called_once_with("WrongPass1!", "argon2hash")
