"""Tests for Prometheus text-exposition rendering."""

from __future__ import annotations

from pyxle.observability.exposition import CONTENT_TYPE, render_prometheus
from pyxle.observability.metrics import MetricsRegistry


def _populated() -> MetricsRegistry:
    reg = MetricsRegistry()
    reg.observe_request(200, 12.0)
    reg.observe_request(404, 1.0)
    reg.observe_render(40.0)
    reg.observe_loader(5.0)
    reg.observe_action(9.0)
    reg.record_cache("hit")
    reg.record_cache("miss")
    return reg


def test_content_type_is_prometheus_text() -> None:
    assert CONTENT_TYPE.startswith("text/plain")


def test_render_includes_core_series() -> None:
    text = render_prometheus(_populated())
    assert "pyxle_requests_total" in text
    assert "pyxle_cache_hits_total" in text
    assert "pyxle_cache_misses_total" in text
    assert "pyxle_cache_hit_ratio" in text
    assert "pyxle_request_duration_ms_bucket" in text
    assert "pyxle_render_duration_ms_count" in text
    assert "pyxle_loader_duration_ms_sum" in text
    assert "pyxle_action_duration_ms_bucket" in text


def test_every_series_carries_a_worker_label() -> None:
    text = render_prometheus(_populated())
    metric_lines = [
        line
        for line in text.splitlines()
        if line and not line.startswith("#")
    ]
    assert metric_lines  # sanity
    assert all('worker="' in line for line in metric_lines)


def test_histogram_has_inf_bucket_and_status_labels() -> None:
    text = render_prometheus(_populated())
    assert 'le="+Inf"' in text
    # Request status classes are emitted as labelled series.
    assert 'status="2xx"' in text
    assert 'status="4xx"' in text


def test_render_is_valid_for_empty_registry() -> None:
    text = render_prometheus(MetricsRegistry())
    assert "pyxle_requests_total" in text
    # An empty registry still renders a 0 hit-ratio gauge and zero counters.
    assert text.endswith("\n")
