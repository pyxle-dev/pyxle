"""Tests for the server-side page-cache orchestration in the page handler.

``build_page_response`` (the actual SSR render) is monkeypatched to a
controllable fake so these tests exercise *only* the cache decision logic:
cache-first HIT, MISS-and-store, stale serve + background ISR, conditional
304s, and the cacheability gate. The render itself is covered by
``tests/ssr/test_view.py``.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from starlette.requests import Request
from starlette.responses import Response, StreamingResponse

from pyxle.cache.page_cache import PageCache
from pyxle.devserver import starlette_app as app_mod
from pyxle.devserver.starlette_app import (
    _CACHE_STATUS_HEADER,
    _build_cached_page_response,
    _effective_cache_ttl,
    _public_cache_control,
    _read_response_body,
    _read_revalidate_header,
    _synthetic_get_request,
)
from pyxle.ssr.view import REVALIDATE_HEADER


@pytest.fixture
def anyio_backend() -> str:  # pragma: no cover - fixture wiring
    return "asyncio"


class _Clock:
    def __init__(self, t: float = 0.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


class _EdgeCache:
    """Stand-in for CacheConfig: maps an exact path to an s-maxage."""

    def __init__(self, ages: dict[str, int]) -> None:
        self._ages = ages

    def max_age_for(self, path: str) -> int | None:
        return self._ages.get(path)


def _settings(*, cache: object | None = None) -> SimpleNamespace:
    return SimpleNamespace(cache=cache, debug=False)


def _request(path: str = "/", *, method: str = "GET", headers: dict[str, str] | None = None) -> Request:
    raw = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": method,
            "path": path,
            "root_path": "",
            "query_string": b"",
            "headers": raw,
        }
    )


def _patch_render(monkeypatch, *, body: bytes = b"<html>page</html>", status: int = 200, revalidate=None):
    """Replace build_page_response with a fake; return a list of call paths."""

    calls: list[str] = []

    async def _fake(*, request, settings, page, renderer, overlay, error_boundaries):
        calls.append(request.url.path)

        async def _gen():
            yield body

        response = StreamingResponse(_gen(), status_code=status, media_type="text/html")
        if revalidate is not None:
            response.headers[REVALIDATE_HEADER] = str(revalidate)
        return response

    monkeypatch.setattr(app_mod, "build_page_response", _fake)
    return calls


def _route(*, cache_revalidate: float | None = None) -> SimpleNamespace:
    return SimpleNamespace(path="/", cache_revalidate=cache_revalidate)


async def _call(page_cache, *, settings=None, request=None, route=None):
    return await _build_cached_page_response(
        request=request or _request(),
        route=route or _route(),
        settings=settings or _settings(),
        renderer=object(),
        overlay=None,
        error_boundaries=None,
        page_cache=page_cache,
    )


# --------------------------------------------------------------------------- #
# Pure helpers
# --------------------------------------------------------------------------- #


def test_public_cache_control_format() -> None:
    assert _public_cache_control(60) == "public, s-maxage=60, stale-while-revalidate=300"


def test_read_revalidate_header_strips_and_parses() -> None:
    resp = Response()
    resp.headers[REVALIDATE_HEADER] = "60"
    assert _read_revalidate_header(resp) == 60.0
    assert REVALIDATE_HEADER not in resp.headers  # stripped
    assert _read_revalidate_header(Response()) is None


def test_effective_ttl_loader_wins_over_edge() -> None:
    resp = Response()
    resp.headers[REVALIDATE_HEADER] = "30"
    ttl = _effective_cache_ttl(resp, _request("/"), _EdgeCache({"/": 600}))
    assert ttl == 30.0


def test_effective_ttl_falls_back_to_edge_then_none() -> None:
    assert _effective_cache_ttl(Response(), _request("/"), _EdgeCache({"/": 600})) == 600.0
    assert _effective_cache_ttl(Response(), _request("/"), _EdgeCache({})) is None
    assert _effective_cache_ttl(Response(), _request("/"), None) is None


@pytest.mark.anyio
async def test_read_response_body_handles_stream_and_plain() -> None:
    async def _gen():
        yield b"ab"
        yield b"cd"

    assert await _read_response_body(StreamingResponse(_gen())) == b"abcd"
    assert await _read_response_body(Response(content=b"xy")) == b"xy"


def test_synthetic_request_is_get_with_empty_body() -> None:
    original = _request("/posts", method="POST", headers={"x-test": "1"})
    clone = _synthetic_get_request(original)
    assert clone.method == "GET"
    assert clone.url.path == "/posts"
    assert clone.headers["x-test"] == "1"  # scope carried over
    assert clone is not original


# --------------------------------------------------------------------------- #
# Orchestration: MISS / HIT / store / gate
# --------------------------------------------------------------------------- #


@pytest.mark.anyio
async def test_miss_stores_then_hit_skips_render(monkeypatch) -> None:
    calls = _patch_render(monkeypatch, body=b"<html>cached</html>", revalidate=60)
    cache = PageCache()

    first = await _call(cache)
    assert first.headers[_CACHE_STATUS_HEADER] == "MISS"
    assert first.headers["Cache-Control"] == _public_cache_control(60)
    assert "ETag" in first.headers
    assert len(calls) == 1

    second = await _call(cache)
    assert second.headers[_CACHE_STATUS_HEADER] == "HIT"
    assert second.body == b"<html>cached</html>"
    assert len(calls) == 1  # render NOT invoked again — served from cache


@pytest.mark.anyio
async def test_non_cacheable_render_is_not_stored(monkeypatch) -> None:
    calls = _patch_render(monkeypatch, revalidate=None)  # no envelope, no edge config
    cache = PageCache()

    first = await _call(cache)
    assert _CACHE_STATUS_HEADER not in first.headers
    assert first.headers["Cache-Control"] == "private, no-cache"

    await _call(cache)
    assert len(calls) == 2  # re-rendered every time; nothing cached


@pytest.mark.anyio
async def test_edge_config_makes_route_cacheable(monkeypatch) -> None:
    calls = _patch_render(monkeypatch, revalidate=None)  # no loader envelope
    cache = PageCache()
    settings = _settings(cache=_EdgeCache({"/": 120}))

    first = await _call(cache, settings=settings)
    assert first.headers[_CACHE_STATUS_HEADER] == "MISS"
    assert first.headers["Cache-Control"] == _public_cache_control(120)

    second = await _call(cache, settings=settings)
    assert second.headers[_CACHE_STATUS_HEADER] == "HIT"
    assert len(calls) == 1


@pytest.mark.anyio
async def test_compile_time_directive_makes_route_cacheable(monkeypatch) -> None:
    # No loader envelope and no edge config — only a CACHE = {"revalidate": N}
    # directive (e.g. a loader-less static page).
    calls = _patch_render(monkeypatch, revalidate=None)
    cache = PageCache()
    route = _route(cache_revalidate=45.0)

    first = await _call(cache, route=route)
    assert first.headers[_CACHE_STATUS_HEADER] == "MISS"
    assert first.headers["Cache-Control"] == _public_cache_control(45)

    second = await _call(cache, route=route)
    assert second.headers[_CACHE_STATUS_HEADER] == "HIT"
    assert len(calls) == 1


@pytest.mark.anyio
async def test_loader_revalidate_wins_over_directive(monkeypatch) -> None:
    _patch_render(monkeypatch, revalidate=30)  # loader envelope = 30s
    cache = PageCache()
    first = await _call(cache, route=_route(cache_revalidate=999.0))
    assert first.headers["Cache-Control"] == _public_cache_control(30)


@pytest.mark.anyio
async def test_disabled_when_no_page_cache(monkeypatch) -> None:
    calls = _patch_render(monkeypatch, revalidate=60)
    first = await _call(None)
    await _call(None)
    assert _CACHE_STATUS_HEADER not in first.headers
    assert len(calls) == 2  # always renders


@pytest.mark.anyio
async def test_post_request_is_never_served_or_stored(monkeypatch) -> None:
    calls = _patch_render(monkeypatch, revalidate=60)
    cache = PageCache()
    req = _request("/", method="POST")
    first = await _call(cache, request=req)
    assert _CACHE_STATUS_HEADER not in first.headers
    await _call(cache, request=req)
    assert len(calls) == 2  # POST never cached


@pytest.mark.anyio
async def test_non_200_render_is_not_stored(monkeypatch) -> None:
    calls = _patch_render(monkeypatch, status=503, revalidate=60)
    cache = PageCache()
    await _call(cache)
    await _call(cache)
    assert len(calls) == 2  # error responses are never cached


# --------------------------------------------------------------------------- #
# Conditional requests + ISR
# --------------------------------------------------------------------------- #


@pytest.mark.anyio
async def test_if_none_match_returns_304(monkeypatch) -> None:
    _patch_render(monkeypatch, body=b"<html>x</html>", revalidate=60)
    cache = PageCache()
    miss = await _call(cache)
    etag = miss.headers["ETag"]

    conditional = await _call(cache, request=_request("/", headers={"if-none-match": etag}))
    assert conditional.status_code == 304
    assert conditional.headers[_CACHE_STATUS_HEADER] == "HIT"


@pytest.mark.anyio
async def test_stale_entry_serves_stale_then_revalidates(monkeypatch) -> None:
    clock = _Clock(0.0)
    cache = PageCache(clock=clock)
    key = PageCache.make_key("/")
    await cache.store(key, b"<html>old</html>", status_code=200, revalidate=10)

    # Advance past the freshness window so the next read is stale.
    clock.t = 50.0
    calls = _patch_render(monkeypatch, body=b"<html>new</html>", revalidate=10)

    stale = await _call(cache)
    assert stale.headers[_CACHE_STATUS_HEADER] == "STALE"
    assert stale.body == b"<html>old</html>"  # stale bytes served immediately

    # A single background revalidation was scheduled — await it and confirm the
    # cache was refreshed with the new render.
    await cache._inflight[key]
    assert calls == ["/"]
    refreshed = await cache.get(key)
    assert refreshed is not None and refreshed.entry.body == b"<html>new</html>"
