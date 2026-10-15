"""
Minimal Redis clients used by the backend's two shared stores.

The third-party ``redis`` package is deliberately not used: the rate-limit
storage and the idempotency cache must not add a dependency the deployment
image may not carry, and the commands they need -- PING, GET, SET with EX/NX,
EXISTS, DEL, INCRBY, EXPIRE, PTTL and a SCAN loop for reset -- are a small,
stable subset of the protocol. Speaking RESP directly is therefore cheaper
than introducing a package.

Two clients live here on purpose. ``RedisClient`` is blocking: SlowAPI's
storage interface is synchronous and runs inside the request path, so that
path needs a plain socket. ``AsyncRedisClient`` speaks the same protocol over
asyncio streams for the idempotency backend, which is wired during startup.
Both delegate decoding to one ``RespDecoder`` implementation so the wire
protocol lives in exactly one place and is tested once.

Both clients connect on first use and reconnect once when a connection is
found dead, so a Redis restart costs one failed command rather than a cached
error for the lifetime of the process.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from typing import Any
from urllib.parse import unquote, urlsplit

logger = logging.getLogger(__name__)


class RedisProtocolError(RuntimeError):
    """Redis answered with something that is not a valid RESP reply."""


class RedisCommandError(RuntimeError):
    """Redis reported a server-side error for a command."""


class RedisUnavailable(RuntimeError):
    """No Redis connection could be established or answered."""


class _IncompleteReply(Exception):
    """The decoder needs more bytes before a whole reply can be returned."""


def encode_resp_command(args: Sequence[str | bytes]) -> bytes:
    """Encode a command as a RESP array of bulk strings.

    Args:
        args: Command name followed by its arguments, in order. An integer
            argument (a TTL, an increment) is rendered as its decimal digits,
            because ``bytes(1)`` would silently send a NUL byte instead.

    Returns:
        bytes: The ``*<n>`` array with one ``$<len>`` bulk string per argument.
    """
    chunks = [b"*%d\r\n" % len(args)]
    for arg in args:
        if isinstance(arg, str):
            payload = arg.encode()
        elif isinstance(arg, int):
            payload = str(arg).encode()
        else:
            payload = bytes(arg)
        chunks.append(b"$%d\r\n%s\r\n" % (len(payload), payload))
    return b"".join(chunks)


class RespDecoder:
    """Incremental decoder for the RESP2 replies this service asks for.

    One shared implementation serves the blocking and the asyncio clients: both
    feed it bytes as they arrive and pull complete values out, so the wire
    protocol is implemented once and tested once.

    Only the reply types the commands in use can produce are supported: simple
    strings, integers, bulk strings, arrays, the null bulk string and error
    replies.
    """

    def __init__(self) -> None:
        self._buffer = bytearray()

    def feed(self, chunk: bytes) -> None:
        """Append bytes received from the socket to the buffer."""
        self._buffer.extend(chunk)

    def take(self) -> Any:
        """Return the next complete reply.

        Returns:
            The decoded value: ``str`` for simple strings, ``int`` for
            integers, ``bytes`` for bulk strings, ``list`` for arrays, and
            ``None`` for a null reply.

        Raises:
            _IncompleteReply: If more bytes are needed, which the caller is
                expected to handle by reading from the socket again.
            RedisCommandError: If the reply is an error reply.
            RedisProtocolError: If the reply is malformed.
        """
        try:
            value, consumed = self._decode(0)
        except IndexError as exc:  # pragma: no cover - guarded by _byte()
            raise _IncompleteReply from exc
        del self._buffer[:consumed]
        return value

    def _byte(self, position: int) -> int:
        if position >= len(self._buffer):
            raise _IncompleteReply
        return self._buffer[position]

    def _line(self, position: int) -> tuple[bytes, int]:
        end = self._buffer.find(b"\r\n", position)
        if end == -1:
            raise _IncompleteReply
        return bytes(self._buffer[position:end]), end + 2

    def _integer(self, position: int) -> tuple[int, int]:
        line, end = self._line(position)
        try:
            return int(line), end
        except ValueError as exc:
            raise RedisProtocolError(f"Malformed RESP integer {line!r}") from exc

    def _decode(self, position: int) -> tuple[Any, int]:
        marker = self._byte(position)
        if marker == 0x2B:  # "+" simple string
            line, end = self._line(position + 1)
            return line.decode("utf-8", "replace"), end
        if marker == 0x2D:  # "-" error reply
            line, end = self._line(position + 1)
            raise RedisCommandError(line.decode("utf-8", "replace"))
        if marker == 0x3A:  # ":" integer
            line, end = self._line(position + 1)
            try:
                return int(line), end
            except ValueError as exc:
                raise RedisProtocolError(f"Malformed RESP integer {line!r}") from exc
        if marker == 0x24:  # "$" bulk string
            length, end = self._integer(position + 1)
            if length == -1:
                return None, end
            if len(self._buffer) < end + length + 2:
                raise _IncompleteReply
            return bytes(self._buffer[end : end + length]), end + length + 2
        if marker == 0x2A:  # "*" array
            count, end = self._integer(position + 1)
            if count == -1:
                return None, end
            items: list[Any] = []
            for _ in range(count):
                item, end = self._decode(end)
                items.append(item)
            return items, end
        raise RedisProtocolError(f"Unexpected RESP type byte {chr(marker)!r}")


class RedisClient:
    """Blocking Redis client used where the caller cannot await.

    Rate-limiting counters are read and written by SlowAPI's synchronous storage
    interface, which runs inside the request path, so that path needs a blocking
    socket rather than an asyncio stream.

    Attributes:
        host: Redis host name.
        port: Redis TCP port.
        db: Logical database index.
        password: Optional password sent with AUTH after connecting.
        connect_timeout: Seconds to wait for the TCP connection.
        socket_timeout: Seconds to wait for a reply.
    """

    def __init__(  # noqa: PLR0913 - one field per Redis connection setting
        self,
        host: str,
        port: int,
        *,
        db: int = 0,
        password: str | None = None,
        connect_timeout: float = 1.0,
        socket_timeout: float = 2.0,
    ) -> None:
        self.host = host
        self.port = port
        self.db = db
        self.password = password
        self.connect_timeout = connect_timeout
        self.socket_timeout = socket_timeout
        self._socket: Any = None
        self._decoder = RespDecoder()

    @classmethod
    def from_url(
        cls,
        url: str,
        *,
        connect_timeout: float = 1.0,
        socket_timeout: float = 2.0,
    ) -> RedisClient:
        """Build a client from a ``redis://host:port/db`` style URI.

        Args:
            url: Redis URI. A ``rediss://`` scheme is accepted and treated as
                plain TCP because the stores are deployed inside a trusted
                network; anything else raises.
            connect_timeout: Seconds to wait for the TCP connection.
            socket_timeout: Seconds to wait for a reply.

        Returns:
            RedisClient: A client that connects on first use.

        Raises:
            ValueError: If the URI is not a usable Redis address.
        """
        parsed = urlsplit(url)
        if parsed.scheme not in {"redis", "rediss"} or not parsed.hostname:
            raise ValueError(f"Not a Redis URI: {url!r}")
        password = unquote(parsed.password) if parsed.password else None
        db = int(unquote(parsed.path).lstrip("/") or 0)
        return cls(
            parsed.hostname,
            parsed.port or 6379,
            db=db,
            password=password,
            connect_timeout=connect_timeout,
            socket_timeout=socket_timeout,
        )

    @property
    def connected(self) -> bool:
        """Whether a socket is currently open."""
        return self._socket is not None

    def close(self) -> None:
        """Drop the socket, if any, and forget any buffered bytes."""
        socket = self._socket
        self._socket = None
        self._decoder = RespDecoder()
        if socket is not None:
            try:
                socket.close()
            except OSError:  # pragma: no cover - best effort
                logger.debug("Error while closing Redis socket", exc_info=True)

    def _connect(self) -> None:
        import socket as socket_module  # noqa: PLC0415

        self._socket = socket_module.create_connection(
            (self.host, self.port),
            timeout=self.connect_timeout,
        )
        self._socket.settimeout(self.socket_timeout)
        if self.password:
            self._round_trip(["AUTH", self.password])
        if self.db:
            self._round_trip(["SELECT", str(self.db)])

    def _read_reply(self) -> Any:
        while True:
            try:
                return self._decoder.take()
            except _IncompleteReply:
                chunk = self._socket.recv(65536)
                if not chunk:
                    raise ConnectionResetError("Redis closed the connection") from None
                self._decoder.feed(chunk)

    def _round_trip(self, args: Sequence[str | bytes]) -> Any:
        self._socket.sendall(encode_resp_command(args))
        return self._read_reply()

    def execute(self, *args: str | bytes) -> Any:
        """Run one command, reconnecting once if the connection had dropped.

        Args:
            *args: Command name followed by its arguments.

        Returns:
            The decoded Redis reply.

        Raises:
            RedisUnavailable: If Redis could not be reached on both attempts.
        """
        try:
            return self._execute_connected(args)
        except (OSError, RedisProtocolError) as first_error:
            self.close()
            try:
                return self._execute_connected(args)
            except (OSError, RedisProtocolError) as second_error:
                raise RedisUnavailable(str(second_error)) from first_error

    def _execute_connected(self, args: Sequence[str | bytes]) -> Any:
        if not self.connected:
            self._connect()
        return self._round_trip(args)

    def ping(self) -> bool:
        """Check that Redis answers; a connection failure is reported as False."""
        try:
            return self.execute("PING") in ("PONG", b"PONG")
        except RedisUnavailable:
            return False

    def get(self, key: str) -> str | None:
        """Return the stored string for a key, or None when it is absent."""
        reply = self.execute("GET", key)
        return None if reply is None else reply.decode("utf-8", "replace")

    def set(self, key: str, value: str, *, ttl: int, nx: bool = False) -> bool:
        """Store a value, optionally only when the key does not exist yet."""
        args = ["SET", key, value, "EX", str(int(ttl))]
        if nx:
            args.append("NX")
        try:
            return self.execute(*args) in ("OK", b"OK")
        except RedisCommandError:
            return False

    def exists(self, key: str) -> bool:
        """Return whether a key is present."""
        return int(self.execute("EXISTS", key)) == 1

    def delete(self, *keys: str) -> None:
        """Delete keys; missing keys are ignored."""
        if keys:
            self.execute("DEL", *keys)


class AsyncRedisClient:
    """Minimal asyncio Redis client for the commands the stores need.

    The client connects on first use and reconnects once if a connection is
    found dead, so a Redis restart costs one failed command rather than a
    cached error for the lifetime of the process.

    Attributes:
        connect_timeout: Seconds to wait for the TCP connection.
        socket_timeout: Seconds to wait for a reply.
    """

    def __init__(  # noqa: PLR0913 - one field per Redis connection setting
        self,
        host: str,
        port: int,
        *,
        db: int = 0,
        password: str | None = None,
        connect_timeout: float = 1.0,
        socket_timeout: float = 2.0,
    ) -> None:
        self._host = host
        self._port = port
        self._db = db
        self._password = password
        self.connect_timeout = connect_timeout
        self.socket_timeout = socket_timeout
        self._reader: Any = None
        self._writer: Any = None
        self._decoder = RespDecoder()

    @classmethod
    def from_url(
        cls,
        url: str,
        *,
        connect_timeout: float = 1.0,
        socket_timeout: float = 2.0,
    ) -> AsyncRedisClient:
        """Build a client from a ``redis://host:port/db`` style URI.

        Args:
            url: Redis URI. A ``rediss://`` scheme is accepted and treated as
                plain TCP because the stores are deployed inside a trusted
                network; anything else raises.
            connect_timeout: Seconds to wait for the TCP connection.
            socket_timeout: Seconds to wait for a reply.

        Returns:
            AsyncRedisClient: A client that connects on first use.

        Raises:
            ValueError: If the URI is not a usable Redis address.
        """
        parsed = urlsplit(url)
        if parsed.scheme not in {"redis", "rediss"} or not parsed.hostname:
            raise ValueError(f"Not a Redis URI: {url!r}")
        password = unquote(parsed.password) if parsed.password else None
        db = int(unquote(parsed.path).lstrip("/") or 0)
        return cls(
            parsed.hostname,
            parsed.port or 6379,
            db=db,
            password=password,
            connect_timeout=connect_timeout,
            socket_timeout=socket_timeout,
        )

    @property
    def connected(self) -> bool:
        """Whether a connection is currently open."""
        return self._reader is not None

    async def close(self) -> None:
        """Close the connection, if any, and forget any buffered bytes."""
        writer, self._writer, self._reader = self._writer, None, None
        self._decoder = RespDecoder()
        if writer is None:
            return
        try:
            writer.close()
            await writer.wait_closed()
        except OSError:  # pragma: no cover - best effort
            logger.debug("Error while closing Redis connection", exc_info=True)

    async def _connect(self) -> None:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(self._host, self._port),
            timeout=self.connect_timeout,
        )
        self._reader, self._writer = reader, writer
        self._decoder = RespDecoder()
        if self._password:
            await self._round_trip(["AUTH", self._password])
        if self._db:
            await self._round_trip(["SELECT", str(self._db)])

    async def _read_reply(self) -> Any:
        while True:
            try:
                return self._decoder.take()
            except _IncompleteReply:
                chunk = await self._reader.read(65536)
                if not chunk:
                    raise ConnectionResetError("Redis closed the connection") from None
                self._decoder.feed(chunk)

    async def _round_trip(self, args: Sequence[str | bytes]) -> Any:
        self._writer.write(encode_resp_command(args))
        await self._writer.drain()
        return await asyncio.wait_for(self._read_reply(), timeout=self.socket_timeout)

    async def execute(self, *args: str | bytes) -> Any:
        """Run one command, reconnecting once if the connection had dropped.

        Args:
            *args: Command name followed by its arguments.

        Returns:
            The decoded Redis reply.

        Raises:
            RedisUnavailable: If Redis could not be reached on both attempts.
        """
        try:
            return await self._execute_connected(args)
        except (OSError, TimeoutError, RedisProtocolError) as first_error:
            await self.close()
            try:
                return await self._execute_connected(args)
            except (OSError, TimeoutError, RedisProtocolError) as second_error:
                raise RedisUnavailable(str(second_error)) from first_error

    async def _execute_connected(self, args: Sequence[str | bytes]) -> Any:
        if not self.connected:
            await self._connect()
        return await self._round_trip(args)

    async def ping(self) -> bool:
        """Check that Redis answers; a connection failure is reported as False."""
        try:
            return await self.execute("PING") in ("PONG", b"PONG")
        except RedisUnavailable:
            return False

    async def get(self, key: str) -> str | None:
        """Return the stored string for a key, or None when it is absent."""
        reply = await self.execute("GET", key)
        return None if reply is None else reply.decode("utf-8", "replace")

    async def set(self, key: str, value: str, *, ttl: int, nx: bool = False) -> bool:
        """Store a value, optionally only when the key does not exist yet."""
        args: list[str] = ["SET", key, value, "EX", str(int(ttl))]
        if nx:
            args.append("NX")
        try:
            return await self.execute(*args) in ("OK", b"OK")
        except RedisCommandError:
            return False

    async def exists(self, key: str) -> bool:
        """Return whether a key is present."""
        return int(await self.execute("EXISTS", key)) == 1

    async def delete(self, *keys: str) -> None:
        """Delete keys; missing keys are ignored."""
        if keys:
            await self.execute("DEL", *keys)
