"""Tests for the WebSocket auth helpers (session resolution + origin check)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from pyxle.realtime import authenticate_websocket, origin_allowed

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class FakePlugins:
    def __init__(self, services: dict) -> None:
        self._services = services

    def get(self, name: str):
        return self._services.get(name)


class FakeAuthService:
    def __init__(self, sessions: dict, *, cookie_name: str = "pyxle_session") -> None:
        self.settings = SimpleNamespace(cookie_name=cookie_name)
        self._sessions = sessions
        self.calls: list[tuple[str, bool]] = []

    async def resolve_session(self, *, cookie_value: str, extend: bool = True):
        self.calls.append((cookie_value, extend))
        return self._sessions.get(cookie_value)


class FakeWS:
    """Minimal WebSocket stand-in: ``.app`` (KeyError when absent),
    ``.cookies``, ``.headers``."""

    def __init__(self, *, plugins=None, has_app: bool = True, cookies=None, headers=None) -> None:
        self._has_app = has_app
        self._plugins = plugins
        self.cookies = cookies or {}
        self.headers = headers or {}

    @property
    def app(self):
        if not self._has_app:
            raise KeyError("app")  # mirrors Starlette's scope["app"] KeyError
        return SimpleNamespace(state=SimpleNamespace(pyxle_plugins=self._plugins))


USER = SimpleNamespace(id="u1", email="a@b.c")


async def test_authenticates_with_service_and_cookie() -> None:
    auth = FakeAuthService({"good-cookie": USER})
    ws = FakeWS(plugins=FakePlugins({"auth.service": auth}), cookies={"pyxle_session": "good-cookie"})
    result = await authenticate_websocket(ws)
    assert result is USER
    assert auth.calls == [("good-cookie", True)]


async def test_no_cookie_returns_none_zero_work() -> None:
    auth = FakeAuthService({"good-cookie": USER})
    ws = FakeWS(plugins=FakePlugins({"auth.service": auth}), cookies={})
    assert await authenticate_websocket(ws) is None
    assert auth.calls == []  # no DB round-trip without a cookie


async def test_no_auth_plugin_returns_none() -> None:
    ws = FakeWS(plugins=FakePlugins({}), cookies={"pyxle_session": "x"})
    assert await authenticate_websocket(ws) is None


async def test_no_plugins_on_app_returns_none() -> None:
    ws = FakeWS(plugins=None, cookies={"pyxle_session": "x"})
    assert await authenticate_websocket(ws) is None


async def test_plugins_without_get_returns_none() -> None:
    # A pyxle_plugins object that doesn't implement .get() is ignored safely.
    ws = FakeWS(plugins=object(), cookies={"pyxle_session": "x"})
    assert await authenticate_websocket(ws) is None


async def test_no_app_on_scope_returns_none() -> None:
    ws = FakeWS(has_app=False, cookies={"pyxle_session": "x"})
    assert await authenticate_websocket(ws) is None


async def test_unknown_cookie_resolves_to_none() -> None:
    auth = FakeAuthService({"valid": USER})
    ws = FakeWS(plugins=FakePlugins({"auth.service": auth}), cookies={"pyxle_session": "forged"})
    assert await authenticate_websocket(ws) is None


async def test_resolve_session_error_degrades_to_none() -> None:
    # Regression: a backend failure (DB down) inside resolve_session must NOT
    # crash the WS upgrade — it degrades to anonymous per the documented
    # safe-degradation contract.
    class RaisingAuth(FakeAuthService):
        async def resolve_session(self, *, cookie_value: str, extend: bool = True):
            raise RuntimeError("database is down")

    ws = FakeWS(
        plugins=FakePlugins({"auth.service": RaisingAuth({})}),
        cookies={"pyxle_session": "x"},
    )
    assert await authenticate_websocket(ws) is None


# --- origin_allowed --------------------------------------------------------


def _ws_with_origin(origin: str | None) -> FakeWS:
    headers = {"origin": origin} if origin is not None else {}
    return FakeWS(headers=headers)


def test_origin_empty_allowlist_allows_all() -> None:
    assert origin_allowed(_ws_with_origin("https://evil.com"), set()) is True


def test_origin_matching_is_allowed() -> None:
    assert origin_allowed(_ws_with_origin("https://app.example.com"), ["https://app.example.com"]) is True


def test_origin_trailing_slash_normalized() -> None:
    assert origin_allowed(_ws_with_origin("https://app.example.com/"), ["https://app.example.com"]) is True


def test_origin_mismatch_is_rejected() -> None:
    assert origin_allowed(_ws_with_origin("https://evil.com"), ["https://app.example.com"]) is False


def test_missing_origin_header_is_allowed() -> None:
    # Same-origin navigations / non-browser clients don't send Origin.
    assert origin_allowed(_ws_with_origin(None), ["https://app.example.com"]) is True
