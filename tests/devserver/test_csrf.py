"""Tests for pyxle.devserver.csrf — CSRF protection middleware."""

from __future__ import annotations

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from pyxle.devserver.csrf import CsrfMiddleware, _generate_token, _tokens_match


# ---------------------------------------------------------------------------
# Token helpers
# ---------------------------------------------------------------------------


class TestTokenHelpers:
    def test_generate_token_without_secret(self):
        token = _generate_token("")
        assert isinstance(token, str)
        assert len(token) > 10

    def test_generate_token_with_secret(self):
        token = _generate_token("my-secret")
        assert "." in token
        raw, sig = token.rsplit(".", 1)
        assert len(sig) == 16

    def test_tokens_match_identical(self):
        assert _tokens_match("abc", "abc", "") is True

    def test_tokens_mismatch(self):
        assert _tokens_match("abc", "xyz", "") is False

    def test_tokens_match_empty_rejected(self):
        assert _tokens_match("", "abc", "") is False
        assert _tokens_match("abc", "", "") is False
        assert _tokens_match("", "", "") is False


# ---------------------------------------------------------------------------
# Middleware integration tests
# ---------------------------------------------------------------------------


def _build_app(
    *,
    secret: str = "test-secret",
    exempt_paths: tuple[str, ...] = (),
) -> Starlette:
    async def get_handler(request: Request) -> PlainTextResponse:
        return PlainTextResponse("ok")

    async def post_handler(request: Request) -> JSONResponse:
        return JSONResponse({"ok": True})

    app = Starlette(
        routes=[
            Route("/page", get_handler, methods=["GET"]),
            Route("/action", post_handler, methods=["POST"]),
            Route("/webhook", post_handler, methods=["POST"]),
        ],
        middleware=[
            Middleware(
                CsrfMiddleware,
                secret=secret,
                exempt_paths=exempt_paths,
            ),
        ],
    )
    return app


class TestCsrfMiddleware:
    def test_get_request_sets_cookie(self):
        client = TestClient(_build_app())
        response = client.get("/page")
        assert response.status_code == 200
        assert "pyxle-csrf" in response.cookies

    def test_post_without_token_returns_403(self):
        client = TestClient(_build_app())
        response = client.post("/action")
        assert response.status_code == 403
        data = response.json()
        assert data["error"] == "CSRF token missing"

    def test_post_with_valid_header_token_succeeds(self):
        client = TestClient(_build_app())
        # First, do a GET to get the CSRF cookie
        get_response = client.get("/page")
        csrf_token = get_response.cookies["pyxle-csrf"]

        # POST with the token in the header
        response = client.post(
            "/action",
            headers={"x-csrf-token": csrf_token},
        )
        assert response.status_code == 200
        assert response.json()["ok"] is True

    def test_post_with_mismatched_token_returns_403(self):
        client = TestClient(_build_app())
        # Get a real cookie
        client.get("/page")

        # POST with a bogus token
        response = client.post(
            "/action",
            headers={"x-csrf-token": "completely-wrong-token"},
        )
        assert response.status_code == 403
        data = response.json()
        assert data["error"] == "CSRF token mismatch"

    def test_post_with_form_field_token_succeeds(self):
        client = TestClient(_build_app())
        get_response = client.get("/page")
        csrf_token = get_response.cookies["pyxle-csrf"]

        # Form fields require echoing the cookie via the header as well
        # because some form submissions may include the token in both places.
        # The primary mechanism is the header.
        response = client.post(
            "/action",
            data={"_csrf_token": csrf_token, "name": "test"},
            headers={"x-csrf-token": csrf_token},
        )
        assert response.status_code == 200

    def test_exempt_path_skips_check(self):
        client = TestClient(_build_app(exempt_paths=("/webhook",)))
        response = client.post("/webhook")
        assert response.status_code == 200

    def test_non_exempt_path_still_checked(self):
        client = TestClient(_build_app(exempt_paths=("/webhook",)))
        response = client.post("/action")
        assert response.status_code == 403

    def test_cookie_has_samesite_and_no_httponly(self):
        client = TestClient(_build_app())
        response = client.get("/page")
        # Double-submit cookies must NOT be HttpOnly so JS can read them
        set_cookie_header = response.headers.get("set-cookie", "")
        assert "HttpOnly" not in set_cookie_header
        assert "SameSite=lax" in set_cookie_header

    def test_safe_methods_never_blocked(self):
        """GET and HEAD should never be blocked by CSRF."""
        client = TestClient(_build_app())
        assert client.get("/page").status_code == 200
        assert client.head("/page").status_code == 200


# ---------------------------------------------------------------------------
# Progressive enhancement: the form-field CSRF path used by no-JS <Form>
# submissions. Header-only callers (useAction / fetch) are covered above.
# ---------------------------------------------------------------------------


class TestProgressiveEnhancement:
    """End-to-end coverage for the no-JS ``<Form>`` POST path.

    Without these guarantees, ``<Form>``'s docstring promise to fall back
    to a real POST when JavaScript is unavailable is silently false:
    the middleware would silently swallow ``python-multipart`` import
    errors, the body would be drained before user code could read it,
    and the active token wouldn't be visible to SSR.
    """

    def test_post_with_form_field_only_succeeds(self):
        """A no-JS form POST sends the token as a hidden field — no header."""
        client = TestClient(_build_app())
        get_response = client.get("/page")
        csrf_token = get_response.cookies["pyxle-csrf"]

        response = client.post(
            "/action",
            data={"_csrf_token": csrf_token, "name": "Shivam"},
        )
        assert response.status_code == 200
        assert response.json()["ok"] is True

    def test_post_with_multipart_form_field_succeeds(self):
        """Same path also works for multipart forms (file uploads etc.)."""
        client = TestClient(_build_app())
        get_response = client.get("/page")
        csrf_token = get_response.cookies["pyxle-csrf"]

        response = client.post(
            "/action",
            files={
                "_csrf_token": (None, csrf_token),
                "name": (None, "Shivam"),
            },
        )
        assert response.status_code == 200

    def test_post_with_form_field_mismatch_returns_403(self):
        """Wrong token in the form field still surfaces a clean 403."""
        client = TestClient(_build_app())
        client.get("/page")  # mint a cookie

        response = client.post(
            "/action",
            data={"_csrf_token": "definitely-not-the-real-token", "name": "x"},
        )
        assert response.status_code == 403
        assert response.json()["error"] == "CSRF token mismatch"

    def test_form_field_missing_returns_clear_403(self):
        """No token anywhere → 'CSRF token missing' (not the misleading
        message we used to surface when the missing dependency caused
        the form parse to silently throw)."""
        client = TestClient(_build_app())
        client.get("/page")
        response = client.post("/action", data={"name": "x"})
        assert response.status_code == 403
        assert response.json()["error"] == "CSRF token missing"

    def test_inner_app_can_still_read_body_after_csrf_check(self):
        """The middleware drains the body to validate the form-field
        token. It MUST replay the body to the inner app — otherwise user
        code that does ``await request.form()`` / ``request.json()`` sees
        nothing."""
        captured: dict[str, object] = {}

        async def echo_handler(request: Request) -> JSONResponse:
            form = await request.form()
            captured["data"] = dict(form)
            return JSONResponse({"ok": True})

        app = Starlette(
            routes=[
                Route("/page", lambda r: PlainTextResponse("ok"), methods=["GET"]),
                Route("/echo", echo_handler, methods=["POST"]),
            ],
            middleware=[Middleware(CsrfMiddleware, secret="test-secret")],
        )
        client = TestClient(app)
        csrf_token = client.get("/page").cookies["pyxle-csrf"]

        response = client.post(
            "/echo",
            data={"_csrf_token": csrf_token, "name": "Shivam", "tier": "pro"},
        )
        assert response.status_code == 200
        assert captured["data"]["name"] == "Shivam"
        assert captured["data"]["tier"] == "pro"
        # The CSRF field also remains in the body — user code is
        # responsible for stripping it. (Action dispatch does this
        # automatically; arbitrary middleware wiring may not want to.)
        assert captured["data"]["_csrf_token"] == csrf_token

    def test_token_exposed_on_scope_for_ssr(self):
        """SSR needs the active token at render time so ``<Form>`` can
        embed it as a hidden field. The middleware stashes it on
        ``scope['pyxle.csrf_token']`` regardless of method."""
        captured: dict[str, str | None] = {}

        async def handler(request: Request) -> JSONResponse:
            captured["token"] = request.scope.get("pyxle.csrf_token")
            captured["cookie"] = request.cookies.get("pyxle-csrf")
            return JSONResponse({"ok": True})

        app = Starlette(
            routes=[Route("/page", handler, methods=["GET"])],
            middleware=[Middleware(CsrfMiddleware, secret="test-secret")],
        )
        client = TestClient(app)
        client.get("/page")  # primes the cookie
        client.get("/page")  # second visit reuses the existing token
        assert captured["token"]
        # On the second call, the cookie token survives request-to-request
        # so what SSR sees matches what's already in the browser.
        assert captured["token"] == captured["cookie"]

    def test_token_minted_eagerly_for_unsafe_first_request(self):
        """If somehow a user lands directly on a POST endpoint without
        an existing cookie, the middleware still rejects with a clear
        403 — it must not attempt to mint and accept on the same hop."""
        client = TestClient(_build_app())
        # Fresh client → no cookie
        response = client.post("/action", data={"name": "x"})
        assert response.status_code == 403


# ---------------------------------------------------------------------------
# Stdlib body parsers (``_extract_csrf_form_field``)
# ---------------------------------------------------------------------------


class TestExtractCsrfFormField:
    """Direct coverage for the stdlib parsers used by the middleware.

    These run BEFORE Starlette's ``request.form()`` so the CSRF check
    can't accidentally depend on optional dependencies and silently
    return 'token missing' if one is unavailable.
    """

    def test_urlencoded_extracts_field(self):
        from pyxle.devserver.csrf import _extract_csrf_form_field

        body = b"name=Shivam&_csrf_token=abc123&tier=pro"
        out = _extract_csrf_form_field(
            body,
            content_type="application/x-www-form-urlencoded",
            field_name="_csrf_token",
        )
        assert out == "abc123"

    def test_urlencoded_returns_empty_when_field_absent(self):
        from pyxle.devserver.csrf import _extract_csrf_form_field

        body = b"name=Shivam&tier=pro"
        out = _extract_csrf_form_field(
            body,
            content_type="application/x-www-form-urlencoded",
            field_name="_csrf_token",
        )
        assert out == ""

    def test_multipart_extracts_field(self):
        from pyxle.devserver.csrf import _extract_csrf_form_field

        boundary = "----pyxleboundary"
        body = (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="name"\r\n\r\n'
            "Shivam\r\n"
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="_csrf_token"\r\n\r\n'
            "tok-xyz\r\n"
            f"--{boundary}--\r\n"
        ).encode()
        out = _extract_csrf_form_field(
            body,
            content_type=f"multipart/form-data; boundary={boundary}",
            field_name="_csrf_token",
        )
        assert out == "tok-xyz"

    def test_empty_body_returns_empty(self):
        from pyxle.devserver.csrf import _extract_csrf_form_field

        out = _extract_csrf_form_field(
            b"",
            content_type="application/x-www-form-urlencoded",
            field_name="_csrf_token",
        )
        assert out == ""

    def test_unsupported_content_type_returns_empty(self):
        from pyxle.devserver.csrf import _extract_csrf_form_field

        out = _extract_csrf_form_field(
            b"_csrf_token=abc",
            content_type="application/json",
            field_name="_csrf_token",
        )
        assert out == ""

    def test_multipart_without_boundary_header_returns_empty(self):
        """Garbage multipart body (no boundary in content-type) shouldn't
        crash the middleware — it should just say 'no token'."""
        from pyxle.devserver.csrf import _extract_csrf_form_field

        out = _extract_csrf_form_field(
            b"--anything\r\nContent-Disposition: form-data\r\n\r\nstuff\r\n--anything--",
            content_type="multipart/form-data",  # boundary omitted
            field_name="_csrf_token",
        )
        # No boundary => stdlib email parser produces a non-multipart
        # message → we fall through and return "".
        assert out == ""

    def test_multipart_field_absent_returns_empty(self):
        from pyxle.devserver.csrf import _extract_csrf_form_field

        boundary = "abcboundary"
        body = (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="other"\r\n\r\n'
            "value\r\n"
            f"--{boundary}--\r\n"
        ).encode()
        out = _extract_csrf_form_field(
            body,
            content_type=f"multipart/form-data; boundary={boundary}",
            field_name="_csrf_token",
        )
        assert out == ""


# ---------------------------------------------------------------------------
# Body buffering primitives (``_drain_body`` / ``_replay_receive``)
# ---------------------------------------------------------------------------


class TestBodyBuffering:
    """The middleware drains the body to validate the CSRF form field
    and replays it to the inner app. These primitives need their own
    coverage so a regression in either doesn't surface as a misleading
    'CSRF token missing' under load."""

    def test_drain_body_concatenates_chunks(self):
        import asyncio

        from pyxle.devserver.csrf import _drain_body

        frames = iter(
            [
                {"type": "http.request", "body": b"hello ", "more_body": True},
                {"type": "http.request", "body": b"world", "more_body": False},
            ]
        )

        async def receive():
            return next(frames)

        result = asyncio.run(_drain_body(receive))
        assert result == b"hello world"

    def test_drain_body_caps_oversize_payload(self):
        import asyncio

        from pyxle.devserver import csrf

        # Drop the cap to a tiny value so the test stays fast.
        original = csrf._MAX_BUFFERED_BODY_BYTES
        csrf._MAX_BUFFERED_BODY_BYTES = 8
        try:
            frames = iter(
                [
                    {"type": "http.request", "body": b"AAAAA", "more_body": True},
                    {"type": "http.request", "body": b"BBBBBBB", "more_body": True},
                    {"type": "http.request", "body": b"CCCC", "more_body": False},
                ]
            )

            async def receive():
                return next(frames)

            result = asyncio.run(csrf._drain_body(receive))
            # First 5 bytes + first 3 of the second chunk = cap of 8.
            # Remaining frames are drained but their bytes are dropped.
            assert result == b"AAAAABBB"
        finally:
            csrf._MAX_BUFFERED_BODY_BYTES = original

    def test_drain_body_stops_on_disconnect(self):
        import asyncio

        from pyxle.devserver.csrf import _drain_body

        async def receive():
            return {"type": "http.disconnect"}

        result = asyncio.run(_drain_body(receive))
        assert result == b""

    def test_replay_receive_yields_body_then_blocks(self):
        """First call to the replay receive returns the buffered body.
        Subsequent calls block indefinitely — yielding a synthetic
        ``http.disconnect`` would race response-sending code, since
        ``Request.is_disconnected()`` polls ``receive()`` and a fake
        disconnect makes uvicorn drop the in-flight response."""
        import asyncio

        from pyxle.devserver.csrf import _replay_receive

        async def runner():
            recv = _replay_receive(b"hello")
            first = await recv()
            # Second call must NOT resolve immediately — wrap with a
            # short timeout to assert it blocks.
            try:
                await asyncio.wait_for(recv(), timeout=0.05)
                blocked = False
            except asyncio.TimeoutError:
                blocked = True
            return first, blocked

        first, blocked = asyncio.run(runner())
        assert first == {"type": "http.request", "body": b"hello", "more_body": False}
        assert blocked, "Second call to replay receive must block, not return disconnect."

    def test_drain_body_continues_dropping_frames_after_cap(self):
        """Past the cap, additional ``more_body=True`` frames are
        consumed but their bytes dropped — we must still drain to avoid
        deadlocking downstream readers."""
        import asyncio

        from pyxle.devserver import csrf

        original = csrf._MAX_BUFFERED_BODY_BYTES
        csrf._MAX_BUFFERED_BODY_BYTES = 4
        try:
            frames = iter(
                [
                    {"type": "http.request", "body": b"AAAA", "more_body": True},
                    {"type": "http.request", "body": b"DROP", "more_body": True},
                    {"type": "http.request", "body": b"DROP", "more_body": False},
                ]
            )

            async def receive():
                return next(frames)

            result = asyncio.run(csrf._drain_body(receive))
            assert result == b"AAAA"
        finally:
            csrf._MAX_BUFFERED_BODY_BYTES = original

    def test_drain_body_handles_empty_body_frame(self):
        """A body=`b''` frame is allowed; the loop should fall through
        the chunk-handling block and respect ``more_body``."""
        import asyncio

        from pyxle.devserver.csrf import _drain_body

        frames = iter(
            [
                {"type": "http.request", "body": b"", "more_body": True},
                {"type": "http.request", "body": b"final", "more_body": False},
            ]
        )

        async def receive():
            return next(frames)

        result = asyncio.run(_drain_body(receive))
        assert result == b"final"

    def test_drain_body_terminates_after_cap_when_more_body_false(self):
        """When the cap is hit on a frame where ``more_body`` is False,
        we still break out cleanly instead of waiting for another frame."""
        import asyncio

        from pyxle.devserver import csrf

        original = csrf._MAX_BUFFERED_BODY_BYTES
        csrf._MAX_BUFFERED_BODY_BYTES = 4
        try:
            frames = iter(
                [
                    {"type": "http.request", "body": b"AAAA", "more_body": True},
                    {"type": "http.request", "body": b"BBBBB", "more_body": False},
                ]
            )

            async def receive():
                return next(frames)

            result = asyncio.run(csrf._drain_body(receive))
            # First chunk fills the cap; second chunk's bytes are dropped
            # but its more_body=False signals the stream ended cleanly.
            assert result == b"AAAA"
        finally:
            csrf._MAX_BUFFERED_BODY_BYTES = original


# ---------------------------------------------------------------------------
# Token-integrity helpers — small surface-area edge cases. These are the
# functions the middleware uses to detect forged or malformed cookies, so
# their boundary conditions need explicit coverage.
# ---------------------------------------------------------------------------


class TestVerifyTokenIntegrity:
    def test_empty_token_rejected(self):
        from pyxle.devserver.csrf import _verify_token_integrity

        assert _verify_token_integrity("", "any-secret") is False

    def test_unsigned_token_with_secret_rejected(self):
        """If a secret is configured, tokens MUST carry a signature.
        Catches the case where an attacker tries to set their own
        cookie with a randomly-chosen value."""
        from pyxle.devserver.csrf import _verify_token_integrity

        assert _verify_token_integrity("plain-no-dot", "real-secret") is False

    def test_signed_token_missing_raw_or_sig_rejected(self):
        from pyxle.devserver.csrf import _verify_token_integrity

        assert _verify_token_integrity(".sigonly", "secret") is False
        assert _verify_token_integrity("rawonly.", "secret") is False

    def test_unsigned_token_without_secret_passes(self):
        from pyxle.devserver.csrf import _verify_token_integrity

        assert _verify_token_integrity("any-non-empty-value", "") is True


# ---------------------------------------------------------------------------
# Cookie attributes — the secure flag and non-http scope are easy to
# regress so they get focused unit tests.
# ---------------------------------------------------------------------------


class TestCookieAndScopeBehavior:
    def test_secure_cookie_flag_added_when_configured(self):
        client = TestClient(_build_app_with_secure_cookie())
        response = client.get("/page")
        set_cookie = response.headers.get("set-cookie", "")
        assert "Secure" in set_cookie

    def test_non_http_scope_passthrough(self):
        """Lifespan / websocket scopes must skip the middleware entirely
        — calling ``Request(scope, receive)`` on a lifespan scope blows
        up, so the early-return is load-bearing."""
        import asyncio

        from pyxle.devserver.csrf import CsrfMiddleware

        downstream_called: list[str] = []

        async def downstream(scope, receive, send):
            downstream_called.append(scope["type"])

        async def receive():  # pragma: no cover - never invoked
            return {"type": "http.disconnect"}

        async def send(_message):  # pragma: no cover - never invoked
            return None

        mw = CsrfMiddleware(downstream, secret="x")
        asyncio.run(mw({"type": "lifespan"}, receive, send))
        assert downstream_called == ["lifespan"]


def _build_app_with_secure_cookie() -> Starlette:
    async def get_handler(request: Request) -> PlainTextResponse:
        return PlainTextResponse("ok")

    return Starlette(
        routes=[Route("/page", get_handler, methods=["GET"])],
        middleware=[
            Middleware(CsrfMiddleware, secret="s", cookie_secure=True),
        ],
    )


# ---------------------------------------------------------------------------
# Public-cacheable responses — the edge-cache integration. A page the app
# declared cacheable is sent ``Cache-Control: public, s-maxage=N``; the CSRF
# middleware MUST then omit its per-user ``Set-Cookie`` so a shared cache
# (CDN / proxy) never stores one user's token and replays it to others.
# ---------------------------------------------------------------------------


class TestIsPublicCacheable:
    """Direct coverage for the header predicate that gates the cookie-skip.

    A false negative re-attaches the cookie (defeats CDN caching); a false
    positive drops the cookie from a private page (breaks CSRF). Both are
    bad, so the boundaries get explicit tests."""

    def test_public_smaxage_is_cacheable(self):
        from pyxle.devserver.csrf import _is_public_cacheable

        headers = [(b"cache-control", b"public, s-maxage=60, stale-while-revalidate=300")]
        assert _is_public_cacheable(headers) is True

    def test_public_maxage_is_cacheable(self):
        from pyxle.devserver.csrf import _is_public_cacheable

        assert _is_public_cacheable([(b"cache-control", b"public, max-age=60")]) is True

    def test_private_no_cache_is_not_cacheable(self):
        from pyxle.devserver.csrf import _is_public_cacheable

        assert _is_public_cacheable([(b"cache-control", b"private, no-cache")]) is False

    def test_public_without_shared_lifetime_is_not_cacheable(self):
        """``public`` alone (no s-maxage/max-age) is not enough — without a
        lifetime a shared cache shouldn't store it, so we keep the cookie."""
        from pyxle.devserver.csrf import _is_public_cacheable

        assert _is_public_cacheable([(b"cache-control", b"public")]) is False

    def test_missing_cache_control_is_not_cacheable(self):
        from pyxle.devserver.csrf import _is_public_cacheable

        assert _is_public_cacheable([(b"content-type", b"text/html")]) is False
        assert _is_public_cacheable([]) is False

    def test_header_match_is_case_insensitive(self):
        from pyxle.devserver.csrf import _is_public_cacheable

        headers = [(b"Cache-Control", b"PUBLIC, S-MAXAGE=60")]
        assert _is_public_cacheable(headers) is True


def _build_cacheable_app() -> Starlette:
    async def cached_handler(request: Request) -> PlainTextResponse:
        response = PlainTextResponse("ok")
        response.headers["Cache-Control"] = (
            "public, s-maxage=60, stale-while-revalidate=300"
        )
        return response

    async def dynamic_handler(request: Request) -> PlainTextResponse:
        return PlainTextResponse("ok")

    return Starlette(
        routes=[
            Route("/cached", cached_handler, methods=["GET"]),
            Route("/dynamic", dynamic_handler, methods=["GET"]),
        ],
        middleware=[Middleware(CsrfMiddleware, secret="test-secret")],
    )


class TestPublicCacheableResponses:
    def test_cacheable_response_omits_cookie(self):
        client = TestClient(_build_cacheable_app())
        response = client.get("/cached")
        assert response.status_code == 200
        # A shared-cacheable response must NOT carry a per-user CSRF cookie.
        assert "set-cookie" not in {k.lower() for k in response.headers}
        assert "pyxle-csrf" not in response.cookies
        # The app's cache directive survives untouched.
        assert "public" in response.headers["cache-control"]

    def test_dynamic_response_still_sets_cookie(self):
        """The cookie-skip is scoped to public-cacheable responses only —
        an ordinary (private) page still mints the double-submit cookie."""
        client = TestClient(_build_cacheable_app())
        response = client.get("/dynamic")
        assert response.status_code == 200
        assert "pyxle-csrf" in response.cookies
