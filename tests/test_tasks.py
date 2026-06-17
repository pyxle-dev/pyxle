"""Tests for the in-process background task queue (pyxle.tasks)."""

from __future__ import annotations

import asyncio
import logging

import pytest

from pyxle.tasks import (
    TaskQueue,
    TaskQueueFull,
    TaskQueueNotRunning,
    enqueue,
    get_active_queue,
    set_active_queue,
)


@pytest.fixture(autouse=True)
def _clear_active_queue():
    set_active_queue(None)
    yield
    set_active_queue(None)


# ---------------------------------------------------------------------------
# TaskQueue


def test_runs_coroutine_task() -> None:
    async def _run():
        seen: list[int] = []

        async def task(value: int) -> None:
            seen.append(value)

        queue = TaskQueue(max_workers=2)
        await queue.start()
        queue.enqueue(task, 7)
        await queue._queue.join()
        await queue.stop()
        return seen

    assert asyncio.run(_run()) == [7]


def test_runs_sync_task_in_thread() -> None:
    async def _run():
        seen: list[str] = []

        def task(value: str) -> None:
            seen.append(value)

        queue = TaskQueue()
        await queue.start()
        queue.enqueue(task, "hi")
        await queue._queue.join()
        await queue.stop()
        return seen

    assert asyncio.run(_run()) == ["hi"]


def test_failing_task_is_logged_and_worker_survives(caplog) -> None:
    async def _run():
        ok: list[int] = []

        async def boom() -> None:
            raise ValueError("nope")

        async def fine() -> None:
            ok.append(1)

        queue = TaskQueue(max_workers=1)
        await queue.start()
        with caplog.at_level(logging.ERROR, logger="pyxle.tasks"):
            queue.enqueue(boom)
            queue.enqueue(fine)
            await queue._queue.join()
        await queue.stop()
        return ok

    assert asyncio.run(_run()) == [1]


def test_sync_task_returning_coroutine_is_awaited() -> None:
    async def _run():
        seen: list[str] = []

        async def inner() -> None:
            seen.append("inner")

        def returns_coro():
            return inner()  # a sync callable that returns a coroutine

        queue = TaskQueue()
        await queue.start()
        queue.enqueue(returns_coro)
        await queue._queue.join()
        await queue.stop()
        return seen

    assert asyncio.run(_run()) == ["inner"]


def test_stop_without_drain() -> None:
    async def _run():
        queue = TaskQueue()
        await queue.start()
        await queue.stop(drain=False)
        return queue.running

    assert asyncio.run(_run()) is False


def test_enqueue_before_start_raises() -> None:
    queue = TaskQueue()
    with pytest.raises(TaskQueueNotRunning):
        queue.enqueue(lambda: None)


def test_enqueue_when_full_raises() -> None:
    async def _run():
        queue = TaskQueue(max_pending=1)
        queue._started = True  # pretend running, with no workers draining
        queue.enqueue(lambda: None)  # fills the single slot
        with pytest.raises(TaskQueueFull):
            queue.enqueue(lambda: None)

    asyncio.run(_run())


def test_stop_is_idempotent_and_start_too() -> None:
    async def _run():
        queue = TaskQueue()
        await queue.start()
        await queue.start()  # second start is a no-op
        assert queue.running is True
        await queue.stop()
        await queue.stop()  # second stop is a no-op
        assert queue.running is False

    asyncio.run(_run())


def test_pending_reflects_queue_depth() -> None:
    async def _run():
        queue = TaskQueue(max_pending=5)
        queue._started = True  # no workers, so items accumulate
        queue.enqueue(lambda: None)
        queue.enqueue(lambda: None)
        return queue.pending

    assert asyncio.run(_run()) == 2


# ---------------------------------------------------------------------------
# module-level enqueue() + active queue


def test_module_enqueue_uses_active_queue() -> None:
    async def _run():
        seen: list[int] = []

        async def task(value: int) -> None:
            seen.append(value)

        queue = TaskQueue()
        await queue.start()
        set_active_queue(queue)
        assert get_active_queue() is queue
        enqueue(task, 42)
        await queue._queue.join()
        await queue.stop()
        return seen

    assert asyncio.run(_run()) == [42]


def test_module_enqueue_without_active_queue_raises() -> None:
    with pytest.raises(TaskQueueNotRunning):
        enqueue(lambda: None)
