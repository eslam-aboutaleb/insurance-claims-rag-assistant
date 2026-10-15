"""
Custom middleware for the OmniCare Financial backend.

This module provides reusable Starlette middleware components that enforce
cross-cutting concerns such as request payload size limits. These middleware
are registered globally on the FastAPI application instance in ``app/main.py``
and apply to every incoming request.
"""

from fastapi import Request, Response
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware


class RequestSizeLimitMiddleware(BaseHTTPMiddleware):
    """Enforce a maximum request body size to protect against oversized payloads.

    This middleware checks the ``Content-Length`` header first. If the header is
    missing or the request uses chunked transfer encoding, the middleware reads
    the request body up to the configured limit and returns an HTTP 413
    (Payload Too Large) response if the actual body exceeds the threshold.
    This prevents unnecessary memory allocation and protects downstream
    processing from malformed or maliciously large requests.

    Attributes:
        max_upload_size: Maximum allowed request body size in bytes.
    """

    def __init__(self, app, max_upload_size: int = 5 * 1024 * 1024):
        """Initialize the middleware with the target ASGI app and size limit.

        Args:
            app: The downstream ASGI application to wrap.
            max_upload_size: Maximum allowed request body size in bytes.
                Defaults to 5 MiB.
        """
        super().__init__(app)
        self.max_upload_size = max_upload_size

    async def dispatch(self, request: Request, call_next) -> Response:
        """Intercept the request and enforce the size limit.

        Reads the ``Content-Length`` header and compares it against
        ``max_upload_size``. If the header is missing or unparseable, the
        request body is read and measured. If the body exceeds the limit,
        a 413 response is returned. Otherwise, the downstream application
        receives the request with the cached body.

        Args:
            request: The incoming ASGI request.
            call_next: The downstream middleware or application callable.

        Returns:
            Response: Either a 413 JSON error response or the downstream
            response.
        """
        content_length = request.headers.get("content-length")
        exceeded = False

        if content_length:
            try:
                if int(content_length) > self.max_upload_size:
                    exceeded = True
            except ValueError:
                exceeded = False
        else:
            body = await request.body()
            if len(body) > self.max_upload_size:
                exceeded = True

        if exceeded:
            return JSONResponse(
                status_code=413,
                content={
                    "error": {
                        "code": "PAYLOAD_TOO_LARGE",
                        "message": (
                            f"Request body exceeds the maximum allowed size of "
                            f"{self.max_upload_size} bytes."
                        ),
                    }
                },
            )

        return await call_next(request)
