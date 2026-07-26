"""Pyxle Studio — the dev-only web dashboard served by ``pyxle dev``.

Studio mounts at ``/__pyxle/studio`` and gives X-ray vision into a running
development server: every route with its loader and actions, an interactive
tester that invokes loaders and actions with real inputs, a live request feed,
aggregate metrics, the resolved configuration, and ``pyxle check`` diagnostics.

The package has three parts:

* :class:`StudioManager` (this module) — the per-app coordination object.
  It owns the bounded recent-request log and the fan-out hub for the
  ``/__pyxle/studio/events`` Server-Sent-Events stream. Constructed only in
  debug mode (see ``create_starlette_app``) and stored at
  ``app.state.pyxle_studio`` so it survives hot route-table refreshes.
* :mod:`pyxle.devserver.studio.api` — the HTTP route builders (UI shell,
  static assets, JSON API, SSE stream).
* ``static/`` — the dashboard's dependency-free HTML/CSS/JS, shipped inside
  the wheel and served via :mod:`importlib.resources`.

Studio is dev-only **by construction**: nothing here is reachable from a
production ``pyxle serve`` process, which never constructs the manager.
"""

from __future__ import annotations

import asyncio
import itertools
import time
from collections import deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Deque, Dict, List, Optional, Set

if TYPE_CHECKING:  # pragma: no cover - typing only
    from pyxle.cli.logger import ConsoleLogger

    from ..settings import DevServerSettings
    from ..watcher import WatcherStatistics

#: URL namespace Studio owns. Everything Studio serves lives under this
#: prefix, and the Vite asset proxy + observability middleware treat it as
#: reserved (never proxied, never counted into app metrics).
STUDIO_PATH = "/__pyxle/studio"

#: Bounded size of the recent-request ring buffer. Old entries fall off the
#: far end — Studio is a live window, not a log store.
REQUEST_LOG_LIMIT = 200

#: Per-subscriber SSE queue bound. A subscriber that stops draining (a
#: background tab, a wedged connection) has its oldest events dropped rather
#: than growing the queue without limit.
_SUBSCRIBER_QUEUE_LIMIT = 512


def is_enabled(config: Any) -> bool:
    """Return ``True`` when a ``studio`` config block enables the dashboard.

    ``None`` (no block configured) means enabled — Studio is on by default in
    development, like the error overlay.
    """
    if config is None:
        return True
    return bool(getattr(config, "enabled", False))


@dataclass(frozen=True, slots=True)
class RequestLogEntry:
    """One observed request in Studio's live feed."""

    seq: int
    timestamp: float
    method: str
    path: str
    status: int
    duration_ms: float
    request_id: Optional[str]
    route_target: Optional[str]
    route_path: Optional[str]

    def as_dict(self) -> Dict[str, Any]:
        return {
            "seq": self.seq,
            "timestamp": self.timestamp,
            "method": self.method,
            "path": self.path,
            "status": self.status,
            "durationMs": round(self.duration_ms, 2),
            "requestId": self.request_id,
            "routeTarget": self.route_target,
            "routePath": self.route_path,
        }


@dataclass(slots=True)
class StudioManager:
    """Coordinates Studio's live state for one dev-server app.

    Holds the bounded recent-request log and the SSE subscriber hub. All
    mutation happens on the event loop (the request observer fires from the
    observability middleware's send wrapper; rebuild notifications are
    marshaled onto the loop by the dev server), so no locking is needed —
    the same single-threaded discipline as ``MetricsRegistry``.
    """

    settings: "DevServerSettings"
    config: Any = None
    logger: "ConsoleLogger | None" = None
    _requests: Deque[RequestLogEntry] = field(init=False, repr=False)
    _subscribers: Set[asyncio.Queue] = field(init=False, repr=False)
    _seq: "itertools.count[int]" = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._requests = deque(maxlen=REQUEST_LOG_LIMIT)
        self._subscribers = set()
        self._seq = itertools.count(1)

    # ------------------------------------------------------------------ feed

    def record_request(self, event: Dict[str, Any]) -> None:
        """Observer hook for :class:`~pyxle.observability.RequestIdMiddleware`.

        Called at response start for every non-excluded request. Appends to
        the bounded ring buffer and pushes a ``request`` event to SSE
        subscribers. Never raises (the middleware also guards, but this hook
        is deliberately total).
        """
        route = event.get("route") or {}
        entry = RequestLogEntry(
            seq=next(self._seq),
            timestamp=time.time(),
            method=str(event.get("method", "")),
            path=str(event.get("path", "")),
            status=int(event.get("status", 0)),
            duration_ms=float(event.get("duration_ms", 0.0)),
            request_id=event.get("request_id"),
            route_target=route.get("target") if isinstance(route, dict) else None,
            route_path=route.get("path") if isinstance(route, dict) else None,
        )
        self._requests.append(entry)
        self._publish({"type": "request", "payload": entry.as_dict()})

    def recent_requests(self) -> List[Dict[str, Any]]:
        """The ring buffer as JSON-ready dicts, oldest first."""
        return [entry.as_dict() for entry in self._requests]

    # --------------------------------------------------------------- rebuilds

    async def notify_rebuild(self, stats: "WatcherStatistics") -> None:
        """Broadcast a finished rebuild (success or failure) to subscribers.

        The payload mirrors what the terminal prints: elapsed time, changed
        paths, an error string on failure, and per-category change counts on
        success.
        """
        summary = stats.summary
        payload: Dict[str, Any] = {
            "ok": stats.error is None,
            "elapsedSeconds": round(stats.elapsed_seconds, 3),
            "changedPaths": [str(path) for path in stats.changed_paths],
        }
        if stats.error is not None:
            payload["error"] = str(stats.error)
        if summary is not None:
            payload["compiledPages"] = list(summary.compiled_pages)
            payload["removed"] = list(summary.removed)
        self._publish({"type": "rebuild", "payload": payload})

    # ------------------------------------------------------------ subscribers

    def subscribe(self) -> asyncio.Queue:
        """Register a new SSE subscriber and return its event queue."""
        queue: asyncio.Queue = asyncio.Queue(maxsize=_SUBSCRIBER_QUEUE_LIMIT)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self._subscribers.discard(queue)

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    def _publish(self, event: Dict[str, Any]) -> None:
        """Fan an event out to every subscriber queue, dropping on overflow.

        A full queue means the subscriber stopped draining; dropping its
        oldest pending event keeps the feed live-windowed and the memory
        bounded rather than back-pressuring the request path.
        """
        for queue in tuple(self._subscribers):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                try:
                    queue.get_nowait()
                    queue.put_nowait(event)
                except (asyncio.QueueEmpty, asyncio.QueueFull):  # pragma: no cover - race guard
                    pass


__all__ = [
    "STUDIO_PATH",
    "REQUEST_LOG_LIMIT",
    "RequestLogEntry",
    "StudioManager",
    "is_enabled",
]
