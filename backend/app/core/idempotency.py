"""
Request idempotency cache for the OmniCare chat endpoint.

Industry standard: clients supply an ``Idempotency-Key`` header (UUID or any
opaque string). If the server has already successfully processed a request
with that key it returns the cached response instead of executing the agent
again. This prevents duplicate LLM calls when clients retry on network errors.

If the client omits the header, a deterministic key is derived from
``sha256(user_id + ":" + message)`` — ensuring that byte-identical retries
of the same message from the same user are naturally deduplicated.

Design:
- Pluggable backends: an in-process TTL cache (the default, correct for a
  single-process deployment) or Redis, which shares one cache across every
  replica so a retry replays regardless of which instance receives it.
- Configurable TTL (default: 5 min) and max size (default: 1 000 entries).
- Entries are evicted in FIFO order when the cache is full, in addition to
  TTL expiry on access.
- Thread-safe for async use (single event-loop, no cross-thread mutations).
- Keys are namespaced by user_id to prevent cross-user cache hits.
- Request body hash is stored and verified to prevent body-mismatch replays.
- Per-key asyncio.Lock prevents check-then-act races. With Redis, a claim key
  extends the same guarantee across processes: the instance that wins the
  claim computes the response and the others wait for and replay it.
- Redis problems never fail a request. A missing or unreachable Redis leaves
  the in-process cache in place, which merely costs the deduplication that
  Redis existed to provide.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from collections import defaultdict
from collections.abc import Callable
from functools import wraps
from typing import Any, TypeVar

from fastapi import HTTPException, status

from app.core.config import settings
from app.core.redis_client import AsyncRedisClient, RedisUnavailable

logger = logging.getLogger(__name__)

# ── Configuration ─────────────────────────────────────────────────────────────
# Maximum number of cached responses. When full, the oldest entry is evicted.
_MAX_SIZE: int = 1_000

# Seconds a cached response is considered valid. After this window the key is
# expired and the request is processed fresh.
_TTL_SECONDS: int = 300  # 5 minutes

# Namespace for every key this module writes to Redis, so an operator sharing a
# Redis database between environments can see (and if necessary clear) exactly
# what belongs to this service.
_KEY_PREFIX = "omnicare:idempotency:"

# How long a claim on a key is honoured and how long a losing replica waits for
# the winner's response before giving up and computing it itself. Both are
# deliberately short: a stalled winner must not turn a client retry into a hang.
_CLAIM_TTL_SECONDS = 10.0
_CLAIM_POLL_SECONDS = 0.05
_CLAIM_MAX_WAIT_SECONDS = 5.0


# ── Internal state ────────────────────────────────────────────────────────────
# Each entry: {"response": dict, "request_hash": str, "cached_at": float}
_cache: dict[str, dict[str, Any]] = {}
_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)


# ── In-memory backend ─────────────────────────────────────────────────────────


class IdempotencyBackend:
    """Storage for the chat responses a replay can return."""

    async def lookup(self, key: str) -> dict[str, Any] | None:
        """Return the stored entry for a key, or None when there is none."""
        raise NotImplementedError

    async def store(self, key: str, response: Any, request_hash: str) -> None:
        """Keep a response under a key so a later replay can return it."""
        raise NotImplementedError

    async def claim(self, key: str) -> bool:
        """Reserve the right to compute a response nobody has stored yet.

        Returns:
            bool: True when the caller must compute the response itself. The
            in-process backend never claims because the per-key asyncio lock
            already serialises its callers.
        """
        return True

    async def await_claimed_result(self, key: str) -> dict[str, Any] | None:
        """Wait for another replica to publish the response it claimed."""
        return None

    async def release_claim(self, key: str) -> None:
        """Give up a claim, whether or not the response was published."""


class _MemoryIdempotencyBackend(IdempotencyBackend):
    """The original in-process dictionary, with its TTL and FIFO eviction."""

    async def lookup(self, key: str) -> dict[str, Any] | None:
        return get_cached_entry(key)

    async def store(self, key: str, response: Any, request_hash: str) -> None:
        store_response(key, response, request_hash)


class RedisIdempotencyBackend(IdempotencyBackend):
    """Shared idempotency cache backed by Redis.

    The claim step is what makes this worth the round-trips: without it, two
    replicas that receive the same retry at the same moment would both miss the
    cache and both call the model, which is the duplicate work Redis was
    introduced to prevent. The first replica to claim the key computes and
    publishes the response; the others wait for it and replay it.

    Every Redis failure degrades instead of raising: a cache that is not
    answering must not turn a client retry into a 500.

    Args:
        client: Connected-on-first-use Redis client.
    """

    def __init__(  # noqa: PLR0913 - one knob per behaviour worth tuning
        self,
        client: AsyncRedisClient,
        *,
        key_prefix: str = _KEY_PREFIX,
        ttl_seconds: int = _TTL_SECONDS,
        claim_ttl_seconds: float = _CLAIM_TTL_SECONDS,
        poll_seconds: float = _CLAIM_POLL_SECONDS,
        max_wait_seconds: float = _CLAIM_MAX_WAIT_SECONDS,
    ) -> None:
        self._client = client
        self._key_prefix = key_prefix
        self._ttl_seconds = ttl_seconds
        self._claim_ttl_seconds = claim_ttl_seconds
        self._poll_seconds = poll_seconds
        self._max_wait_seconds = max_wait_seconds

    def _key(self, key: str) -> str:
        return f"{self._key_prefix}{key}"

    def _claim_key(self, key: str) -> str:
        return f"{self._key(key)}:claim"

    async def lookup(self, key: str) -> dict[str, Any] | None:
        """Return the stored entry for a key, or None on a miss or an outage."""
        raw = await self._get(self._key(key))
        if raw is None:
            return None
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("Discarding unreadable idempotency payload for key '%s'.", key)
            return None
        if not isinstance(payload, dict):
            logger.warning("Discarding non-object idempotency payload for key '%s'.", key)
            return None
        return {
            "response": payload.get("response"),
            "request_hash": payload.get("request_hash", ""),
        }

    async def store(self, key: str, response: Any, request_hash: str) -> None:
        """Publish a completed response under the key, overwriting a claim."""
        payload = json.dumps({"response": response, "request_hash": request_hash})
        await self._set(self._key(key), payload)

    async def claim(self, key: str) -> bool:
        """Claim the key, or False when another replica already holds it."""
        try:
            return await self._client.set(
                self._claim_key(key), "1", ttl=self._claim_ttl_seconds, nx=True
            )
        except RedisUnavailable:
            # Behave as if the claim succeeded: computing the response locally
            # is safer than refusing the request.
            return True

    async def await_claimed_result(self, key: str) -> dict[str, Any] | None:
        """Poll for the response another replica claimed until it publishes.

        Giving up as soon as the claim disappears means a replica that died
        mid-computation cannot stall the retry; the waiter computes the
        response itself instead.
        """
        deadline = time.monotonic() + self._max_wait_seconds
        while True:
            entry = await self.lookup(key)
            if entry is not None:
                return entry
            try:
                still_claimed = await self._client.exists(self._claim_key(key))
            except RedisUnavailable:
                return None
            if not still_claimed or time.monotonic() >= deadline:
                return None
            await asyncio.sleep(self._poll_seconds)

    async def release_claim(self, key: str) -> None:
        """Drop the claim so a later retry does not wait on this one."""
        try:
            await self._client.delete(self._claim_key(key))
        except RedisUnavailable:
            logger.debug("Could not release idempotency claim for key '%s'.", key)

    async def ping(self) -> bool:
        """Report whether the backing Redis answers; used when wiring startup."""
        return await self._client.ping()

    async def close(self) -> None:
        """Drop the connection, leaving the next command to open a new one.

        The startup probe must not leave a connection open in the loop that
        happened to run it: whatever context issues the first real command is
        the one that should own the connection.
        """
        await self._client.close()

    async def _get(self, key: str) -> str | None:
        try:
            return await self._client.get(key)
        except RedisUnavailable:
            logger.warning("Redis unreachable while reading key '%s'.", key)
            return None

    async def _set(self, key: str, payload: str) -> None:
        try:
            await self._client.set(key, payload, ttl=self._ttl_seconds)
        except RedisUnavailable:
            logger.warning("Redis unreachable while writing key '%s'.", key)


# ── Backend selection ─────────────────────────────────────────────────────────
# The active backend is held in a one-element list so the module can swap it
# without a ``global`` statement.
_active: list[IdempotencyBackend | None] = [None]


def _backend() -> IdempotencyBackend:
    """Return the configured backend, defaulting to the in-process cache."""
    if _active[0] is None:
        _active[0] = _MemoryIdempotencyBackend()
    return _active[0]


def uses_distributed_store() -> bool:
    """Whether responses are cached in Redis and therefore shared by replicas."""
    return isinstance(_backend(), RedisIdempotencyBackend)


def build_redis_backend(
    url: str,
    *,
    connect_timeout: float,
    socket_timeout: float,
) -> RedisIdempotencyBackend:
    """Build a Redis-backed backend from a URI.

    Args:
        url: Redis URI, for example ``redis://redis:6379/0``.
        connect_timeout: Seconds to wait for the TCP connection.
        socket_timeout: Seconds to wait for a reply.

    Returns:
        RedisIdempotencyBackend: A backend that connects on first use.
    """
    client = AsyncRedisClient.from_url(
        url,
        connect_timeout=connect_timeout,
        socket_timeout=socket_timeout,
    )
    return RedisIdempotencyBackend(client)


async def configure_idempotency_storage() -> None:
    """Select the idempotency backend from the runtime settings.

    Called once from the application lifespan. Redis is used only when the
    deployment asks for it *and* the configured Redis answers a PING; anything
    else leaves the in-process cache in place, because a store that is not
    there must not stop the chat endpoint from accepting requests.
    """
    if settings.idempotency_storage == "redis" and settings.redis_enabled:
        try:
            backend = build_redis_backend(
                settings.redis_url,
                connect_timeout=settings.redis_connect_timeout_seconds,
                socket_timeout=settings.redis_socket_timeout_seconds,
            )
            if await backend.ping():
                await backend.close()
                _active[0] = backend
                logger.info(
                    "Idempotency responses are cached in Redis at %s and shared "
                    "across replicas.",
                    settings.redis_url,
                )
                return
            logger.warning(
                "Redis at %s did not answer; the idempotency cache stays in-process, "
                "so a retry that lands on another replica is not deduplicated.",
                settings.redis_url,
            )
        except ValueError as exc:
            logger.warning(
                "Configured REDIS_URL could not be used (%s); the idempotency cache "
                "stays in-process.",
                exc,
            )
    _active[0] = _MemoryIdempotencyBackend()


def _reset_backend_for_tests() -> None:
    """Return to the default in-process backend. Intended for tests only."""
    _active[0] = None


def _canonical_json(data: Any) -> str:
    """Serialize data to a canonical JSON string for hashing."""
    return json.dumps(data, sort_keys=True, separators=(",", ":"))


def _compute_request_hash(body: Any) -> str:
    """Compute SHA-256 hash of the canonical JSON representation of a request body."""
    canonical = _canonical_json(body)
    return hashlib.sha256(canonical.encode()).hexdigest()


def make_idempotency_key(user_id: str, message: str) -> str:
    """
    Derive a deterministic idempotency key from user and message content.

    Used as the fallback when the client does not supply an explicit
    ``Idempotency-Key`` header.  The SHA-256 digest ensures:
    - Identical (user_id, message) pairs always map to the same key.
    - Different users with the same message text get different keys.
    - The key is a fixed-length hex string safe for use as a dict key.

    Args:
        user_id: The authenticated user identifier.
        message: The raw message text.

    Returns:
        64-character hex SHA-256 digest.
    """
    payload = f"{user_id}:{message}".encode()
    return hashlib.sha256(payload).hexdigest()


def get_cached_entry(key: str) -> dict[str, Any] | None:
    """Return the full cache entry for the key if it exists and has not expired.

    Callers that need the stored request hash must use this rather than
    :func:`get_cached_response`, which returns only the response body and would make
    the body-mismatch check unreachable.

    Args:
        key: The idempotency key to look up.

    Returns:
        The entry dict (``response``, ``request_hash``, ``cached_at``), or ``None`` if
        absent or expired.
    """
    entry = _cache.get(key)
    if entry is None:
        return None

    age = time.monotonic() - entry["cached_at"]
    if age > _TTL_SECONDS:
        _cache.pop(key, None)
        _locks.pop(key, None)
        logger.debug("Idempotency key '%s' expired after %.1fs.", key, age)
        return None

    logger.info(
        "Cache HIT for idempotency key '%s' (age=%.1fs). Returning cached response.",
        key,
        age,
    )
    return entry


def get_cached_response(key: str) -> dict[str, Any] | None:
    """
    Return a cached response if the key exists and has not expired.

    Args:
        key: The idempotency key to look up.

    Returns:
        The cached response dict, or ``None`` if absent or expired.
    """
    entry = get_cached_entry(key)
    return None if entry is None else entry["response"]


def store_response(key: str, response: dict[str, Any], request_hash: str) -> None:
    """
    Store a successfully computed response under the given idempotency key.

    When the cache is at capacity the oldest entry is evicted before inserting
    the new one (FIFO, relying on dict insertion-order guarantee in Python 3.7+).

    Args:
        key: The idempotency key.
        response: The agent response dict to cache.
        request_hash: SHA-256 hash of the request body for integrity checking.
    """
    if len(_cache) >= _MAX_SIZE:
        oldest_key = next(iter(_cache))
        _cache.pop(oldest_key)
        _locks.pop(oldest_key, None)
        logger.debug("Idempotency cache full. Evicted oldest key '%s'.", oldest_key)

    _cache[key] = {
        "response": response,
        "request_hash": request_hash,
        "cached_at": time.monotonic(),
    }
    logger.debug("Stored response under idempotency key '%s'.", key)


def cache_size() -> int:
    """Return the current number of entries in the idempotency cache."""
    return len(_cache)


def clear_cache() -> None:
    """
    Clear all cached entries and locks.

    Intended for use in tests to ensure isolation between test cases.
    """
    _cache.clear()
    _locks.clear()


F = TypeVar("F", bound=Callable[..., Any])


def idempotent_endpoint() -> Callable[[F], F]:
    """
    Decorator for FastAPI route handlers to abstract idempotency cache checking.

    It expects the route handler to have `idempotency_key` (str), `response`
    (Response), `current_user_id` (str), and `payload` (request body) as
    injected keyword arguments. The decorator:
    1. Namespaces the cache key by user_id.
    2. Verifies the request body hash matches the cached hash.
    3. Uses per-key asyncio.Lock to prevent check-then-act races, extended to
       Redis claim keys so replicas cannot both miss and both run the agent.
    """

    def decorator(func: F) -> F:
        @wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            idempotency_key = kwargs.get("idempotency_key")
            response = kwargs.get("response")
            current_user_id = kwargs.get("current_user_id")
            payload = kwargs.get("payload")

            if not idempotency_key and current_user_id and payload is not None:
                idempotency_key = make_idempotency_key(
                    current_user_id,
                    payload.message if hasattr(payload, "message") else str(payload),
                )

            if idempotency_key and response and current_user_id:
                namespaced_key = f"{current_user_id}:{idempotency_key}"
                backend = _backend()

                request_body = (
                    payload.model_dump() if payload and hasattr(payload, "model_dump") else payload
                )
                current_request_hash = (
                    _compute_request_hash(request_body) if request_body is not None else ""
                )

                async def replay(entry: dict[str, Any]) -> Any:
                    """Return a stored response, refusing a key reused with a new body."""
                    cached_hash = entry.get("request_hash", "")
                    if cached_hash and current_request_hash and cached_hash != current_request_hash:
                        logger.warning(
                            "Idempotency key '%s' reused with different body. Returning 409.",
                            namespaced_key,
                        )
                        raise HTTPException(
                            status_code=status.HTTP_409_CONFLICT,
                            detail="Idempotency key reused with a different request body.",
                        )
                    response.headers["X-Idempotent-Replayed"] = "true"
                    response.headers["X-Idempotency-Key"] = idempotency_key
                    return entry.get("response")

                async with _locks[namespaced_key]:
                    cached_entry = await backend.lookup(namespaced_key)
                    if cached_entry is not None:
                        return await replay(cached_entry)

                    if await backend.claim(namespaced_key):
                        try:
                            result = await func(*args, **kwargs)
                        finally:
                            await backend.release_claim(namespaced_key)
                    else:
                        published = await backend.await_claimed_result(namespaced_key)
                        if published is not None:
                            return await replay(published)
                        result = await func(*args, **kwargs)

                    response.headers["X-Idempotency-Key"] = idempotency_key
                    stored = result.model_dump() if hasattr(result, "model_dump") else result
                    await backend.store(namespaced_key, stored, current_request_hash)
            else:
                result = await func(*args, **kwargs)

            return result

        return wrapper  # type: ignore

    return decorator
