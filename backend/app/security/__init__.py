"""
Authentication and authorization for the OmniCare backend.

The package exposes the request-admission seam every protected route depends
on: JWT cookie/bearer resolution, Argon2id password hashing, and the
``get_current_user`` / ``require_role`` FastAPI dependencies.

The implementation lives in ``app.security.auth``; re-exporting the public API
here keeps route modules importing one stable path.
"""

from app.security.auth import (
    clear_session_cookie,
    create_access_token,
    get_current_user,
    hash_password,
    require_role,
    set_session_cookie,
    verify_password,
)

__all__ = [
    "clear_session_cookie",
    "create_access_token",
    "get_current_user",
    "hash_password",
    "require_role",
    "set_session_cookie",
    "verify_password",
]
