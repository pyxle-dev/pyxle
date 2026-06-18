"""Tests for the dev-only terminal observability dashboard."""

from __future__ import annotations

import asyncio
from pathlib import Path

from pyxle.devserver import DevServer
from pyxle.devserver.settings import DevServerSettings
from pyxle.observability.dashboard import _fmt_duration, render_dashboard, run_dashboard
from pyxle.observability.metrics import MetricsRegistry


def _snapshot(requests=((200, 10.0), (500, 5.0))) -> dict:
    reg = MetricsRegistry()
    for status, ms in requests:
        reg.observe_request(status, ms)
    reg.observe_render(40.0)
    reg.observe_loader(5.0)
    reg.observe_action(9.0)
    reg.record_cache("hit")
    reg.record_cache("miss")
    return reg.snapshot()


# ---------------------------------------------------------------------------
# render_dashboard


def test_render_contains_key_stats() -> None:
    text = "\n".join(render_dashboard(_snapshot(), uptime_s=65, requests_delta=2, interval_s=5))
    assert "Pyxle dev" in text
    assert "requests 2" in text  # total
    assert "+2" in text  # delta
    assert "2xx=1" in text and "5xx=1" in text
    assert "errors 50.0%" in text  # 1 of 2 was a 5xx
    assert "render 40.0ms" in text
    assert "hit-ratio 50%" in text  # 1 hit of 2 lookups


def test_render_handles_empty_snapshot() -> None:
    lines = render_dashboard(MetricsRegistry().snapshot(), uptime_s=0, requests_delta=0)
    text = "\n".join(lines)
    assert "errors 0.0%" in text
    assert "requests 0" in text


# ---------------------------------------------------------------------------
# _fmt_duration


def test_fmt_duration() -> None:
    assert _fmt_duration(30) == "30s"
    assert _fmt_duration(65) == "1m05s"
    assert _fmt_duration(3700) == "1h01m"


# ---------------------------------------------------------------------------
# run_dashboard loop


def test_run_dashboard_emits_each_iteration_and_tracks_delta() -> None:
    emitted: list[str] = []

    reg = MetricsRegistry()
    reg.observe_request(200, 1.0)
    first = reg.snapshot()  # total 1
    reg.observe_request(200, 1.0)
    reg.observe_request(200, 1.0)
    second = reg.snapshot()  # total 3
    snapshots = iter([first, second])

    async def _fake_sleep(_seconds: float) -> None:
        return None

    asyncio.run(
        run_dashboard(
            get_snapshot=lambda: next(snapshots),
            emit=emitted.append,
            uptime=lambda: 10.0,
            interval_s=5.0,
            sleep=_fake_sleep,
            max_iterations=2,
        )
    )

    text = "\n".join(emitted)
    # Two panels were emitted (6 lines each).
    assert emitted.count("└───────────────────────────────────────────────") == 2
    # First panel delta is +1 (0 -> 1), second is +2 (1 -> 3).
    assert "+1" in text
    assert "+2" in text


# ---------------------------------------------------------------------------
# DevServer._start_dashboard wiring


class _State:
    def __init__(self, *, registry=None, started_at=None) -> None:
        if registry is not None:
            self.pyxle_metrics = registry
        if started_at is not None:
            self.pyxle_started_at = started_at


class _App:
    def __init__(self, state: _State) -> None:
        self.state = state


def _server(tmp_path: Path, **kwargs) -> DevServer:
    return DevServer(settings=DevServerSettings.from_project_root(tmp_path), **kwargs)


def test_start_dashboard_returns_none_when_disabled(tmp_path: Path) -> None:
    srv = _server(tmp_path, dashboard=False)
    app = _App(_State(registry=MetricsRegistry(), started_at=0.0))
    assert srv._start_dashboard(app, loop=None) is None


def test_start_dashboard_returns_none_without_registry(tmp_path: Path) -> None:
    srv = _server(tmp_path, dashboard=True)
    assert srv._start_dashboard(_App(_State()), loop=None) is None


def test_start_dashboard_creates_task_when_enabled(tmp_path: Path) -> None:
    srv = _server(tmp_path, dashboard=True)
    app = _App(_State(registry=MetricsRegistry(), started_at=0.0))

    async def _run() -> None:
        loop = asyncio.get_running_loop()
        task = srv._start_dashboard(app, loop)
        assert task is not None
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(_run())
