"""Concurrency + per-request isolation tests for the SSR worker pool.

Streaming SSR used to serialise across requests: the Node worker's stdin loop
awaited each stream to completion before reading the next line, and the render
pipeline stored per-request state (pathname, CSRF token, head registry, style
hook) in shared ``globalThis`` slots. Both are now fixed — the worker dispatches
renders concurrently and the per-request state is carried in an
``AsyncLocalStorage`` context.

The most important test here is :func:`test_concurrent_streams_do_not_leak_csrf`:
it drives two *interleaved* streaming renders through a single Node worker with
different CSRF tokens and pathnames and proves each stream's HTML contains only
its own values. If AsyncLocalStorage did not propagate into React's Suspense
continuations, the tokens would cross — a real security defect.

The real-Node tests spawn ``ssr_worker.mjs`` (via the pool) and need Node.js;
they skip cleanly when it is absent.
"""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path
from textwrap import dedent
from unittest.mock import AsyncMock, MagicMock

import pytest

from pyxle.ssr.worker_pool import SsrWorkerPool, WorkerPoolError
from tests.ssr.utils import ensure_test_node_modules


@pytest.fixture
def anyio_backend() -> str:  # pragma: no cover - fixture wiring
    return "asyncio"


# ---------------------------------------------------------------------------
# Least-in-flight picker + in-flight accounting (subprocess mocked)
# ---------------------------------------------------------------------------


def _pool(tmp_path: Path) -> SsrWorkerPool:
    project_root = tmp_path / "project"
    client_root = project_root / ".pyxle-build" / "client"
    client_root.mkdir(parents=True)
    return SsrWorkerPool(size=1, project_root=project_root, client_root=client_root)


def test_pick_worker_prefers_least_in_flight(tmp_path: Path) -> None:
    """A worker already carrying open renders is skipped for an idle one."""
    pool = _pool(tmp_path)
    busy = MagicMock()
    busy.alive = True
    busy.in_flight = 3
    idle = MagicMock()
    idle.alive = True
    idle.in_flight = 0
    pool._workers = [busy, idle]

    # Always the idle worker while it stays least-loaded, regardless of order.
    assert pool._pick_worker() is idle
    assert pool._pick_worker() is idle


def test_pick_worker_breaks_ties_round_robin(tmp_path: Path) -> None:
    """Equally-loaded workers are chosen round-robin so distribution stays fair."""
    pool = _pool(tmp_path)
    workers = []
    for _ in range(3):
        w = MagicMock()
        w.alive = True
        w.in_flight = 2  # all equally loaded
        workers.append(w)
    pool._workers = workers

    picked = [pool._pick_worker() for _ in range(6)]
    assert picked == [workers[0], workers[1], workers[2], workers[0], workers[1], workers[2]]


@pytest.mark.anyio
async def test_render_in_flight_returns_to_zero_after_success(tmp_path: Path) -> None:
    """render() increments in_flight for the dispatch and clears it when done."""
    pool = _pool(tmp_path)
    worker = MagicMock()
    worker.alive = True
    worker.in_flight = 0

    async def fake_send(payload: dict, *, line: bytes | None = None) -> dict:
        # in_flight must reflect the active dispatch while the worker is busy.
        assert worker.in_flight == 1
        return {"id": payload["id"], "ok": True, "html": "<x/>"}

    worker.send = AsyncMock(side_effect=fake_send)
    pool._started = True
    pool._workers = [worker]

    component = tmp_path / "project" / ".pyxle-build" / "client" / "p.jsx"
    component.parent.mkdir(parents=True, exist_ok=True)
    component.touch()

    result = await pool.render(component, {})
    assert result["ok"] is True
    assert worker.in_flight == 0  # released in the finally


@pytest.mark.anyio
async def test_render_in_flight_returns_to_zero_after_error(tmp_path: Path) -> None:
    """A crashing worker still has its in_flight decremented (no leak)."""
    pool = _pool(tmp_path)
    worker = MagicMock()
    worker.alive = True
    worker.in_flight = 0
    worker.send = AsyncMock(side_effect=WorkerPoolError("worker crashed"))
    pool._started = True
    pool._workers = [worker]

    async def _noop_replenish() -> None:
        return None

    pool._replenish = _noop_replenish  # type: ignore[method-assign]

    component = tmp_path / "project" / ".pyxle-build" / "client" / "p.jsx"
    component.parent.mkdir(parents=True, exist_ok=True)
    component.touch()

    with pytest.raises(WorkerPoolError, match="worker crashed"):
        await pool.render(component, {})
    assert worker.in_flight == 0


# ---------------------------------------------------------------------------
# Real-Node concurrency + isolation
# ---------------------------------------------------------------------------


def _real_project(tmp_path: Path) -> tuple[Path, Path, Path]:
    project_root = tmp_path / "project"
    client_root = project_root / ".pyxle-build" / "client"
    pages = client_root / "pages"
    pages.mkdir(parents=True)
    ensure_test_node_modules(project_root)
    return project_root, client_root, pages


# A page whose Suspense boundary suspends for ``delayMs`` and, only once resumed,
# reads the request's CSRF token and pathname from the SSR globals. Reading after
# an await forces the value to come from a React continuation — the exact place
# AsyncLocalStorage must still carry the right request's context. A per-id latch
# lets two concurrent renders suspend independently.
_ISOLATION_COMPONENT = dedent(
    """
    import React, { Suspense } from 'react';

    const _latches = new Map();

    function Slow({ id, delayMs }) {
        let latch = _latches.get(id);
        if (!latch) {
            latch = { done: false };
            latch.promise = new Promise((resolve) => {
                setTimeout(() => { latch.done = true; resolve(); }, delayMs);
            });
            _latches.set(id, latch);
        }
        if (!latch.done) {
            throw latch.promise;
        }
        const token = globalThis.__PYXLE_CSRF_TOKEN__ || 'NO_TOKEN';
        const pathname = globalThis.__PYXLE_CURRENT_PATHNAME__ || 'NO_PATH';
        return <p data-testid="slow">token=[{token}] path=[{pathname}]</p>;
    }

    export default function Page({ id, delayMs }) {
        return (
            <main data-testid="shell">
                <Suspense fallback={<span data-testid="fallback">loading</span>}>
                    <Slow id={id} delayMs={delayMs} />
                </Suspense>
            </main>
        );
    }
    """
).strip() + "\n"


@pytest.mark.anyio
@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js required for SSR streaming")
async def test_concurrent_streams_do_not_leak_csrf(tmp_path: Path) -> None:
    """SECURITY: two interleaved streams through one worker keep separate context.

    Both renders suspend simultaneously on the single worker, so their Suspense
    continuations resolve while the other's is still pending. Each must read only
    its own CSRF token and pathname from AsyncLocalStorage — never the other's.
    """
    project_root, client_root, pages = _real_project(tmp_path)
    component = pages / "isolate.jsx"
    component.write_text(_ISOLATION_COMPONENT, encoding="utf-8")

    pool = SsrWorkerPool(size=1, project_root=project_root, client_root=client_root)
    await pool.start()
    try:
        agen_a = pool.render_stream(
            component,
            {"id": "A", "delayMs": 150},
            csrf_token="CSRF_ALPHA_TOKEN",
            request_pathname="/alpha",
        )
        agen_b = pool.render_stream(
            component,
            {"id": "B", "delayMs": 150},
            csrf_token="CSRF_BETA_TOKEN",
            request_pathname="/beta",
        )

        # Kick both streams off: pull each shell frame so both are mid-flight on
        # the SINGLE worker before either Suspense boundary resolves.
        first_a = await agen_a.__anext__()
        first_b = await agen_b.__anext__()
        assert pool._workers[0].in_flight == 2, (
            "both streams should be in-flight on one worker (not serialized)"
        )

        frames_a = [first_a] + [f async for f in agen_a]
        frames_b = [first_b] + [f async for f in agen_b]

        # Both streams ended: the worker released both slots.
        assert pool._workers[0].in_flight == 0
    finally:
        await pool.stop()

    html_a = "".join(f["html"] for f in frames_a if f["type"] == "chunk")
    html_b = "".join(f["html"] for f in frames_b if f["type"] == "chunk")

    assert frames_a[-1]["type"] == "end" and frames_b[-1]["type"] == "end"

    # Each stream sees ONLY its own token + pathname.
    assert "CSRF_ALPHA_TOKEN" in html_a and "/alpha" in html_a
    assert "CSRF_BETA_TOKEN" not in html_a and "/beta" not in html_a

    assert "CSRF_BETA_TOKEN" in html_b and "/beta" in html_b
    assert "CSRF_ALPHA_TOKEN" not in html_b and "/alpha" not in html_b

    # A dropped context would render the fallback literal instead of the token.
    assert "NO_TOKEN" not in html_a and "NO_TOKEN" not in html_b
    assert "NO_PATH" not in html_a and "NO_PATH" not in html_b


@pytest.mark.anyio
@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js required for SSR streaming")
async def test_concurrent_streams_interleave_not_serialize(tmp_path: Path) -> None:
    """Two overlapping streams to a 1-worker pool run concurrently, not one-by-one.

    Before the fix the worker's stdin loop awaited each stream fully before
    reading the next, so the second stream's first byte arrived only after the
    first finished. Here the first frames of both streams arrive while both are
    still suspended, and total wall time is far below the serial sum of the two
    ~200ms boundary delays.
    """
    project_root, client_root, pages = _real_project(tmp_path)
    component = pages / "isolate.jsx"
    component.write_text(_ISOLATION_COMPONENT, encoding="utf-8")

    pool = SsrWorkerPool(size=1, project_root=project_root, client_root=client_root)
    await pool.start()
    try:

        async def drain(stream_id: str) -> list[dict]:
            return [
                f
                async for f in pool.render_stream(
                    component,
                    {"id": stream_id, "delayMs": 200},
                    csrf_token=f"TOK_{stream_id}",
                    request_pathname=f"/{stream_id}",
                )
            ]

        loop = asyncio.get_running_loop()

        # Warm the bundle so neither timed run below pays the one-time cold
        # resolution cost, which would otherwise dwarf the ~200ms signal.
        await drain("warm")

        # Serial baseline: the two 200ms boundaries run back to back.
        t_serial_start = loop.time()
        await drain("serial-a")
        await drain("serial-b")
        serial = loop.time() - t_serial_start

        # Concurrent: the two boundaries overlap when the worker interleaves.
        t_conc_start = loop.time()
        results = await asyncio.gather(drain("one"), drain("two"))
        concurrent = loop.time() - t_conc_start
    finally:
        await pool.stop()

    for frames in results:
        assert frames[-1]["type"] == "end"

    # Comparing concurrent to a serial baseline — rather than an absolute
    # wall-clock bound — keeps this robust under CI load: when the machine is
    # busy both runs scale up together, but interleaving still overlaps the two
    # ~200ms boundaries, so the concurrent run saves close to one full boundary.
    # Full serialization would make the two times roughly equal.
    assert concurrent < serial - 0.1, (
        f"streams appear serialized (serial={serial:.3f}s, concurrent={concurrent:.3f}s)"
    )


# A trivial component with a stable marker — no Suspense. Used to stress the
# cold bundle-resolution path under concurrency.
_COLD_COMPONENT = dedent(
    """
    import React from 'react';
    export default function Page() {
        return React.createElement('div', { id: 'cold' }, 'COLD_MARKER');
    }
    """
)


@pytest.mark.anyio
@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js required for SSR rendering")
async def test_concurrent_cold_resolutions_coalesce(tmp_path: Path) -> None:
    """Concurrent first-time renders of one uncached component don't corrupt.

    With renders now running concurrently, several simultaneous cold requests
    for the same component would each run esbuild against the same deterministic
    outfile and could read a torn bundle. Resolution is coalesced onto a single
    compile, so all concurrent cold renders return the same correct output. This
    also mirrors the real hot-reload case, where a cache invalidation is followed
    by a burst of concurrent requests.
    """
    project_root, client_root, pages = _real_project(tmp_path)
    component = pages / "cold.jsx"
    component.write_text(_COLD_COMPONENT, encoding="utf-8")

    pool = SsrWorkerPool(size=1, project_root=project_root, client_root=client_root)
    await pool.start()
    try:
        # Fire the very first renders concurrently, so every one cold-misses the
        # bundle cache at once and exercises the coalescing path.
        results = await asyncio.gather(*(pool.render(component, {}) for _ in range(8)))
    finally:
        await pool.stop()

    assert len(results) == 8
    for result in results:
        assert result["ok"] is True, result
        assert "COLD_MARKER" in result["html"]
