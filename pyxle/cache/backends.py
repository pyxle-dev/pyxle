"""Pluggable storage backends for the server-side page (SSR HTML) cache.

The page cache stores fully-rendered HTML documents so a cacheable route can be
served without re-running its loader or the Node SSR render. This module defines
the storage contract (:class:`CacheBackend`) and three implementations:

* :class:`InMemoryCacheBackend` -- the default; bounded by entry count *and*
  total bytes with LRU eviction (CLAUDE.md rule 17: caches must be bounded).
* :class:`FileCacheBackend` -- persists entries on disk, surviving restarts and
  shared across worker processes on the same host.
* :class:`RedisCacheBackend` -- a network-shared store for multi-host
  deployments; requires the optional ``pyxle[redis]`` extra.

Backends store opaque :class:`CacheEntry` values. Freshness / ISR policy lives
in :mod:`pyxle.cache.page_cache`, which stamps and interprets the entry
metadata -- a backend never decides whether an entry is "stale", only where the
bytes live.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Protocol, runtime_checkable

from starlette.concurrency import run_in_threadpool

__all__ = [
    "CacheEntry",
    "CacheBackend",
    "InMemoryCacheBackend",
    "FileCacheBackend",
    "RedisCacheBackend",
]


@dataclass(frozen=True, slots=True)
class CacheEntry:
    """A stored, fully-rendered page response plus the metadata ISR needs.

    ``body`` is the encoded HTML document exactly as it would be written to the
    wire. ``stored_at`` is the wall-clock epoch second the entry was rendered;
    ``revalidate`` is its freshness window in seconds (``None`` means the entry
    stays fresh until it is explicitly invalidated). ``etag`` is a strong
    validator derived from the body so conditional requests can 304.
    """

    body: bytes
    status_code: int
    etag: str
    stored_at: float
    revalidate: Optional[float]

    def age(self, now: float) -> float:
        """Seconds elapsed since the entry was rendered (never negative)."""

        return max(0.0, now - self.stored_at)

    def is_stale(self, now: float) -> bool:
        """Whether the entry has outlived its ``revalidate`` window.

        Entries with no ``revalidate`` are never stale on their own -- they are
        served until explicitly invalidated.
        """

        if self.revalidate is None:
            return False
        return self.age(now) >= self.revalidate


def _serialize(entry: CacheEntry) -> bytes:
    """Frame an entry as ``<json-header>\\n<raw-body-bytes>`` for off-heap stores.

    The header is a single JSON line so the body bytes are stored verbatim --
    no base64 bloat -- and the metadata round-trips losslessly.
    """

    header = json.dumps(
        {
            "status_code": entry.status_code,
            "etag": entry.etag,
            "stored_at": entry.stored_at,
            "revalidate": entry.revalidate,
        },
        separators=(",", ":"),
    ).encode("utf-8")
    return header + b"\n" + entry.body


def _deserialize(blob: bytes) -> CacheEntry:
    header_line, _, body = blob.partition(b"\n")
    meta = json.loads(header_line.decode("utf-8"))
    return CacheEntry(
        body=body,
        status_code=int(meta["status_code"]),
        etag=str(meta["etag"]),
        stored_at=float(meta["stored_at"]),
        revalidate=(None if meta["revalidate"] is None else float(meta["revalidate"])),
    )


@runtime_checkable
class CacheBackend(Protocol):
    """Storage contract for the page cache.

    Implementations are async so file and network stores never block the SSR
    event loop (CLAUDE.md rule 15). ``get`` returns ``None`` on a miss; ``set``
    overwrites; ``delete`` reports whether a key was present; ``clear`` empties
    the store. Keys are opaque strings produced by the page cache.
    """

    async def get(self, key: str) -> Optional[CacheEntry]: ...

    async def set(self, key: str, entry: CacheEntry) -> None: ...

    async def delete(self, key: str) -> bool: ...

    async def clear(self) -> None: ...


class InMemoryCacheBackend:
    """Bounded, in-process LRU store -- the default backend.

    Bounded twice over: by entry count (``max_entries``) and by total body bytes
    (``max_bytes``). On overflow the least-recently-used entries are evicted
    until both bounds are satisfied, so a busy app can never grow this cache
    without limit. A single entry larger than ``max_bytes`` is simply not stored
    (the same "skip oversized" stance as the static-asset cache).

    The store is per process: under ``--workers N`` each worker keeps its own
    copy, so an in-memory invalidation only reaches the worker that issued it.
    Use :class:`FileCacheBackend` or :class:`RedisCacheBackend` when invalidation
    must fan out across workers.
    """

    def __init__(self, *, max_entries: int = 512, max_bytes: int = 64 * 1024 * 1024) -> None:
        if max_entries <= 0:
            raise ValueError("max_entries must be positive")
        if max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        self._store: "OrderedDict[str, CacheEntry]" = OrderedDict()
        self._total_bytes = 0
        self._max_entries = max_entries
        self._max_bytes = max_bytes
        self._lock = asyncio.Lock()

    async def get(self, key: str) -> Optional[CacheEntry]:
        async with self._lock:
            entry = self._store.get(key)
            if entry is not None:
                self._store.move_to_end(key)  # mark most-recently-used
            return entry

    async def set(self, key: str, entry: CacheEntry) -> None:
        async with self._lock:
            existing = self._store.pop(key, None)
            if existing is not None:
                self._total_bytes -= len(existing.body)
            # An entry that alone exceeds the byte budget is never cached --
            # storing it would immediately evict everything else, defeating the
            # cache. Drop it and leave any prior value already removed above.
            if len(entry.body) > self._max_bytes:
                return
            self._store[key] = entry
            self._total_bytes += len(entry.body)
            self._evict_to_bounds()

    def _evict_to_bounds(self) -> None:
        while self._store and (
            len(self._store) > self._max_entries or self._total_bytes > self._max_bytes
        ):
            _key, evicted = self._store.popitem(last=False)  # least-recently-used
            self._total_bytes -= len(evicted.body)

    async def delete(self, key: str) -> bool:
        async with self._lock:
            existing = self._store.pop(key, None)
            if existing is None:
                return False
            self._total_bytes -= len(existing.body)
            return True

    async def clear(self) -> None:
        async with self._lock:
            self._store.clear()
            self._total_bytes = 0


class FileCacheBackend:
    """On-disk page cache: one file per entry, shared across local workers.

    Keys are hashed to fixed-length filenames, so an attacker-influenced route
    can never escape ``base_dir`` via path traversal -- the key is never used as
    a path segment. File I/O runs in a worker thread to keep the SSR event loop
    free.
    """

    _SUFFIX = ".cache"

    def __init__(self, base_dir: Path | str) -> None:
        self._base_dir = Path(base_dir)

    def _path_for(self, key: str) -> Path:
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        return self._base_dir / f"{digest}{self._SUFFIX}"

    async def get(self, key: str) -> Optional[CacheEntry]:
        return await run_in_threadpool(self._get_sync, key)

    def _get_sync(self, key: str) -> Optional[CacheEntry]:
        path = self._path_for(key)
        try:
            blob = path.read_bytes()
        except FileNotFoundError:
            return None
        try:
            return _deserialize(blob)
        except (ValueError, KeyError, json.JSONDecodeError):
            # A corrupt/partial file behaves as a miss; drop it so the next
            # request re-renders and rewrites a clean entry.
            path.unlink(missing_ok=True)
            return None

    async def set(self, key: str, entry: CacheEntry) -> None:
        await run_in_threadpool(self._set_sync, key, entry)

    def _set_sync(self, key: str, entry: CacheEntry) -> None:
        path = self._path_for(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Write to a temp file then atomically rename so a concurrent reader
        # never observes a half-written entry.
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_bytes(_serialize(entry))
        tmp.replace(path)

    async def delete(self, key: str) -> bool:
        return await run_in_threadpool(self._delete_sync, key)

    def _delete_sync(self, key: str) -> bool:
        path = self._path_for(key)
        try:
            path.unlink()
            return True
        except FileNotFoundError:
            return False

    async def clear(self) -> None:
        await run_in_threadpool(self._clear_sync)

    def _clear_sync(self) -> None:
        if not self._base_dir.exists():
            return
        for path in self._base_dir.glob(f"*{self._SUFFIX}"):
            path.unlink(missing_ok=True)


class RedisCacheBackend:
    """Network-shared page cache backed by Redis (``pyxle[redis]`` extra).

    The right choice for multi-host deployments and for cross-worker
    invalidation: every worker and host reads and writes the same store, so
    ``pyxle.cache.invalidate(...)`` from any process is seen everywhere.

    The ``redis`` dependency is imported lazily so projects that never select
    this backend pay nothing for it -- a clear ``ImportError`` with install
    guidance is raised only if it is actually constructed without the extra.
    """

    def __init__(self, client: object | None = None, *, url: str | None = None, namespace: str = "pyxle:page:") -> None:
        if client is None:
            try:
                from redis.asyncio import Redis  # noqa: PLC0415
            except ImportError as exc:  # pragma: no cover - exercised via the lazy-import test
                raise ImportError(
                    "RedisCacheBackend requires the 'redis' package. "
                    "Install it with: pip install 'pyxle[redis]'"
                ) from exc
            client = Redis.from_url(url or "redis://localhost:6379")
        self._client = client
        self._namespace = namespace

    def _key(self, key: str) -> str:
        return f"{self._namespace}{key}"

    async def get(self, key: str) -> Optional[CacheEntry]:
        blob = await self._client.get(self._key(key))
        if blob is None:
            return None
        if isinstance(blob, str):
            blob = blob.encode("utf-8")
        try:
            return _deserialize(blob)
        except (ValueError, KeyError, json.JSONDecodeError):
            await self._client.delete(self._key(key))
            return None

    async def set(self, key: str, entry: CacheEntry) -> None:
        await self._client.set(self._key(key), _serialize(entry))

    async def delete(self, key: str) -> bool:
        removed = await self._client.delete(self._key(key))
        return bool(removed)

    async def clear(self) -> None:
        pattern = f"{self._namespace}*"
        keys = [key async for key in self._client.scan_iter(match=pattern)]
        if keys:
            await self._client.delete(*keys)
