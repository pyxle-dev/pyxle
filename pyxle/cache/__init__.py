"""Server-side page cache for Pyxle.

Public surface for application code -- in particular the invalidation hook the
roadmap promises::

    from pyxle import cache

    @action
    async def publish_post(request):
        ...
        await cache.invalidate("/posts/123")   # purge that page's cached render
        return {"ok": True}

The active :class:`PageCache` is owned by the running app (created in the
Starlette lifespan and registered with :func:`set_active_cache`). The
module-level helpers below resolve it so callers never thread a cache handle
through the request.

Cross-worker note: with the default in-memory backend each ``--workers N``
process holds its own cache, so :func:`invalidate` only reaches the calling
worker. Select a shared backend (file or Redis) when invalidation must fan out
across every worker and host.
"""

from __future__ import annotations

from typing import Optional

from .backends import (
    CacheBackend,
    CacheEntry,
    FileCacheBackend,
    InMemoryCacheBackend,
    RedisCacheBackend,
)
from .factory import PageCacheConfigError, build_page_cache, warm_page_cache
from .page_cache import CacheLookup, PageCache

__all__ = [
    "CacheBackend",
    "CacheEntry",
    "InMemoryCacheBackend",
    "FileCacheBackend",
    "RedisCacheBackend",
    "PageCache",
    "CacheLookup",
    "build_page_cache",
    "warm_page_cache",
    "PageCacheConfigError",
    "set_active_cache",
    "get_active_cache",
    "invalidate",
    "invalidate_all",
]

_active_page_cache: Optional[PageCache] = None


def set_active_cache(cache: Optional[PageCache]) -> None:
    """Register (or clear, with ``None``) the process's active page cache."""

    global _active_page_cache
    _active_page_cache = cache


def get_active_cache() -> Optional[PageCache]:
    """Return the process's active page cache, or ``None`` if caching is off."""

    return _active_page_cache


async def invalidate(path: str) -> bool:
    """Purge the cached render for ``path``.

    Returns ``True`` if an entry was removed, ``False`` if nothing was cached
    (or the page cache is disabled). Safe to call whether or not caching is on.
    """

    cache = _active_page_cache
    if cache is None:
        return False
    return await cache.invalidate(PageCache.make_key(path))


async def invalidate_all() -> None:
    """Purge every cached render. No-op when the page cache is disabled."""

    cache = _active_page_cache
    if cache is not None:
        await cache.clear()
