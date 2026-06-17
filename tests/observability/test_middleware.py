"""Tests for the request-id + timing ASGI middleware."""

from __future__ import annotations

import re

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.responses import JSONResponse
from starlette.routing import Route, WebSocketRoute
from starlette.testclient import TestClient

from pyxle.observability import RequestIdMiddleware, get_request_id, request_timing_ms

_HEX32 = re.compile(r"\A[0-9a-f]{32}\Z")


async def _echo(request):
    # Stash the live per-request namespace so a test can inspect the timing
    # field, which the middleware fills at http.response.start (after this
    # handler returns) — the dict reference is shared, so it's visible later.
    _CAPTURED.append(request.scope.get("pyxle"))
    return JSONResponse(
        {
            "state_request_id": getattr(request.state, "request_id", None),
            "helper_request_id": get_request_id(request),
        }
    )


async def _ws(websocket):
    await websocket.accept()
    await websocket.send_text("ok")
    await websocket.close()


_CAPTURED: list = []


def _client(**mw_kwargs) -> TestClient:
    _CAPTURED.clear()
    app = Starlette(
        routes=[Route("/", _echo), WebSocketRoute("/ws", _ws)],
        middleware=[Middleware(RequestIdMiddleware, **mw_kwargs)],
    )
    return TestClient(app)


# ---------------------------------------------------------------------------
# request id


def test_generates_request_id_and_response_header() -> None:
    resp = _client().get("/")
    header = resp.headers["x-request-id"]
    assert _HEX32.match(header)
    body = resp.json()
    # The handler sees the same id via request.state and the helper.
    assert body["state_request_id"] == header
    assert body["helper_request_id"] == header


def test_custom_header_name() -> None:
    resp = _client(header_name="X-Trace-Id").get("/")
    assert "x-trace-id" in resp.headers
    assert "x-request-id" not in resp.headers


def test_emit_request_id_false_sets_no_header_but_still_times() -> None:
    resp = _client(emit_request_id=False, timing=True).get("/")
    assert "x-request-id" not in resp.headers
    assert resp.json()["state_request_id"] is None
    # Timing is recorded even without a request id.
    assert isinstance(_CAPTURED[0]["duration_ms"], float)


def test_each_request_gets_a_distinct_id() -> None:
    client = _client()
    first = client.get("/").headers["x-request-id"]
    second = client.get("/").headers["x-request-id"]
    assert first != second


# ---------------------------------------------------------------------------
# trusting (or not) an incoming id


def test_incoming_id_ignored_by_default() -> None:
    resp = _client().get("/", headers={"X-Request-Id": "client-supplied-id"})
    assert resp.headers["x-request-id"] != "client-supplied-id"
    assert _HEX32.match(resp.headers["x-request-id"])


def test_incoming_id_honoured_when_trusted() -> None:
    resp = _client(trust_incoming=True).get(
        "/", headers={"X-Request-Id": "trace-abc.123"}
    )
    assert resp.headers["x-request-id"] == "trace-abc.123"


def test_trusted_but_no_incoming_header_generates_fresh_id() -> None:
    # trust_incoming on, but the client sends no id: fall through to a fresh one.
    resp = _client(trust_incoming=True).get("/")
    assert _HEX32.match(resp.headers["x-request-id"])


def test_unsafe_incoming_id_replaced_even_when_trusted() -> None:
    client = _client(trust_incoming=True)
    # A space is a valid header byte but not an allowed id character.
    spaced = client.get("/", headers={"X-Request-Id": "has space"})
    assert spaced.headers["x-request-id"] != "has space"
    assert _HEX32.match(spaced.headers["x-request-id"])
    # Over-length ids (>128 chars) are rejected too.
    long_id = "a" * 200
    over = client.get("/", headers={"X-Request-Id": long_id})
    assert over.headers["x-request-id"] != long_id
    assert _HEX32.match(over.headers["x-request-id"])


# ---------------------------------------------------------------------------
# timing


def test_timing_records_duration() -> None:
    _client().get("/")
    # Recorded at http.response.start, visible on the shared scope dict after.
    duration = _CAPTURED[0]["duration_ms"]
    assert isinstance(duration, float)
    assert duration >= 0.0


def test_timing_disabled_records_nothing() -> None:
    _client(timing=False).get("/")
    assert "duration_ms" not in _CAPTURED[0]


def test_metrics_registry_records_request() -> None:
    from pyxle.observability.metrics import MetricsRegistry

    registry = MetricsRegistry()
    _client(metrics=registry).get("/")
    assert registry.requests_total == 1
    assert registry.requests_by_status.get("2xx") == 1
    assert registry.request_duration.total == 1


def test_metrics_recorded_even_with_request_id_and_timing_off() -> None:
    from pyxle.observability.metrics import MetricsRegistry

    registry = MetricsRegistry()
    _client(emit_request_id=False, timing=False, metrics=registry).get("/")
    # A registry forces measurement even when the scope timing field is not set.
    assert registry.requests_total == 1
    assert registry.request_duration.total == 1
    assert "duration_ms" not in _CAPTURED[0]


def test_request_timing_ms_reads_scope_field() -> None:
    class _Req:
        scope = {"pyxle": {"duration_ms": 12.5}}

    assert request_timing_ms(_Req()) == 12.5


# ---------------------------------------------------------------------------
# pass-through


def test_websocket_scope_passes_through() -> None:
    # Non-HTTP scopes must not be touched (no id, no header rewrite).
    with _client().websocket_connect("/ws") as ws:
        assert ws.receive_text() == "ok"


# ---------------------------------------------------------------------------
# helpers in isolation


def test_get_request_id_returns_none_without_context() -> None:
    class _Bare:
        pass

    assert get_request_id(_Bare()) is None


def test_request_timing_ms_returns_none_without_scope() -> None:
    class _Bare:
        pass

    assert request_timing_ms(_Bare()) is None


def test_get_request_id_falls_back_to_scope() -> None:
    # An object with only the raw ASGI scope populated (no request.state).
    class _ScopeOnly:
        scope = {"pyxle": {"request_id": "from-scope"}}

    assert get_request_id(_ScopeOnly()) == "from-scope"


def test_get_request_id_none_when_scope_has_no_namespace() -> None:
    # scope is a mapping but carries no "pyxle" namespace yet.
    class _Req:
        scope = {}

    assert get_request_id(_Req()) is None


def test_get_request_id_empty_state_falls_back_to_scope() -> None:
    # request.state present but request_id unset -> fall through to the scope.
    class _State:
        request_id = None

    class _Req:
        state = _State()
        scope = {"pyxle": {"request_id": "scoped"}}

    assert get_request_id(_Req()) == "scoped"


def test_request_timing_ms_none_when_namespace_absent() -> None:
    class _Req:
        scope = {}  # no "pyxle" namespace

    assert request_timing_ms(_Req()) is None


def test_request_timing_ms_none_when_value_not_numeric() -> None:
    class _Req:
        scope = {"pyxle": {"duration_ms": "fast"}}

    assert request_timing_ms(_Req()) is None
