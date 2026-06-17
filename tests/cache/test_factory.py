"""Tests for page-cache backend selection (``pyxle.cache.build_page_cache``)."""

from __future__ import annotations

from pathlib import Path

import pytest

from pyxle.cache import PageCache, PageCacheConfigError, build_page_cache
from pyxle.cache.backends import (
    FileCacheBackend,
    InMemoryCacheBackend,
    RedisCacheBackend,
)


def test_debug_disables_the_cache() -> None:
    assert build_page_cache(debug=True, env={}) is None


def test_default_is_bounded_in_memory() -> None:
    cache = build_page_cache(debug=False, env={})
    assert isinstance(cache, PageCache)
    assert isinstance(cache.backend, InMemoryCacheBackend)


@pytest.mark.parametrize("value", ["off", "none", "disabled", "OFF", " Off "])
def test_off_disables_the_cache(value: str) -> None:
    assert build_page_cache(debug=False, env={"PYXLE_PAGE_CACHE_BACKEND": value}) is None


def test_memory_honours_bound_overrides() -> None:
    cache = build_page_cache(
        debug=False,
        env={
            "PYXLE_PAGE_CACHE_BACKEND": "memory",
            "PYXLE_PAGE_CACHE_MAX_ENTRIES": "10",
            "PYXLE_PAGE_CACHE_MAX_BYTES": "2048",
        },
    )
    backend = cache.backend
    assert isinstance(backend, InMemoryCacheBackend)
    assert backend._max_entries == 10
    assert backend._max_bytes == 2048


@pytest.mark.parametrize("var", ["PYXLE_PAGE_CACHE_MAX_ENTRIES", "PYXLE_PAGE_CACHE_MAX_BYTES"])
@pytest.mark.parametrize("bad", ["abc", "-5", "0"])
def test_memory_rejects_invalid_bounds(var: str, bad: str) -> None:
    with pytest.raises(PageCacheConfigError):
        build_page_cache(debug=False, env={"PYXLE_PAGE_CACHE_BACKEND": "memory", var: bad})


def test_file_backend_requires_a_directory(tmp_path: Path) -> None:
    with pytest.raises(PageCacheConfigError, match="PYXLE_PAGE_CACHE_DIR"):
        build_page_cache(debug=False, env={"PYXLE_PAGE_CACHE_BACKEND": "file"})

    cache = build_page_cache(
        debug=False,
        env={"PYXLE_PAGE_CACHE_BACKEND": "file", "PYXLE_PAGE_CACHE_DIR": str(tmp_path / "c")},
    )
    assert isinstance(cache.backend, FileCacheBackend)


def test_unknown_backend_raises() -> None:
    with pytest.raises(PageCacheConfigError, match="Unknown"):
        build_page_cache(debug=False, env={"PYXLE_PAGE_CACHE_BACKEND": "memcached"})


def test_redis_backend_selected_or_guides_to_extra() -> None:
    env = {"PYXLE_PAGE_CACHE_BACKEND": "redis", "PYXLE_PAGE_CACHE_REDIS_URL": "redis://localhost:6379"}
    try:
        import redis.asyncio  # noqa: F401, PLC0415
    except ImportError:
        with pytest.raises(ImportError, match=r"pyxle-framework\[redis\]"):
            build_page_cache(debug=False, env=env)
    else:  # pragma: no cover - depends on the test environment having redis
        cache = build_page_cache(debug=False, env=env)
        assert isinstance(cache.backend, RedisCacheBackend)
