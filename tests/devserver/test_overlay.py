from __future__ import annotations

import json

import pytest
from starlette.websockets import WebSocketDisconnect

from pyxle.cli.logger import ConsoleLogger
from pyxle.devserver.overlay import OverlayManager, _format_stacktrace


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
