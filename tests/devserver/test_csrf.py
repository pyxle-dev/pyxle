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
    cookie_name: str = "pyxle-csrf",
    header_name: str = "x-csrf-token",
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
            Route("/webhook/sub", post_handler, methods=["POST"]),
            Route("/webhook-admin", post_handler, methods=["POST"]),
        ],
        middleware=[
            Middleware(
                CsrfMiddleware,
                secret=secret,
                cookie_name=cookie_name,
                header_name=header_name,
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

    def test_exempt_is_segment_boundary_not_substring(self):
        """SEC-CSRF-EXEMPT-PREFIX-1: an exempt entry must not exempt an
        adjacently-named sibling (``/webhook`` must NOT exempt
        ``/webhook-admin``)."""
        client = TestClient(_build_app(exempt_paths=("/webhook",)))
        response = client.post("/webhook-admin")
        assert response.status_code == 403

    def test_exempt_covers_subpaths_at_boundary(self):
        """An exempt prefix still covers paths beneath it at a ``/`` boundary."""
        client = TestClient(_build_app(exempt_paths=("/webhook",)))
        assert client.post("/webhook/sub").status_code == 200

    def test_exempt_trailing_slash_entry_matches_exact(self):
        """A trailing-slash exempt entry still matches the exact base path."""
        client = TestClient(_build_app(exempt_paths=("/webhook/",)))
        assert client.post("/webhook").status_code == 200
        assert client.post("/webhook/sub").status_code == 200

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


class TestCustomCookieAndHeaderNames:
    """Round-trip coverage for ``csrf.cookieName`` / ``csrf.headerName``.

    The middleware must set and validate the cookie under the configured
    name and accept the token via the configured header — and nothing
    must still answer to the defaults when custom names are configured
    (the client runtime resolves the same names from the document-shell
    globals, so a drift here is exactly the silent 403 trap that hit
    pyxle-cloud).
    """

    def test_get_sets_cookie_under_configured_name(self):
        client = TestClient(
            _build_app(cookie_name="cloud-csrf", header_name="x-cloud-csrf")
        )
        response = client.get("/page")
        assert response.status_code == 200
        assert "cloud-csrf" in response.cookies
        assert "pyxle-csrf" not in response.cookies

    def test_round_trip_with_custom_names_succeeds(self):
        client = TestClient(
            _build_app(cookie_name="cloud-csrf", header_name="x-cloud-csrf")
        )
        token = client.get("/page").cookies["cloud-csrf"]

        response = client.post("/action", headers={"x-cloud-csrf": token})
        assert response.status_code == 200
        assert response.json()["ok"] is True

    def test_default_header_rejected_when_custom_configured(self):
        client = TestClient(
            _build_app(cookie_name="cloud-csrf", header_name="x-cloud-csrf")
        )
        token = client.get("/page").cookies["cloud-csrf"]

        # A client still sending the default header name never satisfies a
        # middleware configured with a custom one.
        response = client.post("/action", headers={"x-csrf-token": token})
        assert response.status_code == 403
        assert response.json()["error"] == "CSRF token missing"

    def test_stale_default_cookie_is_ignored(self):
        """A leftover ``pyxle-csrf`` cookie from another localhost app must
        not be consulted — only the configured cookie name counts."""
        client = TestClient(_build_app(cookie_name="cloud-csrf"))
        token = client.get("/page").cookies["cloud-csrf"]
        client.cookies.set("pyxle-csrf", "stale-token-from-another-app")

        # Echoing the stale default-named cookie's value fails…
        response = client.post(
            "/action", headers={"x-csrf-token": "stale-token-from-another-app"}
        )
        assert response.status_code == 403

        # …while the configured cookie's token still round-trips.
        response = client.post("/action", headers={"x-csrf-token": token})
        assert response.status_code == 200


class TestExemptPathNormalization:
    """Defence-in-depth: a non-canonical request path (``..`` / ``//``) must
    not dodge the canonical exemption decision. Tested against ``_is_exempt``
    directly because TestClient/httpx canonicalises the path before it reaches
    the middleware."""

    @staticmethod
    def _mw(exempt: tuple[str, ...]) -> CsrfMiddleware:
        async def downstream(scope, receive, send):  # pragma: no cover
            return None

        return CsrfMiddleware(downstream, secret="s", exempt_paths=exempt)

    def test_canonical_path_resolves_dotdot_and_double_slash(self):
        from pyxle.devserver.csrf import _canonical_path

        assert _canonical_path("/api/webhooks/../action") == "/api/action"
        assert _canonical_path("/api/webhooks//run") == "/api/webhooks/run"
        assert _canonical_path("//api/x") == "/api/x"
        assert _canonical_path("") == "/"

    def test_dotdot_cannot_dodge_into_exemption(self):
        mw = self._mw(("/api/webhooks",))
        # Resolves to /api/webhooks-admin (a sibling) — must NOT be exempt.
        assert mw._is_exempt("/api/webhooks/../webhooks-admin") is False
        assert mw._is_exempt("/api/webhooks-admin") is False

    def test_double_slash_under_prefix_still_exempt(self):
        mw = self._mw(("/api/webhooks",))
        assert mw._is_exempt("/api/webhooks//run") is True
        assert mw._is_exempt("/api/webhooks") is True
        assert mw._is_exempt("/api/webhooks/run") is True

    def test_root_exempt_entry_is_ignored(self):
        # An exempt entry of "/" must not silently disable CSRF site-wide.
        mw = self._mw(("/",))
        assert mw._is_exempt("/anything") is False


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

    def test_oversize_form_body_returns_413_not_truncated(self, monkeypatch):
        """QUAL-CSRF-BODY-TRUNC-1: a no-header form POST whose body exceeds the
        buffer cap is rejected with 413 rather than silently replaying a
        truncated body to the inner app."""
        from pyxle.devserver import csrf as csrf_mod

        monkeypatch.setattr(csrf_mod, "_MAX_BUFFERED_BODY_BYTES", 16)
        client = TestClient(_build_app())
        client.get("/page")  # mint a cookie
        big = "name=" + ("A" * 200)
        response = client.post(
            "/action",
            content=big.encode(),
            headers={"content-type": "application/x-www-form-urlencoded"},
        )
        assert response.status_code == 413

    def test_header_token_path_unaffected_by_body_cap(self, monkeypatch):
        """A header-token (fetch / useAction) caller never drains the body, so
        a large payload above the form-path cap still succeeds."""
        from pyxle.devserver import csrf as csrf_mod

        monkeypatch.setattr(csrf_mod, "_MAX_BUFFERED_BODY_BYTES", 16)
        client = TestClient(_build_app())
        token = client.get("/page").cookies["pyxle-csrf"]
        response = client.post(
            "/action",
            content=b'{"name":"' + b"A" * 200 + b'"}',
            headers={"content-type": "application/json", "x-csrf-token": token},
        )
        assert response.status_code == 200

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
            middleware=[
                Middleware(CsrfMiddleware, secret="test-secret", cookie_name="pyxle-csrf")
            ],
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
            middleware=[
                Middleware(CsrfMiddleware, secret="test-secret", cookie_name="pyxle-csrf")
            ],
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
# Urlencoded body parser (``_urlencoded_field_value``)
# ---------------------------------------------------------------------------


class TestUrlencodedFieldValue:
    """Direct coverage for the stdlib urlencoded parser the middleware uses.

    This runs BEFORE Starlette's ``request.form()`` so the CSRF check never
    re-consumes the receive stream that was already drained. Multipart bodies
    take the streaming scanner path (``_scan_multipart_for_token``), covered
    separately below.
    """

    def test_urlencoded_extracts_field(self):
        from pyxle.devserver.csrf import _urlencoded_field_value

        body = b"name=Shivam&_csrf_token=abc123&tier=pro"
        assert _urlencoded_field_value(body, "_csrf_token") == "abc123"

    def test_urlencoded_returns_empty_when_field_absent(self):
        from pyxle.devserver.csrf import _urlencoded_field_value

        assert _urlencoded_field_value(b"name=Shivam&tier=pro", "_csrf_token") == ""

    def test_empty_body_returns_empty(self):
        from pyxle.devserver.csrf import _urlencoded_field_value

        assert _urlencoded_field_value(b"", "_csrf_token") == ""

    def test_blank_value_is_preserved(self):
        """``keep_blank_values`` matters: an empty token must read as empty
        (→ 'token missing'), not silently fall through to another field."""
        from pyxle.devserver.csrf import _urlencoded_field_value

        assert _urlencoded_field_value(b"_csrf_token=&name=x", "_csrf_token") == ""


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

        body, truncated = asyncio.run(_drain_body(receive))
        assert body == b"hello world"
        assert truncated is False

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

            body, truncated = asyncio.run(csrf._drain_body(receive))
            # First 5 bytes + first 3 of the second chunk = cap of 8.
            # Remaining frames are drained but their bytes are dropped.
            assert body == b"AAAAABBB"
            assert truncated is True
        finally:
            csrf._MAX_BUFFERED_BODY_BYTES = original

    def test_drain_body_stops_on_disconnect(self):
        import asyncio

        from pyxle.devserver.csrf import _drain_body

        async def receive():
            return {"type": "http.disconnect"}

        body, truncated = asyncio.run(_drain_body(receive))
        assert body == b""
        assert truncated is False

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

            body, truncated = asyncio.run(csrf._drain_body(receive))
            assert body == b"AAAA"
            assert truncated is True
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

        body, truncated = asyncio.run(_drain_body(receive))
        assert body == b"final"
        assert truncated is False

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

            body, truncated = asyncio.run(csrf._drain_body(receive))
            # First chunk fills the cap; second chunk's bytes are dropped
            # but its more_body=False signals the stream ended cleanly.
            assert body == b"AAAA"
            assert truncated is True
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
        middleware=[
            Middleware(CsrfMiddleware, secret="test-secret", cookie_name="pyxle-csrf")
        ],
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


# ---------------------------------------------------------------------------
# Auto (port-namespaced) cookie naming. Cookies ignore ports, so a fixed
# default name collides between two Pyxle apps on the same host — each app
# overwrites the other's token and every action in the other app 403s. The
# default cookie name is therefore namespaced by the bind port from the
# ASGI scope (``pyxle-csrf-<port>``).
# ---------------------------------------------------------------------------


def _build_auto_named_app(*, secret: str = "test-secret") -> Starlette:
    """App whose CSRF middleware uses the auto (port-namespaced) cookie name."""

    async def get_handler(request: Request) -> PlainTextResponse:
        return PlainTextResponse("ok")

    async def post_handler(request: Request) -> JSONResponse:
        return JSONResponse({"ok": True})

    return Starlette(
        routes=[
            Route("/page", get_handler, methods=["GET"]),
            Route("/action", post_handler, methods=["POST"]),
        ],
        middleware=[Middleware(CsrfMiddleware, secret=secret)],
    )


class TestAutoCookieName:
    def test_cookie_name_derives_from_bind_port(self):
        client = TestClient(_build_auto_named_app(), base_url="http://127.0.0.1:8103")
        response = client.get("/page")
        assert response.status_code == 200
        assert "pyxle-csrf-8103" in response.cookies
        assert "pyxle-csrf" not in response.cookies

    def test_round_trip_with_auto_name(self):
        client = TestClient(_build_auto_named_app(), base_url="http://127.0.0.1:8103")
        token = client.get("/page").cookies["pyxle-csrf-8103"]
        response = client.post("/action", headers={"x-csrf-token": token})
        assert response.status_code == 200
        assert response.json()["ok"] is True

    def test_two_apps_on_different_ports_use_distinct_cookies(self):
        """The collision scenario: a browser at 127.0.0.1 holds BOTH apps'
        cookies (cookies ignore ports). Each app must read only its own —
        with the old fixed name the second app's Set-Cookie stomped the
        first's and every action in the first app failed with a mismatch."""
        client_a = TestClient(_build_auto_named_app(), base_url="http://127.0.0.1:8101")
        client_b = TestClient(_build_auto_named_app(), base_url="http://127.0.0.1:8102")
        token_a = client_a.get("/page").cookies["pyxle-csrf-8101"]
        token_b = client_b.get("/page").cookies["pyxle-csrf-8102"]
        assert token_a != token_b

        # Simulate the shared browser jar: both cookies present on one host.
        client_a.cookies.set("pyxle-csrf-8102", token_b)
        ok = client_a.post("/action", headers={"x-csrf-token": token_a})
        assert ok.status_code == 200
        # The other app's token never validates against this app's cookie.
        crossed = client_a.post("/action", headers={"x-csrf-token": token_b})
        assert crossed.status_code == 403
        assert crossed.json()["error"] == "CSRF token mismatch"

    def test_legacy_unnamespaced_cookie_is_ignored(self):
        """Transition path from the old fixed name: a leftover ``pyxle-csrf``
        cookie is simply never consulted — the token re-issues under the
        namespaced name on the next GET."""
        client = TestClient(_build_auto_named_app(), base_url="http://127.0.0.1:8101")
        client.cookies.set("pyxle-csrf", "stale-token-from-before-the-upgrade")

        response = client.get("/page")
        assert "pyxle-csrf-8101" in response.cookies

        posted = client.post(
            "/action",
            headers={"x-csrf-token": "stale-token-from-before-the-upgrade"},
        )
        assert posted.status_code == 403

    def test_explicit_cookie_name_wins_over_auto(self):
        client = TestClient(
            _build_app(cookie_name="pinned-csrf"), base_url="http://127.0.0.1:8103"
        )
        response = client.get("/page")
        assert "pinned-csrf" in response.cookies
        assert "pyxle-csrf-8103" not in response.cookies

    def test_missing_server_scope_falls_back_to_bare_prefix(self):
        """A unix-socket bind (or a scope without ``server``) has no port;
        the bare ``pyxle-csrf`` keeps the cookie working."""
        import asyncio

        sent: list[dict] = []

        async def inner(scope, receive, send):
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"ok"})

        async def receive():  # pragma: no cover - never invoked for GET
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            sent.append(message)

        mw = CsrfMiddleware(inner, secret="test-secret")
        scope = {
            "type": "http",
            "method": "GET",
            "path": "/page",
            "headers": [],
            "query_string": b"",
        }
        asyncio.run(mw(scope, receive, send))
        start = next(m for m in sent if m["type"] == "http.response.start")
        cookies = [value for name, value in start["headers"] if name == b"set-cookie"]
        assert cookies and cookies[0].startswith(b"pyxle-csrf=")


# ---------------------------------------------------------------------------
# Multipart bodies. The middleware stream-parses multipart/form-data only
# until the ``_csrf_token`` field is obtained; the consumed frames are
# replayed downstream and the rest of the stream passes through untouched,
# so file parts are never buffered by the CSRF layer.
# ---------------------------------------------------------------------------

_MP_BOUNDARY = "pyxle-test-boundary"


def _multipart_body(
    *parts: tuple[str, bytes, str | None], boundary: str = _MP_BOUNDARY
) -> bytes:
    """Assemble a raw multipart/form-data body from (name, content, filename)."""
    out = bytearray()
    for name, content, filename in parts:
        out += f"--{boundary}\r\n".encode()
        disposition = f'Content-Disposition: form-data; name="{name}"'
        if filename is not None:
            disposition += f'; filename="{filename}"'
        out += disposition.encode() + b"\r\n"
        if filename is not None:
            out += b"Content-Type: application/octet-stream\r\n"
        out += b"\r\n" + content + b"\r\n"
    out += f"--{boundary}--\r\n".encode()
    return bytes(out)


def _multipart_headers(boundary: str = _MP_BOUNDARY) -> dict[str, str]:
    return {"content-type": f"multipart/form-data; boundary={boundary}"}


def _build_multipart_echo_app(captured: dict) -> Starlette:
    """CSRF-protected app whose POST handler records the raw body it saw."""

    async def get_handler(request: Request) -> PlainTextResponse:
        return PlainTextResponse("ok")

    async def echo_handler(request: Request) -> JSONResponse:
        captured["body"] = await request.body()
        return JSONResponse({"ok": True})

    return Starlette(
        routes=[
            Route("/page", get_handler, methods=["GET"]),
            Route("/echo", echo_handler, methods=["POST"]),
        ],
        middleware=[
            Middleware(CsrfMiddleware, secret="test-secret", cookie_name="pyxle-csrf")
        ],
    )


class TestMultipartCsrf:
    """End-to-end multipart coverage: a progressive-enhancement ``<Form>``
    containing a file input posts multipart/form-data with the token as a
    hidden field — that must validate, and the inner app must receive the
    complete original body byte-for-byte."""

    def test_token_first_no_header_succeeds_and_body_is_intact(self):
        captured: dict = {}
        client = TestClient(_build_multipart_echo_app(captured))
        token = client.get("/page").cookies["pyxle-csrf"]

        body = _multipart_body(
            ("_csrf_token", token.encode(), None),
            ("upload", b"\x00\x01binary-file-content" * 128, "a.bin"),
        )
        response = client.post("/echo", content=body, headers=_multipart_headers())
        assert response.status_code == 200
        assert captured["body"] == body

    def test_token_after_small_file_succeeds_and_body_is_intact(self):
        captured: dict = {}
        client = TestClient(_build_multipart_echo_app(captured))
        token = client.get("/page").cookies["pyxle-csrf"]

        body = _multipart_body(
            ("upload", b"small file first" * 64, "a.txt"),
            ("_csrf_token", token.encode(), None),
        )
        response = client.post("/echo", content=body, headers=_multipart_headers())
        assert response.status_code == 200
        assert captured["body"] == body

    def test_mixed_case_boundary_round_trips(self):
        """curl (and other clients) generate boundaries containing uppercase
        characters. The boundary parameter is case-SENSITIVE, so the
        middleware must not lowercase it while matching the media type —
        that silently breaks every real-world multipart post."""
        captured: dict = {}
        client = TestClient(_build_multipart_echo_app(captured))
        token = client.get("/page").cookies["pyxle-csrf"]

        boundary = "------------------------MiXeDCaseBoundARY42"
        body = _multipart_body(
            ("_csrf_token", token.encode(), None),
            ("upload", b"file data", "a.bin"),
            boundary=boundary,
        )
        response = client.post(
            "/echo",
            content=body,
            headers={"content-type": f"multipart/form-data; boundary={boundary}"},
        )
        assert response.status_code == 200
        assert captured["body"] == body

    def test_wrong_token_returns_mismatch(self):
        client = TestClient(_build_multipart_echo_app({}))
        client.get("/page")  # mint a cookie
        body = _multipart_body(("_csrf_token", b"definitely-wrong", None))
        response = client.post("/echo", content=body, headers=_multipart_headers())
        assert response.status_code == 403
        assert response.json()["error"] == "CSRF token mismatch"

    def test_token_absent_returns_actionable_403(self):
        client = TestClient(_build_multipart_echo_app({}))
        client.get("/page")
        body = _multipart_body(("upload", b"file only", "a.txt"))
        response = client.post("/echo", content=body, headers=_multipart_headers())
        assert response.status_code == 403
        error = response.json()["error"]
        assert "CSRF token missing" in error
        assert "_csrf_token" in error
        assert "x-csrf-token" in error
        assert "before any file fields" in error

    def test_token_beyond_cap_returns_actionable_403(self, monkeypatch):
        """A big file part ahead of the token exhausts the scan cap — the
        request is rejected with guidance rather than buffering the upload."""
        from pyxle.devserver import csrf as csrf_mod

        monkeypatch.setattr(csrf_mod, "_MAX_MULTIPART_SCAN_BYTES", 512)
        client = TestClient(_build_multipart_echo_app({}))
        token = client.get("/page").cookies["pyxle-csrf"]

        body = _multipart_body(
            ("upload", b"F" * 4096, "big.bin"),
            ("_csrf_token", token.encode(), None),
        )
        response = client.post("/echo", content=body, headers=_multipart_headers())
        assert response.status_code == 403
        error = response.json()["error"]
        assert "CSRF token missing" in error
        assert "x-csrf-token" in error
        assert "before any file fields" in error

    def test_header_token_skips_body_scan_entirely(self, monkeypatch):
        """The fetch/useAction path: a header token means the body is never
        scanned, so an upload far beyond the cap still succeeds — intact."""
        from pyxle.devserver import csrf as csrf_mod

        monkeypatch.setattr(csrf_mod, "_MAX_MULTIPART_SCAN_BYTES", 512)
        captured: dict = {}
        client = TestClient(_build_multipart_echo_app(captured))
        token = client.get("/page").cookies["pyxle-csrf"]

        body = _multipart_body(("upload", b"F" * 8192, "big.bin"))
        response = client.post(
            "/echo",
            content=body,
            headers={**_multipart_headers(), "x-csrf-token": token},
        )
        assert response.status_code == 200
        assert captured["body"] == body

    def test_empty_token_field_returns_missing(self):
        client = TestClient(_build_multipart_echo_app({}))
        client.get("/page")
        body = _multipart_body(("_csrf_token", b"", None))
        response = client.post("/echo", content=body, headers=_multipart_headers())
        assert response.status_code == 403
        assert response.json()["error"] == "CSRF token missing"

    def test_token_without_cookie_returns_cookie_missing(self):
        """The other half of the double-submit pair: a body token with no
        cookie to compare against gets its own precise message."""
        client = TestClient(_build_multipart_echo_app({}))  # no GET → no cookie
        body = _multipart_body(("_csrf_token", b"some-token", None))
        response = client.post("/echo", content=body, headers=_multipart_headers())
        assert response.status_code == 403
        assert response.json()["error"] == "CSRF cookie missing"

    def test_content_type_without_boundary_rejected(self):
        client = TestClient(_build_multipart_echo_app({}))
        client.get("/page")
        response = client.post(
            "/echo",
            content=b"anything",
            headers={"content-type": "multipart/form-data"},
        )
        assert response.status_code == 403
        assert "CSRF token missing" in response.json()["error"]

    def test_malformed_multipart_body_rejected(self):
        """Bytes that don't start with the declared boundary abort the scan
        — the check fails closed with 'token missing'."""
        client = TestClient(_build_multipart_echo_app({}))
        client.get("/page")
        response = client.post(
            "/echo",
            content=b"this is not multipart data at all",
            headers=_multipart_headers(),
        )
        assert response.status_code == 403
        assert "CSRF token missing" in response.json()["error"]

    def test_urlencoded_replay_is_byte_for_byte(self):
        """Regression for the urlencoded path: the drained body reaches the
        inner app exactly as sent."""
        captured: dict = {}
        client = TestClient(_build_multipart_echo_app(captured))
        token = client.get("/page").cookies["pyxle-csrf"]
        body = f"_csrf_token={token}&name=Shivam&note=a%26b%3Dc".encode()
        response = client.post(
            "/echo",
            content=body,
            headers={"content-type": "application/x-www-form-urlencoded"},
        )
        assert response.status_code == 200
        assert captured["body"] == body


# ---------------------------------------------------------------------------
# The streaming scan primitives — exercised at the raw ASGI level so frame
# boundaries and consumption behaviour are under precise control.
# ---------------------------------------------------------------------------


class TestMultipartStreamScan:
    @staticmethod
    def _frames_receive(frames):
        """Scripted ``receive`` that also counts how many frames were pulled."""
        state = {"calls": 0}

        async def receive():
            frame = frames[state["calls"]]
            state["calls"] += 1
            return frame

        return receive, state

    def test_scan_stops_consuming_once_token_part_ends(self):
        """Frames after the one that completes the token part are left on the
        stream for downstream — proof that file parts are not buffered by
        the CSRF layer."""
        import asyncio

        from pyxle.devserver.csrf import CsrfMiddleware, _generate_token

        token = _generate_token("test-secret")
        # Frame 1 carries the complete token part AND the next part's opening
        # boundary + headers (the parser detects part-end at the boundary).
        full_body = _multipart_body(
            ("_csrf_token", token.encode(), None),
            ("upload", b"D" * 512, "d.bin"),
        )
        # Split so the token part (and following boundary line) sit in frame 1.
        split_at = full_body.index(b'name="upload"') + len(b'name="upload"')
        frame1, rest = full_body[:split_at], full_body[split_at:]
        mid = len(rest) // 2
        frames = [
            {"type": "http.request", "body": frame1, "more_body": True},
            {"type": "http.request", "body": rest[:mid], "more_body": True},
            {"type": "http.request", "body": rest[mid:], "more_body": False},
        ]
        receive, state = self._frames_receive(frames)

        downstream_body = bytearray()
        calls_when_downstream_started: list[int] = []

        async def inner(scope, receive_inner, send):
            calls_when_downstream_started.append(state["calls"])
            while True:
                message = await receive_inner()
                downstream_body.extend(message.get("body", b""))
                if not message.get("more_body"):
                    break
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"ok"})

        sent: list[dict] = []

        async def send(message):
            sent.append(message)

        mw = CsrfMiddleware(inner, secret="test-secret", cookie_name="pyxle-csrf")
        scope = {
            "type": "http",
            "method": "POST",
            "path": "/echo",
            "query_string": b"",
            "headers": [
                (b"content-type", f"multipart/form-data; boundary={_MP_BOUNDARY}".encode()),
                (b"cookie", f"pyxle-csrf={token}".encode()),
            ],
            "server": ("127.0.0.1", 8103),
        }
        asyncio.run(mw(scope, receive, send))

        start = next(m for m in sent if m["type"] == "http.response.start")
        assert start["status"] == 200
        # The scan consumed only frame 1 before handing over to the app…
        assert calls_when_downstream_started == [1]
        # …and the app still saw the complete original body, byte-for-byte.
        assert bytes(downstream_body) == full_body

    def test_scan_finds_token_split_across_frames(self):
        import asyncio

        from pyxle.devserver.csrf import _scan_multipart_for_token

        body = _multipart_body(("_csrf_token", b"split-token-value", None))
        third = len(body) // 3
        frames = [
            {"type": "http.request", "body": body[:third], "more_body": True},
            {"type": "http.request", "body": body[third : 2 * third], "more_body": True},
            {"type": "http.request", "body": body[2 * third :], "more_body": False},
        ]
        receive, _ = self._frames_receive(frames)
        scan = asyncio.run(
            _scan_multipart_for_token(
                receive,
                content_type=f"multipart/form-data; boundary={_MP_BOUNDARY}",
                field_name="_csrf_token",
            )
        )
        assert scan.token == "split-token-value"
        assert scan.consumed == body
        assert scan.stream_exhausted is True
        assert scan.over_cap is False

    def test_scan_missing_boundary_consumes_nothing(self):
        import asyncio

        from pyxle.devserver.csrf import _scan_multipart_for_token

        async def receive():  # pragma: no cover - never invoked
            raise AssertionError("scan must not read the stream without a boundary")

        scan = asyncio.run(
            _scan_multipart_for_token(
                receive,
                content_type="multipart/form-data",
                field_name="_csrf_token",
            )
        )
        assert scan.token is None
        assert scan.consumed == b""
        assert scan.over_cap is False

    def test_scan_reports_over_cap(self, monkeypatch):
        import asyncio

        from pyxle.devserver import csrf as csrf_mod

        monkeypatch.setattr(csrf_mod, "_MAX_MULTIPART_SCAN_BYTES", 64)
        body = _multipart_body(
            ("upload", b"Z" * 512, "z.bin"),
            ("_csrf_token", b"tok", None),
        )
        frames = [
            {"type": "http.request", "body": body[:128], "more_body": True},
            {"type": "http.request", "body": body[128:], "more_body": False},
        ]
        receive, state = self._frames_receive(frames)
        scan = asyncio.run(
            csrf_mod._scan_multipart_for_token(
                receive,
                content_type=f"multipart/form-data; boundary={_MP_BOUNDARY}",
                field_name="_csrf_token",
            )
        )
        assert scan.token is None
        assert scan.over_cap is True
        # Only the first frame was consumed — the scan stops at the cap.
        assert state["calls"] == 1
        assert scan.consumed == body[:128]

    def test_scan_handles_disconnect_mid_body(self):
        import asyncio

        from pyxle.devserver.csrf import _scan_multipart_for_token

        body = _multipart_body(("_csrf_token", b"tok", None))
        frames = [
            {"type": "http.request", "body": body[:10], "more_body": True},
            {"type": "http.disconnect"},
        ]
        receive, _ = self._frames_receive(frames)
        scan = asyncio.run(
            _scan_multipart_for_token(
                receive,
                content_type=f"multipart/form-data; boundary={_MP_BOUNDARY}",
                field_name="_csrf_token",
            )
        )
        assert scan.token is None
        assert scan.stream_exhausted is True
        assert scan.consumed == body[:10]

    def test_scan_skips_empty_body_frames(self):
        import asyncio

        from pyxle.devserver.csrf import _scan_multipart_for_token

        body = _multipart_body(("_csrf_token", b"tok", None))
        frames = [
            {"type": "http.request", "body": b"", "more_body": True},
            {"type": "http.request", "body": body, "more_body": False},
        ]
        receive, _ = self._frames_receive(frames)
        scan = asyncio.run(
            _scan_multipart_for_token(
                receive,
                content_type=f"multipart/form-data; boundary={_MP_BOUNDARY}",
                field_name="_csrf_token",
            )
        )
        assert scan.token == "tok"
        assert scan.consumed == body

    def test_resume_receive_replays_prefix_then_delegates(self):
        import asyncio

        from pyxle.devserver.csrf import _resume_receive

        frames = [{"type": "http.request", "body": b" world", "more_body": False}]
        receive, _ = self._frames_receive(frames)
        resumed = _resume_receive(b"hello", receive)

        async def collect():
            first = await resumed()
            second = await resumed()
            return first, second

        first, second = asyncio.run(collect())
        assert first == {"type": "http.request", "body": b"hello", "more_body": True}
        assert second == {"type": "http.request", "body": b" world", "more_body": False}
