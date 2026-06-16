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
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pyxle.ssr.worker_pool import SsrWorkerPool, WorkerPoolError, _WorkerState


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
