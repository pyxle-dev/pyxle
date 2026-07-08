"""Developer error overlay websocket coordination."""

from __future__ import annotations

import asyncio
import json
import traceback
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Set
from urllib.parse import urlparse

from starlette.websockets import WebSocket, WebSocketDisconnect

from pyxle.cli.logger import ConsoleLogger


@dataclass(frozen=True)
class OverlayEvent:
    """Structured payload sent to connected developer overlay clients."""

    type: str
    payload: Dict[str, Any]


class OverlayManager:
    """Tracks websocket connections and broadcasts overlay events.

    Parameters
    ----------
    allowed_origins:
        Set of allowed WebSocket origins (e.g. ``{"http://localhost:8000"}``).
        An empty set disables origin validation (not recommended).
    """

    def __init__(
        self,
        *,
        logger: Optional[ConsoleLogger] = None,
        allowed_origins: Set[str] | None = None,
    ) -> None:
        self._connections: Set[WebSocket] = set()
        self._lock = asyncio.Lock()
        self._logger = logger or ConsoleLogger()
        self._allowed_origins: Set[str] = allowed_origins or set()

    def _is_allowed_origin(self, origin: str) -> bool:
        """Check whether *origin* is in the allowed set.

        When no allowed origins are configured, all origins are accepted
        (backwards-compatible default for dev servers started without
        explicit configuration).
        """
        if not self._allowed_origins:
            return True
        if not origin:
            # Missing Origin header — browsers always send it for
            # cross-origin WebSocket, so an absent header indicates
            # a same-origin connection or a non-browser client.
            return True
        # Normalise trailing slashes for comparison.
        normalised = origin.rstrip("/")
        if normalised in self._allowed_origins:
            return True
        # Allow the origin if its host part is localhost/127.0.0.1
        # and the port matches one of the allowed origins.
        try:
            parsed = urlparse(normalised)
            if parsed.hostname in ("localhost", "127.0.0.1"):
                for allowed in self._allowed_origins:
                    allowed_parsed = urlparse(allowed)
                    if (
                        allowed_parsed.hostname in ("localhost", "127.0.0.1")
                        and parsed.port == allowed_parsed.port
                    ):
                        return True
        except Exception:  # pragma: no cover — defensive
            pass
        return False

    async def register(self, websocket: WebSocket) -> None:
        await websocket.accept()
        async with self._lock:
            self._connections.add(websocket)

    async def unregister(self, websocket: WebSocket) -> None:
        async with self._lock:
            self._connections.discard(websocket)

    async def broadcast(self, event: OverlayEvent) -> None:
        message = json.dumps({"type": event.type, "payload": event.payload})
        async with self._lock:
            connections = list(self._connections)
        stale: List[WebSocket] = []
        for connection in connections:
            try:
                await connection.send_text(message)
            except Exception:  # pragma: no cover - defensive cleanup
                stale.append(connection)
        for connection in stale:
            await self.unregister(connection)
        if stale:
            self._logger.warning(
                f"[Pyxle] Removed {len(stale)} overlay connection(s) due to send failure"
            )

    async def notify_error(
        self,
        *,
        route_path: str,
        error: BaseException,
        stack: Optional[str] = None,
        breadcrumbs: Optional[List[Dict[str, str]]] = None,
    ) -> None:
        payload = {
            "routePath": route_path,
            "message": str(error),
            "stack": stack or _format_stacktrace(error),
            "breadcrumbs": breadcrumbs or [],
        }
        await self.broadcast(OverlayEvent(type="error", payload=payload))

    async def notify_clear(self, *, route_path: str) -> None:
        await self.broadcast(
            OverlayEvent(
                type="clear",
                payload={"routePath": route_path},
            )
        )

    async def notify_reload(self, *, changed_paths: Sequence[str] | None = None) -> None:
        await self.broadcast(
            OverlayEvent(
                type="reload",
                payload={"changedPaths": list(changed_paths or [])},
            )
        )

    async def notify_log(
        self,
        *,
        level: str,
        message: str,
        logger_name: str = "",
    ) -> None:
        """Forward a server-side log record to connected overlay clients.

        Dev-only. Sends a ``"log"`` event whose ``level`` names the browser
        ``console`` method the client should call (``"log"``, ``"info"``,
        ``"warn"``, ``"error"`` or ``"debug"``). Used by
        :class:`pyxle.devserver.log_forwarding.BrowserConsoleLogHandler` to
        surface server logs in the browser devtools console.

        Parameters
        ----------
        level:
            The ``console`` method name the client should invoke.
        message:
            The already-formatted log message.
        logger_name:
            The originating logger's name (shown alongside the message).
        """
        await self.broadcast(
            OverlayEvent(
                type="log",
                payload={
                    "level": level,
                    "message": message,
                    "logger": logger_name,
                },
            )
        )

    async def websocket_endpoint(self, websocket: WebSocket) -> None:
        origin = websocket.headers.get("origin", "")
        if not self._is_allowed_origin(origin):
            await websocket.close(code=4003)
            return

        await self.register(websocket)
        try:
            while True:
                await websocket.receive_text()
        except WebSocketDisconnect:  # pragma: no cover - normal shutdown path
            pass
        finally:
            await self.unregister(websocket)


def _format_stacktrace(error: BaseException) -> str:
    return "".join(traceback.format_exception(type(error), error, error.__traceback__))


__all__ = ["OverlayManager", "OverlayEvent"]
