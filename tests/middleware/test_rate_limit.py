"""Tests for pyxle.middleware.rate_limit — token-bucket rate limiter."""

from __future__ import annotations

from typing import Any

import pytest
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from pyxle.middleware.rate_limit import (
    DEFAULT_MAX_BUCKETS,
    RateLimitMiddleware,
    _canonical_path,
)


class _Clock:
    """A hand-cranked monotonic clock for deterministic refill tests."""

    def __init__(self, start: float = 1_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _make(**kwargs: Any) -> RateLimitMiddleware:
    """Construct middleware around a no-op app with sensible defaults."""

    async def _app(scope, receive, send):  # pragma: no cover - never called here
        raise AssertionError("inner app should not run in unit tests")

    params: dict[str, Any] = {"requests": 5, "window_seconds": 10.0}
    params.update(kwargs)
    return RateLimitMiddleware(_app, **params)


# ---------------------------------------------------------------------------
# Construction / validation
# ---------------------------------------------------------------------------


class TestConstruction:
    def test_rejects_zero_requests(self):
        with pytest.raises(ValueError, match="requests >= 1"):
            _make(requests=0)

    def test_rejects_negative_requests(self):
        with pytest.raises(ValueError, match="requests >= 1"):
            _make(requests=-3)

    def test_rejects_zero_window(self):
        with pytest.raises(ValueError, match="window_seconds > 0"):
            _make(window_seconds=0)

    def test_rejects_negative_window(self):
        with pytest.raises(ValueError, match="window_seconds > 0"):
            _make(window_seconds=-1.0)

    def test_capacity_and_refill_rate_derived(self):
        mw = _make(requests=10, window_seconds=5.0)
        assert mw._capacity == 10.0
        assert mw._refill_per_second == pytest.approx(2.0)

    def test_max_buckets_floored_to_one(self):
        mw = _make(max_buckets=0)
        assert mw._max_buckets == 1

    def test_default_max_buckets(self):
        mw = _make()
        assert mw._max_buckets == DEFAULT_MAX_BUCKETS


# ---------------------------------------------------------------------------
# Token-bucket math (_consume)
# ---------------------------------------------------------------------------


class TestConsume:
    def test_burst_up_to_capacity_then_blocks(self):
        clock = _Clock()
        mw = _make(requests=5, window_seconds=10.0, time_fn=clock)
        # Five requests drain the full bucket.
        for _ in range(5):
            allowed, retry_after = mw._consume("client")
            assert allowed is True
            assert retry_after == 0
        # The sixth is rejected because the bucket is empty.
        allowed, retry_after = mw._consume("client")
        assert allowed is False
        assert retry_after >= 1

    def test_retry_after_reflects_refill_rate(self):
        clock = _Clock()
        # 5 tokens / 10s => 0.5 tokens/sec => ~2s to refill one token.
        mw = _make(requests=5, window_seconds=10.0, time_fn=clock)
        for _ in range(5):
            mw._consume("c")
        _, retry_after = mw._consume("c")
        # Conservative ceiling: int(1.0 / 0.5) + 1 == 3.
        assert retry_after == 3

    def test_tokens_refill_over_time(self):
        clock = _Clock()
        mw = _make(requests=5, window_seconds=10.0, time_fn=clock)
        for _ in range(5):
            mw._consume("c")
        assert mw._consume("c")[0] is False
        # 4 seconds * 0.5 tokens/sec == 2 tokens replenished.
        clock.advance(4.0)
        assert mw._consume("c")[0] is True
        assert mw._consume("c")[0] is True
        # Bucket empty again.
        assert mw._consume("c")[0] is False

    def test_refill_never_exceeds_capacity(self):
        clock = _Clock()
        mw = _make(requests=3, window_seconds=3.0, time_fn=clock)
        for _ in range(3):
            mw._consume("c")
        # Idle far longer than the window — bucket caps at capacity, not beyond.
        clock.advance(10_000.0)
        allowed = [mw._consume("c")[0] for _ in range(4)]
        assert allowed == [True, True, True, False]

    def test_per_client_isolation(self):
        clock = _Clock()
        mw = _make(requests=2, window_seconds=10.0, time_fn=clock)
        assert mw._consume("a")[0] is True
        assert mw._consume("a")[0] is True
        assert mw._consume("a")[0] is False  # a is exhausted
        # b has its own untouched bucket.
        assert mw._consume("b")[0] is True
        assert mw._consume("b")[0] is True
        assert mw._consume("b")[0] is False

    def test_lru_eviction_bounds_bucket_count(self):
        clock = _Clock()
        mw = _make(requests=1, window_seconds=10.0, max_buckets=2, time_fn=clock)
        mw._consume("a")
        mw._consume("b")
        mw._consume("c")  # exceeds cap -> evicts least-recently-seen ("a")
        assert list(mw._buckets.keys()) == ["b", "c"]
        assert len(mw._buckets) == 2

    def test_recently_seen_key_survives_eviction(self):
        clock = _Clock()
        mw = _make(requests=1, window_seconds=10.0, max_buckets=2, time_fn=clock)
        mw._consume("a")
        mw._consume("b")
        mw._consume("a")  # touch "a" so it is most-recently-seen
        mw._consume("c")  # evicts "b", not "a"
        assert list(mw._buckets.keys()) == ["a", "c"]


# ---------------------------------------------------------------------------
# Client keying (_client_key)
# ---------------------------------------------------------------------------


class TestClientKey:
    def test_uses_remote_ip_by_default(self):
        mw = _make()
        scope = {"client": ("203.0.113.7", 5555), "headers": []}
        assert mw._client_key(scope) == "203.0.113.7"

    def test_unknown_when_no_client(self):
        mw = _make()
        assert mw._client_key({"headers": []}) == "unknown"

    def test_ignores_forwarded_for_when_untrusted(self):
        mw = _make(trust_forwarded_for=False)
        scope = {
            "client": ("203.0.113.7", 5555),
            "headers": [(b"x-forwarded-for", b"9.9.9.9")],
        }
        assert mw._client_key(scope) == "203.0.113.7"

    def test_uses_first_forwarded_hop_when_trusted(self):
        mw = _make(trust_forwarded_for=True)
        scope = {
            "client": ("10.0.0.1", 5555),
            "headers": [(b"x-forwarded-for", b"9.9.9.9, 8.8.8.8")],
        }
        assert mw._client_key(scope) == "9.9.9.9"

    def test_falls_back_to_client_when_forwarded_absent(self):
        mw = _make(trust_forwarded_for=True)
        scope = {"client": ("10.0.0.1", 5555), "headers": []}
        assert mw._client_key(scope) == "10.0.0.1"

    def test_falls_back_when_forwarded_value_empty(self):
        mw = _make(trust_forwarded_for=True)
        scope = {
            "client": ("10.0.0.1", 5555),
            "headers": [(b"x-forwarded-for", b"  ")],
        }
        assert mw._client_key(scope) == "10.0.0.1"


# ---------------------------------------------------------------------------
# Exempt-path matching (_is_exempt / _canonical_path)
# ---------------------------------------------------------------------------


class TestExemptPaths:
    def test_exact_match_is_exempt(self):
        mw = _make(exempt_paths=("/health",))
        assert mw._is_exempt("/health") is True

    def test_subpath_is_exempt(self):
        mw = _make(exempt_paths=("/health",))
        assert mw._is_exempt("/health/live") is True

    def test_segment_boundary_respected(self):
        mw = _make(exempt_paths=("/health",))
        # "/healthcheck" must NOT match the "/health" prefix.
        assert mw._is_exempt("/healthcheck") is False

    def test_non_exempt_path(self):
        mw = _make(exempt_paths=("/health",))
        assert mw._is_exempt("/api/users") is False

    def test_root_and_empty_exempt_entries_ignored(self):
        # "/" and empty strings would exempt everything; they are dropped.
        mw = _make(exempt_paths=("/", "", "/metrics"))
        assert mw._exempt == ("/metrics",)
        assert mw._is_exempt("/anything") is False
        assert mw._is_exempt("/metrics") is True

    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("//a//b/", "/a/b"),
            ("/a/./b", "/a/b"),
            ("/a/../b", "/b"),
            ("/a/../../b", "/b"),
            ("/", "/"),
            ("", "/"),
            ("/a/b/", "/a/b"),
        ],
    )
    def test_canonical_path(self, raw: str, expected: str):
        assert _canonical_path(raw) == expected


# ---------------------------------------------------------------------------
# Integration through Starlette
# ---------------------------------------------------------------------------


def _build_client(**kwargs: Any) -> TestClient:
    async def handler(request: Request) -> PlainTextResponse:
        return PlainTextResponse("ok")

    params: dict[str, Any] = {"requests": 2, "window_seconds": 60.0}
    params.update(kwargs)
    app = Starlette(
        routes=[
            Route("/api/data", handler),
            Route("/health", handler),
        ],
        middleware=[Middleware(RateLimitMiddleware, **params)],
    )
    return TestClient(app)


class TestIntegration:
    def test_allows_up_to_capacity_then_429(self):
        clock = _Clock()
        client = _build_client(requests=2, window_seconds=60.0, time_fn=clock)
        assert client.get("/api/data").status_code == 200
        assert client.get("/api/data").status_code == 200
        blocked = client.get("/api/data")
        assert blocked.status_code == 429

    def test_429_carries_retry_after_and_json_body(self):
        clock = _Clock()
        client = _build_client(requests=1, window_seconds=60.0, time_fn=clock)
        assert client.get("/api/data").status_code == 200
        blocked = client.get("/api/data")
        assert blocked.status_code == 429
        assert int(blocked.headers["retry-after"]) >= 1
        assert blocked.headers["content-type"] == "application/json"
        assert blocked.json() == {"ok": False, "error": "Too Many Requests"}

    def test_exempt_path_is_never_limited(self):
        clock = _Clock()
        client = _build_client(
            requests=1, window_seconds=60.0, exempt_paths=("/health",), time_fn=clock
        )
        # Exhaust the limit on the regular route.
        assert client.get("/api/data").status_code == 200
        assert client.get("/api/data").status_code == 429
        # The exempt route keeps answering regardless.
        for _ in range(5):
            assert client.get("/health").status_code == 200

    def test_forwarded_for_keys_distinct_clients(self):
        clock = _Clock()
        client = _build_client(
            requests=1,
            window_seconds=60.0,
            trust_forwarded_for=True,
            time_fn=clock,
        )
        # Two different forwarded clients each get their own bucket.
        assert (
            client.get("/api/data", headers={"x-forwarded-for": "1.1.1.1"}).status_code
            == 200
        )
        assert (
            client.get("/api/data", headers={"x-forwarded-for": "2.2.2.2"}).status_code
            == 200
        )
        # Re-using the first client's IP is now rate-limited.
        assert (
            client.get("/api/data", headers={"x-forwarded-for": "1.1.1.1"}).status_code
            == 429
        )

    def test_recovers_after_refill(self):
        clock = _Clock()
        client = _build_client(requests=1, window_seconds=10.0, time_fn=clock)
        assert client.get("/api/data").status_code == 200
        assert client.get("/api/data").status_code == 429
        # 10s fully refills the single-token bucket.
        clock.advance(10.0)
        assert client.get("/api/data").status_code == 200


# ---------------------------------------------------------------------------
# Non-HTTP scopes bypass the limiter
# ---------------------------------------------------------------------------


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_non_http_scope_bypasses_limit(anyio_backend: str):
    seen: list[str] = []

    async def inner(scope, receive, send):
        seen.append(scope["type"])

    mw = RateLimitMiddleware(inner, requests=1, window_seconds=1.0)

    async def receive():  # pragma: no cover - not awaited
        return {"type": "websocket.connect"}

    async def send(message):  # pragma: no cover - not awaited
        return None

    # Far more calls than the capacity of 1 — none are throttled.
    for _ in range(5):
        await mw({"type": "websocket", "path": "/ws"}, receive, send)
    await mw({"type": "lifespan"}, receive, send)
    assert seen == ["websocket"] * 5 + ["lifespan"]
    assert mw._buckets == {}  # no buckets created for non-HTTP traffic
