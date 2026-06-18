"""In-process pub/sub channels for WebSocket handlers.

A small broadcast layer so a page's ``async def websocket(ws)`` handler can join
a named channel (a "room") and publish to everyone else in it without
hand-rolling connection bookkeeping::

    from pyxle.realtime import channel

    async def websocket(ws):
        await ws.accept()
        async with channel(ws, f"room:{ws.path_params['room']}") as room:
            async for message in ws.iter_text():
                await room.publish(message)

The default :class:`InProcessBroker` holds the channel→connections map in
memory, guarded by an :class:`asyncio.Lock`, and prunes connections that fail
on send (mirroring the dev overlay's connection manager). It is a
:class:`Broker` (a Protocol), so a Redis/NATS-backed broker can drop in for
multi-worker deployments.

.. warning::

   **Multi-worker caveat.** The in-process broker lives in ONE process. Under
   ``pyxle serve --workers N`` each worker has its OWN broker — a client on
   worker 1 never receives a message published on worker 2. For cross-worker
   realtime, use a shared backend (Redis pub/sub) or sticky-session routing at
   the load balancer.

This module is standalone — it imports nothing from ``pyxle.devserver`` or
``pyxle.compiler`` (only :class:`starlette.websockets.WebSocket` as a type), so
the module-boundary and no-circular-import rules hold.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from collections.abc import Mapping
from typing import Any, AsyncIterator, Protocol, runtime_checkable

from starlette.websockets import WebSocket

#: A message is delivered as the frame matching its type: ``dict``/``list`` as
#: JSON, ``str`` as a text frame, ``bytes`` as a binary frame.
Message = Any


@runtime_checkable
class Broker(Protocol):
    """The pub/sub contract a realtime backend implements.

    The default is :class:`InProcessBroker`; a Redis/NATS broker satisfying
    this Protocol can be swapped in (e.g. on ``app.state.pyxle_broker``) for
    multi-worker delivery.
    """

    async def subscribe(self, channel: str, ws: WebSocket) -> None: ...

    async def unsubscribe(self, channel: str, ws: WebSocket) -> None: ...

    async def publish(self, channel: str, message: Message) -> None: ...


# A message is encoded ONCE per publish into a ready-to-send frame, BEFORE any
# socket is touched. A non-serializable message therefore raises to the
# publisher (a clear TypeError) instead of failing mid-fan-out — where it would
# be misread as a dead socket and evict every healthy subscriber.
_Frame = tuple[bool, "str | bytes"]  # (is_binary, payload)


def _encode(message: Message) -> _Frame:
    if isinstance(message, (bytes, bytearray)):
        return (True, bytes(message))
    if isinstance(message, str):
        return (False, message)
    return (False, json.dumps(message))  # dict/list/number/… — may raise here


async def _deliver(ws: WebSocket, frame: _Frame) -> None:
    is_binary, payload = frame
    if is_binary:
        await ws.send_bytes(payload)  # type: ignore[arg-type]
    else:
        await ws.send_text(payload)  # type: ignore[arg-type]


class InProcessBroker:
    """The default :class:`Broker`: a channel→connections map in this process.

    Single-process only — see the module-level multi-worker caveat. Thread-safe
    is not a concern (one event loop); the lock serialises the connection-set
    mutations against concurrent subscribes/publishes.
    """

    def __init__(self) -> None:
        self._channels: dict[str, set[WebSocket]] = {}
        self._lock = asyncio.Lock()

    async def subscribe(self, channel: str, ws: WebSocket) -> None:
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
        """Send ``message`` to every connection subscribed to ``channel``.

        A no-op for an empty/unknown channel. A non-serializable ``message``
        raises :class:`TypeError`/:class:`ValueError` to the caller (encoded
        once, up front, before any socket is touched). Connections that fail on
        send (closed sockets) are pruned from every channel.
        """
        frame = _encode(message)
        async with self._lock:
            members = list(self._channels.get(channel, ()))
        await self._fanout(members, frame)

    async def broadcast(self, message: Message) -> None:
        """Send ``message`` to every connection across all channels (deduped)."""
        frame = _encode(message)
        async with self._lock:
            members = {ws for group in self._channels.values() for ws in group}
        await self._fanout(list(members), frame)

    async def _fanout(self, members: list[WebSocket], frame: _Frame) -> None:
        # The message is already encoded, so the ONLY thing that can raise here
        # is a socket send on a dead/closing connection — which we prune.
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

    def channel_count(self) -> int:
        """Number of channels with at least one subscriber (introspection)."""
        return len(self._channels)

    async def start(self) -> None:
        """No-op: the in-process broker holds no external connection. Present so
        the app can drive every broker's lifecycle uniformly (the Redis broker
        opens its connection + listener here)."""

    async def aclose(self) -> None:
        """No-op teardown, the counterpart to :meth:`start`."""


class ChannelHandle:
    """A subscription returned by :func:`channel` — publish to its channel."""

    __slots__ = ("_broker", "name")

    def __init__(self, broker: Broker, name: str) -> None:
        self._broker = broker
        self.name = name

    async def publish(self, message: Message) -> None:
        await self._broker.publish(self.name, message)


# A process-global fallback broker for handlers that run without an app on the
# scope (rare — every Pyxle app sets ``app.state.pyxle_broker``). Created lazily
# so importing this module costs nothing.
_FALLBACK_BROKER: InProcessBroker | None = None


def _fallback_broker() -> InProcessBroker:
    global _FALLBACK_BROKER
    if _FALLBACK_BROKER is None:
        _FALLBACK_BROKER = InProcessBroker()
    return _FALLBACK_BROKER


def broker_for(ws: WebSocket) -> Broker:
    """Return the broker for ``ws`` — the app-scoped one, creating it if absent.

    Every Pyxle app installs one :class:`InProcessBroker` on
    ``app.state.pyxle_broker`` so all connections in a process share it. If the
    scope carries no app (a bare test socket), a process-global fallback is
    used so the helper still works.
    """
    try:
        app = ws.app
    except (KeyError, AttributeError):
        # No app on the scope (a bare test socket) — use the process fallback.
        return _fallback_broker()
    broker = getattr(app.state, "pyxle_broker", None)
    if broker is None:
        broker = InProcessBroker()
        app.state.pyxle_broker = broker
    return broker


@asynccontextmanager
async def channel(
    ws: WebSocket, name: str, *, broker: Broker | None = None
) -> AsyncIterator[ChannelHandle]:
    """Subscribe ``ws`` to ``name`` for the life of the ``async with`` block.

    On exit (including disconnect) the connection is unsubscribed, so handler
    bodies stay leak-free. ``broker`` defaults to the app-scoped broker.
    """
    resolved = broker or broker_for(ws)
    await resolved.subscribe(name, ws)
    try:
        yield ChannelHandle(resolved, name)
    finally:
        await resolved.unsubscribe(name, ws)


def build_broker(env: "Mapping[str, str] | None" = None) -> Broker:
    """Construct the realtime broker selected by ``PYXLE_REALTIME_BROKER``.

    * ``memory`` (default) — :class:`InProcessBroker`; correct for ``pyxle dev``
      and single-worker ``pyxle serve``.
    * ``redis`` — a Redis-pub/sub :class:`RedisBroker` that spans worker
      processes (needs the ``pyxle-framework[redis]`` extra). The connection URL
      comes from ``PYXLE_REALTIME_REDIS_URL`` (default ``redis://localhost:6379``)
      and an optional key namespace from ``PYXLE_REALTIME_CHANNEL_PREFIX``.

    The app awaits ``broker.start()`` after construction and ``broker.aclose()``
    on shutdown; the in-process broker's are no-ops.
    """
    import os  # noqa: PLC0415 - lazy, only when wiring the app

    resolved = os.environ if env is None else env
    name = (resolved.get("PYXLE_REALTIME_BROKER") or "memory").strip().lower()
    if name in ("memory", "inprocess", "in-process", ""):
        return InProcessBroker()
    if name == "redis":
        from pyxle.realtime.redis_broker import RedisBroker  # noqa: PLC0415 - optional

        return RedisBroker(
            resolved.get("PYXLE_REALTIME_REDIS_URL") or "redis://localhost:6379",
            channel_prefix=resolved.get("PYXLE_REALTIME_CHANNEL_PREFIX") or "pyxle:rt:",
        )
    raise ValueError(
        f"Unknown PYXLE_REALTIME_BROKER={name!r}; expected 'memory' or 'redis'."
    )


__all__ = [
    "Broker",
    "InProcessBroker",
    "ChannelHandle",
    "build_broker",
    "channel",
    "broker_for",
    "Message",
]
