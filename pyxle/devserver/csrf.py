"""CSRF protection middleware using the double-submit cookie pattern.

The middleware sets a CSRF cookie on every response. By default the cookie
name is namespaced by the app's bind port (``pyxle-csrf-<port>``, read from
the ASGI scope's ``server`` entry) so two Pyxle apps on the same host —
cookies ignore ports — never overwrite each other's token; pass
``cookie_name`` to pin an explicit name. Requests that use state-changing
methods (POST, PUT, PATCH, DELETE) must echo the cookie value back via the
``X-CSRF-Token`` header **or** a ``_csrf_token`` form field. If the values
do not match, the request is rejected with 403.

Form bodies are handled without unbounded buffering: urlencoded bodies are
buffered up to a cap, and multipart bodies are *stream-parsed* only until
the ``_csrf_token`` field is found — file parts after the token are never
held in memory.

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
import posixpath
import re
import secrets
from dataclasses import dataclass
from typing import Any, Sequence
from urllib.parse import parse_qsl

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

try:  # python-multipart >= 0.0.12 ships under the renamed import package.
    from python_multipart.exceptions import FormParserError
    from python_multipart.multipart import MultipartParser, parse_options_header
except ImportError:  # pragma: no cover - legacy python-multipart module name
    from multipart.exceptions import FormParserError
    from multipart.multipart import MultipartParser, parse_options_header

from pyxle.config import default_csrf_cookie_name
from pyxle.security import constant_time_equals

_SAFE_METHODS: frozenset[str] = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})
_HEADER_NAME = "x-csrf-token"
_FORM_FIELD = "_csrf_token"
_TOKEN_LENGTH = 32
# The exact shape :func:`_generate_token` emits: a URL-safe base64 random
# value, optionally followed by ``.`` and a 16-character hex HMAC segment.
# Used to reject values this server could never have minted before one is
# trusted or written back into a ``Set-Cookie`` header.
_TOKEN_SHAPE = re.compile(r"[A-Za-z0-9_-]+(?:\.[0-9a-f]{16})?")
# Cap how much of a urlencoded body we buffer when extracting the CSRF form
# field. Larger payloads short-circuit and force the caller to send the
# token via the ``X-CSRF-Token`` header (the JS / fetch path).
_MAX_BUFFERED_BODY_BYTES = 1 * 1024 * 1024
# Cap on how many multipart bytes are scanned (and buffered for replay)
# while looking for the ``_csrf_token`` field. ``<Form>`` renders the hidden
# token field first, so legitimate progressive-enhancement posts find it in
# the first frames; 1 MiB leaves generous room for text fields and part
# headers ahead of it while bounding worst-case memory per request. Beyond
# the cap the request is rejected with guidance to use the header instead.
_MAX_MULTIPART_SCAN_BYTES = 1 * 1024 * 1024

_logger = logging.getLogger(__name__)


def _is_https(scope: Scope) -> bool:
    """Whether this request reached us over TLS.

    Reads ``X-Forwarded-Proto`` first, because the overwhelmingly common
    production shape is a TLS-terminating proxy speaking plain HTTP to the app
    — where the ASGI scheme says ``http`` and the *browser's* connection was
    HTTPS all along. Falls back to the scheme the server itself sees.
    """
    for name, value in scope.get("headers", ()):
        if name == b"x-forwarded-proto":
            first = value.decode("latin-1").split(",")[0].strip().lower()
            return first == "https"
    return str(scope.get("scheme", "")).lower() in ("https", "wss")


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
        Name of the CSRF cookie. ``None`` (the default) means *auto*: the
        cookie is named ``pyxle-csrf-<port>`` after the request's server
        port from the ASGI scope — a stable per-app discriminator that stops
        two Pyxle apps on the same host (cookies ignore ports) from stomping
        each other's token. Pass an explicit name to pin it instead.
    header_name:
        Name of the request header containing the CSRF token
        (default ``x-csrf-token``).
    cookie_secure:
        Set the ``Secure`` flag on the cookie. ``True`` in production — but a
        ``Secure`` cookie is *dropped entirely* by the browser over plain
        HTTP, so it is applied only when the request actually arrived over
        HTTPS (directly, or via ``X-Forwarded-Proto`` from a TLS-terminating
        proxy). Marking it unconditionally made every plain-HTTP production
        server reject its own forms with "CSRF token missing" and no clue
        why — and it protected nothing, because a connection with no
        confidentiality has no cookie confidentiality to lose.
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
        cookie_name: str | None = None,
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
        cookie_name = self._cookie_name or default_csrf_cookie_name(
            _scope_server_port(scope)
        )

        # The receive handed to the inner app. Replaced whenever the CSRF
        # check consumes body bytes (form-field validation), so downstream
        # code that calls ``request.form()`` / ``request.json()`` still sees
        # the complete original payload.
        downstream_receive: Receive = receive

        unsafe_method = method not in _SAFE_METHODS and not self._is_exempt(request.url.path)
        if unsafe_method:
            cookie_token = request.cookies.get(cookie_name, "")
            header_token = request.headers.get(self._header_name, "")

            # Header check is enough for fetch-driven submissions; only
            # touch the body for the progressive-enhancement form path.
            submitted_token = header_token
            missing_message = "CSRF token missing"
            if not submitted_token:
                # Media type matching is case-insensitive, but the multipart
                # boundary parameter is case-SENSITIVE — pass the original
                # header to the scanner and lowercase only for the checks.
                raw_content_type = request.headers.get("content-type") or ""
                content_type = raw_content_type.lower()
                if "application/x-www-form-urlencoded" in content_type:
                    body_bytes, truncated = await _drain_body(receive)
                    if truncated:
                        # The body exceeded the buffer cap, so we can neither
                        # trust the CSRF field nor safely replay the payload
                        # (replaying a truncated body silently corrupts the
                        # submission). Fail loud: large no-JS submissions must
                        # carry the token in the header instead.
                        await _reject(
                            scope,
                            receive,
                            send,
                            "Request body too large for form CSRF validation; "
                            f"send the CSRF token via the '{self._header_name}' header.",
                            status_code=413,
                        )
                        return
                    submitted_token = _urlencoded_field_value(body_bytes, _FORM_FIELD)
                    downstream_receive = _replay_receive(body_bytes)
                elif "multipart/form-data" in content_type:
                    # Stream-parse only until the token field is complete:
                    # the consumed frames are buffered for replay, the rest
                    # of the stream (file parts) is never read here.
                    scan = await _scan_multipart_for_token(
                        receive, content_type=raw_content_type, field_name=_FORM_FIELD
                    )
                    if scan.token is None:
                        missing_message = _multipart_missing_message(
                            self._header_name, over_cap=scan.over_cap
                        )
                    else:
                        submitted_token = scan.token
                    downstream_receive = (
                        _replay_receive(scan.consumed)
                        if scan.stream_exhausted
                        else _resume_receive(scan.consumed, receive)
                    )

            if not submitted_token:
                await _reject(scope, receive, send, missing_message)
                return

            if not cookie_token:
                # A token was submitted but there is no cookie to compare it
                # against — distinct from a missing token so the developer
                # sees which half of the double-submit pair is absent.
                await _reject(scope, receive, send, "CSRF cookie missing")
                return

            if not _tokens_match(cookie_token, submitted_token, self._secret):
                await _reject(scope, receive, send, "CSRF token mismatch")
                return

        # Reuse the existing valid cookie token when available to avoid
        # race conditions with concurrent requests (M-9).  Only mint a
        # fresh token when no valid cookie is present.
        existing_cookie = request.cookies.get(cookie_name, "")
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
                        f"{cookie_name}={token}; Path=/"
                        f"; SameSite={self._cookie_samesite}"
                    )
                    if self._cookie_secure and _is_https(scope):
                        cookie_value += "; Secure"
                    headers.append((b"set-cookie", cookie_value.encode("latin-1")))
                    message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, downstream_receive, send_with_cookie)

    def _is_exempt(self, path: str) -> bool:
        """Return ``True`` when ``path`` is CSRF-exempt.

        Matches on path-segment boundaries, not arbitrary string prefixes: an
        exempt entry ``/api/webhooks`` covers ``/api/webhooks`` and
        ``/api/webhooks/<anything>`` but NOT an adjacently-named sibling such
        as ``/api/webhooks-admin`` (which a bare ``startswith`` would wrongly
        exempt, silently widening the CSRF hole).

        The request path is canonicalised first (``..``/``.`` resolved,
        repeated slashes collapsed) so a non-canonical path like
        ``/api/webhooks/../action`` cannot dodge the canonical exemption
        decision; the check fails closed (CSRF enforced) on the resolved path.
        """
        norm_path = _canonical_path(path)
        for prefix in self._exempt_paths:
            base = _canonical_path(prefix).rstrip("/")
            if not base:
                # An exempt entry of "/" (or empty) would exempt everything;
                # ignore it rather than silently disabling CSRF site-wide.
                continue
            if norm_path == base or norm_path.startswith(base + "/"):
                return True
        return False


async def _reject(
    scope: Scope,
    receive: Receive,
    send: Send,
    error: str,
    *,
    status_code: int = 403,
) -> None:
    """Send the standard CSRF rejection payload and end the request."""
    response = JSONResponse({"ok": False, "error": error}, status_code=status_code)
    await response(scope, receive, send)


def _scope_server_port(scope: Scope) -> int | None:
    """Bind port from the ASGI ``server`` scope entry.

    Returns ``None`` when the port is unknown — an absent ``server`` entry or
    a unix-socket bind, where the ASGI spec puts ``None`` in the port slot.
    """
    server = scope.get("server")
    if not server:
        return None
    try:
        port = server[1]
    except (IndexError, TypeError):
        return None
    return port if isinstance(port, int) else None


def _multipart_missing_message(header_name: str, *, over_cap: bool) -> str:
    """Actionable 403 message for a multipart body with no usable token."""
    hint = (
        f"send the CSRF token in the '{header_name}' request header, or place "
        f"the '{_FORM_FIELD}' field before any file fields (Pyxle's <Form> "
        "renders it first automatically)"
    )
    if over_cap:
        cap_mib = _MAX_MULTIPART_SCAN_BYTES // (1024 * 1024)
        return (
            f"CSRF token missing: scanned the first {cap_mib} MiB of the "
            f"multipart body without finding a '{_FORM_FIELD}' field. For "
            f"large multipart posts, {hint}."
        )
    return (
        f"CSRF token missing: the multipart body contains no '{_FORM_FIELD}' "
        f"field. To fix, {hint}."
    )


def _canonical_path(path: str) -> str:
    """Canonicalise a URL path for exemption matching.

    Resolves ``.``/``..`` segments and collapses repeated slashes via
    ``posixpath.normpath``, always returning an absolute path. ``normpath``
    preserves a leading ``//`` (a POSIX quirk), so that is collapsed too.
    """
    if not path:
        return "/"
    normalized = posixpath.normpath(path)
    while normalized.startswith("//"):
        normalized = normalized[1:]
    if not normalized.startswith("/"):
        normalized = "/" + normalized
    return normalized


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
    """Check that a token is one this server could have minted.

    Two gates:

    1. **Shape** — the value must match the alphabet
       :func:`_generate_token` emits (URL-safe base64, plus an optional
       ``.<16 hex>`` signature segment). Anything else cannot be one of our
       tokens, so rejecting it costs nothing and buys a great deal: the
       caller *echoes an accepted token straight back into the response's*
       ``Set-Cookie`` *header*, and a cookie value can smuggle ``;`` and
       ``=`` through ``http.cookies`` octal escapes (``"x\\073 Domain\\075…"``
       parses to ``x; Domain=…``). Without this check a request could dictate
       the attributes of the cookie the server sets on itself.
    2. **HMAC signature** (only when a secret is configured) — proves the
       token was minted here rather than chosen by whoever sent the cookie.

    With no secret configured the shape gate is all there is, which is the
    documented weaker mode the constructor already warns about.
    """
    if not token or not _TOKEN_SHAPE.fullmatch(token):
        return False
    if not secret:
        # No secret → unsigned tokens; any well-formed value is acceptable.
        return True
    if "." not in token:
        return False
    raw, _, sig = token.rpartition(".")
    if not raw or not sig:
        return False
    expected = _compute_signature(raw, secret)
    return constant_time_equals(sig, expected)


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

    # Double-submit: submitted value must match cookie value. Both sides are
    # attacker-controlled strings decoded from the wire, so the comparison
    # must tolerate any byte (see :func:`constant_time_equals`).
    if not constant_time_equals(cookie_token, submitted_token):
        return False

    # HMAC integrity: verify the cookie token was minted by this server.
    if not _verify_token_integrity(cookie_token, secret):
        return False

    return True


async def _drain_body(receive: Receive) -> tuple[bytes, bool]:
    """Buffer the ASGI request body, stopping at the size cap.

    Returns ``(body, truncated)`` where ``truncated`` is ``True`` if the body
    exceeded :data:`_MAX_BUFFERED_BODY_BYTES` and bytes were dropped. The
    caller must not replay a truncated body to the inner app (it would be
    silently corrupted); instead it rejects the request with ``413`` and asks
    for the CSRF token via the header, so we never hold a giant upload in
    memory just to hunt for ``_csrf_token``.
    """
    chunks: list[bytes] = []
    total = 0
    truncated = False
    while True:
        message = await receive()
        if message["type"] != "http.request":
            break
        chunk: bytes = message.get("body", b"") or b""
        if chunk:
            remaining = _MAX_BUFFERED_BODY_BYTES - total
            if remaining <= 0:
                # Over the cap already — record loss and drain remaining
                # frames so receive() doesn't block downstream.
                truncated = True
                if not message.get("more_body"):
                    break
                continue
            if len(chunk) > remaining:
                chunks.append(chunk[:remaining])
                total = _MAX_BUFFERED_BODY_BYTES
                truncated = True
            else:
                chunks.append(chunk)
                total += len(chunk)
        if not message.get("more_body"):
            break
    return b"".join(chunks), truncated


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


def _resume_receive(prefix: bytes, receive: Receive) -> Receive:
    """Build a ``receive`` that replays ``prefix`` then resumes the stream.

    Used after the multipart CSRF scan found the token part-way through the
    body: the frames the scan consumed are handed downstream first (as one
    ``more_body=True`` frame), then every later call delegates to the
    original ``receive`` — so the inner app sees the complete original body
    byte-for-byte while file parts after the token are streamed, never
    buffered here.
    """
    sent_prefix = False

    async def resumed() -> Message:
        nonlocal sent_prefix
        if not sent_prefix:
            sent_prefix = True
            return {"type": "http.request", "body": prefix, "more_body": True}
        return await receive()

    return resumed


def _urlencoded_field_value(body: bytes, field_name: str) -> str:
    """Pull ``field_name`` out of an ``application/x-www-form-urlencoded``
    body without going through Starlette's ``request.form()`` (which would
    re-consume the receive stream we already drained).

    Returns ``""`` for any parse failure — the caller treats that the same
    as a missing token, which surfaces a 403 to the client.
    """
    if not body:
        return ""
    try:
        decoded = body.decode("utf-8", errors="replace")
        for key, value in parse_qsl(decoded, keep_blank_values=True):
            if key == field_name:
                return value
    except Exception as exc:  # pragma: no cover - defensive
        _logger.debug("urlencoded CSRF parse failed: %s", exc)
    return ""


class _MultipartFieldScanner:
    """Callback target for python-multipart's streaming ``MultipartParser``.

    Tracks part headers as they stream past and captures the value of one
    named field (the CSRF token). ``token`` flips from ``None`` to the
    decoded value the moment the target part *ends* — the feed loop stops
    consuming the request stream right there, so parts after the token
    (file uploads) are never parsed or buffered.
    """

    __slots__ = (
        "token",
        "_field_name",
        "_header_field",
        "_header_value",
        "_content_disposition",
        "_capturing",
        "_value",
    )

    def __init__(self, field_name: str) -> None:
        self.token: str | None = None
        self._field_name = field_name.encode("utf-8")
        self._header_field = bytearray()
        self._header_value = bytearray()
        self._content_disposition = b""
        self._capturing = False
        self._value = bytearray()

    def callbacks(self) -> dict[str, Any]:
        """Callback mapping for ``MultipartParser(boundary, callbacks)``."""
        return {
            "on_part_begin": self._on_part_begin,
            "on_header_field": self._on_header_field,
            "on_header_value": self._on_header_value,
            "on_header_end": self._on_header_end,
            "on_headers_finished": self._on_headers_finished,
            "on_part_data": self._on_part_data,
            "on_part_end": self._on_part_end,
        }

    def _on_part_begin(self) -> None:
        self._content_disposition = b""
        self._capturing = False

    def _on_header_field(self, data: bytes, start: int, end: int) -> None:
        self._header_field += data[start:end]

    def _on_header_value(self, data: bytes, start: int, end: int) -> None:
        self._header_value += data[start:end]

    def _on_header_end(self) -> None:
        if bytes(self._header_field).lower() == b"content-disposition":
            self._content_disposition = bytes(self._header_value)
        self._header_field.clear()
        self._header_value.clear()

    def _on_headers_finished(self) -> None:
        if self.token is not None or not self._content_disposition:
            return
        _, params = parse_options_header(self._content_disposition)
        if params.get(b"name") == self._field_name:
            self._capturing = True
            self._value.clear()

    def _on_part_data(self, data: bytes, start: int, end: int) -> None:
        if self._capturing:
            self._value += data[start:end]

    def _on_part_end(self) -> None:
        if self._capturing:
            self.token = bytes(self._value).decode("utf-8", errors="replace")
            self._capturing = False


@dataclass(frozen=True, slots=True)
class _MultipartScan:
    """Outcome of stream-scanning a multipart body for the CSRF field.

    ``consumed`` holds the raw bytes read off the receive stream (needed to
    replay them downstream); ``stream_exhausted`` records whether the final
    body frame was read, and ``over_cap`` whether the scan stopped at
    :data:`_MAX_MULTIPART_SCAN_BYTES` without finding the field.
    """

    token: str | None
    consumed: bytes
    stream_exhausted: bool
    over_cap: bool


async def _scan_multipart_for_token(
    receive: Receive,
    *,
    content_type: str,
    field_name: str,
) -> _MultipartScan:
    """Stream-parse a multipart body only until ``field_name`` is obtained.

    Feeds request frames into python-multipart's callback parser and stops
    the moment the target field's part ends, the stream ends, the byte cap
    is hit, or the body proves malformed. Every consumed byte is kept for
    replay; nothing past the stop point is read, so file parts after the
    token are never buffered by the CSRF layer.
    """
    _, params = parse_options_header(content_type)
    boundary = params.get(b"boundary")
    if not boundary:
        # Malformed content-type: nothing has been consumed, and there is no
        # way to locate the field. The caller rejects with 403.
        return _MultipartScan(token=None, consumed=b"", stream_exhausted=False, over_cap=False)

    scanner = _MultipartFieldScanner(field_name)
    parser = MultipartParser(boundary, scanner.callbacks())

    chunks: list[bytes] = []
    total = 0
    fed = 0
    stream_exhausted = False
    over_cap = False
    while True:
        message = await receive()
        if message["type"] != "http.request":
            # Client disconnected mid-body; no more bytes will arrive.
            stream_exhausted = True
            break
        chunk: bytes = message.get("body", b"") or b""
        more_body = bool(message.get("more_body"))
        if chunk:
            chunks.append(chunk)
            total += len(chunk)
            # Feed the parser only up to the scan cap. A frame's tail past
            # the cap is kept for replay (the transport already delivered it
            # into memory) but never parsed, so the cap is a hard bound on
            # the pre-token prefix regardless of how the body was framed.
            allowed = _MAX_MULTIPART_SCAN_BYTES - fed
            to_feed = chunk[:allowed] if allowed < len(chunk) else chunk
            if to_feed:
                fed += len(to_feed)
                try:
                    parser.write(to_feed)
                except FormParserError as exc:
                    # Malformed multipart — fail closed (token treated missing).
                    _logger.debug("multipart CSRF scan stopped on parse error: %s", exc)
                    stream_exhausted = not more_body
                    break
        if scanner.token is not None:
            stream_exhausted = not more_body
            break
        if fed >= _MAX_MULTIPART_SCAN_BYTES and (total > fed or more_body):
            # The cap is exhausted and unscanned bytes exist (or more are
            # coming) — the field may still be out there, but finding it
            # would mean unbounded scanning. Reject with guidance.
            over_cap = True
            stream_exhausted = not more_body
            break
        if not more_body:
            stream_exhausted = True
            break
    return _MultipartScan(
        token=scanner.token,
        consumed=b"".join(chunks),
        stream_exhausted=stream_exhausted,
        over_cap=over_cap,
    )


__all__ = ["CsrfMiddleware"]
