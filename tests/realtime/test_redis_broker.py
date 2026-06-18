"""Tests for the Redis-backed cross-worker realtime broker.

A hand-written ``_FakeRedis`` (a shared in-process pub/sub bus, no ``fakeredis``
dependency — mirroring ``tests/cache/test_backends.py``'s ``_FakeRedis``) lets two
``RedisBroker`` instances share one "Redis", so a publish on one broker can be
observed delivering to a subscriber on the *other* — the multi-worker property
the real broker exists for, tested deterministically without a live server.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from pyxle.realtime import InProcessBroker, build_broker
from pyxle.realtime.redis_broker import RedisBroker, _frame_to_wire, _wire_to_frame

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class FakeWS:
    """Records frames; can simulate a dead socket that raises on send."""

    def __init__(self, *, fail: bool = False) -> None:
        self.sent: list[tuple[str, object]] = []
        self._fail = fail

    async def send_text(self, text: str) -> None:
        if self._fail:
            raise RuntimeError("dead socket")
        self.sent.append(("text", text))

    async def send_bytes(self, data: bytes) -> None:
        if self._fail:
            raise RuntimeError("dead socket")
        self.sent.append(("bytes", data))


# --- a shared fake Redis pub/sub bus ---------------------------------------

_STOP = object()


class _FakePubSub:
    def __init__(self, *, fail_once: bool = False, fail_psubscribe: bool = False) -> None:
        self._patterns: list[str] = []
        self._queue: asyncio.Queue = asyncio.Queue()
        self._closed = False
        self._fail_once = fail_once
        self._fail_psubscribe = fail_psubscribe

    async def psubscribe(self, pattern) -> None:
        if self._fail_psubscribe:
            raise ConnectionError("simulated psubscribe failure")
        self._patterns.append(_str(pattern))

    def _matches(self, channel: str) -> bool:
        for p in self._patterns:
            if p.endswith("*") and channel.startswith(p[:-1]):
                return True
            if p == channel:
                return True
        return False

    async def _push(self, channel: str, data: bytes) -> None:
        await self._queue.put(
            {"type": "pmessage", "pattern": self._patterns[0].encode(),
             "channel": channel.encode(), "data": data}
        )

    async def listen(self):
        if self._fail_once:
            self._fail_once = False
            raise ConnectionError("simulated redis blip")
        while not self._closed:
            item = await self._queue.get()
            if item is _STOP:
                break
            yield item

    async def aclose(self) -> None:
        self._closed = True
        await self._queue.put(_STOP)


class _FakeRedis:
    """One shared bus: every ``pubsub()`` registers a listener; ``publish`` fans
    out to all listeners whose pattern matches (across all brokers sharing it)."""

    def __init__(self, *, fail_first_listen: bool = False, fail_psubscribe: bool = False) -> None:
        self._subs: list[_FakePubSub] = []
        self.publish_calls = 0
        self._fail_next = fail_first_listen
        self._fail_psubscribe = fail_psubscribe

    async def ping(self) -> bool:
        return True

    def pubsub(self) -> _FakePubSub:
        ps = _FakePubSub(fail_once=self._fail_next, fail_psubscribe=self._fail_psubscribe)
        self._fail_next = False  # only the first listener blips
        self._subs.append(ps)
        return ps

    async def publish(self, channel, data) -> int:
        self.publish_calls += 1
        ch, payload = _str(channel), data if isinstance(data, bytes) else _str(data).encode()
        n = 0
        for ps in self._subs:
            if ps._matches(ch):
                await ps._push(ch, payload)
                n += 1
        return n

    async def aclose(self) -> None:
        pass


def _str(v) -> str:
    return v.decode("utf-8") if isinstance(v, (bytes, bytearray)) else str(v)


async def _wait_for(predicate, timeout: float = 1.0) -> None:
    """Yield the loop until ``predicate()`` is true (the listener task delivers
    asynchronously), or fail after ``timeout``."""
    for _ in range(int(timeout / 0.005)):
        if predicate():
            return
        await asyncio.sleep(0.005)
    raise AssertionError("timed out waiting for delivery")


async def _two_workers() -> tuple[RedisBroker, RedisBroker, _FakeRedis]:
    redis = _FakeRedis()
    a, b = RedisBroker(client=redis), RedisBroker(client=redis)
    await a.start()
    await b.start()
    return a, b, redis


# --- the headline property: cross-worker delivery --------------------------


async def test_publish_on_one_worker_reaches_subscriber_on_another() -> None:
    a, b, _ = await _two_workers()
    try:
        ws = FakeWS()
        await b.subscribe("room:1", ws)  # subscriber lives on worker B
        await a.publish("room:1", {"hello": "world"})  # published on worker A
        await _wait_for(lambda: ws.sent)
        assert ws.sent == [("text", json.dumps({"hello": "world"}))]
    finally:
        await a.aclose()
        await b.aclose()


async def test_only_subscribers_of_the_channel_receive() -> None:
    a, b, _ = await _two_workers()
    try:
        on_room, other = FakeWS(), FakeWS()
        await b.subscribe("room:1", on_room)
        await b.subscribe("room:2", other)
        await a.publish("room:1", "hi")
        await _wait_for(lambda: on_room.sent)
        await asyncio.sleep(0.02)  # give any stray delivery a chance
        assert on_room.sent == [("text", "hi")]
        assert other.sent == []  # different channel — untouched
    finally:
        await a.aclose()
        await b.aclose()


async def test_text_binary_and_json_survive_the_redis_round_trip() -> None:
    a, b, _ = await _two_workers()
    try:
        ws = FakeWS()
        await b.subscribe("c", ws)
        await a.publish("c", "plain text")
        await a.publish("c", b"\x00\x01\xff")  # binary stays binary
        await a.publish("c", {"n": 1})  # dict → JSON text
        await _wait_for(lambda: len(ws.sent) == 3)
        assert ws.sent == [
            ("text", "plain text"),
            ("bytes", b"\x00\x01\xff"),
            ("text", json.dumps({"n": 1})),
        ]
    finally:
        await a.aclose()
        await b.aclose()


async def test_unsubscribe_stops_delivery() -> None:
    a, b, _ = await _two_workers()
    try:
        ws = FakeWS()
        await b.subscribe("c", ws)
        await b.unsubscribe("c", ws)
        await a.publish("c", "after-unsub")
        await asyncio.sleep(0.03)
        assert ws.sent == []  # no local subscriber left on B
    finally:
        await a.aclose()
        await b.aclose()


async def test_dead_socket_is_pruned() -> None:
    a, b, _ = await _two_workers()
    try:
        good, dead = FakeWS(), FakeWS(fail=True)
        await b.subscribe("c", good)
        await b.subscribe("c", dead)
        await a.publish("c", "x")
        await _wait_for(lambda: good.sent)
        await asyncio.sleep(0.02)
        # The dead socket raised on send and was dropped from the channel.
        assert b.channel_count() == 1
        await a.publish("c", "y")
        await _wait_for(lambda: len(good.sent) == 2)
        assert good.sent == [("text", "x"), ("text", "y")]
    finally:
        await a.aclose()
        await b.aclose()


async def test_broadcast_reaches_every_channel_on_every_worker() -> None:
    a, b, _ = await _two_workers()
    try:
        w1, w2 = FakeWS(), FakeWS()
        await a.subscribe("room:1", w1)  # on worker A
        await b.subscribe("room:2", w2)  # on worker B, different channel
        await a.broadcast("everyone")
        await _wait_for(lambda: w1.sent and w2.sent)
        assert w1.sent == [("text", "everyone")]
        assert w2.sent == [("text", "everyone")]
    finally:
        await a.aclose()
        await b.aclose()


async def test_listener_reconnects_after_a_redis_blip() -> None:
    """The production-resilience path: if the pub/sub connection drops (the first
    ``listen()`` raises), the listener re-subscribes on a fresh connection and
    delivery resumes — without losing the subscriber's local subscription."""
    redis = _FakeRedis(fail_first_listen=True)
    sub = RedisBroker(client=redis, reconnect_delay=0.01)
    await sub.start()  # first listen() raises; listener reconnects in the background
    try:
        ws = FakeWS()
        await sub.subscribe("c", ws)
        wire = _frame_to_wire((False, "after-reconnect"))
        # Re-publish (as another worker would) until the reconnected listener,
        # on its fresh pub/sub connection, delivers it.
        for _ in range(200):
            await redis.publish("pyxle:rt:c", wire)
            await asyncio.sleep(0.01)
            if ws.sent:
                break
        assert ("text", "after-reconnect") in ws.sent
        assert len(redis._subs) >= 2  # a fresh pub/sub was created on reconnect
    finally:
        await sub.aclose()


async def test_start_fails_loudly_if_psubscribe_keeps_failing() -> None:
    """If Redis accepts PING but PSUBSCRIBE persistently fails, start() must raise
    within connect_timeout rather than hang the worker's whole lifespan startup
    forever (the listener would otherwise retry the subscription indefinitely)."""
    redis = _FakeRedis(fail_psubscribe=True)
    broker = RedisBroker(client=redis, reconnect_delay=0.01, connect_timeout=0.2)
    with pytest.raises(RuntimeError, match="could not establish its pub/sub subscription"):
        await asyncio.wait_for(broker.start(), timeout=2.0)  # must not hang past timeout
    assert broker._listener is None  # aclose() cancelled the retry loop


async def test_non_serializable_message_raises_before_touching_redis() -> None:
    redis = _FakeRedis()
    broker = RedisBroker(client=redis)
    await broker.start()
    try:
        with pytest.raises(TypeError):
            await broker.publish("c", {"bad": object()})  # not JSON-serializable
        assert redis.publish_calls == 0  # encoded up front; Redis never hit
    finally:
        await broker.aclose()


# --- internal helpers ------------------------------------------------------


async def test_safe_aclose_handles_every_client_shape() -> None:
    from pyxle.realtime.redis_broker import _safe_aclose

    calls: list[str] = []

    class _AClose:
        async def aclose(self) -> None:
            calls.append("aclose")

    class _SyncClose:
        def close(self) -> None:
            calls.append("close")

    class _AsyncClose:
        async def close(self) -> None:
            calls.append("async-close")

    class _Raises:
        async def aclose(self) -> None:
            raise RuntimeError("boom")  # must be swallowed

    class _Neither:
        pass

    for obj in (_AClose(), _SyncClose(), _AsyncClose(), _Raises(), _Neither()):
        await _safe_aclose(obj)  # never raises
    assert calls == ["aclose", "close", "async-close"]


def test_channel_data_coercion_helpers() -> None:
    from pyxle.realtime.redis_broker import _as_bytes, _as_str

    assert _as_str(b"x") == "x"
    assert _as_str("y") == "y"
    assert _as_bytes(b"x") == b"x"
    assert _as_bytes("y") == b"y"


async def test_dispatch_ignores_channels_outside_the_prefix() -> None:
    broker = RedisBroker(client=_FakeRedis(), channel_prefix="pyxle:rt:")
    await broker.start()
    try:
        ws = FakeWS()
        await broker.subscribe("c", ws)
        # A message whose channel lacks our namespace prefix is ignored.
        await broker._dispatch("other:c", _frame_to_wire((False, "nope")))
        assert ws.sent == []
    finally:
        await broker.aclose()


# --- framing round-trip ----------------------------------------------------


def test_frame_wire_round_trip() -> None:
    assert _wire_to_frame(_frame_to_wire((False, "héllo"))) == (False, "héllo")
    assert _wire_to_frame(_frame_to_wire((True, b"\x00\xff"))) == (True, b"\x00\xff")
    # The tag byte keeps text and binary distinct even for identical bytes.
    assert _frame_to_wire((False, "b")) == b"tb"
    assert _frame_to_wire((True, b"b")) == b"bb"


# --- factory + lifecycle ---------------------------------------------------


def test_factory_defaults_to_inprocess() -> None:
    assert isinstance(build_broker(env={}), InProcessBroker)
    assert isinstance(build_broker(env={"PYXLE_REALTIME_BROKER": "memory"}), InProcessBroker)


def test_factory_selects_redis() -> None:
    broker = build_broker(env={"PYXLE_REALTIME_BROKER": "redis",
                               "PYXLE_REALTIME_REDIS_URL": "redis://example:6379",
                               "PYXLE_REALTIME_CHANNEL_PREFIX": "app:"})
    assert isinstance(broker, RedisBroker)
    assert broker._url == "redis://example:6379"
    assert broker._prefix == "app:"


def test_factory_rejects_unknown_broker() -> None:
    with pytest.raises(ValueError, match="Unknown PYXLE_REALTIME_BROKER"):
        build_broker(env={"PYXLE_REALTIME_BROKER": "nats"})


async def test_inprocess_broker_lifecycle_is_noop() -> None:
    broker = InProcessBroker()
    await broker.start()  # no-op; present for uniform lifecycle
    await broker.aclose()


async def test_missing_redis_extra_raises_clear_error() -> None:
    # With no injected client and the import unavailable, start() must explain
    # how to install the extra. Simulate the missing import.
    broker = RedisBroker("redis://localhost:6379")
    import builtins

    real_import = builtins.__import__

    def _no_redis(name, *args, **kwargs):
        if name == "redis.asyncio" or name.startswith("redis"):
            raise ImportError("no redis")
        return real_import(name, *args, **kwargs)

    builtins.__import__ = _no_redis
    try:
        with pytest.raises(RuntimeError, match="pip install 'pyxle-framework\\[redis\\]'"):
            await broker.start()
    finally:
        builtins.__import__ = real_import
