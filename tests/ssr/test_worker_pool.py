"""Tests for pyxle.ssr.worker_pool — the persistent Node.js SSR worker pool."""

from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pyxle.ssr.worker_pool import SsrWorkerPool, WorkerPoolError, _WorkerState
from tests.ssr.utils import ensure_test_node_modules


@pytest.fixture
def anyio_backend() -> str:  # pragma: no cover - fixture wiring
    return "asyncio"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_mock_process(responses: list[dict[str, Any]] | None = None) -> MagicMock:
    """Return a fake asyncio subprocess whose stdout yields the given NDJSON responses.

    The mock provides both ``readline()`` (legacy) and ``read()`` (current)
    so that the worker pool's chunk-based ``read_loop`` works correctly.
    """
    proc = MagicMock()
    proc.stdin = MagicMock()
    proc.stdin.is_closing.return_value = False
    proc.stdin.write = MagicMock()
    proc.stdin.drain = AsyncMock()
    proc.stdin.close = MagicMock()

    # Build the full byte stream: each response as a JSON line, then EOF.
    stream = b""
    for r in responses or []:
        stream += (json.dumps(r) + "\n").encode()

    read_offset = [0]

    async def _read(n: int = -1) -> bytes:
        """Return up to *n* bytes from the stream, then b'' for EOF."""
        if read_offset[0] >= len(stream):
            await asyncio.sleep(0)
            return b""
        end = len(stream) if n < 0 else min(read_offset[0] + n, len(stream))
        chunk = stream[read_offset[0]:end]
        read_offset[0] = end
        return chunk

    # Also keep readline for any test that calls it directly.
    lines: list[bytes] = []
    for r in responses or []:
        lines.append((json.dumps(r) + "\n").encode())
    lines.append(b"")
    line_idx = [0]

    async def _readline() -> bytes:
        idx = line_idx[0]
        if idx < len(lines):
            line_idx[0] += 1
            return lines[idx]
        await asyncio.sleep(0)
        return b""

    proc.stdout = MagicMock()
    proc.stdout.read = _read
    proc.stdout.readline = _readline

    async def _wait():
        return 0

    proc.wait = _wait
    proc.kill = MagicMock()
    return proc


# ---------------------------------------------------------------------------
# _WorkerState unit tests
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_worker_state_read_loop_resolves_pending_future() -> None:
    """read_loop should resolve the matching future when a response arrives."""
    response = {"id": "test-id", "ok": True, "html": "<h1>hi</h1>"}
    proc = _make_mock_process([response])

    worker = _WorkerState(process=proc)
    task = asyncio.create_task(worker.read_loop())

    loop = asyncio.get_running_loop()
    future: asyncio.Future[dict[str, Any]] = loop.create_future()
    worker.pending["test-id"] = future

    await task
    assert future.done()
    result = future.result()
    assert result["html"] == "<h1>hi</h1>"
    assert result["ok"] is True


@pytest.mark.anyio
async def test_worker_state_read_loop_marks_dead_on_eof() -> None:
    """read_loop should set alive=False when stdout closes (EOF)."""
    proc = _make_mock_process([])  # no responses, immediate EOF
    worker = _WorkerState(process=proc)
    task = asyncio.create_task(worker.read_loop())
    await task
    assert worker.alive is False


@pytest.mark.anyio
async def test_worker_state_read_loop_fails_pending_on_eof() -> None:
    """Pending futures should receive WorkerPoolError when the worker exits unexpectedly."""
    proc = _make_mock_process([])  # EOF before any response
    worker = _WorkerState(process=proc)

    loop = asyncio.get_running_loop()
    future: asyncio.Future[dict[str, Any]] = loop.create_future()
    worker.pending["orphan"] = future

    await worker.read_loop()

    assert future.done()
    with pytest.raises(WorkerPoolError, match="terminated unexpectedly"):
        future.result()


@pytest.mark.anyio
async def test_worker_state_send_writes_ndjson() -> None:
    """send() should write the request as a newline-terminated JSON line."""
    proc = _make_mock_process([{"id": "req-1", "ok": True, "html": "<p/>"}])
    worker = _WorkerState(process=proc)
    # Start reader in background
    task = asyncio.create_task(worker.read_loop())

    result = await worker.send({"id": "req-1", "componentPath": "/a.jsx", "props": {}})
    await task

    written_data = proc.stdin.write.call_args[0][0]
    decoded = json.loads(written_data.decode().strip())
    assert decoded["id"] == "req-1"
    assert result["ok"] is True


@pytest.mark.anyio
async def test_worker_state_send_raises_on_drain_failure() -> None:
    """send() should raise WorkerPoolError and mark alive=False if drain fails."""
    proc = _make_mock_process()
    proc.stdin.drain = AsyncMock(side_effect=BrokenPipeError("pipe closed"))
    worker = _WorkerState(process=proc)

    with pytest.raises(WorkerPoolError, match="stdin closed"):
        await worker.send({"id": "x", "componentPath": "/b.jsx", "props": {}})

    assert worker.alive is False


@pytest.mark.anyio
async def test_worker_state_send_future_already_cancelled_on_drain_error() -> None:
    """send() should still mark alive=False even if future.cancel() is a no-op."""
    proc = _make_mock_process()
    proc.stdin.drain = AsyncMock(side_effect=ConnectionResetError("reset"))
    worker = _WorkerState(process=proc)

    with pytest.raises(WorkerPoolError):
        await worker.send({"id": "already-cancelled", "componentPath": "/c.jsx", "props": {}})

    assert worker.alive is False


@pytest.mark.anyio
async def test_worker_state_read_loop_ignores_response_with_unknown_id() -> None:
    """read_loop should skip responses whose id is not in pending."""
    response = {"id": "not-in-pending", "ok": True, "html": "<p/>"}
    proc = _make_mock_process([response])

    worker = _WorkerState(process=proc)
    # No futures in pending — the response should be silently discarded
    await worker.read_loop()
    assert not worker.pending  # nothing was added or crashed


@pytest.mark.anyio
async def test_worker_state_read_loop_skips_already_done_future() -> None:
    """read_loop should not double-set a future that's already done."""
    proc = _make_mock_process([{"id": "done", "ok": True, "html": "x"}])
    worker = _WorkerState(process=proc)

    loop = asyncio.get_running_loop()
    future: asyncio.Future[dict] = loop.create_future()
    future.set_result({"already": "set"})  # mark it done before the response
    worker.pending["done"] = future

    await worker.read_loop()
    # Future should still have the original result (not overwritten)
    assert future.result() == {"already": "set"}


@pytest.mark.anyio
async def test_worker_state_read_loop_handles_exception_in_readline() -> None:
    """read_loop should handle an exception from stdout.read and mark worker dead."""
    proc = MagicMock()
    proc.stdin = MagicMock()
    proc.stdout = MagicMock()
    proc.stdout.read = AsyncMock(side_effect=RuntimeError("stream broken"))
    proc.wait = AsyncMock(return_value=1)
    proc.kill = MagicMock()

    worker = _WorkerState(process=proc)
    loop = asyncio.get_running_loop()
    future: asyncio.Future[dict] = loop.create_future()
    worker.pending["orphan"] = future

    await worker.read_loop()

    assert worker.alive is False
    assert future.done()
    with pytest.raises(WorkerPoolError):
        future.result()


@pytest.mark.anyio
async def test_worker_state_stop_when_stdin_already_closing() -> None:
    """stop() should handle stdin that is already closing."""
    proc = _make_mock_process()
    proc.stdin.is_closing.return_value = True  # already closing
    worker = _WorkerState(process=proc, alive=True)
    await worker.stop()
    # close() should NOT be called since is_closing() is True
    proc.stdin.close.assert_not_called()
    assert worker.alive is False


@pytest.mark.anyio
async def test_worker_state_stop_when_stdin_close_raises() -> None:
    """stop() should swallow exceptions from stdin.close()."""
    proc = _make_mock_process()
    proc.stdin.is_closing.return_value = False
    proc.stdin.close.side_effect = OSError("already closed")
    worker = _WorkerState(process=proc, alive=True)
    await worker.stop()  # should not raise
    assert worker.alive is False


@pytest.mark.anyio
async def test_worker_state_stop_kills_on_timeout() -> None:
    """stop() should kill the process if it does not exit within the timeout."""
    import pyxle.ssr.worker_pool as pool_module

    proc = MagicMock()
    proc.stdin = MagicMock()
    proc.stdin.is_closing.return_value = False
    proc.stdin.close = MagicMock()

    original_timeout = pool_module._WORKER_STOP_TIMEOUT

    async def _never_exits():
        await asyncio.sleep(9999)

    proc.wait = _never_exits
    proc.kill = MagicMock()

    worker = _WorkerState(process=proc)
    try:
        pool_module._WORKER_STOP_TIMEOUT = 0.05
        await worker.stop()
    finally:
        pool_module._WORKER_STOP_TIMEOUT = original_timeout

    proc.kill.assert_called_once()


@pytest.mark.anyio
async def test_worker_state_stop_closes_stdin_and_waits() -> None:
    """stop() should close stdin and await process exit."""
    proc = _make_mock_process()
    worker = _WorkerState(process=proc, alive=True)
    await worker.stop()
    proc.stdin.close.assert_called_once()
    assert worker.alive is False


@pytest.mark.anyio
async def test_worker_state_read_loop_skips_non_json_lines() -> None:
    """read_loop should silently skip lines that are not valid JSON."""
    # Mix of a bad line and a good response — delivered as a single byte stream.
    stream = b"not-json\n" + (json.dumps({"id": "abc", "ok": True, "html": "x"}) + "\n").encode()

    proc = MagicMock()
    proc.stdin = MagicMock()
    offset = [0]

    async def _read(n: int = -1) -> bytes:
        if offset[0] >= len(stream):
            return b""
        end = len(stream) if n < 0 else min(offset[0] + n, len(stream))
        chunk = stream[offset[0]:end]
        offset[0] = end
        return chunk

    proc.stdout = MagicMock()
    proc.stdout.read = _read
    proc.wait = AsyncMock(return_value=0)
    proc.kill = MagicMock()

    worker = _WorkerState(process=proc)
    loop = asyncio.get_running_loop()
    future: asyncio.Future[dict] = loop.create_future()
    worker.pending["abc"] = future

    await worker.read_loop()
    assert future.done()
    assert future.result()["ok"] is True


# ---------------------------------------------------------------------------
# SsrWorkerPool unit tests (subprocess mocked)
# ---------------------------------------------------------------------------


@pytest.fixture
def pool_paths(tmp_path: Path):
    """Create minimal directory structure expected by the pool."""
    project_root = tmp_path / "project"
    client_root = project_root / ".pyxle-build" / "client"
    project_root.mkdir(parents=True)
    client_root.mkdir(parents=True)
    # Create a stub ssr_worker.mjs so the pool doesn't raise FileNotFoundError
    (Path(__file__).parent.parent.parent / "pyxle" / "ssr" / "ssr_worker.mjs").touch(exist_ok=True)
    return project_root, client_root


@pytest.mark.anyio
async def test_pool_start_spawns_workers(tmp_path: Path) -> None:
    """SsrWorkerPool.start() should spawn the configured number of workers."""
    project_root = tmp_path / "project"
    client_root = project_root / ".pyxle-build" / "client"
    project_root.mkdir(parents=True)
    client_root.mkdir(parents=True)

    mock_proc = _make_mock_process()

    with (
        patch("pyxle.ssr.worker_pool.shutil.which", return_value="/usr/bin/node"),
        patch(
            "pyxle.ssr.worker_pool.asyncio.create_subprocess_exec",
            new_callable=AsyncMock,
            return_value=mock_proc,
        ) as mock_spawn,
        patch(
            "pyxle.ssr.worker_pool.Path.exists",
            return_value=True,
        ),
    ):
        pool = SsrWorkerPool(size=3, project_root=project_root, client_root=client_root)
        await pool.start()

        assert mock_spawn.call_count == 3
        assert pool.alive_count == 3
        await pool.stop()


@pytest.mark.anyio
async def test_pool_start_idempotent(tmp_path: Path) -> None:
    """start() called twice should only spawn workers once."""
    project_root = tmp_path / "project"
    client_root = project_root / ".pyxle-build" / "client"
    project_root.mkdir(parents=True)
    client_root.mkdir(parents=True)

    mock_proc = _make_mock_process()

    with (
        patch("pyxle.ssr.worker_pool.shutil.which", return_value="/usr/bin/node"),
        patch(
            "pyxle.ssr.worker_pool.asyncio.create_subprocess_exec",
            new_callable=AsyncMock,
            return_value=mock_proc,
        ) as mock_spawn,
        patch("pyxle.ssr.worker_pool.Path.exists", return_value=True),
    ):
        pool = SsrWorkerPool(size=2, project_root=project_root, client_root=client_root)
        await pool.start()
        await pool.start()  # second call must be no-op

        assert mock_spawn.call_count == 2  # spawned only once
        await pool.stop()


@pytest.mark.anyio
async def test_pool_raises_when_node_missing(tmp_path: Path) -> None:
    """start() should raise WorkerPoolError when node is not installed."""
    project_root = tmp_path / "project"
    project_root.mkdir()
    client_root = project_root / ".pyxle-build" / "client"
    client_root.mkdir(parents=True)

    with patch("pyxle.ssr.worker_pool.shutil.which", return_value=None):
        pool = SsrWorkerPool(size=1, project_root=project_root, client_root=client_root)
        with pytest.raises(WorkerPoolError, match="Node.js executable not found"):
            await pool.start()


@pytest.mark.anyio
async def test_pool_raises_when_script_missing(tmp_path: Path) -> None:
    """start() should raise WorkerPoolError when ssr_worker.mjs is absent."""
    project_root = tmp_path / "project"
    project_root.mkdir()
    client_root = project_root / ".pyxle-build" / "client"
    client_root.mkdir(parents=True)

    with (
        patch("pyxle.ssr.worker_pool.shutil.which", return_value="/usr/bin/node"),
        patch("pyxle.ssr.worker_pool.Path.exists", return_value=False),
    ):
        pool = SsrWorkerPool(size=1, project_root=project_root, client_root=client_root)
        with pytest.raises(WorkerPoolError, match="SSR worker script not found"):
            await pool.start()


@pytest.mark.anyio
async def test_pool_render_dispatches_request(tmp_path: Path) -> None:
    """render() should dispatch to a worker and parse the response."""
    project_root = tmp_path / "project"
    client_root = project_root / ".pyxle-build" / "client"
    project_root.mkdir(parents=True)
    client_root.mkdir(parents=True)
    component = client_root / "pages" / "index.jsx"
    component.parent.mkdir(parents=True)
    component.touch()

    # Worker responds with success
    response_template: dict[str, Any] = {
        "ok": True,
        "html": "<div>hello</div>",
        "styles": [],
        "headElements": [],
    }

    responses: list[bytes] = []

    async def fake_read(n: int = -1) -> bytes:
        await asyncio.sleep(0)
        if responses:
            return responses.pop(0)
        return b""

    mock_proc = MagicMock()
    mock_proc.stdin = MagicMock()
    mock_proc.stdin.is_closing.return_value = False
    mock_proc.stdin.write = MagicMock()
    mock_proc.stdin.close = MagicMock()
    mock_proc.stdout = MagicMock()
    mock_proc.stdout.read = fake_read
    mock_proc.wait = AsyncMock(return_value=0)
    mock_proc.kill = MagicMock()

    # Intercept stdin.write to capture the request id and queue a response
    def capture_write(data: bytes) -> None:
        payload = json.loads(data.decode().strip())
        resp = dict(response_template)
        resp["id"] = payload["id"]
        responses.append((json.dumps(resp) + "\n").encode())

    mock_proc.stdin.write.side_effect = capture_write
    mock_proc.stdin.drain = AsyncMock()

    with (
        patch("pyxle.ssr.worker_pool.shutil.which", return_value="/usr/bin/node"),
        patch(
            "pyxle.ssr.worker_pool.asyncio.create_subprocess_exec",
            new_callable=AsyncMock,
            return_value=mock_proc,
        ),
        patch("pyxle.ssr.worker_pool.Path.exists", return_value=True),
    ):
        pool = SsrWorkerPool(size=1, project_root=project_root, client_root=client_root)
        await pool.start()
        result = await pool.render(component, {"title": "Hello"})
        assert result["ok"] is True
        assert result["html"] == "<div>hello</div>"
        await pool.stop()


@pytest.mark.anyio
async def test_pool_render_includes_request_pathname(tmp_path: Path) -> None:
    """request_pathname kwarg propagates to the worker payload."""
    project_root = tmp_path / "project"
    client_root = project_root / ".pyxle-build" / "client"
    project_root.mkdir(parents=True)
    client_root.mkdir(parents=True)
    component = client_root / "pages" / "page.jsx"
    component.parent.mkdir(parents=True)
    component.touch()

    captured_payloads: list[dict[str, Any]] = []
    # A real worker's stdout blocks until it emits a line and only returns EOF
    # when the process exits. Model that with a queue (instead of returning b""
    # when idle) so the read loop can't observe a premature EOF and mark the
    # worker dead before the first render — a race that surfaced only under the
    # asyncio scheduling on Python 3.10/3.11. stop() cancels the reader task, so
    # leaving the queue empty at teardown is fine.
    read_queue: asyncio.Queue = asyncio.Queue()

    async def fake_read(n: int = -1) -> bytes:
        return await read_queue.get()

    mock_proc = MagicMock()
    mock_proc.stdin = MagicMock()
    mock_proc.stdin.is_closing.return_value = False
    mock_proc.stdin.close = MagicMock()
    mock_proc.stdout = MagicMock()
    mock_proc.stdout.read = fake_read
    mock_proc.wait = AsyncMock(return_value=0)
    mock_proc.kill = MagicMock()

    def capture_write(data: bytes) -> None:
        payload = json.loads(data.decode().strip())
        captured_payloads.append(payload)
        read_queue.put_nowait(
            (json.dumps({"id": payload["id"], "ok": True, "html": "<x/>", "styles": [], "headElements": []}) + "\n").encode()
        )

    mock_proc.stdin.write = MagicMock(side_effect=capture_write)
    mock_proc.stdin.drain = AsyncMock()

    with (
        patch("pyxle.ssr.worker_pool.shutil.which", return_value="/usr/bin/node"),
        patch(
            "pyxle.ssr.worker_pool.asyncio.create_subprocess_exec",
            new_callable=AsyncMock,
            return_value=mock_proc,
        ),
        patch("pyxle.ssr.worker_pool.Path.exists", return_value=True),
    ):
        pool = SsrWorkerPool(size=1, project_root=project_root, client_root=client_root)
        await pool.start()
        await pool.render(component, {}, request_pathname="/dashboard")
        await pool.render(component, {})  # None should omit the key entirely
        await pool.stop()

    assert captured_payloads[0]["requestPathname"] == "/dashboard"
    assert "requestPathname" not in captured_payloads[1]


@pytest.mark.anyio
async def test_pool_render_auto_starts_if_not_started(tmp_path: Path) -> None:
    """render() should auto-start the pool if start() was not called explicitly."""
    project_root = tmp_path / "project"
    client_root = project_root / ".pyxle-build" / "client"
    project_root.mkdir(parents=True)
    client_root.mkdir(parents=True)
    component = client_root / "pages" / "x.jsx"
    component.parent.mkdir(parents=True)
    component.touch()

    responses: list[bytes] = []

    async def fake_read(n: int = -1) -> bytes:
        await asyncio.sleep(0)
        if responses:
            return responses.pop(0)
        return b""

    mock_proc = MagicMock()
    mock_proc.stdin = MagicMock()
    mock_proc.stdin.is_closing.return_value = False
    mock_proc.stdin.write = MagicMock()
    mock_proc.stdin.close = MagicMock()
    mock_proc.stdout = MagicMock()
    mock_proc.stdout.read = fake_read
    mock_proc.wait = AsyncMock(return_value=0)
    mock_proc.kill = MagicMock()

    def capture_write(data: bytes) -> None:
        payload = json.loads(data.decode().strip())
        responses.append((json.dumps({"id": payload["id"], "ok": True, "html": "<b/>", "styles": [], "headElements": []}) + "\n").encode())

    mock_proc.stdin.write.side_effect = capture_write
    mock_proc.stdin.drain = AsyncMock()

    with (
        patch("pyxle.ssr.worker_pool.shutil.which", return_value="/usr/bin/node"),
        patch(
            "pyxle.ssr.worker_pool.asyncio.create_subprocess_exec",
            new_callable=AsyncMock,
            return_value=mock_proc,
        ),
        patch("pyxle.ssr.worker_pool.Path.exists", return_value=True),
    ):
        pool = SsrWorkerPool(size=1, project_root=project_root, client_root=client_root)
        assert not pool._started
        result = await pool.render(component, {})
        assert pool._started
        assert result["ok"] is True
        await pool.stop()


@pytest.mark.anyio
async def test_pool_round_robin_dispatch(tmp_path: Path) -> None:
    """_pick_worker should cycle across workers in round-robin order."""
    project_root = tmp_path / "project"
    client_root = project_root / ".pyxle-build" / "client"
    project_root.mkdir(parents=True)
    client_root.mkdir(parents=True)

    w1 = MagicMock()
    w1.alive = True
    w1.in_flight = 0
    w2 = MagicMock()
    w2.alive = True
    w2.in_flight = 0
    w3 = MagicMock()
    w3.alive = True
    w3.in_flight = 0

    pool = SsrWorkerPool(size=3, project_root=project_root, client_root=client_root)
    pool._workers = [w1, w2, w3]

    # All idle (equal in_flight) -> ties broken round-robin, so the classic cycle.
    picked = [pool._pick_worker() for _ in range(6)]
    assert picked == [w1, w2, w3, w1, w2, w3]


@pytest.mark.anyio
async def test_pool_pick_worker_skips_dead_workers(tmp_path: Path) -> None:
    """_pick_worker should only return alive workers."""
    project_root = tmp_path / "project"
    client_root = project_root / ".pyxle-build" / "client"
    project_root.mkdir(parents=True)
    client_root.mkdir(parents=True)

    w_dead = MagicMock()
    w_dead.alive = False
    w_dead.in_flight = 0
    w_alive = MagicMock()
    w_alive.alive = True
    w_alive.in_flight = 0

    pool = SsrWorkerPool(size=2, project_root=project_root, client_root=client_root)
    pool._workers = [w_dead, w_alive]

    for _ in range(5):
        assert pool._pick_worker() is w_alive


@pytest.mark.anyio
async def test_pool_pick_worker_returns_none_when_all_dead(tmp_path: Path) -> None:
    """_pick_worker should return None when all workers are dead."""
    project_root = tmp_path / "project"
    client_root = project_root / ".pyxle-build" / "client"
    project_root.mkdir(parents=True)
    client_root.mkdir(parents=True)

    pool = SsrWorkerPool(size=1, project_root=project_root, client_root=client_root)
    pool._workers = []

    assert pool._pick_worker() is None


@pytest.mark.anyio
async def test_pool_render_raises_when_no_workers(tmp_path: Path) -> None:
    """render() should raise WorkerPoolError if no alive workers exist."""
    project_root = tmp_path / "project"
    client_root = project_root / ".pyxle-build" / "client"
    project_root.mkdir(parents=True)
    client_root.mkdir(parents=True)
    component = client_root / "x.jsx"
    component.touch()

    with patch("pyxle.ssr.worker_pool.shutil.which", return_value="/usr/bin/node"), \
         patch("pyxle.ssr.worker_pool.Path.exists", return_value=True):
        pool = SsrWorkerPool(size=1, project_root=project_root, client_root=client_root)
        pool._started = True  # pretend started
        pool._workers = []   # but no workers

        with pytest.raises(WorkerPoolError, match="No healthy SSR workers"):
            await pool.render(component, {})


@pytest.mark.anyio
async def test_pool_render_removes_dead_worker_and_replenishes(tmp_path: Path) -> None:
    """When render raises WorkerPoolError, the dead worker is removed and replenish is scheduled."""
    project_root = tmp_path / "project"
    client_root = project_root / ".pyxle-build" / "client"
    project_root.mkdir(parents=True)
    client_root.mkdir(parents=True)
    component = client_root / "dead.jsx"
    component.touch()

    dying_worker = MagicMock()
    dying_worker.alive = True
    dying_worker.send = AsyncMock(side_effect=WorkerPoolError("worker crashed"))

    pool = SsrWorkerPool(size=1, project_root=project_root, client_root=client_root)
    pool._started = True
    pool._workers = [dying_worker]

    # Mock _replenish to prevent it from spawning real subprocesses as a background task
    replenish_calls: list[None] = []

    async def fake_replenish() -> None:
        replenish_calls.append(None)

    pool._replenish = fake_replenish  # type: ignore[method-assign]

    with pytest.raises(WorkerPoolError, match="worker crashed"):
        await pool.render(component, {})

    # dying worker should have been removed from the pool
    assert dying_worker not in pool._workers
    # Give the event loop a chance to run the replenish task
    await asyncio.sleep(0)


@pytest.mark.anyio
async def test_pool_replenish_adds_workers_up_to_size(tmp_path: Path) -> None:
    """_replenish should create new workers to fill the configured pool size."""
    project_root = tmp_path / "project"
    client_root = project_root / ".pyxle-build" / "client"
    project_root.mkdir(parents=True)
    client_root.mkdir(parents=True)

    mock_proc = _make_mock_process()

    with (
        patch("pyxle.ssr.worker_pool.shutil.which", return_value="/usr/bin/node"),
        patch(
            "pyxle.ssr.worker_pool.asyncio.create_subprocess_exec",
            new_callable=AsyncMock,
            return_value=mock_proc,
        ) as mock_spawn,
        patch("pyxle.ssr.worker_pool.Path.exists", return_value=True),
    ):
        pool = SsrWorkerPool(size=2, project_root=project_root, client_root=client_root)
        pool._started = True
        # Only 1 of 2 workers is alive
        dead_worker = MagicMock()
        dead_worker.alive = False
        alive_worker = MagicMock()
        alive_worker.alive = True
        pool._workers = [dead_worker, alive_worker]

        await pool._replenish()

        # Should have spawned exactly 1 replacement (deficit = 2 - 1 = 1)
        assert mock_spawn.call_count == 1


@pytest.mark.anyio
async def test_pool_replenish_handles_spawn_failure(tmp_path: Path) -> None:
    """_replenish should log and continue if spawning a worker fails."""
    project_root = tmp_path / "project"
    client_root = project_root / ".pyxle-build" / "client"
    project_root.mkdir(parents=True)
    client_root.mkdir(parents=True)

    with (
        patch("pyxle.ssr.worker_pool.shutil.which", return_value="/usr/bin/node"),
        patch(
            "pyxle.ssr.worker_pool.asyncio.create_subprocess_exec",
            new_callable=AsyncMock,
            side_effect=OSError("spawn failed"),
        ),
        patch("pyxle.ssr.worker_pool.Path.exists", return_value=True),
    ):
        pool = SsrWorkerPool(size=2, project_root=project_root, client_root=client_root)
        pool._started = True
        pool._workers = []

        # Should not raise — spawn failures are logged and swallowed
        await pool._replenish()
        assert pool._workers == []


@pytest.mark.anyio
async def test_pool_size_property(tmp_path: Path) -> None:
    """size property should return configured pool size."""
    pool = SsrWorkerPool(size=4, project_root=tmp_path, client_root=tmp_path)
    assert pool.size == 4


@pytest.mark.anyio
async def test_pool_size_minimum_one(tmp_path: Path) -> None:
    """Pool size must be at least 1 even if 0 is passed."""
    pool = SsrWorkerPool(size=0, project_root=tmp_path, client_root=tmp_path)
    assert pool.size == 1


@pytest.mark.anyio
async def test_pool_stop_is_safe_when_not_started(tmp_path: Path) -> None:
    """stop() on an unstarted pool should not raise."""
    pool = SsrWorkerPool(size=1, project_root=tmp_path, client_root=tmp_path)
    await pool.stop()  # should not raise


# ---------------------------------------------------------------------------
# pool_render_factory integration tests
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_pool_render_factory_success() -> None:
    """pool_render_factory should produce a factory that renders via the pool."""
    from pyxle.ssr.renderer import ComponentRenderer, pool_render_factory

    mock_pool = AsyncMock()
    mock_pool.render = AsyncMock(
        return_value={
            "ok": True,
            "html": "<main>ok</main>",
            "styles": [],
            "headElements": [],
        }
    )

    renderer = ComponentRenderer(factory=pool_render_factory(mock_pool))
    component = Path("/fake/.pyxle-build/client/pages/index.jsx")

    result = await renderer.render(component, {"x": 1})
    assert result.html == "<main>ok</main>"
    assert result.inline_styles == ()
    assert result.head_elements == ()
    mock_pool.render.assert_awaited_once()


@pytest.mark.anyio
async def test_pool_render_factory_propagates_error() -> None:
    """pool_render_factory should raise ComponentRenderError on pool failure."""
    from pyxle.ssr.renderer import ComponentRenderError, ComponentRenderer, pool_render_factory
    from pyxle.ssr.worker_pool import WorkerPoolError

    mock_pool = AsyncMock()
    mock_pool.render = AsyncMock(side_effect=WorkerPoolError("worker died"))

    renderer = ComponentRenderer(factory=pool_render_factory(mock_pool))
    component = Path("/fake/.pyxle-build/client/pages/index.jsx")

    with pytest.raises(ComponentRenderError, match="worker died"):
        await renderer.render(component, {})


@pytest.mark.anyio
async def test_pool_render_factory_raises_on_bad_html() -> None:
    """pool_render_factory should raise ComponentRenderError if html is missing."""
    from pyxle.ssr.renderer import ComponentRenderError, ComponentRenderer, pool_render_factory

    mock_pool = AsyncMock()
    mock_pool.render = AsyncMock(return_value={"ok": True, "html": None})

    renderer = ComponentRenderer(factory=pool_render_factory(mock_pool))
    component = Path("/fake/.pyxle-build/client/pages/index.jsx")

    with pytest.raises(ComponentRenderError, match="malformed HTML payload"):
        await renderer.render(component, {})


@pytest.mark.anyio
async def test_pool_render_factory_raises_on_worker_reported_failure() -> None:
    """pool_render_factory should raise ComponentRenderError when ok=False."""
    from pyxle.ssr.renderer import ComponentRenderError, ComponentRenderer, pool_render_factory

    mock_pool = AsyncMock()
    mock_pool.render = AsyncMock(return_value={"ok": False, "message": "Component crashed"})

    renderer = ComponentRenderer(factory=pool_render_factory(mock_pool))
    component = Path("/fake/.pyxle-build/client/pages/index.jsx")

    with pytest.raises(ComponentRenderError, match="Component crashed"):
        await renderer.render(component, {})


@pytest.mark.anyio
async def test_pool_render_factory_raises_on_unserializable_props() -> None:
    """pool_render_factory should raise ComponentRenderError for non-JSON props."""
    from pyxle.ssr.renderer import ComponentRenderError, ComponentRenderer, pool_render_factory

    mock_pool = AsyncMock()
    renderer = ComponentRenderer(factory=pool_render_factory(mock_pool))
    component = Path("/fake/.pyxle-build/client/pages/index.jsx")

    with pytest.raises(ComponentRenderError, match="serialize props"):
        await renderer.render(component, {"bad": object()})


# ---------------------------------------------------------------------------
# settings.py integration
# ---------------------------------------------------------------------------


def test_devserver_settings_has_ssr_workers_field() -> None:
    """DevServerSettings must expose ssr_workers with a default of 1."""
    from pyxle.devserver.settings import DevServerSettings

    settings = DevServerSettings.from_project_root("/tmp")
    assert hasattr(settings, "ssr_workers")
    assert settings.ssr_workers == 1


def test_devserver_settings_ssr_workers_zero() -> None:
    """ssr_workers=0 should be preserved (disables pool mode)."""
    from pyxle.devserver.settings import DevServerSettings

    settings = DevServerSettings.from_project_root("/tmp", ssr_workers=0)
    assert settings.ssr_workers == 0


def test_devserver_settings_to_dict_includes_ssr_workers() -> None:
    """to_dict() should include ssr_workers for observability."""
    from pyxle.devserver.settings import DevServerSettings

    settings = DevServerSettings.from_project_root("/tmp", ssr_workers=2)
    d = settings.to_dict()
    assert d["ssr_workers"] == 2


# ---------------------------------------------------------------------------
# Node.js integration test (skipped if node is not available)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js required for SSR worker integration test")
async def test_worker_script_exists_and_is_valid_mjs() -> None:
    """ssr_worker.mjs must exist and be importable as ESM by Node.js."""
    import asyncio

    script = Path(__file__).parent.parent.parent / "pyxle" / "ssr" / "ssr_worker.mjs"
    assert script.exists(), "ssr_worker.mjs not found in pyxle/ssr/"

    # Just check Node can parse the syntax (--input-type=module + check-syntax not available
    # in all Node versions; use --check flag via node -e "import('./x')" would start the
    # event loop. Instead, verify the process starts and waits for stdin (doesn't exit immediately).
    proc = await asyncio.create_subprocess_exec(
        "node",
        str(script),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    # Send EOF immediately
    assert proc.stdin is not None
    proc.stdin.close()
    try:
        returncode = await asyncio.wait_for(proc.wait(), timeout=10.0)
    except asyncio.TimeoutError:
        proc.kill()
        pytest.fail("ssr_worker.mjs did not exit after stdin was closed")

    assert returncode == 0, f"ssr_worker.mjs exited with code {returncode}"


@pytest.mark.anyio
@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js required for SSR worker integration test")
async def test_worker_substitutes_public_env_during_ssr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The persistent worker bakes ``import.meta.env.PYXLE_PUBLIC_*`` (F4).

    The pool forwards ``PYXLE_PUBLIC_*`` to the Node process; ``ssr_worker.mjs``
    must add the matching esbuild ``define`` so server render agrees with the
    Vite-substituted client bundle instead of emitting ``undefined``.
    """
    monkeypatch.setenv("PYXLE_PUBLIC_BRAND", "Pulse")

    project_root = tmp_path / "project"
    client_root = project_root / ".pyxle-build" / "client"
    component = client_root / "pages" / "env.jsx"
    component.parent.mkdir(parents=True, exist_ok=True)
    component.write_text(
        (
            "import React from 'react';\n"
            "export default function EnvProbe() {\n"
            "  return <span data-brand={import.meta.env.PYXLE_PUBLIC_BRAND}>ok</span>;\n"
            "}\n"
        ),
        encoding="utf-8",
    )

    ensure_test_node_modules(project_root)

    pool = SsrWorkerPool(size=1, project_root=project_root, client_root=client_root)
    try:
        result = await pool.render(component, {})
    finally:
        await pool.stop()

    assert result["ok"] is True
    assert 'data-brand="Pulse"' in result["html"]
    assert "undefined" not in result["html"]


# ---------------------------------------------------------------------------
# Invalidation tests
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_pool_invalidate_broadcasts_to_all_workers() -> None:
    """invalidate() should send an invalidation message to every alive worker."""
    proc1 = _make_mock_process([])
    proc2 = _make_mock_process([])

    with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_spawn:
        mock_spawn.side_effect = [proc1, proc2]
        pool = SsrWorkerPool(
            size=2,
            project_root=Path("/tmp/proj"),
            client_root=Path("/tmp/proj/client"),
        )
        await pool.start()

    assert pool.alive_count == 2

    captured1: list[dict] = []
    captured2: list[dict] = []
    _setup_dynamic_invalidation_response(proc1, captured1)
    _setup_dynamic_invalidation_response(proc2, captured2)

    await pool.invalidate()

    # Both workers should have received an invalidation message.
    assert len(captured1) == 1, "Worker 1 did not receive invalidation"
    assert captured1[0].get("type") == "invalidate"
    assert "componentPath" not in captured1[0]

    assert len(captured2) == 1, "Worker 2 did not receive invalidation"
    assert captured2[0].get("type") == "invalidate"

    await pool.stop()


@pytest.mark.anyio
async def test_pool_invalidate_specific_component() -> None:
    """invalidate(path) should include componentPath in the message."""
    proc = _make_mock_process([])
    with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_spawn:
        mock_spawn.return_value = proc
        pool = SsrWorkerPool(
            size=1,
            project_root=Path("/tmp/proj"),
            client_root=Path("/tmp/proj/client"),
        )
        await pool.start()

    captured: list[dict] = []
    _setup_dynamic_invalidation_response(proc, captured)

    target = Path("/tmp/proj/pages/index.jsx")
    await pool.invalidate(component_path=target)

    assert len(captured) == 1
    assert captured[0]["type"] == "invalidate"
    # ASYNC240 flags the blocking ``resolve()``; here it is an assertion on a
    # literal path, not event-loop work, and a thread hop would only obscure
    # what is being compared. Scoped to this line so the rule still guards
    # the rest of the file.
    assert captured[0]["componentPath"] == str(target.resolve())  # noqa: ASYNC240

    await pool.stop()


@pytest.mark.anyio
async def test_pool_invalidate_tolerates_dead_workers() -> None:
    """invalidate() should skip dead workers without raising."""
    proc = _make_mock_process([])
    with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_spawn:
        mock_spawn.return_value = proc
        pool = SsrWorkerPool(
            size=1,
            project_root=Path("/tmp/proj"),
            client_root=Path("/tmp/proj/client"),
        )
        await pool.start()

    # Kill the worker.
    for w in pool._workers:
        w.alive = False

    # Should not raise.
    await pool.invalidate()
    await pool.stop()


@pytest.mark.anyio
async def test_pool_invalidate_before_start_is_noop() -> None:
    """invalidate() on an unstarted pool should be a no-op."""
    pool = SsrWorkerPool(
        size=1,
        project_root=Path("/tmp/proj"),
        client_root=Path("/tmp/proj/client"),
    )
    # Should not raise.
    await pool.invalidate()


def _setup_dynamic_invalidation_response(
    proc: MagicMock,
    captured: list[dict] | None = None,
) -> None:
    """Patch a mock process so that stdin writes with type=invalidate get a
    matching response on stdout.  Optionally appends received messages to *captured*."""
    original_write = proc.stdin.write
    pending_responses: list[bytes] = []

    def _capturing_write(data: bytes) -> None:
        original_write(data)
        try:
            msg = json.loads(data.decode())
            if msg.get("type") == "invalidate":
                if captured is not None:
                    captured.append(msg)
                resp = json.dumps({"id": msg["id"], "ok": True, "invalidated": True}) + "\n"
                pending_responses.append(resp.encode())
        except (json.JSONDecodeError, UnicodeDecodeError):
            pass

    proc.stdin.write = _capturing_write

    async def _read(n: int = -1) -> bytes:
        # Drain pending invalidation responses first.
        if pending_responses:
            return pending_responses.pop(0)
        # Then return EOF.
        await asyncio.sleep(0)
        return b""

    proc.stdout.read = _read


@pytest.mark.anyio
async def test_pool_rerenders_after_source_change(tmp_path: Path) -> None:
    """Regression: a real worker must serve FRESH output after a component's source
    changes and the cache is invalidated.

    The worker writes each (re)bundle to a fixed file named from the entry PATH, so
    re-importing the same file URL returns Node's cached (stale) ESM module even
    after esbuild rewrote it — which froze every ``pyxle dev`` edit (title, body,
    anything SSR'd through the worker pool) until restart. The worker now busts
    Node's import cache with a hash of the bundled output. Requires Node.js.
    """
    project_root = tmp_path / "project"
    client_root = project_root / ".pyxle-build" / "client"
    (client_root / "routes").mkdir(parents=True)
    ensure_test_node_modules(project_root)

    component = client_root / "routes" / "page.jsx"
    component.write_text(
        "import React from 'react';\n"
        "export default function Page() { return <main>VERSION_ALPHA</main>; }\n",
        encoding="utf-8",
    )

    pool = SsrWorkerPool(size=1, project_root=project_root, client_root=client_root)
    try:
        await pool.start()
        first = await pool.render(component, {})
        assert "VERSION_ALPHA" in first["html"]

        # Simulate a hot-reload recompile: SAME path, new content.
        component.write_text(
            "import React from 'react';\n"
            "export default function Page() { return <main>VERSION_BETA</main>; }\n",
            encoding="utf-8",
        )
        await pool.invalidate()

        second = await pool.render(component, {})
        assert "VERSION_BETA" in second["html"], (
            "worker served the stale compile after invalidate "
            f"(Node ESM import cache not busted): {second['html']!r}"
        )
        assert "VERSION_ALPHA" not in second["html"]
    finally:
        await pool.stop()


def test_build_node_env_forwards_worker_concurrency(monkeypatch, tmp_path) -> None:
    """The documented PYXLE_SSR_WORKER_CONCURRENCY knob must survive the
    sanitized spawn environment (it was silently stripped, a documented no-op)."""
    from pyxle.ssr.worker_pool import _build_node_env

    monkeypatch.setenv("PYXLE_SSR_WORKER_CONCURRENCY", "4")
    monkeypatch.setenv("NODE_OPTIONS", "--evil")  # must still be stripped
    env = _build_node_env(tmp_path)
    assert env.get("PYXLE_SSR_WORKER_CONCURRENCY") == "4"
    assert "NODE_OPTIONS" not in env
