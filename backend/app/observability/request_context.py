"""Correlation identifiers shared by logs, telemetry, and HTTP responses.

The identifier lives in a :class:`~contextvars.ContextVar` rather than being
threaded through function arguments so any code on the request's call stack --
the logging formatter, the LiteLLM callback, a tool wrapper -- can read it
without changing a signature.
"""

from __future__ import annotations

import re
import secrets
from contextvars import ContextVar, Token

REQUEST_ID_HEADER = "X-Request-ID"

# A caller may supply its own id and it is echoed back, so the value has to be
# constrained before it can reach a log line: an unbounded header would let a
# client inject newlines and therefore forge log records.
_MAX_REQUEST_ID_LENGTH = 64
_REQUEST_ID_PATTERN = re.compile(r"[A-Za-z0-9._-]+")

_UNKNOWN_REQUEST_ID = "-"

_request_id: ContextVar[str] = ContextVar("omnicare_request_id", default="")


def get_request_id() -> str:
    """Return the correlation id bound to the current context.

    Returns:
        str: The id, or an empty string when no request is bound (background
        workers, startup, tests).
    """
    return _request_id.get()


def set_request_id(request_id: str) -> Token[str]:
    """Bind ``request_id`` to the current context.

    Args:
        request_id: Correlation id to make visible to the current call stack.

    Returns:
        Token[str]: Token to pass to :func:`reset_request_id` once the request
        that owns the id is finished.
    """
    return _request_id.set(request_id)


def reset_request_id(token: Token[str]) -> None:
    """Restore the correlation id that was active before ``token`` was created.

    Args:
        token: Token returned by :func:`set_request_id`.
    """
    _request_id.reset(token)


def generate_request_id() -> str:
    """Return a fresh correlation id.

    Returns:
        str: A 32-character hexadecimal identifier.

    The value is opaque, so it is minted from the standard library's CSPRNG
    rather than shaped into a ``UUID``: a correlation id gains nothing from
    UUID semantics, and reaching for ``uuid`` here would couple request
    handling to a module the test-suite routinely patches.
    """
    return secrets.token_hex(16)


def resolve_request_id(inbound: str | None) -> str:
    """Return the caller's correlation id when it is safe, otherwise a new one.

    Args:
        inbound: The value supplied by the caller, typically the ``X-Request-ID``
            header. May be ``None`` when the caller supplied nothing.

    Returns:
        str: A usable correlation id: either the validated caller value or a
        freshly generated one.
    """
    candidate = (inbound or "").strip()
    if (
        candidate
        and len(candidate) <= _MAX_REQUEST_ID_LENGTH
        and _REQUEST_ID_PATTERN.fullmatch(candidate)
    ):
        return candidate
    return generate_request_id()


def display_request_id() -> str:
    """Return the correlation id in the form used on every log record.

    Returns:
        str: The bound id, or ``"-"`` when no request is in context, so a log
        query can key on one field for every record in the stream.
    """
    return get_request_id() or _UNKNOWN_REQUEST_ID
