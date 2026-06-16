"""Tests for the page-cache storage backends."""

from __future__ import annotations

import fnmatch
from pathlib import Path

import pytest

from pyxle.cache.backends import (
    CacheEntry,
    FileCacheBackend,
    InMemoryCacheBackend,
    RedisCacheBackend,
    _deserialize,
    _serialize,
)


@pytest.fixture
def anyio_backend() -> str:  # pragma: no cover - fixture wiring
    return "asyncio"


def _entry(body: bytes = b"<html>", *, revalidate: float | None = None) -> CacheEntry:
    return CacheEntry(
        body=body,
        status_code=200,
        etag='"abc"',
        stored_at=1000.0,
        revalidate=revalidate,
    )


# --------------------------------------------------------------------------- #
# CacheEntry + (de)serialization
# --------------------------------------------------------------------------- #


def test_entry_age_never_negative() -> None:
    entry = _entry()
    assert entry.age(now=500.0) == 0.0  # clock earlier than stored_at
    assert entry.age(now=1005.0) == 5.0


def test_entry_staleness_respects_revalidate_window() -> None:
    entry = _entry(revalidate=10.0)
    assert entry.is_stale(now=1005.0) is False
    assert entry.is_stale(now=1010.0) is True
    assert entry.is_stale(now=1011.0) is True


def test_entry_without_revalidate_is_never_stale() -> None:
    entry = _entry(revalidate=None)
    assert entry.is_stale(now=10**9) is False


def test_serialize_round_trip_preserves_body_and_metadata() -> None:
    entry = _entry(body=b"<html>\n<body>x</body></html>", revalidate=30.0)
    restored = _deserialize(_serialize(entry))
    assert restored == entry


def test_serialize_handles_newlines_in_body() -> None:
    # The framing splits on the first newline only; body newlines must survive.
    entry = _entry(body=b"line1\nline2\nline3")
    assert _deserialize(_serialize(entry)).body == b"line1\nline2\nline3"


def test_serialize_round_trips_null_revalidate() -> None:
    entry = _entry(revalidate=None)
    assert _deserialize(_serialize(entry)).revalidate is None


# --------------------------------------------------------------------------- #
# InMemoryCacheBackend
# --------------------------------------------------------------------------- #


@pytest.mark.anyio
async def test_in_memory_set_get_round_trip() -> None:
    backend = InMemoryCacheBackend()
    assert await backend.get("missing") is None
    entry = _entry()
    await backend.set("k", entry)
    assert await backend.get("k") == entry


@pytest.mark.anyio
async def test_in_memory_evicts_least_recently_used_by_count() -> None:
    backend = InMemoryCacheBackend(max_entries=2)
    await backend.set("a", _entry(b"a"))
    await backend.set("b", _entry(b"b"))
    # Touch "a" so "b" becomes the LRU victim.
    await backend.get("a")
    await backend.set("c", _entry(b"c"))
    assert await backend.get("a") is not None
    assert await backend.get("b") is None  # evicted
    assert await backend.get("c") is not None


@pytest.mark.anyio
async def test_in_memory_evicts_by_total_bytes() -> None:
    backend = InMemoryCacheBackend(max_entries=100, max_bytes=10)
    await backend.set("a", _entry(b"x" * 6))
    await backend.set("b", _entry(b"y" * 6))  # 12 bytes total > 10 -> evict "a"
    assert await backend.get("a") is None
    assert await backend.get("b") is not None


@pytest.mark.anyio
async def test_in_memory_skips_entry_larger_than_byte_budget() -> None:
    backend = InMemoryCacheBackend(max_entries=10, max_bytes=8)
    await backend.set("big", _entry(b"x" * 20))
    assert await backend.get("big") is None


@pytest.mark.anyio
async def test_in_memory_overwrite_updates_byte_accounting() -> None:
    backend = InMemoryCacheBackend(max_entries=1, max_bytes=100)
    await backend.set("k", _entry(b"x" * 50))
    await backend.set("k", _entry(b"y" * 3))  # same key, smaller body
    entry = await backend.get("k")
    assert entry is not None and entry.body == b"yyy"


@pytest.mark.anyio
async def test_in_memory_delete_reports_presence() -> None:
    backend = InMemoryCacheBackend()
    await backend.set("k", _entry())
    assert await backend.delete("k") is True
    assert await backend.delete("k") is False


@pytest.mark.anyio
async def test_in_memory_clear_empties_store() -> None:
    backend = InMemoryCacheBackend()
    await backend.set("a", _entry())
    await backend.set("b", _entry())
    await backend.clear()
    assert await backend.get("a") is None
    assert await backend.get("b") is None


@pytest.mark.parametrize("kwargs", [{"max_entries": 0}, {"max_bytes": 0}])
def test_in_memory_rejects_non_positive_bounds(kwargs: dict) -> None:
    with pytest.raises(ValueError):
        InMemoryCacheBackend(**kwargs)


# --------------------------------------------------------------------------- #
# FileCacheBackend
# --------------------------------------------------------------------------- #


@pytest.mark.anyio
async def test_file_backend_round_trip(tmp_path: Path) -> None:
    backend = FileCacheBackend(tmp_path / "cache")
    assert await backend.get("k") is None
    entry = _entry(b"<html>persisted</html>", revalidate=15.0)
    await backend.set("k", entry)
    assert await backend.get("k") == entry


@pytest.mark.anyio
async def test_file_backend_key_is_hashed_no_traversal(tmp_path: Path) -> None:
    base = tmp_path / "cache"
    backend = FileCacheBackend(base)
    await backend.set("../../etc/passwd", _entry())
    # Every stored file lives directly under base_dir with a hashed name.
    files = list(base.iterdir())
    assert len(files) == 1
    assert files[0].parent == base
    assert ".." not in files[0].name


@pytest.mark.anyio
async def test_file_backend_corrupt_entry_is_a_miss_and_is_removed(tmp_path: Path) -> None:
    base = tmp_path / "cache"
    backend = FileCacheBackend(base)
    await backend.set("k", _entry())
    # Corrupt the stored file.
    stored = next(base.glob("*.cache"))
    stored.write_bytes(b"not json\nbody")
    assert await backend.get("k") is None
    assert not stored.exists()  # dropped so the next request rewrites cleanly


@pytest.mark.anyio
async def test_file_backend_delete_and_clear(tmp_path: Path) -> None:
    base = tmp_path / "cache"
    backend = FileCacheBackend(base)
    await backend.set("a", _entry())
    await backend.set("b", _entry())
    assert await backend.delete("a") is True
    assert await backend.delete("a") is False
    # An unrelated file in the dir must survive clear().
    (base / "keep.txt").write_text("keep", encoding="utf-8")
    await backend.clear()
    assert await backend.get("b") is None
    assert (base / "keep.txt").exists()


@pytest.mark.anyio
async def test_file_backend_clear_is_safe_when_dir_absent(tmp_path: Path) -> None:
    backend = FileCacheBackend(tmp_path / "never-created")
    await backend.clear()  # must not raise


# --------------------------------------------------------------------------- #
# RedisCacheBackend (against an in-memory fake client)
# --------------------------------------------------------------------------- #


class _FakeRedis:
    """Minimal async stand-in covering the subset RedisCacheBackend uses."""

    def __init__(self) -> None:
        self.store: dict[str, bytes] = {}

    async def get(self, key: str) -> bytes | None:
        return self.store.get(key)

    async def set(self, key: str, value: bytes) -> None:
        self.store[key] = value

    async def delete(self, *keys: str) -> int:
        removed = 0
        for key in keys:
            if key in self.store:
                del self.store[key]
                removed += 1
        return removed

    async def scan_iter(self, match: str | None = None):
        for key in list(self.store):
            if match is None or fnmatch.fnmatch(key, match):
                yield key


@pytest.mark.anyio
async def test_redis_backend_round_trip_and_namespacing() -> None:
    fake = _FakeRedis()
    backend = RedisCacheBackend(client=fake, namespace="ns:")
    entry = _entry(b"<html>redis</html>", revalidate=20.0)
    await backend.set("route", entry)
    assert "ns:route" in fake.store  # namespaced on the wire
    assert await backend.get("route") == entry
    assert await backend.get("absent") is None


@pytest.mark.anyio
async def test_redis_backend_delete_and_clear_only_namespace() -> None:
    fake = _FakeRedis()
    fake.store["other:leaveme"] = b"x"  # a key outside our namespace
    backend = RedisCacheBackend(client=fake, namespace="ns:")
    await backend.set("a", _entry())
    await backend.set("b", _entry())
    assert await backend.delete("a") is True
    assert await backend.delete("a") is False
    await backend.clear()
    assert await backend.get("b") is None
    assert "other:leaveme" in fake.store  # untouched


@pytest.mark.anyio
async def test_redis_backend_decodes_str_payloads() -> None:
    fake = _FakeRedis()
    backend = RedisCacheBackend(client=fake, namespace="ns:")
    entry = _entry(b"<html>x</html>")
    # Simulate a client configured with decode_responses=True (returns str).
    fake.store["ns:k"] = _serialize(entry).decode("utf-8")
    assert await backend.get("k") == entry


@pytest.mark.anyio
async def test_redis_backend_corrupt_payload_is_a_miss_and_removed() -> None:
    fake = _FakeRedis()
    backend = RedisCacheBackend(client=fake, namespace="ns:")
    fake.store["ns:k"] = b"garbage\nbody"
    assert await backend.get("k") is None
    assert "ns:k" not in fake.store


@pytest.mark.anyio
async def test_redis_backend_clear_is_noop_when_namespace_empty() -> None:
    backend = RedisCacheBackend(client=_FakeRedis(), namespace="ns:")
    await backend.clear()  # nothing matches -> must not call delete() with no keys


def test_redis_backend_without_client_requires_the_redis_extra() -> None:
    try:
        import redis.asyncio  # noqa: F401, PLC0415
    except ImportError:
        with pytest.raises(ImportError, match=r"pyxle\[redis\]"):
            RedisCacheBackend()
    else:  # pragma: no cover - depends on the test environment having redis
        pytest.skip("redis is installed; the lazy-import guard is not exercised here")
