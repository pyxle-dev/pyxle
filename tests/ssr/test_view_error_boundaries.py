"""Tests for error boundary integration in pyxle.ssr.view."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock


from pyxle.devserver.error_pages import ErrorBoundaryRegistry
from pyxle.devserver.routes import PageRoute
from pyxle.runtime import ActionError, LoaderError
from pyxle.ssr.view import (
    _build_error_context,
    _try_error_boundary,
    build_not_found_response,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _stub_page_route(
    path: str = "/test",
    source_rel: str = "test.pyxl",
) -> PageRoute:
    return PageRoute(
        path=path,
        source_relative_path=Path(source_rel),
        source_absolute_path=Path("/project/pages") / source_rel,
        server_module_path=Path("/build/server/pages") / Path(source_rel).with_suffix(".py"),
        client_module_path=Path("/build/client") / Path(source_rel).with_suffix(".jsx"),
        metadata_path=Path("/build/metadata/pages") / Path(source_rel).with_suffix(".json"),
        module_key=f"pyxle.server.pages.{Path(source_rel).stem}",
        client_asset_path=f"/{Path(source_rel).stem}.jsx",
        server_asset_path=f"pages/{Path(source_rel).stem}",
        content_hash="abc123",
        loader_name=None,
        loader_line=None,
        head_elements=(),
        head_is_dynamic=False,
    )


def _stub_request(path: str = "/test"):
    req = MagicMock()
    req.url.path = path
    req.headers = {}
    return req


def _stub_settings(tmp_path: Path, *, debug: bool = False) -> MagicMock:
    """A settings double whose head lookup reads a real (empty) build tree.

    The boundary render merges the layout chain's head contributions, which
    are loaded from ``metadata_build_dir``. A bare ``MagicMock`` answers that
    path with another mock, so pointing it at a real directory is what makes
    the double behave like a project that simply has no layouts.
    """
    settings = MagicMock()
    settings.debug = debug
    settings.vite_host = "127.0.0.1"
    settings.vite_port = 5173
    settings.page_manifest = None
    settings.global_stylesheets = ()
    settings.metadata_build_dir = tmp_path / "metadata"
    return settings


# ---------------------------------------------------------------------------
# _build_error_context
# ---------------------------------------------------------------------------


class TestBuildErrorContext:
    def test_generic_exception_in_dev_shows_message(self):
        err = RuntimeError("something failed")
        ctx = _build_error_context(err, 500, debug=True)
        assert ctx["message"] == "something failed"
        assert ctx["statusCode"] == 500
        assert ctx["type"] == "RuntimeError"

    def test_generic_exception_in_prod_hides_message(self):
        # A framework / SSR-runtime exception in production must not leak its
        # raw message (CLAUDE.md rule 18): the boundary gets a generic string.
        err = RuntimeError("Traceback: /srv/app/pages/dash.py line 42 boom")
        ctx = _build_error_context(err, 500, debug=False)
        assert ctx["message"] == "An unexpected error occurred."
        assert ctx["statusCode"] == 500
        # The class name is also sanitized in prod (it can itself disclose the
        # failing subsystem), mirroring the JSON nav-error path's "ServerError".
        assert ctx["type"] == "ServerError"

    def test_custom_exception_class_name_sanitized_in_prod(self):
        # A third-party / custom exception class name (e.g. a DB driver's
        # InsufficientPrivilege) must not reach the browser in production.
        class StripeWebhookSignatureError(RuntimeError):
            pass

        err = StripeWebhookSignatureError("sig mismatch from host 10.0.0.5")
        ctx = _build_error_context(err, 500, debug=False)
        assert ctx["message"] == "An unexpected error occurred."
        assert ctx["type"] == "ServerError"
        # In dev the real class name is kept (it's useful while debugging).
        assert _build_error_context(err, 500, debug=True)["type"] == "StripeWebhookSignatureError"

    def test_generic_exception_in_dev_redacts_secrets(self):
        # In dev the message is surfaced but obvious secrets are redacted,
        # mirroring the dev error overlay and _navigation_error_response.
        err = RuntimeError("connect failed for postgres://user:hunter2@db:5432/app")
        ctx = _build_error_context(err, 500, debug=True)
        assert "hunter2" not in ctx["message"]

    def test_generic_exception_in_dev_falls_back_to_type_name(self):
        # An empty message becomes the class name in dev (never empty).
        err = RuntimeError()
        ctx = _build_error_context(err, 500, debug=True)
        assert ctx["message"] == "RuntimeError"

    def test_loader_error_passes_through_in_prod(self):
        # Author-raised LoaderError copy is intentional, user-facing, and must
        # survive verbatim even in production.
        err = LoaderError("not authorized", status_code=403, data={"reason": "no token"})
        ctx = _build_error_context(err, 403, debug=False)
        assert ctx["message"] == "not authorized"
        assert ctx["statusCode"] == 403
        assert ctx["data"] == {"reason": "no token"}

    def test_loader_error_passes_through_in_dev(self):
        err = LoaderError("not authorized", status_code=403, data={"reason": "no token"})
        ctx = _build_error_context(err, 403, debug=True)
        assert ctx["message"] == "not authorized"
        assert ctx["data"] == {"reason": "no token"}

    def test_loader_error_without_data(self):
        err = LoaderError("oops")
        ctx = _build_error_context(err, 500, debug=False)
        assert "data" not in ctx

    def test_action_error_passes_through_in_prod(self):
        err = ActionError("bad request", status_code=400, data={"field": "email"})
        ctx = _build_error_context(err, 400, debug=False)
        assert ctx["message"] == "bad request"
        assert ctx["data"] == {"field": "email"}

    def test_action_error_without_data(self):
        err = ActionError("forbidden", status_code=403)
        ctx = _build_error_context(err, 403, debug=False)
        assert "data" not in ctx


# ---------------------------------------------------------------------------
# _try_error_boundary
# ---------------------------------------------------------------------------


class TestTryErrorBoundary:
    def _error_page(self) -> PageRoute:
        return _stub_page_route(path="/error", source_rel="error.pyxl")

    def _registry_with_root(self) -> ErrorBoundaryRegistry:
        return ErrorBoundaryRegistry(
            error_pages={".": self._error_page()},
            not_found_pages={},
        )

    def test_returns_none_when_no_registry(self):
        result = asyncio.run(
            _try_error_boundary(
                request=_stub_request(),
                settings=MagicMock(),
                renderer=MagicMock(),
                error_boundaries=None,
                route_path="/test",
                error=RuntimeError("fail"),
                status_code=500,
            )
        )
        assert result is None

    def test_returns_none_when_no_boundary_found(self):
        empty = ErrorBoundaryRegistry(error_pages={}, not_found_pages={})
        result = asyncio.run(
            _try_error_boundary(
                request=_stub_request(),
                settings=MagicMock(),
                renderer=MagicMock(),
                error_boundaries=empty,
                route_path="/test",
                error=RuntimeError("fail"),
                status_code=500,
            )
        )
        assert result is None

    def test_renders_error_boundary_when_found(self, tmp_path: Path):
        mock_renderer = MagicMock()
        mock_render_result = MagicMock()
        mock_render_result.html = "<div>Error Page</div>"
        mock_render_result.inline_styles = ()
        mock_render_result.head_elements = ()
        mock_renderer.render = AsyncMock(return_value=mock_render_result)

        settings = _stub_settings(tmp_path)

        result = asyncio.run(
            _try_error_boundary(
                request=_stub_request(),
                settings=settings,
                renderer=mock_renderer,
                error_boundaries=self._registry_with_root(),
                route_path="/test",
                error=LoaderError("broken", status_code=500),
                status_code=500,
            )
        )
        assert result is not None
        assert result.status_code == 500

    def test_uses_correct_status_code(self, tmp_path: Path):
        mock_renderer = MagicMock()
        mock_render_result = MagicMock()
        mock_render_result.html = "<div>Not Found</div>"
        mock_render_result.inline_styles = ()
        mock_render_result.head_elements = ()
        mock_renderer.render = AsyncMock(return_value=mock_render_result)

        settings = _stub_settings(tmp_path)

        result = asyncio.run(
            _try_error_boundary(
                request=_stub_request(),
                settings=settings,
                renderer=mock_renderer,
                error_boundaries=self._registry_with_root(),
                route_path="/test",
                error=LoaderError("gone", status_code=404),
                status_code=404,
            )
        )
        assert result is not None
        assert result.status_code == 404

    def test_boundary_hides_internals_in_prod(self, tmp_path: Path):
        # End-to-end: a non-author exception routed through the boundary must
        # hand the error.pyxl component a generic message in production, never
        # the raw internal detail.
        mock_renderer = MagicMock()
        mock_render_result = MagicMock()
        mock_render_result.html = "<div>Error Page</div>"
        mock_render_result.inline_styles = ()
        mock_render_result.head_elements = ()
        mock_renderer.render = AsyncMock(return_value=mock_render_result)

        settings = _stub_settings(tmp_path)

        asyncio.run(
            _try_error_boundary(
                request=_stub_request(),
                settings=settings,
                renderer=mock_renderer,
                error_boundaries=self._registry_with_root(),
                route_path="/test",
                error=RuntimeError("/srv/secret/path.py exploded"),
                status_code=500,
            )
        )
        props = mock_renderer.render.call_args.args[1]
        assert props["error"]["message"] == "An unexpected error occurred."
        assert "secret" not in props["error"]["message"]

    def test_boundary_shows_internals_in_dev(self, tmp_path: Path):
        mock_renderer = MagicMock()
        mock_render_result = MagicMock()
        mock_render_result.html = "<div>Error Page</div>"
        mock_render_result.inline_styles = ()
        mock_render_result.head_elements = ()
        mock_renderer.render = AsyncMock(return_value=mock_render_result)

        settings = _stub_settings(tmp_path, debug=True)

        asyncio.run(
            _try_error_boundary(
                request=_stub_request(),
                settings=settings,
                renderer=mock_renderer,
                error_boundaries=self._registry_with_root(),
                route_path="/test",
                error=RuntimeError("dev detail here"),
                status_code=500,
            )
        )
        props = mock_renderer.render.call_args.args[1]
        assert props["error"]["message"] == "dev detail here"

    def test_returns_none_when_boundary_itself_fails(self):
        mock_renderer = MagicMock()
        mock_renderer.render = AsyncMock(side_effect=RuntimeError("boundary crashed"))

        result = asyncio.run(
            _try_error_boundary(
                request=_stub_request(),
                settings=MagicMock(),
                renderer=mock_renderer,
                error_boundaries=self._registry_with_root(),
                route_path="/test",
                error=RuntimeError("original"),
                status_code=500,
            )
        )
        assert result is None


# ---------------------------------------------------------------------------
# build_not_found_response
# ---------------------------------------------------------------------------


class TestBuildNotFoundResponse:
    def test_returns_none_without_registry(self):
        result = asyncio.run(
            build_not_found_response(
                request=_stub_request("/missing"),
                settings=MagicMock(),
                renderer=MagicMock(),
                error_boundaries=None,
            )
        )
        assert result is None

    def test_returns_none_without_not_found_boundary(self):
        empty = ErrorBoundaryRegistry(error_pages={}, not_found_pages={})
        result = asyncio.run(
            build_not_found_response(
                request=_stub_request("/missing"),
                settings=MagicMock(),
                renderer=MagicMock(),
                error_boundaries=empty,
            )
        )
        assert result is None

    def test_returns_none_when_boundary_fails(self):
        nf_page = _stub_page_route("/not-found", "not-found.pyxl")
        registry = ErrorBoundaryRegistry(
            error_pages={},
            not_found_pages={".": nf_page},
        )

        mock_renderer = MagicMock()
        mock_renderer.render = AsyncMock(side_effect=RuntimeError("render fail"))

        settings = MagicMock()
        settings.debug = False
        settings.pages_dir = Path("/fake/pages")

        result = asyncio.run(
            build_not_found_response(
                request=_stub_request("/missing"),
                settings=settings,
                renderer=mock_renderer,
                error_boundaries=registry,
            )
        )
        assert result is None
