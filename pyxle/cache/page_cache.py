"""The server-side page cache: key derivation, read-through, and ISR.

:class:`PageCache` sits in front of a :class:`~pyxle.cache.backends.CacheBackend`
and adds the policy a backend deliberately does not have:

* **Key derivation** -- a cached render is keyed by its canonical route path.
  Caching is opt-in per route (a route only reaches this layer when it declares
  a ``cache`` directive, the developer's promise that it renders no per-user
  data), so a plain path key is both correct and trivially invalidatable.
* **Freshness** -- :meth:`get` reports whether the stored entry has outlived its
  ``revalidate`` window.
* **ISR** -- :meth:`schedule_revalidation` refreshes a stale entry in the
  background, single-flight per key, so the request that observed staleness is
  served the stale (fast) bytes while exactly one refresh runs.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from dataclasses import dataclass
from typing import Awaitable, Callable, Dict, Optional

from .backends import CacheBackend, CacheEntry, InMemoryCacheBackend

__all__ = ["PageCache", "CacheLookup"]

_logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class CacheLookup:
    """The result of a cache read: the stored entry plus its freshness."""

    entry: CacheEntry
    is_stale: bool


class PageCache:
    """Read-through SSR HTML cache with incremental static regeneration.

    The cache is backed by any :class:`CacheBackend` (in-memory by default).
    ``clock`` is injectable so freshness and ISR can be tested deterministically
    without sleeping.
    """

    def __init__(
        self,
        backend: CacheBackend | None = None,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._backend: CacheBackend = backend or InMemoryCacheBackend()
        self._clock = clock
        self._inflight: Dict[str, asyncio.Task] = {}

    @property
    def backend(self) -> CacheBackend:
        return self._backend

    @staticmethod
    def make_key(route_path: str) -> str:
        """Derive the stable cache key for a route path.

        Hashing keeps keys fixed-length (filesystem/Redis friendly) and means a
        later switch to a richer key shape never breaks existing callers.
        """

        return hashlib.sha256(route_path.encode("utf-8")).hexdigest()

    @staticmethod
    def make_etag(body: bytes) -> str:
        """A strong ETag derived from the response body, for 304 negotiation."""

        return '"' + hashlib.sha256(body).hexdigest()[:32] + '"'

    async def get(self, key: str) -> Optional[CacheLookup]:
        entry = await self._backend.get(key)
        if entry is None:
            return None
        return CacheLookup(entry=entry, is_stale=entry.is_stale(self._clock()))

    async def store(
        self,
        key: str,
        body: bytes,
        *,
        status_code: int,
        revalidate: Optional[float],
    ) -> CacheEntry:
        entry = CacheEntry(
            body=body,
            status_code=status_code,
            etag=self.make_etag(body),
            stored_at=self._clock(),
            revalidate=revalidate,
        )
        await self._backend.set(key, entry)
        return entry

    async def put_entry(self, key: str, entry: CacheEntry) -> None:
        """Insert a pre-built entry verbatim (used to warm from pre-rendered files)."""

        await self._backend.set(key, entry)

    async def invalidate(self, key: str) -> bool:
        return await self._backend.delete(key)

    async def clear(self) -> None:
        await self._backend.clear()

    def schedule_revalidation(
        self, key: str, refresh: Callable[[], Awaitable[None]]
    ) -> bool:
        """Refresh a stale entry in the background, at most once per key.

        Returns ``True`` if a refresh was scheduled, ``False`` if one was
        already in flight for this key (the thundering-herd guard). A failed
        refresh is logged and swallowed -- the existing stale entry keeps being
        served until a later refresh succeeds or it is invalidated.
        """

        existing = self._inflight.get(key)
        if existing is not None and not existing.done():
            return False

        task = asyncio.ensure_future(self._run_refresh(key, refresh))
        self._inflight[key] = task
        return True

    async def _run_refresh(self, key: str, refresh: Callable[[], Awaitable[None]]) -> None:
        try:
            await refresh()
        except Exception:  # noqa: BLE001 - a background refresh must never crash the worker
            _logger.warning("Background cache revalidation failed for %s", key, exc_info=True)
        finally:
            self._inflight.pop(key, None)

    async def aclose(self) -> None:
        """Cancel any in-flight background revalidations (called on shutdown)."""

        tasks = [task for task in self._inflight.values() if not task.done()]
        for task in tasks:
            task.cancel()
        for task in tasks:
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001 - shutdown best-effort
                pass
        self._inflight.clear()
