"""Tests for the in-process metrics registry."""

from __future__ import annotations

from pyxle.observability.metrics import (
    CacheCounters,
    Histogram,
    MetricsRegistry,
    get_metrics,
)


# ---------------------------------------------------------------------------
# Histogram


def test_histogram_buckets_and_sum() -> None:
    h = Histogram()
    for value in (0.5, 7.0, 7.0, 3000.0):
        h.observe(value)
    assert h.total == 4
    assert h.sum_ms == 0.5 + 7.0 + 7.0 + 3000.0
    # 3000ms exceeds the largest bound (2500), so it lands in +Inf.
    assert h.inf_count == 1
    snap = h.snapshot()
    assert snap["count"] == 4
    assert snap["avg_ms"] == round(h.sum_ms / 4, 3)


def test_histogram_cumulative_buckets_are_monotonic() -> None:
    h = Histogram()
    for value in (0.5, 7.0, 60.0, 3000.0):
        h.observe(value)
    buckets = h.cumulative_buckets()
    counts = [count for _, count in buckets]
    # Cumulative counts never decrease, and the final (+Inf) bucket holds all.
    assert counts == sorted(counts)
    assert buckets[-1][0] == float("inf")
    assert buckets[-1][1] == 4


def test_empty_histogram_snapshot_has_zero_average() -> None:
    assert Histogram().snapshot() == {"count": 0, "sum_ms": 0.0, "avg_ms": 0.0}


# ---------------------------------------------------------------------------
# CacheCounters


def test_cache_hit_ratio() -> None:
    c = CacheCounters(hits=3, stale=1, misses=4)
    assert c.total == 8
    # hits + stale counts as served-from-cache.
    assert c.hit_ratio == 0.5


def test_cache_hit_ratio_zero_when_empty() -> None:
    assert CacheCounters().hit_ratio == 0.0


# ---------------------------------------------------------------------------
# MetricsRegistry


def test_observe_request_tallies_status_class_and_duration() -> None:
    reg = MetricsRegistry()
    reg.observe_request(200, 12.0)
    reg.observe_request(204, 3.0)
    reg.observe_request(404, 1.0)
    reg.observe_request(500, 8.0)
    assert reg.requests_total == 4
    assert reg.requests_by_status == {"2xx": 2, "4xx": 1, "5xx": 1}
    assert reg.request_duration.total == 4


def test_observe_render_loader_action() -> None:
    reg = MetricsRegistry()
    reg.observe_render(40.0)
    reg.observe_loader(5.0)
    reg.observe_action(9.0)
    assert reg.render_duration.total == 1
    assert reg.loader_duration.total == 1
    assert reg.action_duration.total == 1


def test_record_cache_outcomes() -> None:
    reg = MetricsRegistry()
    reg.record_cache("hit")
    reg.record_cache("hit")
    reg.record_cache("stale")
    reg.record_cache("miss")
    reg.record_cache("unknown")  # ignored
    assert reg.cache.hits == 2
    assert reg.cache.stale == 1
    assert reg.cache.misses == 1


def test_snapshot_shape() -> None:
    reg = MetricsRegistry()
    reg.observe_request(200, 10.0)
    reg.record_cache("hit")
    snap = reg.snapshot()
    assert snap["requests_total"] == 1
    assert snap["requests_by_status"] == {"2xx": 1}
    assert snap["cache"]["hits"] == 1
    assert snap["cache"]["hit_ratio"] == 1.0
    for key in ("request_duration", "render_duration", "loader_duration", "action_duration"):
        assert "count" in snap[key]


# ---------------------------------------------------------------------------
# get_metrics


def test_get_metrics_returns_registry_when_bound() -> None:
    reg = MetricsRegistry()

    class _State:
        pyxle_metrics = reg

    class _App:
        state = _State()

    class _Req:
        app = _App()

    assert get_metrics(_Req()) is reg


def test_get_metrics_returns_none_when_absent() -> None:
    class _Req:
        pass

    assert get_metrics(_Req()) is None


def test_get_metrics_swallows_request_app_keyerror() -> None:
    # Starlette's Request.app raises KeyError when no app is in scope.
    class _Req:
        @property
        def app(self):
            raise KeyError("app")

    assert get_metrics(_Req()) is None


def test_get_metrics_reads_app_from_scope() -> None:
    reg = MetricsRegistry()

    class _State:
        pyxle_metrics = reg

    class _App:
        state = _State()

    class _Req:
        scope = {"app": _App()}

    assert get_metrics(_Req()) is reg
