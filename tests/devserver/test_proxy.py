from __future__ import annotations

from collections.abc import Iterable
from typing import Any, List

import httpx
import pytest
from starlette.requests import Request

from pyxle.cli.logger import ConsoleLogger
from pyxle.devserver.proxy import ViteProxy
from pyxle.devserver.settings import DevServerSettings

pytestmark = pytest.mark.anyio("asyncio")


@pytest.fixture
def settings(tmp_path):
    root = tmp_path / "project"
    (root / "pages").mkdir(parents=True)
    (root / "public").mkdir()
    return DevServerSettings.from_project_root(root)


@pytest.fixture
def anyio_backend() -> str:  # pragma: no cover - fixture wiring
    return "asyncio"


def make_request(path: str = "/main.js", method: str = "GET", *, body: bytes = b"", query: bytes = b"", headers: list[tuple[bytes, bytes]] | None = None) -> Request:
    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "root_path": "",
        "query_string": query,
        "headers": headers or [],
    }

    async def receive() -> dict[str, Any]:
        nonlocal body
        data = body
        body = b""
        return {"type": "http.request", "body": data, "more_body": False}

    return Request(scope, receive)


class StubResponse:
    def __init__(self, status_code: int, headers: Iterable[tuple[str, str]], body: Iterable[bytes]) -> None:
        self.status_code = status_code
        self.headers = httpx.Headers(headers)
        self._body = list(body)

    async def aiter_raw(self):
        for chunk in self._body:
            yield chunk


class StubStreamContext:
    def __init__(self, response: StubResponse | None = None, *, exc: Exception | None = None) -> None:
        self._response = response
        self._exc = exc
        self.exited = False

    async def __aenter__(self) -> StubResponse:
        if self._exc is not None:
            raise self._exc
        assert self._response is not None
        return self._response

    async def __aexit__(self, exc_type, exc, tb) -> None:
        self.exited = True


class StubAsyncClient:
    def __init__(self, context: StubStreamContext) -> None:
        self._context = context
        self.requests: List[dict[str, Any]] = []

    def stream(self, method: str, path: str, *, params=None, headers=None, content=None):
        self.requests.append(
            {
                "method": method,
                "path": path,
                "params": list(params or []),
                "headers": headers or {},
                "content": content,
            }
        )
        return self._context


async def test_vite_proxy_streams_assets(settings: DevServerSettings) -> None:
    response = StubResponse(
        status_code=200,
        headers={("content-type", "text/javascript"), ("connection", "keep-alive")},
        body=[b"chunk1", b"chunk2"],
    )
    context = StubStreamContext(response)
    client = StubAsyncClient(context)
    logger = ConsoleLogger(secho=lambda msg, **_: None)

    proxy = ViteProxy(settings, logger=logger, client=client)
    request = make_request(
        "/main.js",
        query=b"v=1",
        headers=[(b"accept", b"text/javascript"), (b"host", b"example.com")],
    )

    starlette_response = await proxy.handle(request)

    body = bytearray()
    async for chunk in starlette_response.body_iterator:
        body.extend(chunk)

    assert body == b"chunk1chunk2"
    assert starlette_response.status_code == 200
    assert context.exited is True
    assert client.requests[0]["params"] == [("v", "1")]
    assert "content-type" in starlette_response.headers
    assert "connection" not in starlette_response.headers
    # Host header is now explicitly set to the upstream Vite server address
    # rather than forwarding the client's original Host header.
    assert client.requests[0]["headers"]["host"] == "127.0.0.1:5173"
    assert len(client.requests) == 1


async def test_vite_proxy_handles_request_error(settings: DevServerSettings) -> None:
    exc = httpx.ConnectError("connection refused")
    context = StubStreamContext(response=None, exc=exc)
    client = StubAsyncClient(context)
    capture: list[str] = []
    logger = ConsoleLogger(secho=lambda msg, **_: capture.append(msg))

    proxy = ViteProxy(settings, logger=logger, client=client)
    request = make_request("/@vite/client")

    response = await proxy.handle(request)

    assert response.status_code == 502
    assert "not reachable" in response.body.decode()
    assert any("Failed to proxy" in message for message in capture)


def test_vite_proxy_should_proxy_patterns(settings: DevServerSettings) -> None:
    proxy = ViteProxy(settings)
    assert proxy.should_proxy(make_request("/@vite/client")) is True
    assert proxy.should_proxy(make_request("/module.ts")) is True
    assert proxy.should_proxy(make_request("/styles.css")) is True
    assert proxy.should_proxy(make_request("/api/pulse")) is False
    assert proxy.should_proxy(make_request("/robots.txt")) is False


def test_vite_proxy_never_proxies_framework_internal_paths(
    settings: DevServerSettings,
) -> None:
    """Pyxle-internal namespaces serve their own assets from the wheel; their
    ``.js``/``.css`` URLs must never be forwarded to the user's Vite instance,
    even though the asset suffix alone would otherwise match."""
    proxy = ViteProxy(settings)
    assert proxy.should_proxy(make_request("/__pyxle/studio/assets/studio.js")) is False
    assert proxy.should_proxy(make_request("/__pyxle/studio/assets/studio.css")) is False
    assert proxy.should_proxy(make_request("/__pyxle__/overlay.js")) is False
    # Ordinary user source files still proxy.
    assert proxy.should_proxy(make_request("/src/x.js")) is True


async def test_vite_proxy_handles_non_proxied_request(settings: DevServerSettings) -> None:
    proxy = ViteProxy(settings)
    response = await proxy.handle(make_request("/robots.txt"))
    assert response.status_code == 404
    assert b"No Vite proxy route" in response.body


async def test_vite_proxy_logs_error_status(settings: DevServerSettings) -> None:
    response = StubResponse(
        status_code=500,
        headers={("content-type", "text/html")},
        body=[b"boom"],
    )
    context = StubStreamContext(response)
    client = StubAsyncClient(context)
    captured: list[str] = []
    logger = ConsoleLogger(secho=lambda msg, **_: captured.append(msg))

    proxy = ViteProxy(settings, logger=logger, client=client)
    result = await proxy.handle(make_request("/main.js"))

    assert result.status_code == 500
    assert any("Upstream responded 500" in msg for msg in captured)


async def test_vite_proxy_close_owns_client(settings: DevServerSettings) -> None:
    proxy = ViteProxy(settings)
    client = proxy._client

    assert client.is_closed is False

    await proxy.close()

    assert client.is_closed is True
