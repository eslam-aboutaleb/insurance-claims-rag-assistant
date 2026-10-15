"""
Tests for the distributed state: the Redis-backed shared stores and the
PostgreSQL-backed session service.

What is under test:

- Two idempotency backends pointing at the same Redis URL share one replay
  cache, including the claim that stops two replicas from both running the
  agent for the same retry.
- A key reused with a different body is refused with 409, over Redis as well as
  in-process, and the agent runs once.
- Rate-limit counters written by one limiter instance are visible to another,
  and a reset clears only this service's keys.
- A session created through one Runner is retrievable through a second Runner
  built against a fresh session service, which is what makes a second replica
  able to continue a conversation.

Redis is exercised against an in-process RESP server on a background thread
rather than a container, so two clients connecting to its URI really do share
one keyspace -- the property the stores exist for -- without the suite depending
on a service it does not deploy.
"""

from __future__ import annotations

import asyncio
import re
import socket
import threading
import time
from collections.abc import Iterator
from unittest.mock import patch

import pytest

from app.core.idempotency import build_redis_backend


# ── In-process Redis stand-in ─────────────────────────────────────────────────


class _NeedMoreData(Exception):
    """A command is only partly received; read more bytes and try again."""


def _read_line(buffer: bytearray, position: int) -> tuple[bytes, int]:
    """Return the line starting at ``position`` and the offset after it."""
    end = buffer.find(b"\r\n", position)
    if end == -1:
        raise _NeedMoreData
    return bytes(buffer[position:end]), end + 2


def _parse_one_request(buffer: bytearray, position: int) -> tuple[list[bytes], int]:
    """Decode one RESP array of bulk strings from the front of the buffer."""
    header, position = _read_line(buffer, position)
    if not header.startswith(b"*"):
        raise ValueError("The clients under test only emit RESP arrays.")
    args: list[bytes] = []
    for _ in range(int(header[1:])):
        line, position = _read_line(buffer, position)
        if not line.startswith(b"$"):
            raise ValueError("The clients under test only emit RESP bulk strings.")
        length = int(line[1:])
        end = position + length
        if len(buffer) < end + 2:
            raise _NeedMoreData
        args.append(bytes(buffer[position:end]))
        position = end + 2
    return args, position


def _parse_requests(buffer: bytearray) -> tuple[list[list[bytes]], int]:
    """Decode every complete request in the buffer, reporting bytes consumed."""
    commands: list[list[bytes]] = []
    position = 0
    while True:
        try:
            args, position = _parse_one_request(buffer, position)
        except _NeedMoreData:
            return commands, position
        commands.append(args)


def _encode_reply(value: object) -> bytes:
    """Encode one reply in RESP2.

    Args:
        value: ``bytes``/``str`` for a bulk reply, ``bool``/``int`` for an
            integer reply, ``list`` for an array, and ``None`` for a null reply.

    Returns:
        bytes: The encoded reply, ready to write to the socket.
    """
    if value is None:
        return b"$-1\r\n"
    if isinstance(value, bool):
        return b":1\r\n" if value else b":0\r\n"
    if isinstance(value, int):
        return b":%d\r\n" % value
    if isinstance(value, str):
        value = value.encode()
    if isinstance(value, bytes):
        return b"$%d\r\n%s\r\n" % (len(value), value)
    if isinstance(value, list):
        return b"*%d\r\n%s" % (len(value), b"".join(_encode_reply(item) for item in value))
    raise TypeError(f"Cannot encode {type(value)!r} as a RESP reply")


def _glob_to_pattern(pattern: bytes) -> re.Pattern[bytes]:
    """Translate a Redis glob pattern into an equivalent regular expression."""
    translated = ""
    for byte in pattern:
        if byte == 0x2A:  # "*"
            translated += ".*"
        elif byte == 0x3F:  # "?"
            translated += "."
        else:
            translated += re.escape(bytes([byte]).decode("latin-1"))
    return re.compile(translated.encode("latin-1") + b"$")


class _FakeRedisServer:
    """The subset of Redis the stores speak, served on one background thread.

    A real TCP socket is used rather than a mock so the asyncio client and the
    blocking client can both connect to one keyspace exactly as they would in a
    deployment.
    """

    def __init__(self) -> None:
        self._values: dict[bytes, tuple[bytes, float | None]] = {}
        self._lock = threading.Lock()
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(16)
        self.host, self.port = self._listener.getsockname()
        self.url = f"redis://{self.host}:{self.port}/0"
        self._stopping = threading.Event()
        self._thread = threading.Thread(target=self._accept, name="fake-redis", daemon=True)

    def __enter__(self) -> _FakeRedisServer:
        self._thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def close(self) -> None:
        """Stop accepting connections and free the listening socket."""
        self._stopping.set()
        try:
            self._listener.close()
        except OSError:  # pragma: no cover - best effort teardown
            pass
        self._thread.join(timeout=2)

    def _accept(self) -> None:
        while not self._stopping.is_set():
            try:
                connection, _ = self._listener.accept()
            except OSError:
                return
            worker = threading.Thread(target=self._serve, args=(connection,), daemon=True)
            worker.start()

    def _serve(self, connection: socket.socket) -> None:
        with connection:
            connection.settimeout(5)
            buffer = bytearray()
            while not self._stopping.is_set():
                try:
                    chunk = connection.recv(65536)
                except OSError:
                    return
                if not chunk:
                    return
                buffer.extend(chunk)
                commands, consumed = _parse_requests(buffer)
                del buffer[:consumed]
                for args in commands:
                    connection.sendall(_encode_reply(self._execute(args)))

    def _execute(self, args: list[bytes]) -> object:  # noqa: PLR0911 - one branch per command
        command = args[0].upper()
        with self._lock:
            if command == b"PING":
                return b"PONG"
            if command == b"SET":
                return self._set(args[1:])
            if command == b"GET":
                return self._fetch(args[1])
            if command == b"DEL":
                return self._delete(args[1:])
            if command == b"EXISTS":
                return self._count_live(args[1:])
            if command == b"EXPIRE":
                return self._expire(args[1], int(args[2]))
            if command == b"PTTL":
                return self._pttl(args[1])
            if command == b"TTL":
                return self._ttl(args[1])
            if command == b"INCRBY":
                return self._incrby(args[1], int(args[2]))
            if command == b"SCAN":
                return self._scan(args[1:])
            if command in (b"AUTH", b"SELECT"):
                return b"OK"
        raise ValueError(f"The fake Redis server does not implement {command!r}")

    def _fetch(self, key: bytes) -> bytes | None:
        self._drop_if_expired(key)
        stored = self._values.get(key)
        return None if stored is None else stored[0]

    def _set(self, args: list[bytes]) -> bytes | None:
        key, value = args[0], args[1]
        ttl: int | None = None
        only_if_new = False
        position = 2
        while position < len(args):
            option = args[position].upper()
            if option in (b"EX", b"PX"):
                ttl = int(args[position + 1])
                if option == b"PX":
                    ttl = max(1, ttl // 1000)
                position += 2
            elif option == b"NX":
                only_if_new = True
                position += 1
            else:
                raise ValueError(f"The fake Redis server does not implement SET {option!r}")
        self._drop_if_expired(key)
        if only_if_new and key in self._values:
            return None
        self._values[key] = (value, None if ttl is None else self._deadline(ttl))
        return b"OK"

    def _delete(self, keys: list[bytes]) -> int:
        removed = 0
        for key in keys:
            self._drop_if_expired(key)
            removed += self._values.pop(key, None) is not None
        return removed

    def _count_live(self, keys: list[bytes]) -> int:
        live = 0
        for key in keys:
            self._drop_if_expired(key)
            live += key in self._values
        return live

    def _expire(self, key: bytes, seconds: int) -> int:
        self._drop_if_expired(key)
        stored = self._values.get(key)
        if stored is None:
            return 0
        self._values[key] = (stored[0], self._deadline(seconds))
        return 1

    def _pttl(self, key: bytes) -> int:
        self._drop_if_expired(key)
        stored = self._values.get(key)
        if stored is None:
            return -2
        if stored[1] is None:
            return -1
        return max(0, round((stored[1] - time.monotonic()) * 1000))

    def _ttl(self, key: bytes) -> int:
        ttl_ms = self._pttl(key)
        return -1 if ttl_ms < 0 else max(1, ttl_ms // 1000)

    def _incrby(self, key: bytes, amount: int) -> int:
        self._drop_if_expired(key)
        current = int(self._fetch(key) or 0)
        current += amount
        stored = self._values.get(key)
        expires_at = None if stored is None else stored[1]
        self._values[key] = (str(current).encode(), expires_at)
        return current

    def _scan(self, args: list[bytes]) -> list[object]:
        pattern = _glob_to_pattern(b"*")
        position = 1
        while position < len(args):
            option = args[position].upper()
            if option == b"MATCH":
                pattern = _glob_to_pattern(args[position + 1])
                position += 2
            elif option == b"COUNT":
                position += 2
            else:
                raise ValueError(f"The fake Redis server does not implement SCAN {option!r}")
        self._drop_expired()
        # Every key is returned in one pass: all a reset at this scale needs, and
        # it keeps the fake server honest about the SCAN reply shape.
        return [b"0", [key for key in self._values if pattern.match(key)]]

    def _deadline(self, seconds: int) -> float:
        return time.monotonic() + seconds

    def _drop_if_expired(self, key: bytes) -> None:
        stored = self._values.get(key)
        if stored is not None and stored[1] is not None and stored[1] <= time.monotonic():
            del self._values[key]

    def _drop_expired(self) -> None:
        for key in list(self._values):
            self._drop_if_expired(key)


@pytest.fixture()
def fake_redis() -> Iterator[_FakeRedisServer]:
    """Provide a running Redis stand-in that shares one keyspace between clients."""
    with _FakeRedisServer() as server:
        yield server


@pytest.fixture()
def redis_backends(fake_redis: _FakeRedisServer) -> Iterator[tuple[object, object]]:
    """Build two idempotency backends over one Redis URI.

    Two objects rather than one, because the property under test is that a
    *second* instance sees the first instance's replay.
    """
    first = build_redis_backend(fake_redis.url, connect_timeout=2.0, socket_timeout=2.0)
    second = build_redis_backend(fake_redis.url, connect_timeout=2.0, socket_timeout=2.0)
    yield first, second


@pytest.fixture()
def wired_idempotency(test_client, fake_redis: _FakeRedisServer, monkeypatch) -> Iterator[None]:
    """Point the idempotency cache at the Redis stand-in and restore afterwards.

    The store is wired with the application's own startup function, but from
    this test's loop: the probe connection is closed when the probe succeeds, so
    the loop that serves the request opens its own. Depending on ``test_client``
    guarantees the application has already started.
    """
    from app.core.config import settings
    from app.core.idempotency import _reset_backend_for_tests, configure_idempotency_storage

    monkeypatch.setattr(settings, "idempotency_storage", "redis")
    monkeypatch.setattr(settings, "redis_url", fake_redis.url)
    asyncio.run(configure_idempotency_storage())
    try:
        yield
    finally:
        _reset_backend_for_tests()


# ── Idempotency across instances ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_replay_is_shared_between_two_instances(redis_backends) -> None:
    """A response stored by one instance is replayed by the other."""
    first, second = redis_backends

    await first.store("user@example.test:key-shared", {"answer": "water damage"}, "hash-1")

    entry = await second.lookup("user@example.test:key-shared")
    assert entry == {"response": {"answer": "water damage"}, "request_hash": "hash-1"}


@pytest.mark.asyncio
async def test_second_instance_is_refused_while_a_claim_is_held(redis_backends) -> None:
    """Only one replica computes a response for an unclaimed key."""
    first, second = redis_backends

    assert await first.claim("user@example.test:key-claim") is True
    assert await second.claim("user@example.test:key-claim") is False

    await second.store("user@example.test:key-claim", {"answer": "one shot"}, "hash-2")
    await first.release_claim("user@example.test:key-claim")

    # The claim is gone, but the published response is what a later retry reads.
    assert await first.lookup("user@example.test:key-claim") == {
        "response": {"answer": "one shot"},
        "request_hash": "hash-2",
    }
    assert await second.claim("user@example.test:key-claim") is True


@pytest.mark.asyncio
async def test_claim_waiter_receives_the_published_response(redis_backends) -> None:
    """A replica that loses the claim waits for and replays the response."""
    first, second = redis_backends
    key = "user@example.test:key-waits"

    assert await first.claim(key) is True

    async def publish() -> None:
        await asyncio.sleep(0.1)
        await second.store(key, {"answer": "eventually"}, "hash-3")

    publisher = asyncio.create_task(publish())
    entry = await first.await_claimed_result(key)
    await publisher

    assert entry == {"response": {"answer": "eventually"}, "request_hash": "hash-3"}


@pytest.mark.asyncio
async def test_claim_waits_stop_once_the_claim_disappears(redis_backends) -> None:
    """A winner that never publishes cannot stall the retry."""
    first, second = redis_backends
    key = "user@example.test:key-abandoned"

    assert await first.claim(key) is True
    await second.release_claim(key)

    assert await first.await_claimed_result(key) is None


@pytest.mark.asyncio
async def test_backend_is_redis_when_configured(fake_redis, monkeypatch) -> None:
    """The startup wiring selects Redis when the settings ask for it."""
    from app.core.config import settings
    from app.core.idempotency import configure_idempotency_storage, uses_distributed_store

    monkeypatch.setattr(settings, "idempotency_storage", "redis")
    monkeypatch.setattr(settings, "redis_url", fake_redis.url)

    await configure_idempotency_storage()

    assert uses_distributed_store()


@pytest.mark.asyncio
async def test_backend_stays_in_process_when_redis_url_is_invalid(monkeypatch) -> None:
    """An unusable Redis URI leaves the in-process cache in place."""
    from app.core.config import settings
    from app.core.idempotency import configure_idempotency_storage, uses_distributed_store

    monkeypatch.setattr(settings, "idempotency_storage", "redis")
    monkeypatch.setattr(settings, "redis_url", "not-a-redis-url")

    await configure_idempotency_storage()

    assert not uses_distributed_store()


@pytest.mark.asyncio
async def test_backend_falls_back_when_redis_is_unreachable(monkeypatch) -> None:
    """Redis that does not answer leaves the in-process cache in place."""
    # Nothing is listening on this address, so the startup probe fails.
    from app.core.config import settings
    from app.core.idempotency import configure_idempotency_storage, uses_distributed_store

    monkeypatch.setattr(settings, "idempotency_storage", "redis")
    monkeypatch.setattr(settings, "redis_url", "redis://127.0.0.1:1/0")

    await configure_idempotency_storage()

    assert not uses_distributed_store()


def test_body_mismatch_is_rejected_over_redis(
    test_client, mock_current_user, wired_idempotency
) -> None:
    """The same key with a different body returns 409 and the agent runs once."""
    calls: list[str] = []

    async def fake_run_agent(**kwargs):
        calls.append(kwargs["message"])
        return {
            "response": f"answer to {kwargs['message']}",
            "sources": [],
            "tool_calls": [],
            "session_id": "session-body-mismatch",
        }

    with patch("app.api.v1.chat.run_agent", side_effect=fake_run_agent):
        first = test_client.post(
            "/api/v1/chat",
            json={"message": "is water damage covered"},
            headers={**mock_current_user, "Idempotency-Key": "t9-body-mismatch"},
        )
        second = test_client.post(
            "/api/v1/chat",
            json={"message": "is fire damage covered"},
            headers={**mock_current_user, "Idempotency-Key": "t9-body-mismatch"},
        )

    assert first.status_code == 200
    assert second.status_code == 409
    assert calls == ["is water damage covered"]


def test_replayed_response_is_marked_over_redis(
    test_client,
    mock_current_user,
    wired_idempotency,
) -> None:
    """A retry of the same body replays and names the key that replayed it."""
    calls: list[str] = []

    async def fake_run_agent(**kwargs):
        calls.append(kwargs["message"])
        return {
            "response": "water damage is covered",
            "sources": [],
            "tool_calls": [],
            "session_id": "session-replay",
        }

    with patch("app.api.v1.chat.run_agent", side_effect=fake_run_agent):
        first = test_client.post(
            "/api/v1/chat",
            json={"message": "is water damage covered"},
            headers={**mock_current_user, "Idempotency-Key": "t9-replay"},
        )
        second = test_client.post(
            "/api/v1/chat",
            json={"message": "is water damage covered"},
            headers={**mock_current_user, "Idempotency-Key": "t9-replay"},
        )

    assert first.status_code == second.status_code == 200
    assert second.headers["X-Idempotent-Replayed"] == "true"
    assert second.headers["X-Idempotency-Key"] == "t9-replay"
    assert second.json()["response"] == "water damage is covered"
    assert calls == ["is water damage covered"]


# ── Rate limiting across instances ────────────────────────────────────────────


def test_rate_limit_counters_are_shared_between_instances(fake_redis) -> None:
    """One limiter instance spends the budget another instance reads."""
    from app.core.rate_limiter import RedisRateLimitStorage

    first = RedisRateLimitStorage(fake_redis.url)
    second = RedisRateLimitStorage(fake_redis.url)
    key = "LIMITER/omnicare_financial/api/v1/chat/user:1/1/minute"

    for _ in range(3):
        first.incr(key, 60, 1)

    assert second.get(key) == 3

    assert first.reset() >= 1
    assert second.get(key) == 0


def test_rate_limiter_falls_back_when_redis_url_is_unusable(monkeypatch) -> None:
    """A Redis URI that cannot become a storage keeps the in-process limiter."""
    from limits.storage import MemoryStorage

    from app.core import rate_limiter
    from app.core.rate_limiter import RedisRateLimitStorage

    monkeypatch.setattr(rate_limiter.settings, "rate_limit_storage", "redis")
    monkeypatch.setattr(rate_limiter.settings, "redis_url", "unparseable-url")

    limiter = rate_limiter._build_limiter()

    assert not isinstance(limiter._storage, RedisRateLimitStorage)
    assert isinstance(limiter._storage, MemoryStorage)


# ── Sessions across runners ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_session_is_retrievable_across_two_runners(monkeypatch) -> None:
    """A session created by one Runner is readable by a fresh session service.

    Building the second Runner against a *new* service instance is what a second
    replica does; the session id has to resolve to the same row.
    """
    from google.adk.events import Event
    from google.adk.sessions import Session

    from app.agent import agent as agent_module

    user_id = "00000000-0000-0000-0000-00000000c0de"
    monkeypatch.setattr(agent_module.settings, "session_storage", "database")

    original_service = agent_module.session_service
    original_runner = agent_module.runner
    original_agent = agent_module.omnicare_agent
    try:
        agent_module.session_service = None
        agent_module.runner = None
        # The startup path builds the service and creates its tables; each of
        # the two calls below stands in for one replica booting.
        await agent_module.warm_session_service()
        first_runner = agent_module.get_runner()
        session_id = await agent_module._ensure_session(user_id)

        agent_module.session_service = None
        agent_module.runner = None
        await agent_module.warm_session_service()
        second_runner = agent_module.get_runner()

        # A fresh replica resolves the same session rather than starting a new one.
        assert await agent_module._ensure_session(user_id) == session_id

        event = Event(author="user", invocation_id="t9-invocation", timestamp=1_700_000_000.0)
        seeded = Session(
            app_name="omnicare_financial",
            user_id=user_id,
            id=session_id,
            state={},
            last_update_time=1_700_000_000.0,
        )
        await first_runner.session_service.append_event(seeded, event)

        loaded = await second_runner.session_service.get_session(
            app_name="omnicare_financial",
            user_id=user_id,
            session_id=session_id,
        )
        assert loaded is not None
        assert loaded.id == session_id
        assert [stored.id for stored in loaded.events] == [event.id]

        await second_runner.session_service.delete_session(
            app_name="omnicare_financial",
            user_id=user_id,
            session_id=session_id,
        )
        deleted = await second_runner.session_service.get_session(
            app_name="omnicare_financial",
            user_id=user_id,
            session_id=session_id,
        )
        assert deleted is None
    finally:
        # ``get_runner`` rebinds the module-global agent whenever the runner is
        # rebuilt, so nulling the runner below gives the process a new agent
        # object. Restoring only the service and the runner would leave that new
        # agent in place, and every later suite that swaps or captures
        # ``omnicare_agent.model`` would be acting on the wrong object -- which
        # is how the streamed-tool-trace tests ended up running against a
        # rebuilt agent instead of the scripted double they installed.
        agent_module.session_service = original_service
        agent_module.runner = original_runner
        agent_module.omnicare_agent = original_agent
        agent_module._user_sessions.pop(user_id, None)
