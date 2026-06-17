"""In-process metrics registry: bounded, allocation-cheap, lock-free.

One :class:`MetricsRegistry` lives per ASGI app process (on
``app.state.pyxle_metrics``). It records request, SSR-render, loader, and action
durations plus page-cache hit/stale/miss counts, and renders them for the
``/api/__pyxle/metrics`` endpoint and the health probes.

Design constraints (CLAUDE.md rules 15/17 — SSR is the hot path, caches must be
bounded):

* **No per-observation allocation.** Latencies go into fixed-bucket histograms
  (a bucket bump plus ``sum``/``count`` adds), never a growing sample list, so
  memory is ``O(buckets)``, not ``O(requests)``.
* **No locks.** Every observation is a handful of attribute ``+=`` updates. All
  recording happens on the asyncio event loop (the request hook, the cache
  decision, and the render/loader/action sites all ``await`` back onto the
  loop), so there is no cross-thread contention to guard.

**Multi-worker caveat:** under ``pyxle serve --workers N`` each worker process
has its own registry, so the metrics endpoint reports *per-worker* numbers (the
exposition carries a ``worker`` label so a scraper can aggregate). A shared
cross-worker registry is deferred to Phase 2.10, mirroring the in-process
WebSocket broker.
"""

from __future__ import annotations

from collections.abc import MutableMapping
from dataclasses import dataclass, field

# Latency bucket ceilings in milliseconds. Cumulative Prometheus-style buckets
# are derived from these at read time (each bucket counts observations <= its
# ceiling). Chosen to span a fast cache hit (~1ms) to a slow cold render (~2.5s).
_BUCKET_BOUNDS_MS: tuple[float, ...] = (
    1.0, 5.0, 10.0, 25.0, 50.0, 100.0, 250.0, 500.0, 1000.0, 2500.0,
)


@dataclass(slots=True)
class Histogram:
    """A fixed-bucket latency histogram. Bounded: O(len(bucket bounds)) memory."""

    bounds: tuple[float, ...] = _BUCKET_BOUNDS_MS
    counts: list[int] = field(default_factory=lambda: [0] * len(_BUCKET_BOUNDS_MS))
    inf_count: int = 0  # observations greater than the largest bound
    total: int = 0
    sum_ms: float = 0.0

    def observe(self, value_ms: float) -> None:
        self.total += 1
        self.sum_ms += value_ms
        bounds = self.bounds
        for index, bound in enumerate(bounds):
            if value_ms <= bound:
                self.counts[index] += 1
                return
        self.inf_count += 1

    def cumulative_buckets(self) -> list[tuple[float, int]]:
        """Return ``(le, cumulative_count)`` pairs, Prometheus histogram style."""
        running = 0
        result: list[tuple[float, int]] = []
        for bound, count in zip(self.bounds, self.counts):
            running += count
            result.append((bound, running))
        result.append((float("inf"), running + self.inf_count))
        return result

    def snapshot(self) -> dict[str, object]:
        avg = self.sum_ms / self.total if self.total else 0.0
        return {
            "count": self.total,
            "sum_ms": round(self.sum_ms, 3),
            "avg_ms": round(avg, 3),
        }


@dataclass(slots=True)
class CacheCounters:
    """Page-cache outcome tallies (monotonic)."""

    hits: int = 0
    stale: int = 0
    misses: int = 0

    @property
    def total(self) -> int:
        return self.hits + self.stale + self.misses

    @property
    def hit_ratio(self) -> float:
        """Fraction served from cache (fresh or stale) — 0.0 when no lookups."""
        total = self.total
        if total == 0:
            return 0.0
        return (self.hits + self.stale) / total


class MetricsRegistry:
    """Per-process sink for request, render, loader, action, and cache metrics."""

    __slots__ = (
        "requests_total",
        "requests_by_status",
        "request_duration",
        "render_duration",
        "loader_duration",
        "action_duration",
        "cache",
    )

    def __init__(self) -> None:
        self.requests_total: int = 0
        # Tally by status class ("2xx", "3xx", "4xx", "5xx") — bounded to 5 keys.
        self.requests_by_status: dict[str, int] = {}
        self.request_duration = Histogram()
        self.render_duration = Histogram()
        self.loader_duration = Histogram()
        self.action_duration = Histogram()
        self.cache = CacheCounters()

    # -- recording (hot path: a few int adds, no allocation) ----------------

    def observe_request(self, status_code: int, duration_ms: float) -> None:
        self.requests_total += 1
        bucket = f"{status_code // 100}xx"
        self.requests_by_status[bucket] = self.requests_by_status.get(bucket, 0) + 1
        self.request_duration.observe(duration_ms)

    def observe_render(self, duration_ms: float) -> None:
        self.render_duration.observe(duration_ms)

    def observe_loader(self, duration_ms: float) -> None:
        self.loader_duration.observe(duration_ms)

    def observe_action(self, duration_ms: float) -> None:
        self.action_duration.observe(duration_ms)

    def record_cache(self, outcome: str) -> None:
        """Record a page-cache outcome: ``"hit"``, ``"stale"`` or ``"miss"``."""
        if outcome == "hit":
            self.cache.hits += 1
        elif outcome == "stale":
            self.cache.stale += 1
        elif outcome == "miss":
            self.cache.misses += 1

    # -- reading ------------------------------------------------------------

    def snapshot(self) -> dict[str, object]:
        """A plain-data view for the metrics endpoint and health probes."""
        return {
            "requests_total": self.requests_total,
            "requests_by_status": dict(self.requests_by_status),
            "request_duration": self.request_duration.snapshot(),
            "render_duration": self.render_duration.snapshot(),
            "loader_duration": self.loader_duration.snapshot(),
            "action_duration": self.action_duration.snapshot(),
            "cache": {
                "hits": self.cache.hits,
                "stale": self.cache.stale,
                "misses": self.cache.misses,
                "hit_ratio": round(self.cache.hit_ratio, 4),
            },
        }


def get_metrics(request: object) -> MetricsRegistry | None:
    """Return the registry bound to *request*'s app, or ``None`` if unset.

    Reads the app from the ASGI ``scope`` when available, falling back to
    ``request.app`` — guarded, because Starlette's ``Request.app`` raises
    ``KeyError`` (not ``AttributeError``) when no app is in scope, which a bare
    ``getattr(..., None)`` would not swallow.
    """
    app = None
    scope = getattr(request, "scope", None)
    if isinstance(scope, MutableMapping):
        app = scope.get("app")
    if app is None:
        try:
            app = request.app  # type: ignore[attr-defined]
        except (KeyError, AttributeError):
            app = None
    if app is None:
        return None
    state = getattr(app, "state", None)
    return getattr(state, "pyxle_metrics", None)


__all__ = [
    "CacheCounters",
    "Histogram",
    "MetricsRegistry",
    "get_metrics",
]
