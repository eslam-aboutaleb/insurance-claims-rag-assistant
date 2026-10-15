"""
Rate limiting configuration for the OmniCare backend.

This module instantiates a SlowAPI ``Limiter`` that is applied globally to
protect the backend from abuse and ensure fair resource usage across clients.

Rate limit strategy:
  - Key function: Extracts the user ID from the JWT token. If no valid user ID
    can be extracted (unauthenticated), it falls back to the client IP address.
  - Current limit: 20 requests/minute for chat endpoints, 10/minute for
    session reset. These limits are conservative enough for normal human
    interaction while blocking automated abuse.
  - Storage: in-process counters by default, which is exact for a single
    replica. A Redis-backed storage registered against the ``redis`` scheme
    makes the counters global, so the 20/minute chat limit is 20/minute for the
    deployment rather than for each instance.
"""

from __future__ import annotations

import logging
import time
from urllib.parse import urlsplit

from limits.storage import Storage
from slowapi import Limiter
from slowapi.util import get_remote_address

# The blocking RESP client and its codec live in ``core.redis_client``. One
# implementation of the wire protocol serves both this store and the
# idempotency cache, so the two cannot drift apart.
from app.core.config import settings
from app.core.redis_client import RedisClient, RedisProtocolError, RedisUnavailable
from app.observability.metrics import LimiterRejection, get_metrics
from app.observability.request_context import get_request_id

logger = logging.getLogger(__name__)

# Namespace for the counters this module writes to Redis, so an operator sharing
# a Redis database between environments can see what belongs to this service and
# can clear it without touching anything else.
_KEY_PREFIX = "omnicare:ratelimit:"


class RedisRateLimitStorage(Storage):
    """Fixed-window rate-limit counters kept in Redis.

    Registering against the ``redis`` scheme is what makes the ordinary
    ``Limiter(storage_uri=...)`` wiring resolve here: ``limits`` picks a storage
    class from the URI scheme, and the class in the package needs the ``redis``
    client library, which this deployment deliberately does not carry. The
    commands a fixed window actually needs -- INCRBY, EXPIRE, GET, PTTL, DEL,
    PING and a SCAN loop for reset -- are spoken directly instead.

    Only the fixed-window strategy is supported. SlowAPI selects it by default
    and a windowed counter is the whole of what a per-minute quota needs, so
    the moving-window and sliding-window interfaces stay unimplemented.

    Args:
        uri: The Redis URI SlowAPI was given.
        wrap_exceptions: Whether to wrap storage errors in StorageError.

    Returns:
        None
    """

    STORAGE_SCHEME = ["redis", "rediss"]

    # A counter is a few bytes, so a generous SCAN batch keeps the whole reset to
    # one round trip for the key counts this service produces.
    _SCAN_COUNT = 200
    # Bounded so a pathological keyspace degrades to a failed reset rather than
    # an unbounded loop.
    _MAX_SCAN_PASSES = 1_000

    def __init__(
        self,
        uri: str | None = None,
        wrap_exceptions: bool = False,
        **_: str | bool | float,
    ) -> None:
        parsed = urlsplit(uri or "redis://localhost:6379/0")
        self._uri = uri or "redis://localhost:6379/0"
        self._host = parsed.hostname or "localhost"
        self._port = parsed.port or 6379
        self._db = int((parsed.path or "/0").lstrip("/") or 0)
        self._password = parsed.password or None
        self._client: RedisClient | None = None
        super().__init__(uri, wrap_exceptions=wrap_exceptions)

    def _connection(self) -> RedisClient:
        """Return the client, building it on first use so import stays cheap."""
        if self._client is None:
            self._client = RedisClient(
                self._host,
                self._port,
                db=self._db,
                password=self._password,
                connect_timeout=settings.redis_connect_timeout_seconds,
                socket_timeout=settings.redis_socket_timeout_seconds,
            )
        return self._client

    def _namespaced(self, key: str) -> str:
        return f"{_KEY_PREFIX}{key}"

    @property
    def base_exceptions(self) -> tuple[type[BaseException], ...]:
        """The failures that mean Redis, not the caller, is the problem."""
        return (RedisUnavailable, RedisProtocolError)

    def incr(self, key: str, expiry: int, amount: int = 1) -> int:
        """Count one hit in the current window and start the window if new.

        Args:
            key: The rate-limit key SlowAPI derived for this request.
            expiry: Window length in seconds.
            amount: The cost of the hit; the fixed-window strategy leaves it at 1.

        Returns:
            int: The number of hits recorded in the current window, including
            this one.
        """
        client = self._connection()
        prefixed = self._namespaced(key)
        current = int(client.execute("INCRBY", prefixed, int(amount)))
        if current == int(amount):
            # The window starts with the first hit that lands in it.
            client.execute("EXPIRE", prefixed, int(expiry))
        return current

    def get(self, key: str) -> int:
        """Return the hits recorded in the current window.

        Args:
            key: The rate-limit key.

        Returns:
            int: The counter, or 0 when no window is open.
        """
        reply = self._connection().get(self._namespaced(key))
        return 0 if reply is None else int(reply)

    def get_expiry(self, key: str) -> float:
        """Return when the current window for a key ends.

        Args:
            key: The rate-limit key.

        Returns:
            float: A POSIX timestamp. A key with no live window reports the
            current time, matching the in-memory storage.
        """
        ttl_ms = int(self._connection().execute("PTTL", self._namespaced(key)))
        if ttl_ms < 0:
            return time.time()
        return time.time() + ttl_ms / 1000.0

    def clear(self, key: str) -> None:
        """Close the window for a key.

        Args:
            key: The rate-limit key.

        Returns:
            None
        """
        self._connection().delete(self._namespaced(key))

    def check(self) -> bool:
        """Report whether Redis answers, which lets SlowAPI recover after an outage.

        Returns:
            bool: True when Redis responded to PING.
        """
        return self._connection().ping()

    def reset(self) -> int:
        """Clear every counter this service owns and report how many there were.

        Only this module's namespace is scanned, so a shared Redis database is
        never emptied by a reset.

        Returns:
            int: The number of counters removed.
        """
        client = self._connection()
        keys = self._scan_namespace_keys()
        client.delete(*keys)
        return len(keys)

    def _scan_namespace_keys(self) -> list[str]:
        """Collect this service's keys with SCAN, which never blocks Redis."""
        client = self._connection()
        pattern = f"{_KEY_PREFIX}*"
        cursor = "0"
        keys: dict[str, None] = {}
        for _ in range(self._MAX_SCAN_PASSES):
            reply = client.execute(
                "SCAN",
                cursor,
                "MATCH",
                pattern,
                "COUNT",
                str(self._SCAN_COUNT),
            )
            next_cursor, batch = reply[0], reply[1]
            cursor = next_cursor.decode() if isinstance(next_cursor, bytes) else str(next_cursor)
            for key in batch or []:
                keys[key.decode() if isinstance(key, bytes) else str(key)] = None
            if cursor == "0":
                break
        return list(keys)


def _user_or_ip_key_func(request):
    """Extract the client IP address or user ID from the incoming request.

    SlowAPI calls this function for every request to determine which rate
    limit bucket to apply. We prioritize user ID from the token for authenticated
    users to implement production-level rate limiting, and fall back to IP.

    Args:
        request: The ASGI request object.

    Returns:
        str: The rate limit key, e.g., 'user:UUID' or 'ip:127.0.0.1'.
    """
    token = None
    # Check Authorization header first
    auth_header = request.headers.get("Authorization")
    if auth_header and auth_header.startswith("Bearer "):
        token = auth_header.split(" ")[1]

    # Check cookie fallback
    if not token:
        token = request.cookies.get("omnicare_access_token")

    if token:
        try:
            from app.security.auth import _decode_token  # noqa: PLC0415

            user_id = _decode_token(token)
            return f"user:{str(user_id)}"
        except Exception:  # noqa: S110
            pass

    return f"ip:{get_remote_address(request)}"


def record_rate_limit_rejection(route: str) -> None:
    """Report one rejected request to the telemetry store.

    Rejections are the visible symptom of either abuse or a limit set too tight,
    so they belong beside the cost and latency data an operator reads when the
    service looks unhealthy. Kept as a module-level hook so the caller -- the
    application's exception handler -- stays unaware of how telemetry is stored.

    Args:
        route: Request path that was refused.
    """
    get_metrics().record_limiter_rejection(
        LimiterRejection(
            timestamp=time.time(),
            route=route,
            request_id=get_request_id() or "-",
        )
    )


def _build_limiter() -> Limiter:
    """Build the SlowAPI limiter selected by the settings.

    Redis-backed counters are only used when the deployment asks for them *and*
    a Redis URI is configured; otherwise, and if the URI cannot be turned into a
    storage, the limiter keeps its in-process counters. SlowAPI is also told to
    swallow storage errors in the Redis case: a Redis outage should cost the
    service its per-minute quota, not the availability of the chat endpoints
    that quota exists to protect.

    Returns:
        Limiter: A limiter with global counters when Redis is configured, and
        per-process counters otherwise.
    """
    if settings.rate_limit_storage == "redis" and settings.redis_enabled:
        try:
            return Limiter(
                key_func=_user_or_ip_key_func,
                storage_uri=settings.redis_url,
                swallow_errors=True,
            )
        except Exception:
            logger.warning(
                "Redis rate-limit storage could not be initialised from REDIS_URL "
                "'%s'; falling back to per-process counters.",
                settings.redis_url,
                exc_info=True,
            )
    return Limiter(key_func=_user_or_ip_key_func)


# Module-level limiter instance. Import this and apply ``@limiter.limit()``
# decorators to FastAPI route handlers to enforce rate limits.
limiter = _build_limiter()
