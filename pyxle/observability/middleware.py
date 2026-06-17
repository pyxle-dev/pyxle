"""Correlation-ID and request-timing ASGI middleware (zero third-party deps)."""

from __future__ import annotations

import re
import time
import uuid
from typing import Any, Awaitable, Callable, MutableMapping

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

# The per-request namespace already used by the route-metadata hook
# (``request.scope["pyxle"]``). Observability fields live alongside it.
_SCOPE_KEY = "pyxle"
_REQUEST_ID_FIELD = "request_id"
_DURATION_FIELD = "duration_ms"

# An incoming request id is only echoed when explicitly trusted, and even then
# must look like an id — bounded length and a conservative character set — so a
# spoofed header can't inject newlines or control characters into logs.
_SAFE_REQUEST_ID = re.compile(r"\A[A-Za-z0-9._\-]{1,128}\Z")


def _is_safe_request_id(value: str) -> bool:
    return bool(_SAFE_REQUEST_ID.match(value))


def get_request_id(request: Any) -> str | None:
    """Return the correlation id for *request*, or ``None`` if unset.

    Reads ``request.state.request_id`` first (the ergonomic path for loaders
    and actions), falling back to the raw ASGI ``scope["pyxle"]`` namespace for
    code that runs before ``request.state`` is materialised.
    """
    state = getattr(request, "state", None)
    if state is not None:
        rid = getattr(state, _REQUEST_ID_FIELD, None)
        if rid:
            return rid
    scope = getattr(request, "scope", None)
    if isinstance(scope, MutableMapping):
        pyxle_state = scope.get(_SCOPE_KEY)
        if isinstance(pyxle_state, MutableMapping):
            return pyxle_state.get(_REQUEST_ID_FIELD)
    return None


def request_timing_ms(request: Any) -> float | None:
    """Return the elapsed wall-clock time (ms) recorded for *request*, or ``None``.

    Populated when timing is enabled, at the point the response starts.
    """
    scope = getattr(request, "scope", None)
    if isinstance(scope, MutableMapping):
        pyxle_state = scope.get(_SCOPE_KEY)
        if isinstance(pyxle_state, MutableMapping):
            value = pyxle_state.get(_DURATION_FIELD)
            if isinstance(value, (int, float)):
                return float(value)
    return None


class RequestIdMiddleware:
    """Assign a correlation id to every HTTP request and time it.

    For each ``http`` request this middleware:

    * resolves a request id — a freshly generated ``uuid4().hex`` unless
      ``trust_incoming`` is set *and* the inbound ``header_name`` carries a
      well-formed one;
    * exposes it on ``scope["pyxle"]["request_id"]`` and on ``request.state``
      (so loaders and actions can read ``request.state.request_id``);
    * echoes it back as the ``header_name`` response header; and
    * when ``timing`` is enabled, records the wall-clock request duration (ms,
      measured to ``http.response.start``) on ``scope["pyxle"]["duration_ms"]``.

    It is a pure-ASGI callable (not ``BaseHTTPMiddleware``), so it never buffers
    the response body or spawns a task: the per-request cost is one ``uuid4``
    and two ``perf_counter`` reads. Non-HTTP scopes pass straight through.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        emit_request_id: bool = True,
        header_name: str = "X-Request-Id",
        trust_incoming: bool = False,
        timing: bool = True,
        metrics: Any = None,
        access_log: bool = False,
    ) -> None:
        self.app = app
        self.emit_request_id = emit_request_id
        self.header_name = header_name
        self._header_bytes = header_name.encode("latin-1")
        self._header_lower = header_name.lower().encode("latin-1")
        self.trust_incoming = trust_incoming
        self.timing = timing
        # Optional MetricsRegistry; request totals are recorded into it when set.
        self.metrics = metrics
        # Emit one structured access-log line per request when enabled.
        self.access_log = access_log

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        pyxle_state = scope.setdefault(_SCOPE_KEY, {})
        request_id = None
        if self.emit_request_id:
            request_id = self._resolve_request_id(scope)
            pyxle_state[_REQUEST_ID_FIELD] = request_id
            # Mirror onto request.state's backing dict so that
            # ``request.state.request_id`` works inside loaders and actions.
            scope.setdefault("state", {})[_REQUEST_ID_FIELD] = request_id
        # Bind the id (or None) into the logging context so any log emitted while
        # handling this request carries it. Per-task contextvar — no leak across
        # requests (each runs in its own asyncio task/context).
        from pyxle.observability.logging import bind_request_id  # noqa: PLC0415

        bind_request_id(request_id)

        # Measure when timing is on, or when a registry/access log needs it.
        measure = self.timing or self.metrics is not None or self.access_log
        start = time.perf_counter() if measure else 0.0

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                elapsed_ms = (time.perf_counter() - start) * 1000.0 if measure else 0.0
                status = int(message.get("status", 0))
                if self.timing:
                    pyxle_state[_DURATION_FIELD] = elapsed_ms
                if self.metrics is not None:
                    self.metrics.observe_request(status, elapsed_ms)
                if self.access_log:
                    from pyxle.observability.logging import log_access  # noqa: PLC0415

                    log_access(
                        method=scope.get("method", ""),
                        path=scope.get("path", ""),
                        status=status,
                        duration_ms=elapsed_ms,
                    )
                if request_id is not None:
                    headers = message.setdefault("headers", [])
                    # Drop any upstream copy of the header, then append ours so
                    # the response carries exactly one correlation id.
                    headers[:] = [
                        (key, value)
                        for (key, value) in headers
                        if key.lower() != self._header_lower
                    ]
                    headers.append((self._header_bytes, request_id.encode("latin-1")))
            await send(message)

        await self.app(scope, receive, send_wrapper)

    def _resolve_request_id(self, scope: Scope) -> str:
        if self.trust_incoming:
            for key, value in scope.get("headers", ()):
                if key.lower() == self._header_lower:
                    try:
                        candidate = value.decode("latin-1").strip()
                    except (UnicodeDecodeError, AttributeError):  # pragma: no cover
                        candidate = ""
                    if candidate and _is_safe_request_id(candidate):
                        return candidate
                    break
        return uuid.uuid4().hex


__all__ = ["RequestIdMiddleware", "get_request_id", "request_timing_ms"]
