"""CSRF protection middleware using the double-submit cookie pattern.

The middleware sets a ``pyxle-csrf`` cookie on every response. Requests that
use state-changing methods (POST, PUT, PATCH, DELETE) must echo the cookie
value back via the ``X-CSRF-Token`` header **or** a ``_csrf_token`` form
field. If the values do not match, the request is rejected with 403.

Safe methods (GET, HEAD, OPTIONS, TRACE) are never checked.

Usage::

    from pyxle.devserver.csrf import CsrfMiddleware
    from starlette.middleware import Middleware

    middleware = [Middleware(CsrfMiddleware, secret="...")]
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
from email.parser import BytesParser
from email.policy import default as _email_default_policy
from typing import Sequence
from urllib.parse import parse_qsl

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

_SAFE_METHODS: frozenset[str] = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})
_COOKIE_NAME = "pyxle-csrf"
_HEADER_NAME = "x-csrf-token"
_FORM_FIELD = "_csrf_token"
_TOKEN_LENGTH = 32
# Cap how much body we buffer when extracting the CSRF form field.
# Larger uploads short-circuit and force the caller to send the token via
# the ``X-CSRF-Token`` header (the JS / fetch path).
_MAX_BUFFERED_BODY_BYTES = 1 * 1024 * 1024

_logger = logging.getLogger(__name__)


class CsrfMiddleware:
    """Double-submit cookie CSRF protection.

    Parameters
    ----------
    app:
        The ASGI application to wrap.
    secret:
        A server-side secret used to sign CSRF tokens. Should be sourced from
        ``PYXLE_SECRET_KEY`` or a similar environment variable.
    cookie_name:
        Name of the CSRF cookie (default ``pyxle-csrf``).
    header_name:
        Name of the request header containing the CSRF token
        (default ``x-csrf-token``).
    cookie_secure:
        Set the ``Secure`` flag on the cookie. ``True`` in production.
    cookie_samesite:
        ``SameSite`` attribute for the cookie (default ``"lax"``).
    exempt_paths:
        URL path prefixes exempt from CSRF checks (e.g., ``/api/webhooks``).
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        secret: str = "",
        cookie_name: str = _COOKIE_NAME,
        header_name: str = _HEADER_NAME,
        cookie_secure: bool = False,
        cookie_samesite: str = "lax",
        exempt_paths: Sequence[str] = (),
    ) -> None:
        self.app = app
        self._secret = secret
        self._cookie_name = cookie_name
        self._header_name = header_name.lower()
        self._cookie_secure = cookie_secure
        self._cookie_samesite = cookie_samesite
        self._exempt_paths: tuple[str, ...] = tuple(exempt_paths)

        if not self._secret:
            _logger.warning(
                "CsrfMiddleware: no secret key provided (PYXLE_SECRET_KEY). "
                "HMAC token verification is disabled — tokens are validated "
                "by double-submit comparison only."
            )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request = Request(scope, receive)
        method = request.method.upper()

        # Buffer the request body once so we can both validate the CSRF
        # form field AND let downstream code (action dispatch, user code)
        # call ``request.json()`` / ``request.form()`` afterwards. Without
        # buffering, ``await request.form()`` here would consume the ASGI
        # ``receive()`` stream and the inner app would see an empty body.
        body_bytes: bytes | None = None
        unsafe_method = method not in _SAFE_METHODS and not self._is_exempt(request.url.path)
        if unsafe_method:
            cookie_token = request.cookies.get(self._cookie_name, "")
            header_token = request.headers.get(self._header_name, "")

            # Header check is enough for fetch-driven submissions; only
            # touch the body for the progressive-enhancement form path.
            submitted_token = header_token
            if not submitted_token:
                content_type = (request.headers.get("content-type") or "").lower()
                is_form_body = (
                    "application/x-www-form-urlencoded" in content_type
                    or "multipart/form-data" in content_type
                )
                if is_form_body:
                    body_bytes = await _drain_body(receive)
                    submitted_token = _extract_csrf_form_field(
                        body_bytes,
                        content_type=content_type,
                        field_name=_FORM_FIELD,
                    )

            if not cookie_token or not submitted_token:
                response = JSONResponse(
                    {"ok": False, "error": "CSRF token missing"},
                    status_code=403,
                )
                await response(scope, receive, send)
                return

            if not _tokens_match(cookie_token, submitted_token, self._secret):
                response = JSONResponse(
                    {"ok": False, "error": "CSRF token mismatch"},
                    status_code=403,
                )
                await response(scope, receive, send)
                return

        # Reuse the existing valid cookie token when available to avoid
        # race conditions with concurrent requests (M-9).  Only mint a
        # fresh token when no valid cookie is present.
        existing_cookie = request.cookies.get(self._cookie_name, "")
        if existing_cookie and _verify_token_integrity(existing_cookie, self._secret):
            token = existing_cookie
        else:
            token = _generate_token(self._secret)

        # Expose the active token to downstream handlers (notably the SSR
        # renderer) so the page render can embed it in <Form> as a hidden
        # field. Surviving server renders need this BEFORE the response
        # cookie is set — at this point the token is already known.
        # Stored under a dedicated scope key (rather than ``scope['state']``
        # which Starlette manages as a ``State`` object) so we never
        # collide with user-mutated request.state attributes.
        scope["pyxle.csrf_token"] = token

        # If we drained the body for CSRF form-field validation, replay it
        # to the inner app so user code that calls ``await request.form()``
        # / ``await request.json()`` still sees the original payload.
        downstream_receive = (
            _replay_receive(body_bytes) if body_bytes is not None else receive
        )

        async def send_with_cookie(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                # Never attach a per-user CSRF cookie to a response that a
                # shared cache (CDN / proxy) may store and replay to other
                # users — a `Set-Cookie` also stops most CDNs from caching at
                # all. The page handler marks such responses `Cache-Control:
                # public`; those routes render no per-user data and any
                # state-changing actions on them must be CSRF-exempt.
                if not _is_public_cacheable(headers):
                    cookie_value = (
                        f"{self._cookie_name}={token}; Path=/"
                        f"; SameSite={self._cookie_samesite}"
                    )
                    if self._cookie_secure:
                        cookie_value += "; Secure"
                    headers.append((b"set-cookie", cookie_value.encode("latin-1")))
                    message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, downstream_receive, send_with_cookie)

    def _is_exempt(self, path: str) -> bool:
        return any(path.startswith(prefix) for prefix in self._exempt_paths)


def _is_public_cacheable(headers: list[tuple[bytes, bytes]]) -> bool:
    """Return ``True`` when the response is marked publicly cacheable
    (``Cache-Control: public`` with a shared ``s-maxage`` / ``max-age``).

    A shared cache stores and replays such a response to many users, so it must
    never carry a per-user ``Set-Cookie``.
    """
    for name, value in headers:
        if name.lower() == b"cache-control":
            cc = value.lower()
            return b"public" in cc and (b"s-maxage" in cc or b"max-age" in cc)
    return False


def _generate_token(secret: str) -> str:
    """Generate a CSRF token (random value signed with the secret)."""
    raw = secrets.token_urlsafe(_TOKEN_LENGTH)
    if secret:
        sig = hmac.new(secret.encode(), raw.encode(), hashlib.sha256).hexdigest()[:16]
        return f"{raw}.{sig}"
    return raw


def _compute_signature(raw: str, secret: str) -> str:
    """Compute the HMAC-SHA256 signature for a raw token value."""
    return hmac.new(secret.encode(), raw.encode(), hashlib.sha256).hexdigest()[:16]


def _verify_token_integrity(token: str, secret: str) -> bool:
    """Check that a token's HMAC signature is valid (when a secret is set).

    Returns ``True`` if the token is structurally valid.  For unsigned tokens
    (no secret), any non-empty token is considered valid.
    """
    if not token:
        return False
    if not secret:
        # No secret → unsigned tokens; any non-empty value is acceptable.
        return True
    if "." not in token:
        return False
    raw, _, sig = token.rpartition(".")
    if not raw or not sig:
        return False
    expected = _compute_signature(raw, secret)
    return hmac.compare_digest(sig, expected)


def _tokens_match(cookie_token: str, submitted_token: str, secret: str) -> bool:
    """Validate that the submitted token matches the cookie token.

    Performs two checks:

    1. **Double-submit comparison** — the submitted token must match the
       cookie token (constant-time).
    2. **HMAC signature verification** (when a secret is configured) —
       the cookie token's signature must be valid.  This prevents an
       attacker who can set arbitrary cookies from forging tokens.
    """
    if not cookie_token or not submitted_token:
        return False

    # Double-submit: submitted value must match cookie value.
    if not hmac.compare_digest(cookie_token, submitted_token):
        return False

    # HMAC integrity: verify the cookie token was minted by this server.
    if not _verify_token_integrity(cookie_token, secret):
        return False

    return True


async def _drain_body(receive: Receive) -> bytes:
    """Buffer the entire ASGI request body, stopping at the size cap.

    Returns the raw bytes (possibly empty). Bodies above
    :data:`_MAX_BUFFERED_BODY_BYTES` are truncated to that cap — at that
    point the caller is expected to send the CSRF token via the header
    instead of the form field, so we never hold a giant upload in memory
    just to hunt for ``_csrf_token``.
    """
    chunks: list[bytes] = []
    total = 0
    while True:
        message = await receive()
        if message["type"] != "http.request":
            break
        chunk: bytes = message.get("body", b"") or b""
        if chunk:
            remaining = _MAX_BUFFERED_BODY_BYTES - total
            if remaining <= 0:
                # Drain remaining frames so receive() doesn't block downstream.
                if not message.get("more_body"):
                    break
                continue
            if len(chunk) > remaining:
                chunks.append(chunk[:remaining])
                total = _MAX_BUFFERED_BODY_BYTES
            else:
                chunks.append(chunk)
                total += len(chunk)
        if not message.get("more_body"):
            break
    return b"".join(chunks)


def _replay_receive(body: bytes) -> Receive:
    """Build a ``receive`` callable that hands the buffered body back to
    downstream ASGI code, then waits indefinitely for a real disconnect.

    Synthesising a fake ``http.disconnect`` after the body would race
    response-sending code: ``Request.is_disconnected()`` polls
    ``receive()`` while the response is being written, and a premature
    disconnect makes uvicorn drop the in-flight response — surfacing as
    "ASGI callable returned without completing response" with an empty
    body and the cryptic 200/0-bytes pair clients see.

    The forever-pending future is fine because uvicorn cancels the task
    when the connection actually closes; this coroutine never has to
    complete on its own.
    """
    import asyncio as _asyncio

    state = {"sent_body": False}
    forever = _asyncio.Event()  # never set

    async def receive() -> Message:
        if not state["sent_body"]:
            state["sent_body"] = True
            return {"type": "http.request", "body": body, "more_body": False}
        await forever.wait()
        return {"type": "http.disconnect"}  # pragma: no cover - unreachable

    return receive


def _extract_csrf_form_field(
    body: bytes,
    *,
    content_type: str,
    field_name: str,
) -> str:
    """Pull the CSRF token out of a form-encoded body without going through
    Starlette's ``request.form()`` (which would re-consume the receive
    stream we already drained, and which silently fails when
    ``python-multipart`` is missing).

    Supports both ``application/x-www-form-urlencoded`` and
    ``multipart/form-data``. Returns ``""`` for any parse failure — the
    caller treats that the same as a missing token, which surfaces a 403
    to the client.
    """
    if not body:
        return ""

    if "application/x-www-form-urlencoded" in content_type:
        try:
            decoded = body.decode("utf-8", errors="replace")
            for key, value in parse_qsl(decoded, keep_blank_values=True):
                if key == field_name:
                    return value
        except Exception as exc:  # pragma: no cover - defensive
            _logger.debug("urlencoded CSRF parse failed: %s", exc)
        return ""

    if "multipart/form-data" in content_type:
        # Cheap multipart-form parser using stdlib ``email`` — enough to
        # find a small, named text field. We deliberately do NOT call
        # Starlette's ``request.form()`` here: it requires the optional
        # ``python-multipart`` dep, and surfacing that as a 403 ("CSRF
        # token missing") buries the real cause.
        try:
            header_blob = (
                f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode()
            )
            parser = BytesParser(policy=_email_default_policy)
            message = parser.parsebytes(header_blob + body)
            if not message.is_multipart():
                return ""
            for part in message.iter_parts():
                disposition = part.get("content-disposition", "")
                if f'name="{field_name}"' not in disposition:
                    continue
                payload = part.get_payload(decode=True)
                if isinstance(payload, bytes):
                    return payload.decode(part.get_content_charset() or "utf-8", errors="replace")
                if isinstance(payload, str):
                    return payload
        except Exception as exc:  # pragma: no cover - defensive
            _logger.debug("multipart CSRF parse failed: %s", exc)

    return ""


__all__ = ["CsrfMiddleware"]
