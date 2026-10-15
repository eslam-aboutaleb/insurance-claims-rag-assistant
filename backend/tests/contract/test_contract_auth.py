"""
Tier A golden contract tests for ``/api/v1/auth`` and ``/api/v1/health``.

Freezes the authentication handshake (signup, signin, signout, me), the dual-auth
resolution rule, the session cookie attributes, and the health envelope.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tests.contract.conftest import SeededUser, _unique, compare_to_golden, signup

pytestmark = pytest.mark.contract


def test_health_envelope_is_frozen(contract_client: TestClient) -> None:
    """``GET /health`` returns 200 with the frozen status envelope."""
    response = contract_client.get("/api/v1/health")
    assert response.status_code == 200
    compare_to_golden("health", response.json())


def test_signup_returns_token_envelope(contract_client: TestClient) -> None:
    """``POST /auth/signup`` returns 201 with the frozen token envelope."""
    response = contract_client.post(
        "/api/v1/auth/signup",
        json={"username": _unique("golden_signup_user"), "password": "ContractPass123!"},
    )
    assert response.status_code == 201
    compare_to_golden(
        "auth_signup",
        response.json(),
        volatile_keys=frozenset({"access_token", "user_id"}),
    )


def test_signup_sets_session_cookie(contract_client: TestClient) -> None:
    """Signup sets an httponly, lax cookie that is Secure outside development.

    ``auth._set_session_cookie`` marks the cookie Secure unless the environment is
    ``dev``/``development``. The suite runs as ``ENVIRONMENT=test`` so it exercises the
    non-development path, which is the one a deployment uses.
    """
    response = contract_client.post(
        "/api/v1/auth/signup",
        json={"username": _unique("golden_cookie_user"), "password": "ContractPass123!"},
    )
    assert response.status_code == 201
    cookie_header = response.headers.get("set-cookie", "")
    assert "omnicare_access_token=" in cookie_header
    assert "HttpOnly" in cookie_header
    assert "SameSite=lax" in cookie_header.replace("samesite", "SameSite")
    assert "Secure" in cookie_header


def test_signup_duplicate_username_is_409(contract_client: TestClient) -> None:
    """A duplicate username returns the frozen 409 conflict envelope."""
    username = _unique("golden_duplicate")
    signup(contract_client, username)
    response = contract_client.post(
        "/api/v1/auth/signup",
        json={"username": username, "password": "ContractPass123!"},
    )
    assert response.status_code == 409
    compare_to_golden("auth_signup_duplicate", response.json())


def test_signin_returns_token_envelope(contract_client: TestClient, user_a: SeededUser) -> None:
    """``POST /auth/signin`` returns 200 with the frozen token envelope."""
    response = contract_client.post(
        "/api/v1/auth/signin",
        json={"username": user_a.username, "password": user_a.password},
    )
    assert response.status_code == 200, f"SIGNIN 401 DETAIL: {response.text} user={user_a.username}"
    compare_to_golden(
        "auth_signin",
        response.json(),
        volatile_keys=frozenset({"access_token", "user_id"}),
    )


def test_signin_wrong_password_is_401(contract_client: TestClient, user_a: SeededUser) -> None:
    """A wrong password returns the frozen 401 envelope without user data."""
    response = contract_client.post(
        "/api/v1/auth/signin",
        json={"username": user_a.username, "password": "WrongPassword123!"},
    )
    assert response.status_code == 401
    compare_to_golden("auth_signin_bad_password", response.json())


def test_signout_clears_cookie(contract_client: TestClient) -> None:
    """``POST /auth/logout`` returns 204 and expires the session cookie."""
    response = contract_client.post("/api/v1/auth/logout")
    assert response.status_code == 204
    cookie_header = response.headers.get("set-cookie", "")
    assert "omnicare_access_token=" in cookie_header
    assert "Max-Age=0" in cookie_header or "1970" in cookie_header


def test_me_returns_user_id(contract_client: TestClient, user_a: SeededUser) -> None:
    """``GET /auth/me`` returns the frozen identity envelope when authenticated."""
    response = contract_client.get("/api/v1/auth/me", headers=user_a.headers)
    assert response.status_code == 200, f"ME DETAIL: {response.text}"
    compare_to_golden(
        "auth_me",
        response.json(),
        volatile_keys=frozenset({"user_id"}),
    )


def test_me_without_credentials_is_401(contract_client: TestClient) -> None:
    """An unauthenticated ``GET /auth/me`` returns the frozen 401 envelope.

    The plan's Part A1 table describes 204 for this case. The implementation raises
    401 from ``get_current_user``, so 401 is what Tier A freezes.
    """
    response = contract_client.get("/api/v1/auth/me")
    assert response.status_code == 401
    compare_to_golden("auth_me_unauthenticated", response.json())


def test_me_with_malformed_token_is_401(contract_client: TestClient) -> None:
    """A malformed bearer token returns the frozen 401 envelope."""
    response = contract_client.get(
        "/api/v1/auth/me",
        headers={"Authorization": "Bearer not-a-jwt"},
    )
    assert response.status_code == 401
    compare_to_golden("auth_me_invalid_token", response.json())


def test_me_with_nonexistent_subject_is_401(contract_client: TestClient) -> None:
    """A well-signed token for a deleted user returns the frozen 401 envelope."""
    import uuid

    import jwt

    from app.security.auth import ALGORITHM, SECRET_KEY

    token = jwt.encode(
        {"sub": str(uuid.uuid4()), "exp": 9999999999},
        SECRET_KEY,
        algorithm=ALGORITHM,
    )
    response = contract_client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 401
    compare_to_golden("auth_me_missing_user", response.json())
