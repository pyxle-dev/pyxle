"""Persistent Node.js SSR worker pool.

Replaces per-request Node.js subprocess spawning with a pool of long-lived
worker processes that communicate over stdin/stdout using newline-delimited JSON.

Eliminating Node.js startup cost reduces SSR latency from 200-400ms to the
cost of esbuild bundling alone (~30-80ms), with heavy modules (esbuild, React)
loaded once per worker rather than once per request.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_WORKER_STOP_TIMEOUT = 5.0  # seconds to wait for graceful shutdown

# Sentinel pushed onto a streaming request's queue when its worker dies, so the
# stream consumer (send_stream) wakes immediately instead of waiting out the
# frame timeout.
_STREAM_TERMINATED = object()

# Environment variables safe to forward to Node.js worker processes.
# NODE_OPTIONS is explicitly excluded to prevent arbitrary code injection.
_ALLOWED_ENV_KEYS: frozenset[str] = frozenset({
    "PATH", "HOME", "LANG", "TERM", "USER", "SHELL", "TMPDIR",
    "SYSTEMROOT", "APPDATA",  # Windows support
    # The worker reads its in-process render concurrency cap from this
    # documented variable — it must survive the sanitized spawn env.
    "PYXLE_SSR_WORKER_CONCURRENCY",
})


def _build_node_env(project_root: Path) -> dict[str, str]:
    """Build a minimal environment dict for Node.js worker processes.

    Only forwards a safe subset of environment variables to prevent
    ``NODE_OPTIONS``-based code injection and accidental secret leakage.
    """
    env: dict[str, str] = {}
    for key, value in os.environ.items():
        if key in _ALLOWED_ENV_KEYS or key.startswith("PYXLE_PUBLIC_"):
            env[key] = value
    # Set NODE_PATH so the worker can resolve project-local packages.
    node_path = str(project_root / "node_modules")
    existing = env.get("NODE_PATH", "")
    env["NODE_PATH"] = (
        node_path if not existing else os.pathsep.join([node_path, existing])
    )
    return env


class WorkerPoolError(RuntimeError):
    """Raised when the worker pool cannot process a render request."""


@dataclass
class _WorkerState:
    """Tracks one persistent Node.js worker process."""

    process: asyncio.subprocess.Process
    pending: dict[str, asyncio.Future[dict[str, Any]]] = field(default_factory=dict)
    # Streaming requests (multi-frame): request_id -> queue of NDJSON frames.
    # Distinct from ``pending`` (single-frame buffered requests) so the two
    # protocols coexist on one worker connection.
    streaming: dict[str, "asyncio.Queue[Any]"] = field(default_factory=dict)
    alive: bool = True
    # Number of renders (buffered or streaming) currently dispatched to this
    # worker and not yet finished. A single worker now handles several concurrent
    # streams (the Node worker interleaves them and the read loop demuxes frames
    # by request id), so load is tracked explicitly and the pool dispatches to the
    # least-loaded worker instead of blindly round-robin.
    in_flight: int = 0
    reader_task: asyncio.Task[None] | None = field(default=None, compare=False, repr=False)

    async def send(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Write a request to the worker and await its response.

        Raises WorkerPoolError if the worker stdin is closed or dies mid-flight.
        """
        request_id: str = payload["id"]
        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[str, Any]] = loop.create_future()
        self.pending[request_id] = future

        # Explicit UTF-8 so the worker transport never depends on the locale
        # (astral chars like emoji must survive the Python↔Node round-trip).
        line = (json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        assert self.process.stdin is not None  # guaranteed by _spawn_worker
        self.process.stdin.write(line)
        try:
            await self.process.stdin.drain()
        except Exception as exc:
            self.pending.pop(request_id, None)
            if not future.done():
                future.cancel()
            self.alive = False
            raise WorkerPoolError(f"SSR worker stdin closed: {exc}") from exc

        return await future

    async def send_stream(self, payload: dict[str, Any], *, frame_timeout: float):
        """Write a streaming request and yield its NDJSON frames in order.

        Yields each frame dict until (and including) a terminal ``end``/``error``
        frame, then stops. Raises :class:`WorkerPoolError` if the worker stdin
        closes, the worker dies mid-stream, or no frame arrives within
        ``frame_timeout`` seconds (an inactivity guard — a healthy stream emits
        chunks steadily, so a long gap means a hung render).
        """
        request_id: str = payload["id"]
        queue: asyncio.Queue[Any] = asyncio.Queue()
        self.streaming[request_id] = queue

        line = (
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
        ).encode("utf-8")
        assert self.process.stdin is not None
        self.process.stdin.write(line)
        try:
            await self.process.stdin.drain()
        except Exception as exc:
            self.streaming.pop(request_id, None)
            self.alive = False
            raise WorkerPoolError(f"SSR worker stdin closed: {exc}") from exc

        try:
            while True:
                try:
                    frame = await asyncio.wait_for(queue.get(), timeout=frame_timeout)
                except asyncio.TimeoutError as exc:
                    raise WorkerPoolError(
                        f"SSR stream stalled (no frame in {frame_timeout}s)"
                    ) from exc
                if frame is _STREAM_TERMINATED:
                    raise WorkerPoolError("SSR worker terminated mid-stream")
                yield frame
                if frame.get("type") in ("end", "error"):
                    return
        finally:
            self.streaming.pop(request_id, None)

    async def read_loop(self) -> None:
        """Background task: relay stdout lines to waiting futures.

        Uses raw ``read()`` with manual newline splitting instead of
        ``readline()`` so that responses of any size can be received.
        ``readline()`` is capped by the stream's *limit* parameter
        (default 64 KB) and deadlocks when a single NDJSON line is larger
        than the limit because the write side blocks on the full pipe
        buffer while the read side waits for a newline it cannot reach.
        """
        assert self.process.stdout is not None
        _READ_CHUNK = 256 * 1024  # 256 KB per read()
        buf = b""
        try:
            while True:
                chunk = await self.process.stdout.read(_READ_CHUNK)
                if not chunk:
                    # EOF — process closed stdout.  Flush remaining buffer.
                    if buf.strip():
                        self._dispatch_line(buf)
                    break
                buf += chunk
                # Split completed lines (delimited by \n).
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    if line:
                        self._dispatch_line(line)
        except Exception as exc:
            logger.debug("SSR worker read loop terminated: %s", exc)
        finally:
            self.alive = False
            exc = WorkerPoolError("SSR worker terminated unexpectedly")
            for future in list(self.pending.values()):
                if not future.done():
                    future.set_exception(exc)
            self.pending.clear()
            # Wake any in-flight streaming consumers so they fail fast instead
            # of waiting out the frame timeout.
            for queue in list(self.streaming.values()):
                queue.put_nowait(_STREAM_TERMINATED)
            self.streaming.clear()

    def _dispatch_line(self, line: bytes) -> None:
        """Parse one NDJSON line and resolve the matching pending future."""
        try:
            data: dict[str, Any] = json.loads(line)
        except json.JSONDecodeError:
            logger.debug("SSR worker sent non-JSON line: %r", line[:120])
            return
        request_id = data.get("id")
        if not request_id:
            return
        if request_id in self.pending:
            future = self.pending.pop(request_id)
            if not future.done():
                future.set_result(data)
        elif request_id in self.streaming:
            # Multi-frame streaming response: relay every frame to the consumer,
            # which stops when it sees a terminal ("end"/"error") frame.
            self.streaming[request_id].put_nowait(data)

    async def stop(self) -> None:
        """Send EOF to stdin and wait for the process to exit."""
        self.alive = False
        try:
            if self.process.stdin and not self.process.stdin.is_closing():
                self.process.stdin.close()
        except Exception:
            pass
        try:
            await asyncio.wait_for(self.process.wait(), timeout=_WORKER_STOP_TIMEOUT)
        except (asyncio.TimeoutError, Exception):
            try:
                self.process.kill()
            except Exception:
                pass
        if self.reader_task is not None and not self.reader_task.done():
            self.reader_task.cancel()


class SsrWorkerPool:
    """Manages N persistent Node.js SSR worker processes.

    Each worker is a long-lived Node.js process running ``ssr_worker.mjs``.
    Requests are dispatched round-robin across alive workers.  Crashed workers
    are replaced automatically in the background.

    Usage::

        pool = SsrWorkerPool(size=2, project_root=root, client_root=client)
        await pool.start()
        try:
            result = await pool.render(component_path, props)
        finally:
            await pool.stop()
    """

    def __init__(
        self,
        *,
        size: int,
        project_root: Path,
        client_root: Path,
        node_executable: str | None = None,
        render_timeout: float = 30.0,
    ) -> None:
        self._size = max(1, size)
        self._project_root = project_root
        self._client_root = client_root
        self._node_executable = node_executable
        self._render_timeout = render_timeout
        self._workers: list[_WorkerState] = []
        self._rr_index = 0
        self._started = False
        self._start_lock = asyncio.Lock()

    @property
    def size(self) -> int:
        """Configured pool size."""
        return self._size

    @property
    def alive_count(self) -> int:
        """Number of currently healthy workers."""
        return sum(1 for w in self._workers if w.alive)

    async def start(self) -> None:
        """Spawn all worker processes.

        Safe to call multiple times — subsequent calls are no-ops.
        """
        async with self._start_lock:
            if self._started:
                return
            errors: list[Exception] = []
            for _ in range(self._size):
                try:
                    worker = await self._spawn_worker()
                    self._workers.append(worker)
                except Exception as exc:
                    errors.append(exc)
                    logger.warning("Failed to start SSR worker: %s", exc)
            if not self._workers:
                raise WorkerPoolError(
                    f"Could not start any SSR workers ({self._size} attempted). "
                    f"Last error: {errors[-1] if errors else 'unknown'}"
                )
            self._started = True
            logger.debug(
                "SSR worker pool started: %d/%d workers alive",
                self.alive_count,
                self._size,
            )

    async def stop(self) -> None:
        """Gracefully shut down all worker processes."""
        workers = list(self._workers)
        self._workers.clear()
        self._started = False
        await asyncio.gather(*(w.stop() for w in workers), return_exceptions=True)
        logger.debug("SSR worker pool stopped")

    async def render(
        self,
        component_path: Path,
        props: dict[str, Any],
        *,
        request_pathname: str | None = None,
        csrf_token: str | None = None,
    ) -> dict[str, Any]:
        """Send a render request to the next available worker.

        ``request_pathname`` is forwarded to the worker and exposed to
        component code via ``globalThis.__PYXLE_CURRENT_PATHNAME__``
        during SSR, so hooks like ``usePathname`` return the correct
        path instead of a fallback and hydrate cleanly.

        ``csrf_token`` is similarly exposed via
        ``globalThis.__PYXLE_CSRF_TOKEN__``. ``<Form>`` reads it at SSR
        time so a no-JS submission can carry a hidden ``_csrf_token``
        field that satisfies the CSRF middleware.

        Auto-starts the pool on first call if :meth:`start` was not called
        explicitly.  Raises :class:`WorkerPoolError` if no healthy workers
        are available or if the worker crashes during rendering.
        """
        if not self._started:
            await self.start()

        worker = self._pick_worker()
        if worker is None:
            raise WorkerPoolError(
                "No healthy SSR workers available. The pool may be exhausted or all workers crashed."
            )

        request_id = str(uuid.uuid4())
        payload: dict[str, Any] = {
            "id": request_id,
            "componentPath": str(component_path.resolve()),
            "props": props,
            "clientRoot": str(self._client_root),
            "projectRoot": str(self._project_root),
        }
        if request_pathname is not None:
            payload["requestPathname"] = request_pathname
        if csrf_token is not None:
            payload["csrfToken"] = csrf_token

        worker.in_flight += 1
        try:
            result = await asyncio.wait_for(
                worker.send(payload), timeout=self._render_timeout
            )
        except asyncio.TimeoutError:
            self._workers = [w for w in self._workers if w is not worker]
            asyncio.get_running_loop().create_task(self._replenish())
            raise WorkerPoolError(
                f"SSR render timed out after {self._render_timeout}s "
                f"for {component_path.name}"
            )
        except WorkerPoolError:
            self._workers = [w for w in self._workers if w is not worker]
            asyncio.get_running_loop().create_task(self._replenish())
            raise
        finally:
            worker.in_flight -= 1

        return result

    async def render_stream(
        self,
        component_path: Path,
        props: dict[str, Any],
        *,
        request_pathname: str | None = None,
        csrf_token: str | None = None,
        fallback_path: Path | None = None,
    ):
        """Stream a render from the next available worker as a frame sequence.

        Yields NDJSON frame dicts (``{"type": "chunk", "html": ...}``) as they
        arrive, ending with a terminal ``{"type": "end"}`` or
        ``{"type": "error", ...}`` frame. The caller interprets the frames
        (``chunk`` -> body bytes, ``end`` -> done, ``error`` -> fallback). A
        worker that crashes mid-stream is dropped and replaced. Use
        :meth:`render` for the buffered single-frame path (cacheable / SSG
        renders, which must be materialised anyway).
        """
        if not self._started:
            await self.start()

        worker = self._pick_worker()
        if worker is None:
            raise WorkerPoolError(
                "No healthy SSR workers available. The pool may be exhausted or all workers crashed."
            )

        request_id = str(uuid.uuid4())
        payload: dict[str, Any] = {
            "id": request_id,
            "componentPath": str(component_path.resolve()),
            "props": props,
            "clientRoot": str(self._client_root),
            "projectRoot": str(self._project_root),
            "stream": True,
        }
        if request_pathname is not None:
            payload["requestPathname"] = request_pathname
        if csrf_token is not None:
            payload["csrfToken"] = csrf_token
        if fallback_path is not None:
            # The page is wrapped in <Suspense fallback={<Loading/>}> using this
            # compiled loading.pyxl component.
            payload["fallbackPath"] = str(fallback_path.resolve())

        worker.in_flight += 1
        try:
            async for frame in worker.send_stream(
                payload, frame_timeout=self._render_timeout
            ):
                yield frame
        except WorkerPoolError:
            self._workers = [w for w in self._workers if w is not worker]
            asyncio.get_running_loop().create_task(self._replenish())
            raise
        finally:
            worker.in_flight -= 1

    async def invalidate(
        self,
        component_path: Path | None = None,
    ) -> None:
        """Broadcast a cache-invalidation message to all alive workers.

        If *component_path* is given, only that component's cached bundle is
        evicted.  Otherwise every cached bundle is cleared.
        """
        if not self._started:
            return

        payload_base: dict[str, Any] = {"type": "invalidate"}
        if component_path is not None:
            payload_base["componentPath"] = str(component_path.resolve())

        for worker in self._workers:
            if not worker.alive:
                continue
            request_id = str(uuid.uuid4())
            payload = {"id": request_id, **payload_base}
            try:
                await worker.send(payload)
            except WorkerPoolError:
                pass  # worker is dying; skip gracefully

    def _pick_worker(self) -> _WorkerState | None:
        """Return the least-loaded alive worker, breaking ties round-robin.

        Least-in-flight keeps a slow streaming render from piling every new
        request onto the same worker: a worker already carrying an open stream
        is skipped in favour of an idle one. When several workers are equally
        loaded (the common case, and always true for a one-worker pool) the tie
        is broken with the existing round-robin cursor so distribution stays
        fair and deterministic.
        """
        alive = [w for w in self._workers if w.alive]
        if not alive:
            return None
        min_load = min(w.in_flight for w in alive)
        candidates = [w for w in alive if w.in_flight == min_load]
        worker = candidates[self._rr_index % len(candidates)]
        self._rr_index += 1
        return worker

    async def _replenish(self) -> None:
        """Replace dead workers up to the configured pool size."""
        alive = [w for w in self._workers if w.alive]
        deficit = self._size - len(alive)
        for _ in range(max(0, deficit)):
            try:
                worker = await self._spawn_worker()
                self._workers.append(worker)
                logger.debug("SSR worker pool: replacement worker started")
            except Exception as exc:
                logger.warning("SSR worker pool: failed to replenish worker: %s", exc)

    async def _spawn_worker(self) -> _WorkerState:
        node_exec = self._node_executable or shutil.which("node")
        if not node_exec:
            raise WorkerPoolError(
                "Node.js executable not found. Install Node.js to enable the SSR worker pool."
            )

        script = Path(__file__).with_name("ssr_worker.mjs")
        if not script.exists():
            raise WorkerPoolError(
                f"SSR worker script not found at '{script}'. Reinstall Pyxle."
            )

        env = _build_node_env(self._project_root)

        process = await asyncio.create_subprocess_exec(
            node_exec,
            str(script),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(self._project_root),
            env=env,
        )

        state = _WorkerState(process=process)
        state.reader_task = asyncio.create_task(state.read_loop())
        return state


__all__ = ["SsrWorkerPool", "WorkerPoolError"]
