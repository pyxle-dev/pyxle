from __future__ import annotations

import json

import pytest
from starlette.websockets import WebSocketDisconnect

from pyxle.cli.logger import ConsoleLogger
from pyxle.devserver.dev_origins import private_origin_pattern
from pyxle.devserver.overlay import (
    _REFUSED_ORIGIN_MEMORY,
    OverlayEvent,
    OverlayManager,
    _format_stacktrace,
)


@pytest.fixture
def anyio_backend() -> str:  # pragma: no cover - fixture wiring
    return "asyncio"


class StubLogger(ConsoleLogger):
    def __init__(self) -> None:
        super().__init__(secho=lambda *_args, **_kwargs: None)


class _StubHeaders:
    """Minimal dict-like object mimicking Starlette's header access."""

    def __init__(self, data: dict[str, str] | None = None) -> None:
        self._data = {k.lower(): v for k, v in (data or {}).items()}

    def get(self, key: str, default: str = "") -> str:
        return self._data.get(key.lower(), default)


class StubWebSocket:
    def __init__(self, *, origin: str = "") -> None:
        self.accepted = False
        self.sent: list[str] = []
        self.receive_calls = 0
        self.disconnect_after: int | None = None
        self.closed_code: int | None = None
        self.headers = _StubHeaders({"origin": origin} if origin else {})

    async def accept(self) -> None:
        self.accepted = True

    async def close(self, code: int = 1000) -> None:
        self.closed_code = code

    async def send_text(self, data: str) -> None:
        self.sent.append(data)
        if self.disconnect_after is not None and len(self.sent) >= self.disconnect_after:
            raise RuntimeError("fail")

    async def receive_text(self) -> str:
        self.receive_calls += 1
        raise WebSocketDisconnect(1000)


@pytest.mark.anyio
async def test_overlay_manager_broadcasts_error_and_clear() -> None:
    manager = OverlayManager(logger=StubLogger())
    socket = StubWebSocket()

    await manager.register(socket)

    await manager.notify_error(route_path="/", error=RuntimeError("boom"), stack="trace")
    await manager.notify_clear(route_path="/")

    assert socket.accepted is True
    assert len(socket.sent) == 2

    error_message = json.loads(socket.sent[0])
    assert error_message["type"] == "error"
    assert error_message["payload"]["routePath"] == "/"
    assert error_message["payload"]["message"] == "boom"
    assert error_message["payload"]["stack"] == "trace"
    assert error_message["payload"]["breadcrumbs"] == []

    clear_message = json.loads(socket.sent[1])
    assert clear_message["type"] == "clear"
    assert clear_message["payload"]["routePath"] == "/"


@pytest.mark.anyio
async def test_overlay_manager_broadcasts_reload_event() -> None:
    manager = OverlayManager(logger=StubLogger())
    socket = StubWebSocket()

    await manager.register(socket)

    await manager.notify_reload(changed_paths=["pages/index.pyxl"])

    assert socket.sent, "expected reload payload"
    message = json.loads(socket.sent[0])
    assert message["type"] == "reload"
    assert message["payload"]["changedPaths"] == ["pages/index.pyxl"]


@pytest.mark.anyio
async def test_overlay_manager_broadcasts_log_event() -> None:
    manager = OverlayManager(logger=StubLogger())
    socket = StubWebSocket()

    await manager.register(socket)

    await manager.notify_log(level="warn", message="db slow", logger_name="app.db")

    assert socket.sent, "expected log payload"
    message = json.loads(socket.sent[0])
    assert message["type"] == "log"
    assert message["payload"]["level"] == "warn"
    assert message["payload"]["message"] == "db slow"
    assert message["payload"]["logger"] == "app.db"


@pytest.mark.anyio
async def test_overlay_manager_log_event_defaults_logger_name() -> None:
    manager = OverlayManager(logger=StubLogger())
    socket = StubWebSocket()

    await manager.register(socket)

    await manager.notify_log(level="info", message="hello")

    message = json.loads(socket.sent[0])
    assert message["payload"]["logger"] == ""


@pytest.mark.anyio
async def test_overlay_manager_endpoint_unregisters_on_disconnect() -> None:
    manager = OverlayManager(logger=StubLogger())
    socket = StubWebSocket()

    await manager.websocket_endpoint(socket)

    assert socket.accepted is True
    assert socket.receive_calls == 1
    assert len(manager._connections) == 0  # type: ignore[attr-defined]


@pytest.mark.anyio
async def test_overlay_manager_removes_stale_connections() -> None:
    manager = OverlayManager(logger=StubLogger())
    socket = StubWebSocket()
    socket.disconnect_after = 1

    await manager.register(socket)
    await manager.notify_error(route_path="/", error=RuntimeError("boom"), stack="trace")

    assert len(socket.sent) == 1
    assert len(manager._connections) == 0  # type: ignore[attr-defined]


def test_is_allowed_origin_accepts_everything_when_unconfigured() -> None:
    manager = OverlayManager(logger=StubLogger())

    # With no configured origins, any value (including a bogus one) is allowed.
    assert manager._is_allowed_origin("http://evil.example.com") is True  # type: ignore[attr-defined]
    assert manager._is_allowed_origin("") is True  # type: ignore[attr-defined]


def test_is_allowed_origin_accepts_empty_origin_when_configured() -> None:
    manager = OverlayManager(
        logger=StubLogger(), allowed_origins={"http://localhost:8000"}
    )

    # A missing Origin header indicates a same-origin or non-browser client.
    assert manager._is_allowed_origin("") is True  # type: ignore[attr-defined]


def test_is_allowed_origin_matches_after_normalising_trailing_slash() -> None:
    manager = OverlayManager(
        logger=StubLogger(), allowed_origins={"http://localhost:8000"}
    )

    # Trailing slashes are stripped before the exact-set comparison.
    assert manager._is_allowed_origin("http://localhost:8000/") is True  # type: ignore[attr-defined]
    assert manager._is_allowed_origin("http://localhost:8000") is True  # type: ignore[attr-defined]


def test_is_allowed_origin_matches_loopback_alias_by_port() -> None:
    manager = OverlayManager(
        logger=StubLogger(), allowed_origins={"http://localhost:8000"}
    )

    # 127.0.0.1 is treated as an alias of localhost when the port matches an
    # allowed origin, even though the exact string is not in the set.
    assert manager._is_allowed_origin("http://127.0.0.1:8000") is True  # type: ignore[attr-defined]


def test_is_allowed_origin_rejects_loopback_with_wrong_port() -> None:
    manager = OverlayManager(
        logger=StubLogger(), allowed_origins={"http://localhost:8000"}
    )

    # Loopback host but a non-matching port is rejected.
    assert manager._is_allowed_origin("http://127.0.0.1:9999") is False  # type: ignore[attr-defined]


def test_is_allowed_origin_rejects_non_loopback_origin() -> None:
    manager = OverlayManager(
        logger=StubLogger(), allowed_origins={"http://localhost:8000"}
    )

    # A genuinely foreign origin is rejected when origins are configured.
    assert manager._is_allowed_origin("http://attacker.test:8000") is False  # type: ignore[attr-defined]


def test_is_allowed_origin_matches_the_private_network_pattern() -> None:
    """A dev server bound to every interface must accept the browsers it invited.

    ``pyxle dev --host 0.0.0.0`` prints a ``Network:`` URL. A phone that opens it
    and is then refused the overlay socket loses hot reload and the error
    overlay, and the build-failure page it may be looking at — which promises to
    reload itself once the rebuild succeeds — never does.
    """

    manager = OverlayManager(
        logger=StubLogger(),
        allowed_origins={"http://localhost:3000", "http://127.0.0.1:3000"},
        allowed_origin_pattern=private_origin_pattern(3000, 5173),
    )

    assert manager._is_allowed_origin("http://192.168.1.11:3000") is True  # type: ignore[attr-defined]
    assert manager._is_allowed_origin("http://192.168.1.11:5173") is True  # type: ignore[attr-defined]
    assert manager._is_allowed_origin("http://10.0.0.4:3000/") is True  # type: ignore[attr-defined]
    # Deliberately not "any origin": the socket carries source paths, stack
    # traces and forwarded server logs.
    assert manager._is_allowed_origin("http://evil.example.com") is False  # type: ignore[attr-defined]
    assert manager._is_allowed_origin("http://192.168.1.11:9999") is False  # type: ignore[attr-defined]


@pytest.mark.anyio
async def test_websocket_endpoint_reports_a_refused_origin_once() -> None:
    """A refusal is invisible in the browser, so the terminal has to say it."""

    lines: list[str] = []
    logger = ConsoleLogger(secho=lambda message, **_kwargs: lines.append(message))
    manager = OverlayManager(logger=logger, allowed_origins={"http://localhost:3000"})

    await manager.websocket_endpoint(StubWebSocket(origin="http://192.168.1.11:3000"))
    await manager.websocket_endpoint(StubWebSocket(origin="http://192.168.1.11:3000"))
    await manager.websocket_endpoint(StubWebSocket(origin="http://evil.example.com"))

    refusals = [line for line in lines if "Refused a dev overlay connection" in line]
    # One per origin — a browser reconnecting on a timer must not fill the
    # terminal, and a second origin must not be swallowed by the first.
    assert len(refusals) == 2
    assert "http://192.168.1.11:3000" in refusals[0]
    assert "http://evil.example.com" in refusals[1]


@pytest.mark.anyio
async def test_refused_origin_memory_stops_growing() -> None:
    """The dedupe set is a cache, and every cache here has a bound."""

    manager = OverlayManager(logger=StubLogger(), allowed_origins={"http://localhost:3000"})

    for index in range(_REFUSED_ORIGIN_MEMORY + 10):
        await manager.websocket_endpoint(
            StubWebSocket(origin=f"http://host-{index}.test")
        )

    assert len(manager._refused_origins) == _REFUSED_ORIGIN_MEMORY  # type: ignore[attr-defined]


@pytest.mark.anyio
async def test_websocket_endpoint_rejects_disallowed_origin() -> None:
    manager = OverlayManager(
        logger=StubLogger(), allowed_origins={"http://localhost:8000"}
    )
    socket = StubWebSocket(origin="http://attacker.test:8000")

    await manager.websocket_endpoint(socket)

    # The connection is closed with the policy-violation code and never
    # registered or accepted.
    assert socket.closed_code == 4003
    assert socket.accepted is False
    assert socket.receive_calls == 0
    assert len(manager._connections) == 0  # type: ignore[attr-defined]


@pytest.mark.anyio
async def test_websocket_endpoint_accepts_allowed_origin() -> None:
    manager = OverlayManager(
        logger=StubLogger(), allowed_origins={"http://localhost:8000"}
    )
    socket = StubWebSocket(origin="http://localhost:8000")

    await manager.websocket_endpoint(socket)

    # An allowed origin is accepted, drained until disconnect, then cleaned up.
    assert socket.accepted is True
    assert socket.closed_code is None
    assert socket.receive_calls == 1
    assert len(manager._connections) == 0  # type: ignore[attr-defined]


def test_format_stacktrace_includes_exception_and_message() -> None:
    try:
        raise ValueError("kaboom")
    except ValueError as exc:
        formatted = _format_stacktrace(exc)

    assert "ValueError" in formatted
    assert "kaboom" in formatted
    assert "Traceback (most recent call last)" in formatted


@pytest.mark.anyio
async def test_notify_error_formats_stack_when_omitted() -> None:
    manager = OverlayManager(logger=StubLogger())
    socket = StubWebSocket()
    await manager.register(socket)

    try:
        raise RuntimeError("derived stack")
    except RuntimeError as exc:
        await manager.notify_error(route_path="/dashboard", error=exc)

    assert len(socket.sent) == 1
    message = json.loads(socket.sent[0])
    assert message["type"] == "error"
    assert message["payload"]["routePath"] == "/dashboard"
    assert message["payload"]["message"] == "derived stack"
    # No explicit stack was passed, so notify_error falls back to a formatted
    # traceback derived from the live exception.
    assert "RuntimeError" in message["payload"]["stack"]
    assert "derived stack" in message["payload"]["stack"]


class TestErrorSurvivesReload:
    """An error must outlive the tab that happened to be open when it broke.

    Reloading a page closes the overlay socket and opens a new one. Without
    replay the new socket is told nothing, so the browser shows a healthy page
    while the source is still broken — and the developer concludes hot reload
    is what is broken.
    """

    @pytest.mark.anyio
    async def test_error_is_replayed_to_a_client_that_connects_later(self) -> None:
        manager = OverlayManager(logger=StubLogger())
        await manager.notify_error(
            route_path="(rebuild)", error=RuntimeError("pages/about.pyxl:7:9: bad")
        )

        reconnected = StubWebSocket()
        await manager.register(reconnected)

        assert len(reconnected.sent) == 1
        replayed = json.loads(reconnected.sent[0])
        assert replayed["type"] == "error"
        assert replayed["payload"]["routePath"] == "(rebuild)"
        assert "pages/about.pyxl:7:9" in replayed["payload"]["message"]

    @pytest.mark.anyio
    async def test_nothing_is_replayed_when_the_build_is_healthy(self) -> None:
        manager = OverlayManager(logger=StubLogger())
        socket = StubWebSocket()

        await manager.register(socket)

        assert socket.sent == []

    @pytest.mark.anyio
    async def test_clearing_a_route_stops_it_being_replayed(self) -> None:
        manager = OverlayManager(logger=StubLogger())
        await manager.notify_error(route_path="(rebuild)", error=RuntimeError("boom"))
        await manager.notify_clear(route_path="(rebuild)")

        socket = StubWebSocket()
        await manager.register(socket)

        assert socket.sent == []

    @pytest.mark.anyio
    async def test_a_healthy_route_does_not_clear_another_route_error(self) -> None:
        """Every successful render clears its own route — and only its own."""
        manager = OverlayManager(logger=StubLogger())
        await manager.notify_error(route_path="(rebuild)", error=RuntimeError("boom"))
        await manager.notify_clear(route_path="/")

        socket = StubWebSocket()
        await manager.register(socket)

        assert len(socket.sent) == 1
        assert json.loads(socket.sent[0])["payload"]["routePath"] == "(rebuild)"

    @pytest.mark.anyio
    async def test_every_unresolved_error_is_replayed_newest_last(self) -> None:
        """A reconnecting client is told about all of them, not just the newest.

        The client keeps only the errors that apply to the page it is on, so it
        has to hear about every one: replaying only the most recent would hide a
        broken route whenever some *other* route broke after it. Newest last,
        because the client shows the last applicable error it was told about.
        """
        manager = OverlayManager(logger=StubLogger())
        await manager.notify_error(route_path="/first", error=RuntimeError("older"))
        await manager.notify_error(route_path="/second", error=RuntimeError("newer"))

        socket = StubWebSocket()
        await manager.register(socket)

        replayed = [json.loads(m)["payload"]["routePath"] for m in socket.sent]
        assert replayed == ["/first", "/second"]

    @pytest.mark.anyio
    async def test_notify_error_carries_the_url_that_failed(self) -> None:
        """The concrete URL, so the client can tell whose page is broken.

        ``routePath`` is the pattern (``/posts/[id]``); ``requestPath`` is the
        URL the developer actually asked for. Without it the client cannot tell
        an error on the page it is showing from an error on some other page.
        """
        manager = OverlayManager(logger=StubLogger())
        socket = StubWebSocket()
        await manager.register(socket)

        await manager.notify_error(
            route_path="/posts/[id]",
            error=RuntimeError("boom"),
            request_path="/posts/3",
        )

        payload = json.loads(socket.sent[0])["payload"]
        assert payload["routePath"] == "/posts/[id]"
        assert payload["requestPath"] == "/posts/3"

    @pytest.mark.anyio
    async def test_an_error_with_no_url_still_applies_everywhere(self) -> None:
        """A failed rebuild breaks every page, so it is not scoped to one URL."""
        manager = OverlayManager(logger=StubLogger())
        socket = StubWebSocket()
        await manager.register(socket)

        await manager.notify_error(route_path="(rebuild)", error=RuntimeError("boom"))

        assert json.loads(socket.sent[0])["payload"]["requestPath"] is None

    @pytest.mark.anyio
    async def test_repeating_an_error_keeps_one_entry_per_route(self) -> None:
        manager = OverlayManager(logger=StubLogger())
        await manager.notify_error(route_path="/a", error=RuntimeError("one"))
        await manager.notify_error(route_path="/b", error=RuntimeError("two"))
        await manager.notify_error(route_path="/a", error=RuntimeError("three"))
        await manager.notify_clear(route_path="/a")

        socket = StubWebSocket()
        await manager.register(socket)

        assert json.loads(socket.sent[0])["payload"]["routePath"] == "/b"

    @pytest.mark.anyio
    async def test_a_client_that_vanishes_during_replay_is_dropped(self) -> None:
        manager = OverlayManager(logger=StubLogger())
        await manager.notify_error(route_path="(rebuild)", error=RuntimeError("boom"))

        socket = StubWebSocket()
        socket.disconnect_after = 1
        await manager.register(socket)

        await manager.broadcast(OverlayEvent(type="reload", payload={}))
        assert len(socket.sent) == 1
