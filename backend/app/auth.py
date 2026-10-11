"""
Authentication and authorization utilities for the OmniCare backend.

This module provides JWT-based session management using HTTP-only cookies,
Argon2 password hashing, and the ``get_current_user`` FastAPI dependency that
resolves the authenticated user for every protected request.

Security design decisions:
  - JWT payloads store only the user UUID (``sub`` claim). No PII or role
    claims are included to minimize token sensitivity.
  - ``secure=False`` on cookies is intentional for local Docker development.
    Set ``secure=True`` and configure ``TRUSTED_ORIGINS`` when deploying
    behind HTTPS in production.
  - A dummy Argon2 hash is used for timing-safe password comparisons when a
    username does not exist, preventing user enumeration via response timing.
  - ``current_user_id`` is set as a ``ContextVar`` so downstream code (agent
    tools, business logic) can access the authenticated user without threading
    the user object through every function call.
"""

from __future__ import annotations

import datetime
import uuid
from collections.abc import Callable

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from fastapi import Depends, HTTPException, Request, Response, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.context import current_user_id

# JWT configuration. ``JWT_SECRET_KEY`` must be set in production; the
# fallback value is for local development only.
from app.config import settings
from app.database import get_db
from app.models.user import User, UserRole

SECRET_KEY = settings.jwt_secret_key
ALGORITHM = "HS256"
# Access tokens are long-lived (1 week) because the session is cookie-based.
# For higher-security contexts, consider shorter TTLs with refresh tokens.
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24 * 7  # 1 week
COOKIE_NAME = "omnicare_access_token"

# Dummy hash for timing attack protection. This hash is returned when a user
# does not exist so that the Argon2 verification always takes constant time,
# preventing attackers from enumerating valid usernames via response latency.
DUMMY_PASSWORD_HASH = "$argon2id$v=19$m=65536,t=3,p=4$dummySalt$dummyHashDigest"  # noqa: S105

ph = PasswordHasher()
_bearer = HTTPBearer(auto_error=False)


def hash_password(password: str) -> str:
    """Hash a plaintext password using Argon2id.

    Argon2 is the current recommended password hashing algorithm (winner of
    the Password Hashing Competition). The default parameters (memory, time,
    parallelism) are chosen to be resistant to GPU/ASIC attacks while
    remaining fast enough for interactive signup flows.

    Args:
        password: The user's plaintext password.

    Returns:
        str: The Argon2id hash string suitable for storage.
    """
    return ph.hash(password)


def verify_password(password: str, hashed: str) -> bool:
    """Verify a plaintext password against a stored Argon2id hash.

    Args:
        password: The plaintext password provided at login.
        hashed: The stored Argon2id hash from the database.

    Returns:
        bool: True if the password matches, False otherwise.
    """
    try:
        ph.verify(hashed, password)
        return True
    except VerifyMismatchError:
        return False
    except Exception:
        return False


def create_access_token(user_id: uuid.UUID) -> str:
    """Create a signed JWT access token for a user.

    The token contains the user's UUID as the ``sub`` claim and an expiration
    timestamp. No other claims are included to minimize the token's
    sensitivity and reduce the attack surface if the token is intercepted.

    Args:
        user_id: The primary key UUID of the authenticated user.

    Returns:
        str: A signed JWT string suitable for setting as an HTTP-only cookie.
    """
    expire = datetime.datetime.now(datetime.UTC) + datetime.timedelta(
        minutes=ACCESS_TOKEN_EXPIRE_MINUTES
    )
    to_encode = {"sub": str(user_id), "exp": expire}
    encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
    return encoded_jwt


class _InvalidTokenError(Exception):
    """Internal exception used to categorize token validation failures.

    Wrapping PyJWT errors in a domain-specific exception keeps the token
    decoding logic clean and makes it easy to distinguish auth failures from
    other runtime errors in the ``get_current_user`` dependency.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _decode_token(token: str) -> uuid.UUID:
    """Decode and validate a JWT, returning the embedded user UUID.

    Args:
        token: The raw JWT string from the cookie or Authorization header.

    Returns:
        uuid.UUID: The user's primary key extracted from the ``sub`` claim.

    Raises:
        _InvalidTokenError: If the token is expired, malformed, or invalid.
    """
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        sub = payload.get("sub")
        if sub is None:
            raise _InvalidTokenError("malformed-payload")
        return uuid.UUID(sub)
    except jwt.ExpiredSignatureError as exc:
        raise _InvalidTokenError("expired") from exc
    except jwt.PyJWTError as exc:
        raise _InvalidTokenError("invalid-token") from exc
    except ValueError as exc:
        raise _InvalidTokenError("malformed-subject") from exc


def _resolve_token(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None,
) -> str | None:
    """Extract the JWT from the request, preferring the Authorization header.

    The backend accepts tokens from two sources:
      1. ``Authorization: Bearer <token>`` header (used by API clients and
         mobile apps).
      2. ``omnicare_access_token`` HTTP-only cookie (used by the web frontend).

    If both are present, the Authorization header wins. This allows progressive
    migration from cookies to bearer tokens without breaking existing clients.

    Args:
        request: The incoming FastAPI request.
        credentials: Optional Bearer credentials from the HTTPBearer security
            scheme.

    Returns:
        str | None: The raw JWT string, or None if no token was found.
    """
    cookie_token = request.cookies.get(COOKIE_NAME)
    if credentials is not None:
        return credentials.credentials
    return cookie_token


def set_session_cookie(response: Response, token: str) -> None:
    """Set the session cookie on the response after successful authentication.

    Args:
        response: The FastAPI response object to modify.
        token: The signed JWT to store in the cookie.
    """
    is_secure = settings.environment not in ("dev", "development")
    response.set_cookie(
        key=COOKIE_NAME,
        value=token,
        max_age=ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        httponly=True,
        samesite="lax",
        secure=is_secure,
        path="/",
    )


def clear_session_cookie(response: Response) -> None:
    """Delete the session cookie, typically during logout or token invalidation.

    Args:
        response: The FastAPI response object to modify.
    """
    is_secure = settings.environment not in ("dev", "development")
    response.delete_cookie(
        key=COOKIE_NAME,
        path="/",
        httponly=True,
        samesite="lax",
        secure=is_secure,
    )


async def get_current_user(
    request: Request,
    response: Response,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    session: AsyncSession = Depends(get_db),
) -> str:
    """FastAPI dependency that resolves the authenticated user for the request.

    This dependency is applied to every protected route. It extracts the JWT,
    validates it, looks up the user in the database, and sets the user's UUID
    in the ``current_user_id`` context variable so that downstream code (agent
    tools, audit logging) can access it without explicit parameter passing.

    Side effects:
      - Sets ``current_user_id`` ContextVar on success.
      - Clears the session cookie if the token is invalid or the user is not
        found, forcing the client to re-authenticate.

    Args:
        request: The incoming FastAPI request (used for cookie access).
        response: The FastAPI response (used for cookie clearing).
        credentials: Optional Bearer credentials from the HTTPBearer scheme.
        session: The async database session dependency.

    Returns:
        str: The authenticated user's UUID as a string.

    Raises:
        HTTPException: 401 if the token is missing, invalid, expired, or the
        user account does not exist.
    """
    token = _resolve_token(request, credentials)
    if token is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Sign in to continue")
    try:
        user_uuid = _decode_token(token)
    except _InvalidTokenError:
        clear_session_cookie(response)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
        ) from None

    user = (await session.execute(select(User).where(User.id == user_uuid))).scalar_one_or_none()
    if user is None:
        clear_session_cookie(response)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Account not found")

    current_user_id.set(user.id)
    return str(user.id)


def require_role(*roles: UserRole | str) -> Callable[..., str]:
    """Build a dependency that admits only users holding one of ``roles``.

    The authorization seam every privileged route needs. It composes with
    ``get_current_user`` rather than replacing it, so token resolution, cookie
    clearing, and the ``current_user_id`` context variable keep one
    implementation and the dependency contract every existing router and the
    frozen contract suite rely on stays untouched.

    The role is read from the database on every request rather than from the
    token, because the token carries only the user id: promoting or demoting an
    account takes effect on that account's next request instead of whenever its
    week-long token happens to expire.

    Args:
        *roles: The roles allowed through, as ``UserRole`` members or their
            string values. An empty list admits nobody.

    Returns:
        Callable[..., str]: A FastAPI dependency that resolves to the
        authenticated user's UUID string, mirroring ``get_current_user`` so it
        can stand in for it anywhere.
    """
    # The column holds the enum's value, so a caller may pass either the member
    # or the raw string and still compare equal.
    allowed_values = frozenset(
        role.value if isinstance(role, UserRole) else str(role) for role in roles
    )
    allowed = ", ".join(sorted(allowed_values))

    async def _require_role(
        current_user_id: str = Depends(get_current_user),
        session: AsyncSession = Depends(get_db),
    ) -> str:
        try:
            user_uuid = uuid.UUID(current_user_id)
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid user",
            ) from None

        user = (
            await session.execute(select(User).where(User.id == user_uuid))
        ).scalar_one_or_none()
        if user is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Account not found",
            )

        if str(user.role) not in allowed_values:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"This action requires one of the following roles: {allowed}.",
            )

        return current_user_id

    return _require_role
