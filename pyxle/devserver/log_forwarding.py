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
  opts into verbose output — but a compiled page is *not* internals, however
  framework-shaped its module name looks (see :data:`_USER_MODULE_LOGGER_PREFIX`).
* It keeps the terminal in step with the browser, so a record forwarded to the
  console is also printed where the developer already is (see
  :func:`_stderr_fallback_wants`).

Nothing here runs under ``pyxle serve`` — the handler is only attached when the
dev server starts in debug mode.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from pathlib import Path
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

#: Namespace the dev server imports *user* modules under: a page at
#: ``pages/about.pyxl`` is executed as ``pyxle.server.pages.about`` (see
#: :func:`pyxle.devserver.registry._module_key`). It lives beneath ``pyxle.``
#: only because generated modules need a private ``sys.modules`` namespace —
#: there is no ``pyxle.server`` package and the framework logs nothing there.
#: The code is the developer's own, so ``log = logging.getLogger(__name__)`` at
#: the top of a page must behave like any other user logger. This prefix is
#: therefore carved out of :data:`_INTERNAL_LOGGER_PREFIXES` above, which would
#: otherwise swallow it as framework noise.
_USER_MODULE_LOGGER_PREFIX = "pyxle.server"

#: Default cap on records forwarded per second. Bursts above this are dropped so
#: a hot log loop in user code cannot flood the overlay WebSocket.
DEFAULT_MAX_RECORDS_PER_SECOND = 100

_Scheduler = Callable[["asyncio.Future | object", asyncio.AbstractEventLoop], None]


def _in_namespace(name: str, prefix: str) -> bool:
    """Whether logger *name* is *prefix* itself or a logger beneath it.

    Whole-segment comparison: ``pyxletools`` is not inside ``pyxle``.
    """
    return name == prefix or name.startswith(prefix + ".")


def _is_internal_logger(name: str) -> bool:
    """Whether *name* belongs to the server runtime rather than to user code.

    Compiled pages and API modules (``pyxle.server.*``) are user code that
    merely *lives* in a framework-owned ``sys.modules`` namespace, so they are
    explicitly not internal — see :data:`_USER_MODULE_LOGGER_PREFIX`.
    """
    if _in_namespace(name, _USER_MODULE_LOGGER_PREFIX):
        return False
    return any(_in_namespace(name, prefix) for prefix in _INTERNAL_LOGGER_PREFIXES)


def _stderr_fallback_wants(record: logging.LogRecord) -> bool:
    """Whether the stderr fallback installed by :meth:`attach` prints *record*.

    Two separate things have to reach the terminal:

    * Everything :data:`logging.lastResort` printed before the bridge existed —
      WARNING and above, from any logger. Losing this is how a plugin whose
      ``on_startup`` raises could abort a boot in silence.
    * The developer's own ``INFO`` records. :meth:`attach` lowers the root
      logger to ``INFO`` so those reach the browser; a fallback pinned at
      ``WARNING`` would send them *only* there, making a plain ``log.info(...)``
      invisible in the terminal a developer is already watching.

    Framework internals stay out (``uvicorn`` INFO is not new terminal output —
    uvicorn prints its own), and ``DEBUG`` stays out even in verbose mode, where
    forwarding every internal record to the terminal would bury the app's logs.
    """
    if record.levelno >= logging.WARNING:
        return True
    if record.levelno < logging.INFO:
        return False
    return not _is_internal_logger(record.name or "")


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
    project_root:
        The project directory. Used only to label a page's records with the
        source file the developer wrote — see :meth:`_display_name`. Without it
        those records keep their raw logger name.
    build_root:
        The generated-artifact directory (``.pyxle-build``). Records whose file
        lives there are labelled by module key rather than by a path nobody
        edits.
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
        project_root: Optional[Path] = None,
        build_root: Optional[Path] = None,
        scheduler: Optional[_Scheduler] = None,
    ) -> None:
        super().__init__(level=logging.NOTSET)
        self._overlay = overlay
        self._loop = loop
        self._verbose = verbose
        self._project_root = project_root
        self._build_root = build_root
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
        self._stderr_fallback: logging.Handler | None = None
        self.setFormatter(logging.Formatter("%(message)s"))

    # -- attachment ----------------------------------------------------

    def attach(self) -> None:
        """Attach to the root logger, lowering its level to receive records.

        The root logger defaults to ``WARNING``, which would drop ``INFO``
        records before they reach any handler. While attached, the level is
        lowered to ``INFO`` (or ``DEBUG`` when verbose) so server logs are
        forwarded. The previous level is restored on :meth:`detach`.

        Attaching also installs a stderr fallback when the root logger has no
        handlers of its own — see :meth:`_install_stderr_fallback`.
        """
        root = logging.getLogger()
        self._prev_root_level = root.level
        target = logging.DEBUG if self._verbose else logging.INFO
        if root.level == logging.NOTSET or root.level > target:
            root.setLevel(target)
            self._lowered_root = True
        self._install_stderr_fallback(root)
        root.addHandler(self)

    def _install_stderr_fallback(self, root: logging.Logger) -> None:
        """Keep warnings and errors on the terminal once this handler exists.

        Python routes a record to :data:`logging.lastResort` — WARNING and above,
        to stderr — only while it finds *no* handler willing to take it. Adding
        this one ends that fallback for the whole process, which would quietly
        redirect every library warning and error into a browser console that may
        not even be open: a plugin whose ``on_startup`` raises would abort the
        boot without printing anything at all.

        So when nothing else is listening on the root logger, install the
        equivalent stderr sink ourselves. It is removed again in :meth:`detach`.

        The sink is gated by :func:`_stderr_fallback_wants` rather than by a
        level: it must carry both what ``lastResort`` carried *and* the app's
        own ``INFO`` records, which only exist at all because :meth:`attach`
        lowered the root logger to reach them.
        """
        if root.handlers:
            return
        fallback = logging.StreamHandler()
        fallback.setLevel(logging.NOTSET)
        fallback.addFilter(_stderr_fallback_wants)
        fallback.setFormatter(logging.Formatter("%(message)s"))
        root.addHandler(fallback)
        self._stderr_fallback = fallback

    def detach(self) -> None:
        """Remove from the root logger and restore its previous level."""
        root = logging.getLogger()
        root.removeHandler(self)
        if self._stderr_fallback is not None:
            root.removeHandler(self._stderr_fallback)
            self._stderr_fallback.close()
            self._stderr_fallback = None
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
        return not _is_internal_logger(record.name or "")

    # -- labelling -----------------------------------------------------

    def _display_name(self, record: logging.LogRecord) -> str:
        """The logger label shown in the browser console prefix.

        A page's own logger is auto-named from ``__name__``, which inside a
        compiled ``.pyxl`` is the synthetic module key the dev server imports it
        under — ``pyxle.server.pages.about``. That name is an internal detail
        the developer never typed, and reads as if the framework, not their
        page, emitted the record. Show the source file they *did* type
        (``pages/about.pyxl``) instead; in debug mode the record already carries
        it, because compiled pages execute with their ``.pyxl`` as ``co_filename``.

        Every other logger keeps the name its author chose:
        ``logging.getLogger("shopapp")`` stays ``shopapp``.
        """
        name = record.name or ""
        if not _in_namespace(name, _USER_MODULE_LOGGER_PREFIX):
            return name
        return self._source_label(record) or name

    def _source_label(self, record: logging.LogRecord) -> Optional[str]:
        """*record*'s source file relative to the project root, if it is one.

        The label must name a file the developer can open. Records that carry a
        *generated* path instead — an API module runs from its copy under the
        build directory, since only pages get the ``.pyxl`` line remap — are
        rejected here rather than shown as a ``.pyxle-build/...`` path nobody
        edits; the caller falls back to the module key.
        """
        if self._project_root is None:
            return None
        pathname = getattr(record, "pathname", None)
        if not pathname:
            return None
        path = Path(pathname)
        if self._build_root is not None and path.is_relative_to(self._build_root):
            return None
        try:
            return path.relative_to(self._project_root).as_posix()
        except ValueError:
            # Outside the project entirely (an installed package) — the raw
            # logger name is safer than a path with no relation to the app.
            return None

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
            self._dispatch(level, message, self._display_name(record))
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
