"""Request correlation middleware.

Implemented against the raw ASGI interface rather than as a
``BaseHTTPMiddleware`` subclass on purpose: the streaming chat endpoint is
sensitive to an extra task group wrapping its response body, and a plain ASGI
middleware keeps that path byte-identical while still running the downstream
application in the same context, which is what makes the correlation id visible
to the endpoint and to any task it spawns.
"""

from __future__ import annotations

import logging

from starlette.datastructures import Headers
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.observability.request_context import (
    REQUEST_ID_HEADER,
    reset_request_id,
    resolve_request_id,
    set_request_id,
)

logger = logging.getLogger(__name__)

# ASGI header names are lowercase bytes; keeping it pre-encoded avoids rebuilding
# the pair for every response.
_REQUEST_ID_HEADER_BYTES = REQUEST_ID_HEADER.lower().encode("latin-1")


class RequestIdMiddleware:
    """Bind a correlation id to each request and echo it back on the response.

    An inbound ``X-Request-ID`` is honoured when it is safe to echo, so a caller
    that already traces requests can stitch this service's logs into its own.
    """

    def __init__(self, app: ASGIApp) -> None:
        """Wrap ``app``.

        Args:
            app: The downstream ASGI application.
        """
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Bind the request id for the duration of the request.

        Args:
            scope: The ASGI connection scope.
            receive: ASGI receive callable.
            send: ASGI send callable.
        """
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        inbound = Headers(scope=scope).get(REQUEST_ID_HEADER)
        request_id = resolve_request_id(inbound)
        token = set_request_id(request_id)

        async def send_with_request_id(message: Message) -> None:
            if message["type"] == "http.response.start":
                _set_request_id_header(message, request_id)
            await send(message)

        try:
            await self.app(scope, receive, send_with_request_id)
        finally:
            reset_request_id(token)


def _set_request_id_header(message: Message, request_id: str) -> None:
    """Write the correlation id onto a response start message.

    An existing value is replaced rather than appended: an outer layer may have
    echoed a header of its own, and two ``X-Request-ID`` headers would make a
    caller's trace ambiguous rather than better.

    Args:
        message: The ASGI ``http.response.start`` message.
        request_id: The correlation id bound to this request.
    """
    encoded = (_REQUEST_ID_HEADER_BYTES, request_id.encode("latin-1"))
    headers = [
        header
        for header in message.setdefault("headers", [])
        if header[0].lower() != _REQUEST_ID_HEADER_BYTES
    ]
    headers.append(encoded)
    message["headers"] = headers
