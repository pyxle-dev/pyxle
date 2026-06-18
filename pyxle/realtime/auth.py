"""Authentication helpers for WebSocket handlers.

**Why this exists.** Pyxle's auth plugin populates ``request.user`` via a
``BaseHTTPMiddleware``, which Starlette runs ONLY for ``http`` scope — never for
a WebSocket upgrade. So inside a page's ``async def websocket(ws)`` there is no
``request.user`` and no CSRF check. A WS handler that needs the signed-in user
must resolve the session itself; :func:`authenticate_websocket` does exactly
what the HTTP middleware does, from the cookie on the upgrade request.

Because CSRF doesn't apply to a WebSocket upgrade, an **origin check** is the
equivalent guard against a hostile page opening a socket to your app with the
victim's cookie. :func:`origin_allowed` is provided for that; enforce it at the
top of a handler when the socket carries privileged state.

Both helpers degrade safely when the auth plugin isn't installed (returning
``None`` / doing zero work), so this stays zero-coupling core code.
"""

from __future__ import annotations

import logging
from typing import Any, Iterable

from starlette.websockets import WebSocket

_logger = logging.getLogger("pyxle.realtime.auth")

_AUTH_SERVICE = "auth.service"


async def authenticate_websocket(ws: WebSocket) -> Any | None:
    """Resolve the signed-in user for a WebSocket upgrade, or ``None``.

    Reads the auth plugin's session cookie off the handshake and resolves it
    via the registered ``auth.service`` (the same call the HTTP middleware
    makes). Returns ``None`` — doing zero database work — when the auth plugin
    isn't installed or no session cookie is present. Never raises on an
    anonymous connection; branch on the result.

    Usage::

        async def websocket(ws):
            user = await authenticate_websocket(ws)
            if user is None:
                await ws.close(code=4401)  # unauthorized
                return
            await ws.accept()
            ...
    """
    service = _auth_service(ws)
    if service is None:
        return None
    cookie_value = ws.cookies.get(service.settings.cookie_name)
    if not cookie_value:
        return None
    try:
        return await service.resolve_session(cookie_value=cookie_value, extend=True)
    except Exception:
        # A transient backend failure (DB down, timeout) must not crash the WS
        # upgrade — degrade to anonymous, matching the helper's safe-degradation
        # contract. The handler decides whether to close or continue read-only.
        _logger.warning("authenticate_websocket: session resolution failed", exc_info=True)
        return None


def origin_allowed(ws: WebSocket, allowed_origins: Iterable[str]) -> bool:
    """Whether the WS upgrade's ``Origin`` is permitted.

    Cross-site WebSocket requests carry an ``Origin`` header that the same-origin
    policy does NOT block (unlike fetch), so checking it is the WS equivalent of
    CSRF protection. An empty ``allowed_origins`` allows everything (opt-in); a
    missing ``Origin`` header (same-origin navigations and non-browser clients
    never send one cross-site) is allowed. Comparison ignores a trailing slash.
    """
    allowed = {origin.rstrip("/") for origin in allowed_origins if origin}
    if not allowed:
        return True
    origin = ws.headers.get("origin", "")
    if not origin:
        return True
    return origin.rstrip("/") in allowed


def _auth_service(ws: WebSocket) -> Any | None:
    try:
        app = ws.app
    except KeyError:
        return None
    plugins = getattr(app.state, "pyxle_plugins", None)
    if plugins is None:
        return None
    getter = getattr(plugins, "get", None)
    if getter is None:
        return None
    return getter(_AUTH_SERVICE)


__all__ = ["authenticate_websocket", "origin_allowed"]
