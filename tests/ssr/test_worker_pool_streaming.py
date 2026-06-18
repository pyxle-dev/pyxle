"""Tests for the multi-frame streaming transport in the SSR worker pool.

These exercise the Python side (``_WorkerState.send_stream`` and
``SsrWorkerPool.render_stream``) with a fake Node worker, per the project's
"mock Node in unit tests" rule. Frames are pushed onto the read queue only when
a request is written, so they always arrive after the per-request queue is
registered (no race).
"""

from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path
from textwrap import dedent
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pyxle.ssr.worker_pool import SsrWorkerPool, WorkerPoolError, _WorkerState
from tests.ssr.utils import ensure_test_node_modules


@pytest.fixture
def anyio_backend() -> str:  # pragma: no cover - fixture wiring
    return "asyncio"


def _streaming_proc(frames_for):
    """Fake subprocess that, on each written request, pushes ``frames_for(id)``
    onto its stdout queue. A frame of ``b""`` models EOF (worker death)."""

    read_queue: asyncio.Queue = asyncio.Queue()
    proc = MagicMock()
    proc.stdin = MagicMock()
    proc.stdin.is_closing.return_value = False
    proc.stdin.close = MagicMock()
    proc.stdin.drain = AsyncMock()

    def capture_write(data: bytes) -> None:
        payload = json.loads(data.decode().strip())
        for frame in frames_for(payload["id"]):
            read_queue.put_nowait(
                frame if frame == b"" else (json.dumps(frame) + "\n").encode()
            )

    proc.stdin.write = MagicMock(side_effect=capture_write)

    async def fake_read(n: int = -1) -> bytes:
        return await read_queue.get()

    proc.stdout = MagicMock()
    proc.stdout.read = fake_read
    proc.wait = AsyncMock(return_value=0)
    proc.kill = MagicMock()
    return proc


@pytest.mark.anyio
async def test_send_stream_yields_frames_until_terminal() -> None:
    proc = _streaming_proc(
        lambda i: [
            {"id": i, "type": "chunk", "html": "<a>"},
            {"id": i, "type": "chunk", "html": "<b>"},
            {"id": i, "type": "end"},
        ]
    )
    worker = _WorkerState(process=proc)
    task = asyncio.create_task(worker.read_loop())

    got = [frame async for frame in worker.send_stream({"id": "s1"}, frame_timeout=1.0)]

    assert [f["type"] for f in got] == ["chunk", "chunk", "end"]
    assert "s1" not in worker.streaming  # cleaned up after the terminal frame
    task.cancel()


@pytest.mark.anyio
async def test_send_stream_stops_on_error_frame() -> None:
    proc = _streaming_proc(
        lambda i: [{"id": i, "type": "chunk", "html": "<a>"}, {"id": i, "type": "error", "error": "boom"}]
    )
    worker = _WorkerState(process=proc)
    task = asyncio.create_task(worker.read_loop())

    got = [frame async for frame in worker.send_stream({"id": "s1"}, frame_timeout=1.0)]

    assert got[-1]["type"] == "error" and got[-1]["error"] == "boom"
    task.cancel()


@pytest.mark.anyio
async def test_send_stream_raises_when_worker_dies_mid_stream() -> None:
    proc = _streaming_proc(lambda i: [{"id": i, "type": "chunk", "html": "<a>"}, b""])  # chunk, then EOF
    worker = _WorkerState(process=proc)
    task = asyncio.create_task(worker.read_loop())

    got = []
    with pytest.raises(WorkerPoolError, match="terminated mid-stream"):
        async for frame in worker.send_stream({"id": "s1"}, frame_timeout=1.0):
            got.append(frame)

    assert [f["type"] for f in got] == ["chunk"]  # the pre-death chunk was delivered
    task.cancel()


@pytest.mark.anyio
async def test_send_stream_times_out_when_stalled() -> None:
    proc = MagicMock()
    proc.stdin = MagicMock()
    proc.stdin.drain = AsyncMock()
    proc.stdin.write = MagicMock()
    hang = asyncio.Event()

    async def fake_read(n: int = -1) -> bytes:
        await hang.wait()
        return b""

    proc.stdout = MagicMock()
    proc.stdout.read = fake_read
    worker = _WorkerState(process=proc)
    task = asyncio.create_task(worker.read_loop())

    with pytest.raises(WorkerPoolError, match="stalled"):
        async for _ in worker.send_stream({"id": "s1"}, frame_timeout=0.05):
            pass

    hang.set()
    task.cancel()


@pytest.mark.anyio
async def test_pool_render_stream_yields_frames(tmp_path: Path) -> None:
    project_root = tmp_path / "p"
    client_root = project_root / ".pyxle-build" / "client"
    client_root.mkdir(parents=True)
    component = client_root / "pages" / "page.jsx"
    component.parent.mkdir(parents=True)
    component.touch()

    proc = _streaming_proc(
        lambda i: [{"id": i, "type": "chunk", "html": "<main>"}, {"id": i, "type": "end"}]
    )
    with (
        patch("pyxle.ssr.worker_pool.shutil.which", return_value="/usr/bin/node"),
        patch(
            "pyxle.ssr.worker_pool.asyncio.create_subprocess_exec",
            new_callable=AsyncMock,
            return_value=proc,
        ),
        patch("pyxle.ssr.worker_pool.Path.exists", return_value=True),
    ):
        pool = SsrWorkerPool(size=1, project_root=project_root, client_root=client_root)
        await pool.start()
        frames = [frame async for frame in pool.render_stream(component, {"x": 1})]
        assert [f["type"] for f in frames] == ["chunk", "end"]
        written = json.loads(proc.stdin.write.call_args_list[0][0][0].decode().strip())
        assert written["stream"] is True  # the streaming request carried the flag
        await pool.stop()


# --- Real-Node integration: exercise renderToPipeableStream end to end ------


def _real_project(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Create a project tree with node_modules for a real-Node render."""
    project_root = tmp_path / "project"
    client_root = project_root / ".pyxle-build" / "client"
    pages = client_root / "pages"
    pages.mkdir(parents=True)
    ensure_test_node_modules(project_root)
    return project_root, client_root, pages


@pytest.mark.anyio
@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is required for SSR streaming")
async def test_render_stream_real_node_plain_component(tmp_path: Path) -> None:
    project_root, client_root, pages = _real_project(tmp_path)
    component = pages / "plain.jsx"
    component.write_text(
        dedent(
            """
            import React from 'react';

            export default function Plain({ name }) {
                return <main data-testid="plain">Hello {name}</main>;
            }
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )

    pool = SsrWorkerPool(size=1, project_root=project_root, client_root=client_root)
    await pool.start()
    try:
        frames = [f async for f in pool.render_stream(component, {"name": "Ada"})]
    finally:
        await pool.stop()

    assert frames[-1]["type"] == "end"
    assert "styles" in frames[-1] and "headElements" in frames[-1]
    html = "".join(f["html"] for f in frames if f["type"] == "chunk")
    assert 'data-testid="plain"' in html
    # React separates static text from an interpolated value with a comment
    # text-boundary marker, so the rendered form is ``Hello <!-- -->Ada``.
    assert "Hello" in html and "Ada</main>" in html


@pytest.mark.anyio
@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is required for SSR streaming")
async def test_render_stream_real_node_suspense_streams_multiple_chunks(tmp_path: Path) -> None:
    project_root, client_root, pages = _real_project(tmp_path)
    component = pages / "suspense.jsx"
    # A boundary that suspends once (module-level latch) then resolves, so the
    # shell flushes with the fallback first and the resolved content streams in
    # a later frame — the behaviour that distinguishes streaming from buffered.
    component.write_text(
        dedent(
            """
            import React, { Suspense } from 'react';

            let _done = false;
            let _promise;
            function suspendOnce() {
                if (_done) return;
                if (!_promise) {
                    _promise = new Promise((resolve) => {
                        setTimeout(() => { _done = true; resolve(); }, 15);
                    });
                }
                throw _promise;
            }

            function Slow() {
                suspendOnce();
                return <p data-testid="slow">streamed-content</p>;
            }

            export default function Page() {
                return (
                    <main data-testid="shell">
                        <h1>Shell Ready</h1>
                        <Suspense fallback={<p data-testid="fallback">loading-fallback</p>}>
                            <Slow />
                        </Suspense>
                    </main>
                );
            }
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )

    pool = SsrWorkerPool(size=1, project_root=project_root, client_root=client_root)
    await pool.start()
    try:
        frames = [f async for f in pool.render_stream(component, {})]
    finally:
        await pool.stop()

    types = [f["type"] for f in frames]
    assert types[-1] == "end"
    chunks = [f for f in frames if f["type"] == "chunk"]
    # Shell + resolved boundary arrive as separate frames.
    assert len(chunks) >= 2
    html = "".join(f["html"] for f in chunks)
    assert "Shell Ready" in html
    assert "loading-fallback" in html  # the fallback streamed in the shell
    assert "streamed-content" in html  # the boundary resolved and streamed later
