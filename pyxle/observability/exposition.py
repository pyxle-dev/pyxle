"""Render a :class:`MetricsRegistry` to Prometheus text exposition format.

The text format is small and stable, so we emit it by hand — no
``prometheus-client`` dependency. Every series carries a ``worker`` label (the
process id) so a scraper can aggregate across ``pyxle serve --workers N``
processes, each of which exposes its own per-process registry.
"""

from __future__ import annotations

import os

from pyxle.observability.metrics import Histogram, MetricsRegistry

CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"


def _worker_label() -> str:
    return f'worker="{os.getpid()}"'


def _line(name: str, value: float | int, labels: str = "") -> str:
    label_block = f"{{{labels}}}" if labels else ""
    return f"{name}{label_block} {value}"


def _histogram_lines(name: str, hist: Histogram, worker: str) -> list[str]:
    lines = [
        f"# TYPE {name} histogram",
    ]
    for le, count in hist.cumulative_buckets():
        le_str = "+Inf" if le == float("inf") else _format_float(le)
        lines.append(_line(f"{name}_bucket", count, f'{worker},le="{le_str}"'))
    lines.append(_line(f"{name}_sum", _format_float(hist.sum_ms), worker))
    lines.append(_line(f"{name}_count", hist.total, worker))
    return lines


def _format_float(value: float) -> str:
    # Avoid scientific notation and trailing noise in the exposition.
    return f"{value:.6f}".rstrip("0").rstrip(".") or "0"


def render_prometheus(registry: MetricsRegistry) -> str:
    """Return the registry as a Prometheus text-exposition payload."""
    worker = _worker_label()
    lines: list[str] = []

    lines.append("# HELP pyxle_requests_total Total HTTP requests handled.")
    lines.append("# TYPE pyxle_requests_total counter")
    lines.append(_line("pyxle_requests_total", registry.requests_total, worker))
    for status_class, count in sorted(registry.requests_by_status.items()):
        lines.append(
            _line("pyxle_requests_by_status", count, f'{worker},status="{status_class}"')
        )

    lines.append("# HELP pyxle_cache Page-cache outcomes.")
    lines.append("# TYPE pyxle_cache_hits_total counter")
    lines.append(_line("pyxle_cache_hits_total", registry.cache.hits, worker))
    lines.append("# TYPE pyxle_cache_stale_total counter")
    lines.append(_line("pyxle_cache_stale_total", registry.cache.stale, worker))
    lines.append("# TYPE pyxle_cache_misses_total counter")
    lines.append(_line("pyxle_cache_misses_total", registry.cache.misses, worker))
    lines.append("# TYPE pyxle_cache_hit_ratio gauge")
    lines.append(
        _line("pyxle_cache_hit_ratio", _format_float(registry.cache.hit_ratio), worker)
    )

    lines.append("# HELP pyxle_request_duration_ms Request handling time.")
    lines.extend(_histogram_lines("pyxle_request_duration_ms", registry.request_duration, worker))
    lines.append("# HELP pyxle_render_duration_ms SSR render time.")
    lines.extend(_histogram_lines("pyxle_render_duration_ms", registry.render_duration, worker))
    lines.append("# HELP pyxle_loader_duration_ms @server loader time.")
    lines.extend(_histogram_lines("pyxle_loader_duration_ms", registry.loader_duration, worker))
    lines.append("# HELP pyxle_action_duration_ms @action handler time.")
    lines.extend(_histogram_lines("pyxle_action_duration_ms", registry.action_duration, worker))

    return "\n".join(lines) + "\n"


__all__ = ["CONTENT_TYPE", "render_prometheus"]
