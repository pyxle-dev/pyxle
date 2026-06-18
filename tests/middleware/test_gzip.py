"""Tests for pyxle.middleware.gzip — the streaming-aware GZip middleware.

The key property (vs. Starlette's stock GZipMiddleware) is that a *streamed*
response flushes the compressor after every chunk, so the first chunk's bytes
reach the client immediately instead of being withheld until the stream closes.
"""

from __future__ import annotations

import asyncio
import gzip
import zlib

import pytest
from starlette.applications import Starlette
from starlette.responses import StreamingResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from pyxle.middleware.gzip import (
    StreamingGZipMiddleware,
    _StreamingGZipResponder,
    _unattached_send,
)


def _drive(app, accept_encoding: str = "gzip") -> list:
    """Run a raw ASGI HTTP request through the middleware, capturing sent messages."""
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "headers": [(b"accept-encoding", accept_encoding.encode())],
    }
    sent: list = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    asyncio.run(StreamingGZipMiddleware(app)(scope, receive, send))
    return sent


def _start(sent: list) -> dict:
    return next(m for m in sent if m["type"] == "http.response.start")


def _bodies(sent: list) -> list:
    return [m["body"] for m in sent if m["type"] == "http.response.body"]


def test_streaming_response_flushes_each_chunk() -> None:
    """Each streamed chunk's compressed bytes are emitted immediately."""

    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200,
                    "headers": [(b"content-type", b"text/html")]})
        await send({"type": "http.response.body", "body": b"SHELL" * 200, "more_body": True})
        await send({"type": "http.response.body", "body": b"PANEL" * 200, "more_body": False})

    sent = _drive(app)
    bodies = _bodies(sent)
    assert len(bodies) == 2
    # The bug this guards against: the first (shell) chunk being empty because the
    # compressor withheld it until close(). With the per-chunk flush it is not.
    assert len(bodies[0]) > 0
    dec = zlib.decompressobj(16 + zlib.MAX_WBITS)
    shell = dec.decompress(bodies[0])
    assert b"SHELL" in shell  # the shell decompresses BEFORE the stream ends
    rest = dec.decompress(bodies[1]) + dec.flush()
    assert shell + rest == b"SHELL" * 200 + b"PANEL" * 200

    headers = dict(_start(sent)["headers"])
    assert headers[b"content-encoding"] == b"gzip"
    assert b"content-length" not in headers  # unknown length while streaming


def test_buffered_response_is_compressed() -> None:
    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200,
                    "headers": [(b"content-type", b"text/html")]})
        await send({"type": "http.response.body", "body": b"X" * 1000, "more_body": False})

    sent = _drive(app)
    headers = dict(_start(sent)["headers"])
    assert headers[b"content-encoding"] == b"gzip"
    assert headers[b"content-length"] == str(len(_bodies(sent)[0])).encode()
    assert gzip.decompress(_bodies(sent)[0]) == b"X" * 1000


def test_small_response_not_compressed() -> None:
    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"hi", "more_body": False})

    sent = _drive(app)
    headers = dict(_start(sent)["headers"])
    assert b"content-encoding" not in headers
    assert _bodies(sent)[0] == b"hi"


def test_no_accept_encoding_passthrough() -> None:
    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"Z" * 1000, "more_body": False})

    sent = _drive(app, accept_encoding="identity")
    headers = dict(_start(sent)["headers"])
    assert b"content-encoding" not in headers
    assert _bodies(sent)[0] == b"Z" * 1000


def test_preencoded_response_passthrough() -> None:
    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200,
                    "headers": [(b"content-encoding", b"br")]})
        await send({"type": "http.response.body", "body": b"already", "more_body": False})

    sent = _drive(app)
    assert _bodies(sent)[0] == b"already"  # not re-compressed


def test_non_http_scope_passthrough() -> None:
    seen = {}

    async def app(scope, receive, send):
        seen["type"] = scope["type"]

    async def receive():
        return {}

    async def send(message):  # pragma: no cover - not used
        pass

    asyncio.run(StreamingGZipMiddleware(app)({"type": "websocket"}, receive, send))
    assert seen["type"] == "websocket"


def _run_responder(app, *, accept_encoding: str = "gzip") -> _StreamingGZipResponder:
    """Drive a responder directly and return it, so a test can inspect the
    compressor's post-run state (the middleware hides the responder)."""
    responder = _StreamingGZipResponder(app, minimum_size=500, compresslevel=9)
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "headers": [(b"accept-encoding", accept_encoding.encode())],
    }

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        pass

    asyncio.run(responder(scope, receive, send))
    return responder


def test_compressor_closed_on_small_response() -> None:
    """A too-small response takes the uncompressed branch and never writes the
    compressor — it must still be closed deterministically, not left open for the
    GC to finalize (which writes to a possibly-closed buffer and prints
    'Exception ignored while finalizing file ... I/O operation on closed file')."""

    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"hi", "more_body": False})

    responder = _run_responder(app)
    assert responder.gzip_file.closed
    assert responder.gzip_buffer.closed


def test_compressor_closed_on_preencoded_response() -> None:
    """An already-encoded response passes through without touching the compressor;
    it is still closed rather than leaked to the GC."""

    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200,
                    "headers": [(b"content-encoding", b"br")]})
        await send({"type": "http.response.body", "body": b"already", "more_body": False})

    responder = _run_responder(app)
    assert responder.gzip_file.closed
    assert responder.gzip_buffer.closed


def test_compressor_closed_when_stream_errors_midflight() -> None:
    """THE production case: a streamed response that raises after its first chunk
    must still close the compressor in the ``finally``, so the GC never finalizes
    an open GzipFile. The original error propagates unchanged."""

    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200,
                    "headers": [(b"content-type", b"text/html")]})
        await send({"type": "http.response.body", "body": b"SHELL" * 200, "more_body": True})
        raise RuntimeError("upstream blew up mid-stream")

    responder = _StreamingGZipResponder(app, minimum_size=500, compresslevel=9)
    scope = {"type": "http", "method": "GET", "path": "/",
             "headers": [(b"accept-encoding", b"gzip")]}

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        pass

    with pytest.raises(RuntimeError, match="mid-stream"):
        asyncio.run(responder(scope, receive, send))
    assert responder.gzip_file.closed  # closed despite the mid-stream error
    assert responder.gzip_buffer.closed


def test_unattached_send_fails_loud() -> None:
    """The responder's default ``send`` sentinel is overwritten by ``__call__``
    before use; if it is ever reached it must raise rather than silently drop the
    response. (Covers the line directly so it needs no coverage pragma.)"""
    with pytest.raises(RuntimeError, match="send awaitable not set"):
        asyncio.run(_unattached_send({"type": "http.response.start"}))


def test_end_to_end_streaming_via_testclient() -> None:
    """A real StreamingResponse through Starlette round-trips correctly."""

    async def stream():
        yield b"first" * 200
        yield b"second" * 200

    async def endpoint(request):
        return StreamingResponse(stream(), media_type="text/html")

    app = Starlette(routes=[Route("/", endpoint)])
    app.add_middleware(StreamingGZipMiddleware, minimum_size=10)
    client = TestClient(app)
    response = client.get("/", headers={"accept-encoding": "gzip"})
    assert response.headers["content-encoding"] == "gzip"
    # TestClient/httpx transparently decompresses.
    assert response.content == b"first" * 200 + b"second" * 200
