"""Cross-worker realtime :class:`~pyxle.realtime.channels.Broker` backed by Redis
pub/sub (the ``pyxle-framework[redis]`` extra).

The default :class:`~pyxle.realtime.channels.InProcessBroker` lives in one
process, so under ``pyxle serve --workers N`` a client on worker 1 never sees a
message published on worker 2. ``RedisBroker`` bridges that gap: every worker
keeps its own local ``channel -> connections`` map (for delivering to *its* own
sockets) and relays messages between workers over Redis.

**Flow.** ``publish(channel, msg)`` encodes the message once and ``PUBLISH``es it
to Redis under a namespaced channel. Every worker — including the publisher —
runs one background listener that is ``PSUBSCRIBE``d to the whole namespace; when
a message arrives it is decoded and fanned out to that worker's local
subscribers of the channel. So a message published on any worker reaches every
subscriber on every worker, exactly once per connection.

**Why a single pattern subscription** (``PSUBSCRIBE prefix:*``) instead of one
Redis ``SUBSCRIBE`` per channel: ``redis.asyncio``'s ``PubSub`` connection is not
safe for concurrent ``subscribe``/``get_message`` from different tasks. Pattern-
subscribing once at startup means the listener task is the *only* thing that ever
touches the pub/sub connection, so ``subscribe``/``unsubscribe`` are pure local-
map updates with no Redis round-trip and no races. The cost is that each worker
receives every message in the namespace and filters locally — fine for typical
realtime workloads; a per-channel variant is a future optimisation for very high
channel counts.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from starlette.websockets import WebSocket

from pyxle.realtime.channels import Message, _deliver, _encode

_logger = logging.getLogger("pyxle.realtime.redis")

#: Reserved channel name used by :meth:`RedisBroker.broadcast`; the leading NUL
#: keeps it from colliding with any application channel.
_BROADCAST = "\x00broadcast"


def _frame_to_wire(frame: tuple[bool, "str | bytes"]) -> bytes:
    """Serialise an encoded ``(is_binary, payload)`` frame for the Redis wire.

    A 1-byte tag preserves the text/binary distinction across Redis (which only
    carries bytes), so a binary ``send_bytes`` message survives the round trip.
    """
    is_binary, payload = frame
    if is_binary:
        return b"b" + payload  # type: ignore[operator]
    return b"t" + payload.encode("utf-8")  # type: ignore[union-attr]


def _wire_to_frame(data: bytes) -> tuple[bool, "str | bytes"]:
    """Inverse of :func:`_frame_to_wire`."""
    tag, body = data[:1], data[1:]
    if tag == b"b":
        return (True, body)
    return (False, body.decode("utf-8"))


class RedisBroker:
    """A :class:`~pyxle.realtime.channels.Broker` that spans worker processes.

    Construct with a Redis ``url`` (resolved lazily in :meth:`start`, so the
    ``redis`` extra is only required when this broker is actually used), or inject
    a ready client (the ``redis.asyncio.Redis``-compatible object) for tests.
    """

    def __init__(
        self,
        url: str | None = None,
        *,
        client: Any | None = None,
        channel_prefix: str = "pyxle:rt:",
        reconnect_delay: float = 0.5,
        connect_timeout: float = 5.0,
    ) -> None:
        self._url = url
        self._redis = client
        self._prefix = channel_prefix
        self._reconnect_delay = reconnect_delay
        self._connect_timeout = connect_timeout
        self._channels: dict[str, set[WebSocket]] = {}
        self._lock = asyncio.Lock()
        self._pubsub: Any | None = None
        self._listener: asyncio.Task[None] | None = None
        self._ready = asyncio.Event()
        self._closed = False

    # ---- lifecycle -------------------------------------------------------------

    async def start(self) -> None:
        """Connect to Redis and start the background listener.

        Pings Redis up front so a misconfigured URL fails loudly at app startup
        (the same posture as the pyxle-db plugin) rather than silently dropping
        cross-worker messages later.
        """
        if self._redis is None:
            try:
                from redis.asyncio import Redis  # noqa: PLC0415 - optional extra
            except ImportError as exc:
                raise RuntimeError(
                    "The Redis realtime broker requires the 'redis' package. "
                    "Install it with: pip install 'pyxle-framework[redis]'"
                ) from exc
            # decode_responses MUST stay False: the wire framing carries a 1-byte
            # text/binary tag over raw bytes, so a decoding client would corrupt
            # binary (send_bytes) frames.
            self._redis = Redis.from_url(
                self._url or "redis://localhost:6379", decode_responses=False
            )
        await self._redis.ping()
        self._listener = asyncio.create_task(self._listen())
        # Wait until the listener has its pattern subscription live, so a publish
        # issued immediately after start() can't be missed by this worker. Bound
        # the wait: if the pub/sub subscription can't be established (e.g. Redis
        # accepts PING but PSUBSCRIBE persistently fails — an ACL gap, a flap in
        # the ping->psubscribe window), the listener would otherwise retry forever
        # and start() would hang the whole worker's startup. Fail loudly instead,
        # matching this method's "a bad URL fails loudly at app startup" contract.
        try:
            await asyncio.wait_for(self._ready.wait(), timeout=self._connect_timeout)
        except (asyncio.TimeoutError, TimeoutError) as exc:
            await self.aclose()
            raise RuntimeError(
                "RedisBroker could not establish its pub/sub subscription within "
                f"{self._connect_timeout}s (PING succeeded but PSUBSCRIBE did not). "
                "Check the Redis URL, ACL permissions for PSUBSCRIBE, and connectivity."
            ) from exc

    async def aclose(self) -> None:
        """Stop the listener and release the Redis connections. Idempotent."""
        self._closed = True
        if self._listener is not None:
            self._listener.cancel()
            try:
                await self._listener
            except asyncio.CancelledError:
                pass
            self._listener = None
        if self._pubsub is not None:
            await _safe_aclose(self._pubsub)
            self._pubsub = None
        if self._redis is not None:
            await _safe_aclose(self._redis)

    # ---- Broker protocol -------------------------------------------------------

    async def subscribe(self, channel: str, ws: WebSocket) -> None:
        # Local-only: the listener already receives the whole namespace.
        async with self._lock:
            self._channels.setdefault(channel, set()).add(ws)

    async def unsubscribe(self, channel: str, ws: WebSocket) -> None:
        async with self._lock:
            members = self._channels.get(channel)
            if members is not None:
                members.discard(ws)
                if not members:
                    del self._channels[channel]

    async def publish(self, channel: str, message: Message) -> None:
        """Encode once (raising on a non-serializable message, before Redis is
        touched) and relay to every worker via Redis pub/sub."""
        wire = _frame_to_wire(_encode(message))
        assert self._redis is not None  # start() ran
        await self._redis.publish(self._prefix + channel, wire)

    async def broadcast(self, message: Message) -> None:
        """Send ``message`` to every connection on every worker (all channels)."""
        wire = _frame_to_wire(_encode(message))
        assert self._redis is not None
        await self._redis.publish(self._prefix + _BROADCAST, wire)

    def channel_count(self) -> int:
        """Number of locally-subscribed channels (per-worker introspection)."""
        return len(self._channels)

    # ---- listener --------------------------------------------------------------

    async def _listen(self) -> None:
        """Own the pub/sub connection: (re)subscribe to the namespace and fan
        each incoming message out to local subscribers, reconnecting on error."""
        pattern = self._prefix + "*"
        while not self._closed:
            try:
                self._pubsub = self._redis.pubsub()
                await self._pubsub.psubscribe(pattern)
                self._ready.set()
                async for raw in self._pubsub.listen():
                    if self._closed:
                        break
                    if raw.get("type") != "pmessage":
                        continue
                    await self._dispatch(raw["channel"], raw["data"])
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # connection dropped, etc. — reconnect.
                if self._closed:
                    break
                self._ready.clear()
                _logger.warning(
                    "redis realtime broker listener error (%s); reconnecting in %ss",
                    exc,
                    self._reconnect_delay,
                )
                await _safe_aclose(self._pubsub)
                self._pubsub = None
                await asyncio.sleep(self._reconnect_delay)

    async def _dispatch(self, raw_channel: Any, data: Any) -> None:
        channel = _as_str(raw_channel)
        if not channel.startswith(self._prefix):
            return
        name = channel[len(self._prefix) :]
        frame = _wire_to_frame(_as_bytes(data))
        async with self._lock:
            if name == _BROADCAST:
                members = {ws for group in self._channels.values() for ws in group}
                members = list(members)
            else:
                members = list(self._channels.get(name, ()))
        await self._fanout(members, frame)

    async def _fanout(self, members: list[WebSocket], frame: tuple[bool, Any]) -> None:
        stale: list[WebSocket] = []
        for ws in members:
            try:
                await _deliver(ws, frame)
            except Exception:  # a dead/closing socket — drop it everywhere
                stale.append(ws)
        if stale:
            await self._prune(stale)

    async def _prune(self, sockets: list[WebSocket]) -> None:
        async with self._lock:
            for members in self._channels.values():
                for ws in sockets:
                    members.discard(ws)
            for channel in [c for c, m in self._channels.items() if not m]:
                del self._channels[channel]


def _as_str(value: Any) -> str:
    return value.decode("utf-8") if isinstance(value, (bytes, bytearray)) else str(value)


def _as_bytes(value: Any) -> bytes:
    if isinstance(value, (bytes, bytearray)):
        return bytes(value)
    return str(value).encode("utf-8")


async def _safe_aclose(obj: Any) -> None:
    """Close a redis client/pubsub across redis-py versions, swallowing errors."""
    for name in ("aclose", "close"):
        closer = getattr(obj, name, None)
        if closer is None:
            continue
        try:
            result = closer()
            if asyncio.iscoroutine(result):
                await result
        except Exception:  # pragma: no cover - best-effort teardown
            pass
        return


__all__ = ["RedisBroker"]
