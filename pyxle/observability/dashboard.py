"""Dev-only terminal dashboard: a periodic snapshot of request and SSR metrics.

Built entirely on the in-process :class:`MetricsRegistry` (so it costs nothing
extra on the request path) and the stdlib — no ``rich`` or other dependency. The
renderer is a pure function; the reporter loop takes injectable ``sleep`` and an
iteration cap so it is straightforward to test.
"""

from __future__ import annotations

import asyncio
from typing import Awaitable, Callable


def _fmt_duration(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    minutes, secs = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m{secs:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m"


def render_dashboard(
    snapshot: dict,
    *,
    uptime_s: float,
    requests_delta: int = 0,
    interval_s: float = 5.0,
) -> list[str]:
    """Render a metrics snapshot into a compact block of terminal lines."""
    by_status = snapshot.get("requests_by_status", {})
    total = snapshot.get("requests_total", 0)
    errors = by_status.get("5xx", 0)
    error_rate = (errors / total * 100.0) if total else 0.0
    rps = requests_delta / interval_s if interval_s else 0.0

    def _avg(name: str) -> float:
        return float(snapshot.get(name, {}).get("avg_ms", 0.0))

    cache = snapshot.get("cache", {})

    lines = [
        "┌─ Pyxle dev ─ observability ───────────────────",
        f"│ uptime {_fmt_duration(uptime_s)}   "
        f"requests {total} (+{requests_delta}, {rps:.1f}/s)",
        "│ status "
        + " ".join(f"{key}={by_status[key]}" for key in sorted(by_status))
        + f"   errors {error_rate:.1f}%",
        f"│ latency  request {_avg('request_duration'):.1f}ms   "
        f"render {_avg('render_duration'):.1f}ms   "
        f"loader {_avg('loader_duration'):.1f}ms   "
        f"action {_avg('action_duration'):.1f}ms",
        f"│ cache  hit-ratio {cache.get('hit_ratio', 0.0) * 100:.0f}%   "
        f"(hit {cache.get('hits', 0)} / stale {cache.get('stale', 0)} / "
        f"miss {cache.get('misses', 0)})",
        "└───────────────────────────────────────────────",
    ]
    return lines


async def run_dashboard(
    *,
    get_snapshot: Callable[[], dict],
    emit: Callable[[str], None],
    uptime: Callable[[], float],
    interval_s: float = 5.0,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    max_iterations: int | None = None,
) -> None:
    """Periodically render and emit the dashboard until cancelled.

    ``get_snapshot`` returns a :meth:`MetricsRegistry.snapshot`; ``emit`` writes
    one line (e.g. the console logger); ``uptime`` returns seconds since start.
    ``sleep`` and ``max_iterations`` are injection points for tests.
    """
    previous_total = 0
    iterations = 0
    while max_iterations is None or iterations < max_iterations:
        await sleep(interval_s)
        snapshot = get_snapshot()
        total = snapshot.get("requests_total", 0)
        delta = total - previous_total
        previous_total = total
        for line in render_dashboard(
            snapshot,
            uptime_s=uptime(),
            requests_delta=delta,
            interval_s=interval_s,
        ):
            emit(line)
        iterations += 1


__all__ = ["render_dashboard", "run_dashboard"]
