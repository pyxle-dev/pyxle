"""Tests for the PageCache orchestrator and the public invalidation API."""

from __future__ import annotations

import asyncio

import pytest

import pyxle.cache as cache_api
from pyxle.cache.page_cache import PageCache


@pytest.fixture
def anyio_backend() -> str:  # pragma: no cover - fixture wiring
    return "asyncio"


class _Clock:
    """A hand-cranked clock so freshness/ISR is tested without sleeping."""

    def __init__(self, t: float = 0.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


# --------------------------------------------------------------------------- #
# Key / ETag derivation
# --------------------------------------------------------------------------- #


def test_make_key_is_deterministic_and_path_specific() -> None:
    assert PageCache.make_key("/a") == PageCache.make_key("/a")
    assert PageCache.make_key("/a") != PageCache.make_key("/b")


def test_make_etag_is_quoted_stable_and_content_specific() -> None:
    etag = PageCache.make_etag(b"x")
    assert etag.startswith('"') and etag.endswith('"')
    assert etag == PageCache.make_etag(b"x")
    assert etag != PageCache.make_etag(b"y")


# --------------------------------------------------------------------------- #
# store / get / invalidate
# --------------------------------------------------------------------------- #


@pytest.mark.anyio
async def test_store_and_get_round_trip() -> None:
    pc = PageCache(clock=_Clock(100.0))
    key = PageCache.make_key("/home")
    await pc.store(key, b"<html>", status_code=201, revalidate=10.0)

    lookup = await pc.get(key)
    assert lookup is not None
    assert lookup.entry.body == b"<html>"
    assert lookup.entry.status_code == 201
    assert lookup.entry.revalidate == 10.0
    assert lookup.entry.etag == PageCache.make_etag(b"<html>")
    assert lookup.is_stale is False


@pytest.mark.anyio
async def test_get_miss_returns_none() -> None:
    pc = PageCache()
    assert await pc.get(PageCache.make_key("/nope")) is None


def test_page_cache_defaults_to_a_bounded_in_memory_backend() -> None:
    from pyxle.cache.backends import InMemoryCacheBackend

    pc = PageCache()
    assert isinstance(pc.backend, InMemoryCacheBackend)


@pytest.mark.anyio
async def test_lookup_reports_staleness_via_injected_clock() -> None:
    clock = _Clock(0.0)
    pc = PageCache(clock=clock)
    key = PageCache.make_key("/p")
    await pc.store(key, b"x", status_code=200, revalidate=10.0)

    clock.t = 5.0
    fresh = await pc.get(key)
    assert fresh is not None and fresh.is_stale is False

    clock.t = 10.0
    stale = await pc.get(key)
    assert stale is not None and stale.is_stale is True


@pytest.mark.anyio
async def test_invalidate_and_clear() -> None:
    pc = PageCache()
    key = PageCache.make_key("/p")
    await pc.store(key, b"x", status_code=200, revalidate=None)
    assert await pc.invalidate(key) is True
    assert await pc.invalidate(key) is False

    await pc.store(key, b"x", status_code=200, revalidate=None)
    await pc.clear()
    assert await pc.get(key) is None


# --------------------------------------------------------------------------- #
# ISR: single-flight background revalidation
# --------------------------------------------------------------------------- #


@pytest.mark.anyio
async def test_schedule_revalidation_runs_refresh_single_flight() -> None:
    pc = PageCache()
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def refresh() -> None:
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()

    assert pc.schedule_revalidation("k", refresh) is True
    await started.wait()
    # A second request that observes staleness must NOT spawn a duplicate.
    assert pc.schedule_revalidation("k", refresh) is False

    release.set()
    await pc._inflight["k"]
    assert calls == 1
    # Once the refresh completed, the key is free to revalidate again.
    release2 = asyncio.Event()
    release2.set()

    async def refresh2() -> None:
        await release2.wait()

    assert pc.schedule_revalidation("k", refresh2) is True
    await pc._inflight["k"]


@pytest.mark.anyio
async def test_schedule_revalidation_swallows_refresh_errors() -> None:
    pc = PageCache()

    async def boom() -> None:
        raise RuntimeError("refresh failed")

    assert pc.schedule_revalidation("k", boom) is True
    await pc._inflight["k"]  # must not raise -- the error is logged and swallowed
    assert "k" not in pc._inflight  # cleaned up so a later refresh can run


@pytest.mark.anyio
async def test_aclose_cancels_inflight_revalidations() -> None:
    pc = PageCache()
    started = asyncio.Event()
    release = asyncio.Event()

    async def refresh() -> None:
        started.set()
        await release.wait()  # never released -> must be cancelled by aclose

    pc.schedule_revalidation("k", refresh)
    await started.wait()
    await pc.aclose()
    assert pc._inflight == {}


# --------------------------------------------------------------------------- #
# Public module API: pyxle.cache.invalidate / invalidate_all
# --------------------------------------------------------------------------- #


@pytest.mark.anyio
async def test_public_invalidate_without_active_cache_returns_false() -> None:
    cache_api.set_active_cache(None)
    assert await cache_api.invalidate("/x") is False
    await cache_api.invalidate_all()  # no-op, must not raise


@pytest.mark.anyio
async def test_public_invalidate_delegates_to_active_cache() -> None:
    pc = PageCache()
    await pc.store(PageCache.make_key("/posts/123"), b"x", status_code=200, revalidate=None)
    cache_api.set_active_cache(pc)
    try:
        assert cache_api.get_active_cache() is pc
        assert await cache_api.invalidate("/posts/123") is True
        assert await pc.get(PageCache.make_key("/posts/123")) is None
        assert await cache_api.invalidate("/posts/123") is False  # already gone
    finally:
        cache_api.set_active_cache(None)


@pytest.mark.anyio
async def test_public_invalidate_all_clears_active_cache() -> None:
    pc = PageCache()
    await pc.store(PageCache.make_key("/a"), b"x", status_code=200, revalidate=None)
    await pc.store(PageCache.make_key("/b"), b"x", status_code=200, revalidate=None)
    cache_api.set_active_cache(pc)
    try:
        await cache_api.invalidate_all()
        assert await pc.get(PageCache.make_key("/a")) is None
        assert await pc.get(PageCache.make_key("/b")) is None
    finally:
        cache_api.set_active_cache(None)


@pytest.mark.anyio
async def test_warm_page_cache_loads_prerendered_entries(tmp_path) -> None:
    from pyxle.cache import warm_page_cache
    from pyxle.cache.backends import CacheEntry, FileCacheBackend

    prerender_dir = tmp_path / "prerendered"
    backend = FileCacheBackend(prerender_dir)
    entry = CacheEntry(
        body=b"<html>static</html>",
        status_code=200,
        etag='"e"',
        stored_at=1.0,
        revalidate=None,
    )
    await backend.set(PageCache.make_key("/about"), entry)

    cache = PageCache()
    warmed = await warm_page_cache(cache, ["/about", "/missing"], prerender_dir)

    assert warmed == 1
    lookup = await cache.get(PageCache.make_key("/about"))
    assert lookup is not None
    assert lookup.entry.body == b"<html>static</html>"
    assert lookup.is_stale is False  # revalidate=None -> never stale
