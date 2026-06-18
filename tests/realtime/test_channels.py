"""Tests for the in-process pub/sub broker and the ``channel`` helper."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from pyxle.realtime import Broker, InProcessBroker, channel
from pyxle.realtime.channels import ChannelHandle, broker_for

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class FakeWS:
    """Records frames sent to it; can simulate a dead socket that raises.

    ``app`` mirrors Starlette's ``WebSocket.app`` (``scope["app"]``): raises
    ``KeyError`` when no app is attached.
    """

    def __init__(self, *, fail: bool = False, app: object | None = None) -> None:
        self.sent: list[tuple[str, object]] = []
        self._fail = fail
        self._app = app

    @property
    def app(self):
        if self._app is None:
            raise KeyError("app")
        return self._app

    async def send_text(self, text: str) -> None:
        if self._fail:
            raise RuntimeError("dead socket")
        self.sent.append(("text", text))

    async def send_bytes(self, data: bytes) -> None:
        if self._fail:
            raise RuntimeError("dead socket")
        self.sent.append(("bytes", data))


async def test_publish_delivers_to_subscribers_only() -> None:
    broker = InProcessBroker()
    a, b, outsider = FakeWS(), FakeWS(), FakeWS()
    await broker.subscribe("room:1", a)
    await broker.subscribe("room:1", b)
    # outsider never subscribed

    await broker.publish("room:1", "hello")

    assert a.sent == [("text", "hello")]
    assert b.sent == [("text", "hello")]
    assert outsider.sent == []


async def test_cross_room_isolation() -> None:
    broker = InProcessBroker()
    a, b = FakeWS(), FakeWS()
    await broker.subscribe("room:1", a)
    await broker.subscribe("room:2", b)

    await broker.publish("room:1", "for-room-1")

    assert a.sent == [("text", "for-room-1")]
    assert b.sent == []


async def test_publish_to_empty_channel_is_noop() -> None:
    broker = InProcessBroker()
    await broker.publish("nobody-here", "drop")  # must not raise
    assert broker.channel_count() == 0


async def test_dict_message_is_json_encoded() -> None:
    broker = InProcessBroker()
    ws = FakeWS()
    await broker.subscribe("c", ws)
    await broker.publish("c", {"type": "msg", "body": "hi"})
    kind, payload = ws.sent[0]
    assert kind == "text"
    assert json.loads(payload) == {"type": "msg", "body": "hi"}


async def test_bytes_message_uses_binary_frame() -> None:
    broker = InProcessBroker()
    ws = FakeWS()
    await broker.subscribe("c", ws)
    await broker.publish("c", b"\x00\x01")
    assert ws.sent == [("bytes", b"\x00\x01")]


async def test_unsubscribe_stops_delivery_and_cleans_empty_channel() -> None:
    broker = InProcessBroker()
    ws = FakeWS()
    await broker.subscribe("room", ws)
    await broker.unsubscribe("room", ws)
    assert broker.channel_count() == 0  # empty channel pruned
    await broker.publish("room", "after-leave")
    assert ws.sent == []


async def test_send_failure_prunes_stale_connection() -> None:
    broker = InProcessBroker()
    good, dead = FakeWS(), FakeWS(fail=True)
    await broker.subscribe("room", good)
    await broker.subscribe("room", dead)

    await broker.publish("room", "first")
    # The dead socket raised on send and was pruned from the channel.
    assert good.sent == [("text", "first")]

    await broker.publish("room", "second")
    # Only the good socket still receives; the dead one is gone.
    assert good.sent == [("text", "first"), ("text", "second")]


async def test_non_serializable_message_raises_without_evicting_subscribers() -> None:
    # Regression: a non-JSON-serializable message must raise to the PUBLISHER,
    # not be misread as a dead socket and evict every healthy subscriber.
    broker = InProcessBroker()
    a, b = FakeWS(), FakeWS()
    await broker.subscribe("room", a)
    await broker.subscribe("room", b)

    with pytest.raises(TypeError):
        await broker.publish("room", {"bad": object()})

    # Both healthy sockets are untouched and still subscribed.
    assert broker.channel_count() == 1
    assert a.sent == [] and b.sent == []
    await broker.publish("room", "still delivers")
    assert a.sent == [("text", "still delivers")]
    assert b.sent == [("text", "still delivers")]


async def test_broadcast_non_serializable_raises_without_eviction() -> None:
    broker = InProcessBroker()
    ws = FakeWS()
    await broker.subscribe("room", ws)
    with pytest.raises(TypeError):
        await broker.broadcast({1, 2, 3})  # a set is not JSON-serializable
    assert broker.channel_count() == 1
    assert ws.sent == []


async def test_broadcast_reaches_all_channels_deduped() -> None:
    broker = InProcessBroker()
    a, b, shared = FakeWS(), FakeWS(), FakeWS()
    await broker.subscribe("room:1", a)
    await broker.subscribe("room:2", b)
    await broker.subscribe("room:1", shared)
    await broker.subscribe("room:2", shared)  # in two channels

    await broker.broadcast("everyone")

    assert a.sent == [("text", "everyone")]
    assert b.sent == [("text", "everyone")]
    # shared is in two channels but receives the broadcast exactly once.
    assert shared.sent == [("text", "everyone")]


async def test_channel_helper_subscribes_and_unsubscribes() -> None:
    broker = InProcessBroker()
    member, other = FakeWS(), FakeWS()
    await broker.subscribe("room", other)

    async with channel(member, "room", broker=broker) as room:
        assert isinstance(room, ChannelHandle)
        assert room.name == "room"
        await room.publish("hi")
        # A publish reaches every subscriber — including the publisher; a chat
        # handler that doesn't want to echo to the sender filters client-side.
        assert other.sent == [("text", "hi")]
        assert member.sent == [("text", "hi")]

    # On exit, member is unsubscribed: a later publish reaches `other` but not it.
    await broker.publish("room", "after")
    assert other.sent == [("text", "hi"), ("text", "after")]
    assert member.sent == [("text", "hi")]  # unchanged — no longer subscribed


async def test_inprocess_broker_satisfies_protocol() -> None:
    assert isinstance(InProcessBroker(), Broker)


async def test_prune_removes_emptied_channel() -> None:
    # A channel whose only member dies is removed entirely after the prune.
    broker = InProcessBroker()
    dead = FakeWS(fail=True)
    await broker.subscribe("room", dead)
    await broker.publish("room", "x")  # dead raises → pruned → channel emptied
    assert broker.channel_count() == 0


async def test_broadcast_to_no_subscribers_is_noop() -> None:
    broker = InProcessBroker()
    await broker.broadcast("nobody")  # must not raise
    assert broker.channel_count() == 0


def test_broker_for_returns_app_scoped_broker() -> None:
    existing = InProcessBroker()
    app = SimpleNamespace(state=SimpleNamespace(pyxle_broker=existing))
    assert broker_for(FakeWS(app=app)) is existing


def test_broker_for_creates_and_attaches_when_absent() -> None:
    app = SimpleNamespace(state=SimpleNamespace())  # no pyxle_broker yet
    ws = FakeWS(app=app)
    created = broker_for(ws)
    assert isinstance(created, InProcessBroker)
    assert app.state.pyxle_broker is created  # attached to the app
    assert broker_for(ws) is created  # reused on the next call


def test_broker_for_falls_back_without_app() -> None:
    first = broker_for(FakeWS(app=None))  # ws.app raises KeyError
    assert isinstance(first, InProcessBroker)
    # The process fallback is reused for subsequent app-less sockets.
    assert broker_for(FakeWS(app=None)) is first


async def test_channel_uses_app_broker_by_default() -> None:
    app_broker = InProcessBroker()
    app = SimpleNamespace(state=SimpleNamespace(pyxle_broker=app_broker))
    member = FakeWS(app=app)
    other = FakeWS()
    await app_broker.subscribe("room", other)

    # No explicit broker= → channel resolves the app-scoped broker.
    async with channel(member, "room") as room:
        await room.publish("hi")
        assert other.sent == [("text", "hi")]


async def test_two_brokers_do_not_share_state() -> None:
    # The multi-worker caveat, asserted: a message published on one broker is
    # NEVER seen by a subscriber on another broker (each worker has its own).
    worker1, worker2 = InProcessBroker(), InProcessBroker()
    client_on_w1 = FakeWS()
    await worker1.subscribe("room", client_on_w1)

    await worker2.publish("room", "from-worker-2")

    assert client_on_w1.sent == []  # cross-worker delivery does not happen
