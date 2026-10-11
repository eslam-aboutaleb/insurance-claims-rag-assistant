"""
Authentication API routes for the OmniCare backend.

Exposes three endpoints:
  - ``POST /signup`` -- Create a new user account with Argon2 password hashing.
  - ``POST /signin`` -- Authenticate with username and password, issue JWT.
  - ``POST /logout`` -- Clear the session cookie.

All endpoints are public (no authentication required) because they are the
entry point for obtaining credentials. After signup or signin, the client
receives a JWT that is used for subsequent requests.

Security notes:
  - Passwords are hashed with Argon2id in a background threadpool to avoid
    blocking the async event loop during the CPU-intensive hashing operation.
  - A dummy hash is returned for non-existent users during signin to prevent
    timing-based user enumeration.
  - Session cookies are HTTP-only and SameSite=Lax to mitigate CSRF while
    still allowing top-level navigation from external links.
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.concurrency import run_in_threadpool
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import record_audit
from app.auth import (
    DUMMY_PASSWORD_HASH,
    clear_session_cookie,
    create_access_token,
    get_current_user,
    hash_password,
    set_session_cookie,
    verify_password,
)
from app.database import get_db
from app.models.user import User
from app.schemas.models import Token, UserMe, UserSignin, UserSignup

router = APIRouter()


@router.post("/signup", response_model=Token, status_code=status.HTTP_201_CREATED)
async def signup(
    payload: UserSignup,
    response: Response,
    request: Request,
    session: AsyncSession = Depends(get_db),
):
    """Register a new OmniCare user account.

    Validates that the username is unique, hashes the password with Argon2id,
    and issues a JWT session cookie on successful creation.

    Args:
        payload: Validated signup payload containing username and password.
        response: FastAPI response object used to set the session cookie.
        request: The request being audited; supplies the client IP and user
            agent for the audit trail entry.
        session: The async database session.

    Returns:
        Token: The access token, token type, and user ID.

    Raises:
        HTTPException: 409 if the username already exists or a database
        integrity error occurs.
    """
    existing = (
        await session.execute(select(User).where(User.username == payload.username))
    ).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(status_code=409, detail="An account with this username already exists")

    # Argon2 hashing is CPU-intensive; offload to a threadpool to avoid
    # blocking the async event loop.
    password_hash = await run_in_threadpool(hash_password, payload.password)
    user = User(
        id=uuid.uuid4(),
        username=payload.username.strip(),
        password_hash=password_hash,
    )
    session.add(user)

    # Account creation is an audit-worthy event: an operator asked "who opened
    # this account" has no other answer. Recorded inside the signup's own
    # transaction, so a rejected duplicate leaves neither row behind.
    await record_audit(
        session,
        "auth.signup",
        "user",
        str(user.id),
        after={"username": user.username},
        actor_id=user.id,
        request=request,
    )

    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(
            status_code=409,
            detail="An account with this username already exists",
        ) from exc
    await session.refresh(user)

    access_token = create_access_token(user.id)
    set_session_cookie(response, access_token)
    return {"access_token": access_token, "token_type": "bearer", "user_id": str(user.id)}


@router.post("/signin", response_model=Token)
async def signin(
    payload: UserSignin,
    response: Response,
    request: Request,
    session: AsyncSession = Depends(get_db),
):
    """Authenticate an existing user and issue a JWT session.

    Uses a timing-safe password comparison to prevent user enumeration
    attacks. On success, sets an HTTP-only session cookie.

    Args:
        payload: Validated signin payload containing username and password.
        response: FastAPI response object used to set the session cookie.
        request: The request being audited; supplies the client IP and user
            agent for both the success and the failure trail entries.
        session: The async database session.

    Returns:
        Token: The access token, token type, and user ID.

    Raises:
        HTTPException: 401 if credentials are invalid.
    """
    user = (
        await session.execute(select(User).where(User.username == payload.username.strip()))
    ).scalar_one_or_none()

    # Always perform password verification, even for non-existent users, to
    # prevent timing-based user enumeration.
    stored_hash = user.password_hash if user is not None else DUMMY_PASSWORD_HASH
    password_matches = await run_in_threadpool(verify_password, payload.password, stored_hash)

    if user is None or not password_matches:
        # A failed sign-in is the one event an operator needs more than a
        # successful one: it is how a credential-stuffing run announces itself.
        # The attempted username is recorded because it is the only identifier
        # the attempt produced, and no response ever echoes it back. The row is
        # committed before the 401 is raised, because a refusal that leaves
        # nothing behind is a trail with a hole exactly where it matters.
        await record_audit(
            session,
            "auth.signin_failed",
            "user",
            payload.username,
            request=request,
        )
        await session.commit()
        raise HTTPException(status_code=401, detail="Incorrect username or password")

    access_token = create_access_token(user.id)
    set_session_cookie(response, access_token)

    await record_audit(
        session,
        "auth.signin",
        "user",
        str(user.id),
        actor_id=user.id,
        request=request,
    )
    await session.commit()
    return {"access_token": access_token, "token_type": "bearer", "user_id": str(user.id)}


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    response: Response,
    request: Request,
    session: AsyncSession = Depends(get_db),
):
    """Invalidate the current session by clearing the session cookie.

    Args:
        response: FastAPI response object used to clear the session cookie.
        request: The request being audited; supplies the client IP and user
            agent for the audit trail entry.
        session: The async database session.

    Returns:
        Response: 204 No Content on success.
    """
    clear_session_cookie(response)
    response.status_code = status.HTTP_204_NO_CONTENT

    # The endpoint is deliberately public and reads no credential, so a logout
    # is recorded with no actor: what is knowable is that a sign-out happened,
    # from where, under which correlation id. An operator correlating a session
    # against the sign-in and the sign-in failure entries still gets the full
    # picture of the attempt.
    await record_audit(session, "auth.logout", "session", None, request=request)
    await session.commit()
    return response


@router.get("/me", response_model=UserMe)
async def get_current_user_info(current_user_id: str = Depends(get_current_user)):
    """Return the authenticated user's identity.

    This endpoint allows the frontend to validate the session on startup
    and refresh after page reloads without requiring a full sign-in flow.

    Args:
        current_user_id: The authenticated user's UUID (injected by dependency).

    Returns:
        UserMe: The authenticated user's ID.
    """
    return {"user_id": current_user_id}
