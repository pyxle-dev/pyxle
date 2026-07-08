"""Forward server-side ``logging`` records to browser overlay clients.

Development only. During ``pyxle dev`` a developer's server-side ``logging``
output (from loaders, actions, and their own modules) is invisible in the
browser. :class:`BrowserConsoleLogHandler` bridges that gap: it is a bounded
:class:`logging.Handler` that pushes formatted records to the dev overlay
WebSocket so they appear in the browser devtools console, prefixed and mapped
to the matching ``console`` method.

The handler is deliberately robust:

* It never blocks the event loop — records are marshaled onto the loop via
  :func:`asyncio.run_coroutine_threadsafe`, so emitting from a worker thread
  (e.g. ``asyncio.to_thread``) is safe.
* It never crashes the app when no client is connected or a send fails — the
  broadcast path in :class:`~pyxle.devserver.overlay.OverlayManager` swallows
  per-connection send errors, and scheduling failures during shutdown are
  ignored.
* It guards against re-entrancy — a log emitted while a record is being
  forwarded (directly or transitively) is dropped instead of recursing.
* It throttles bursts so a hot log loop cannot flood the socket.
* It does not forward the framework's own noisy internals unless the developer
  opts into verbose output.

Nothing here runs under ``pyxle serve`` — the handler is only attached when the
dev server starts in debug mode.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import TYPE_CHECKING, Callable, Optional

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .overlay import OverlayManager

#: Logger namespaces treated as server-runtime internals. Records from these are
#: noise in the browser console during normal development, so they are only
#: forwarded when the developer opts into verbose output.
_INTERNAL_LOGGER_PREFIXES: tuple[str, ...] = (
    "pyxle",
    "uvicorn",
    "watchfiles",
    "starlette",
    "asyncio",
    "multipart",
)

#: Default cap on records forwarded per second. Bursts above this are dropped so
#: a hot log loop in user code cannot flood the overlay WebSocket.
DEFAULT_MAX_RECORDS_PER_SECOND = 100

_Scheduler = Callable[["asyncio.Future | object", asyncio.AbstractEventLoop], None]


def _console_method(levelno: int) -> str:
    """Map a Python logging level to the browser ``console`` method to call."""
    if levelno >= logging.ERROR:
        return "error"
    if levelno >= logging.WARNING:
        return "warn"
    if levelno >= logging.INFO:
        return "info"
    return "debug"


def _default_scheduler(coro, loop: asyncio.AbstractEventLoop) -> None:
    """Marshal *coro* onto *loop* from any thread without blocking."""
    asyncio.run_coroutine_threadsafe(coro, loop)


class BrowserConsoleLogHandler(logging.Handler):
    """Forward server ``logging`` records to browser overlay clients (dev only).

    Parameters
    ----------
    overlay:
        The dev :class:`~pyxle.devserver.overlay.OverlayManager` whose
        connected clients receive forwarded records.
    loop:
        The running dev-server event loop. Records are scheduled onto it.
    verbose:
        When ``True``, forward every record — including ``DEBUG`` and the
        framework's own internal loggers. When ``False`` (the default), only
        forward ``INFO`` and above from non-internal loggers.
    max_records_per_second:
        Upper bound on forwarded records per second; excess is dropped.
    scheduler:
        Injection seam for tests. Defaults to a non-blocking, thread-safe
        marshal onto *loop*.
    """

    def __init__(
        self,
        overlay: "OverlayManager",
        loop: asyncio.AbstractEventLoop,
        *,
        verbose: bool = False,
        max_records_per_second: int = DEFAULT_MAX_RECORDS_PER_SECOND,
        scheduler: Optional[_Scheduler] = None,
    ) -> None:
        super().__init__(level=logging.NOTSET)
        self._overlay = overlay
        self._loop = loop
        self._verbose = verbose
        self._max_per_second = max(1, max_records_per_second)
        self._scheduler = scheduler or _default_scheduler
        # A record forwarded to a formatter/overlay that itself logs must not
        # recurse into this handler on the same thread.
        self._local = threading.local()
        # Throttle window state. emit() can run on the loop thread or on worker
        # threads (asyncio.to_thread), so guard the counters with a lock.
        self._throttle_lock = threading.Lock()
        self._window_start = 0.0
        self._window_count = 0
        self._prev_root_level = logging.NOTSET
        self._lowered_root = False
        self.setFormatter(logging.Formatter("%(message)s"))

    # -- attachment ----------------------------------------------------

    def attach(self) -> None:
        """Attach to the root logger, lowering its level to receive records.

        The root logger defaults to ``WARNING``, which would drop ``INFO``
        records before they reach any handler. While attached, the level is
        lowered to ``INFO`` (or ``DEBUG`` when verbose) so server logs are
        forwarded. The previous level is restored on :meth:`detach`.
        """
        root = logging.getLogger()
        self._prev_root_level = root.level
        target = logging.DEBUG if self._verbose else logging.INFO
        if root.level == logging.NOTSET or root.level > target:
            root.setLevel(target)
            self._lowered_root = True
        root.addHandler(self)

    def detach(self) -> None:
        """Remove from the root logger and restore its previous level."""
        root = logging.getLogger()
        root.removeHandler(self)
        if self._lowered_root:
            root.setLevel(self._prev_root_level)
            self._lowered_root = False
        self.close()

    # -- filtering -----------------------------------------------------

    def _should_forward(self, record: logging.LogRecord) -> bool:
        if self._verbose:
            return True
        if record.levelno < logging.INFO:
            return False
        name = record.name or ""
        for prefix in _INTERNAL_LOGGER_PREFIXES:
            if name == prefix or name.startswith(prefix + "."):
                return False
        return True

    def _within_rate_limit(self) -> bool:
        now = time.monotonic()
        with self._throttle_lock:
            if now - self._window_start >= 1.0:
                self._window_start = now
                self._window_count = 1
                return True
            if self._window_count >= self._max_per_second:
                return False
            self._window_count += 1
            return True

    # -- emission ------------------------------------------------------

    def emit(self, record: logging.LogRecord) -> None:
        if getattr(self._local, "active", False):
            # Re-entrant call (formatting/dispatch logged something) — drop it
            # rather than recurse.
            return
        self._local.active = True
        try:
            if not self._should_forward(record):
                return
            if not self._within_rate_limit():
                return
            message = self.format(record)
            level = _console_method(record.levelno)
            self._dispatch(level, message, record.name or "")
        except Exception:  # noqa: BLE001 - logging must never crash the app
            self.handleError(record)
        finally:
            self._local.active = False

    def _dispatch(self, level: str, message: str, logger_name: str) -> None:
        coro = self._overlay.notify_log(
            level=level, message=message, logger_name=logger_name
        )
        try:
            self._scheduler(coro, self._loop)
        except RuntimeError:
            # Loop already closed (shutdown race). Close the un-awaited coroutine
            # so it does not warn, and drop the record.
            close = getattr(coro, "close", None)
            if callable(close):
                close()


__all__ = [
    "BrowserConsoleLogHandler",
    "DEFAULT_MAX_RECORDS_PER_SECOND",
]
