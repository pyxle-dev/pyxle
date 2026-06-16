"""Select the page-cache backend from the environment.

*Where* rendered HTML is stored -- process memory, local disk, or a shared
Redis -- is an operational concern, so it is chosen with environment variables,
the same way the mail provider is selected. The default (bounded in-memory)
needs no configuration at all.

| Variable | Meaning |
|---|---|
| ``PYXLE_PAGE_CACHE_BACKEND`` | ``memory`` (default), ``file``, ``redis``, or ``off`` |
| ``PYXLE_PAGE_CACHE_MAX_ENTRIES`` | in-memory: max entries before LRU eviction (default 512) |
| ``PYXLE_PAGE_CACHE_MAX_BYTES`` | in-memory: max total body bytes (default 64 MiB) |
| ``PYXLE_PAGE_CACHE_DIR`` | file: directory for cache files (required for ``file``) |
| ``PYXLE_PAGE_CACHE_REDIS_URL`` | redis: connection URL (falls back to ``REDIS_URL``) |
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable, Mapping, Optional

from .backends import FileCacheBackend, InMemoryCacheBackend, RedisCacheBackend
from .page_cache import PageCache

__all__ = ["build_page_cache", "warm_page_cache", "PageCacheConfigError"]

_DISABLED = {"off", "none", "disabled"}


class PageCacheConfigError(RuntimeError):
    """Raised when the page-cache backend environment configuration is invalid."""


def _positive_int_env(env: Mapping[str, str], name: str, default: int) -> int:
    raw = env.get(name)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise PageCacheConfigError(f"{name} must be an integer, got {raw!r}") from exc
    if value <= 0:
        raise PageCacheConfigError(f"{name} must be a positive integer, got {value}")
    return value


def build_page_cache(
    *, debug: bool, env: Optional[Mapping[str, str]] = None
) -> Optional[PageCache]:
    """Construct the page cache selected by ``PYXLE_PAGE_CACHE_BACKEND``.

    Returns ``None`` (caching disabled) in debug, or when the backend is
    explicitly ``off``. Otherwise selects the in-memory (default), file, or
    Redis backend. Raises :class:`PageCacheConfigError` for an unknown backend
    or a missing required setting.
    """
    if debug:
        return None

    resolved = os.environ if env is None else env
    name = (resolved.get("PYXLE_PAGE_CACHE_BACKEND") or "memory").strip().lower()

    if name in _DISABLED:
        return None

    if name == "memory":
        return PageCache(
            InMemoryCacheBackend(
                max_entries=_positive_int_env(resolved, "PYXLE_PAGE_CACHE_MAX_ENTRIES", 512),
                max_bytes=_positive_int_env(
                    resolved, "PYXLE_PAGE_CACHE_MAX_BYTES", 64 * 1024 * 1024
                ),
            )
        )

    if name == "file":
        directory = resolved.get("PYXLE_PAGE_CACHE_DIR")
        if not directory:
            raise PageCacheConfigError(
                "PYXLE_PAGE_CACHE_BACKEND=file requires PYXLE_PAGE_CACHE_DIR to be set"
            )
        return PageCache(FileCacheBackend(directory))

    if name == "redis":
        url = resolved.get("PYXLE_PAGE_CACHE_REDIS_URL") or resolved.get("REDIS_URL")
        return PageCache(RedisCacheBackend(url=url))

    raise PageCacheConfigError(
        f"Unknown PYXLE_PAGE_CACHE_BACKEND={name!r}; "
        "expected one of: memory, file, redis, off"
    )


async def warm_page_cache(
    cache: PageCache, page_paths: Iterable[str], prerender_dir: Path
) -> int:
    """Load build-time pre-rendered entries into ``cache``.

    For each route path, looks up its pre-rendered entry in ``prerender_dir``
    (written by ``pyxle build --static``) and inserts it into the active cache,
    so the first request for a static page is a hit. Returns how many entries
    were warmed. Missing or unreadable entries are skipped silently.
    """

    backend = FileCacheBackend(prerender_dir)
    warmed = 0
    for path in page_paths:
        key = PageCache.make_key(path)
        entry = await backend.get(key)
        if entry is not None:
            await cache.put_entry(key, entry)
            warmed += 1
    return warmed
