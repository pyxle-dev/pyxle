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

# Environment variables safe to forward to Node.js worker processes.
# NODE_OPTIONS is explicitly excluded to prevent arbitrary code injection.
_ALLOWED_ENV_KEYS: frozenset[str] = frozenset({
    "PATH", "HOME", "LANG", "TERM", "USER", "SHELL", "TMPDIR",
    "SYSTEMROOT", "APPDATA",  # Windows support
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
    alive: bool = True
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

    def _dispatch_line(self, line: bytes) -> None:
        """Parse one NDJSON line and resolve the matching pending future."""
        try:
            data: dict[str, Any] = json.loads(line)
        except json.JSONDecodeError:
            logger.debug("SSR worker sent non-JSON line: %r", line[:120])
            return
        request_id = data.get("id")
        if request_id and request_id in self.pending:
            future = self.pending.pop(request_id)
            if not future.done():
                future.set_result(data)

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

        return result

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
        alive = [w for w in self._workers if w.alive]
        if not alive:
            return None
        worker = alive[self._rr_index % len(alive)]
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
