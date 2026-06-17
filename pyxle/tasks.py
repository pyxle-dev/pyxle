"""In-process async task queue for fire-and-forget background work.

A lightweight alternative to a full job queue (Celery / ARQ / Dramatiq) for work
that can run on the app's event loop and needn't survive a restart — sending an
email, warming a cache, emitting a webhook after a mutation. Enqueued callables
run on a small bounded pool of workers, so a burst can't exhaust the loop, and a
crashing task is logged rather than taking the worker down.

Coroutine functions are awaited; plain callables run in a thread (so a blocking
SDK call doesn't stall the event loop).

**Multi-worker caveat:** the queue is per-process (like the in-process WebSocket
broker), so under ``pyxle serve --workers N`` each worker has its own queue and
tasks are lost on restart. For work that must survive a restart or run exactly
once across workers, use a real job queue — see the background-tasks guide.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable

logger = logging.getLogger("pyxle.tasks")

DEFAULT_MAX_WORKERS = 4
DEFAULT_MAX_PENDING = 1000


class TaskQueueError(RuntimeError):
    """Base class for task-queue errors."""


class TaskQueueNotRunning(TaskQueueError):
    """A task was enqueued but no queue is running."""

    def __init__(self) -> None:
        super().__init__(
            "No running in-process task queue. enqueue() works inside a Pyxle "
            "request (the queue is started with the app); outside one, start a "
            "TaskQueue yourself or use a real job queue."
        )


class TaskQueueFull(TaskQueueError):
    """The queue is at capacity and can't accept more work."""

    def __init__(self, max_pending: int) -> None:
        super().__init__(
            f"Background task queue is full ({max_pending} pending). The task "
            "was dropped; consider a dedicated job queue for sustained load."
        )


class TaskQueue:
    """A bounded pool of workers draining an ``asyncio.Queue``."""

    def __init__(
        self,
        *,
        max_workers: int = DEFAULT_MAX_WORKERS,
        max_pending: int = DEFAULT_MAX_PENDING,
    ) -> None:
        self._max_workers = max(1, max_workers)
        self._max_pending = max(1, max_pending)
        self._queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=self._max_pending)
        self._workers: list[asyncio.Task] = []
        self._started = False

    @property
    def running(self) -> bool:
        return self._started

    @property
    def pending(self) -> int:
        return self._queue.qsize()

    async def start(self) -> None:
        if self._started:
            return
        self._started = True
        self._workers = [
            asyncio.create_task(self._worker(), name=f"pyxle-task-worker-{i}")
            for i in range(self._max_workers)
        ]

    async def stop(self, *, drain: bool = True) -> None:
        """Stop the workers. ``drain`` waits for already-queued tasks to finish."""
        if not self._started:
            return
        self._started = False
        if drain:
            await self._queue.join()
        for worker in self._workers:
            worker.cancel()
        await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers = []

    def enqueue(self, func: Callable[..., Any], *args: Any, **kwargs: Any) -> None:
        """Schedule ``func(*args, **kwargs)`` to run on a worker (non-blocking)."""
        if not self._started:
            raise TaskQueueNotRunning()
        try:
            self._queue.put_nowait((func, args, kwargs))
        except asyncio.QueueFull as exc:
            raise TaskQueueFull(self._max_pending) from exc

    async def _worker(self) -> None:
        while True:
            func, args, kwargs = await self._queue.get()
            try:
                await _run_task(func, args, kwargs)
            except Exception:  # one bad task must not kill the worker
                logger.exception(
                    "Background task %r failed", getattr(func, "__name__", func)
                )
            finally:
                self._queue.task_done()


async def _run_task(func: Callable[..., Any], args: tuple, kwargs: dict) -> None:
    if asyncio.iscoroutinefunction(func):
        await func(*args, **kwargs)
        return
    result = await asyncio.to_thread(func, *args, **kwargs)
    # A plain callable that happens to return a coroutine (e.g. a lambda
    # wrapping an async call) is awaited so it isn't silently dropped.
    if asyncio.iscoroutine(result):
        await result


# --- Active-queue accessor (set by the app lifespan) -----------------------
# Mirrors pyxle.cache.set_active_cache / pyxle.plugins.set_active_context: the
# devserver starts one queue per app and registers it here so ``enqueue`` works
# from any loader/action without threading a handle through the request.
_active_queue: TaskQueue | None = None


def set_active_queue(queue: TaskQueue | None) -> None:
    global _active_queue
    _active_queue = queue


def get_active_queue() -> TaskQueue | None:
    return _active_queue


def enqueue(func: Callable[..., Any], *args: Any, **kwargs: Any) -> None:
    """Enqueue ``func(*args, **kwargs)`` on the app's in-process task queue.

    Raises :class:`TaskQueueNotRunning` when called outside a running app.
    """
    if _active_queue is None:
        raise TaskQueueNotRunning()
    _active_queue.enqueue(func, *args, **kwargs)


__all__ = [
    "TaskQueue",
    "TaskQueueError",
    "TaskQueueFull",
    "TaskQueueNotRunning",
    "enqueue",
    "get_active_queue",
    "set_active_queue",
]
