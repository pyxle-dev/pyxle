"""Tests for the BaseHTTPMiddleware-vs-streaming-SSR startup warning (F28).

Starlette's ``BaseHTTPMiddleware`` buffers responses, so it cannot wrap a
streamed ``StreamingResponse``: when a ``<Suspense>`` boundary defers, the
request fails with ``RuntimeError: No response returned.``. The framework
advertises both custom ``BaseHTTPMiddleware`` and streaming SSR, so it warns at
startup when the two are combined.
"""

from __future__ import annotations

from pathlib import Path

from pyxle.cli.logger import ConsoleLogger
from pyxle.devserver.middleware import load_custom_middlewares
from pyxle.devserver.routes import PageRoute, RouteTable
from pyxle.devserver.starlette_app import (
    _has_streaming_eligible_routes,
    _warn_base_http_middleware_with_streaming,
)


def _page_route(
    path: str = "/",
    *,
    uses_suspense: bool = False,
    loading_boundary: PageRoute | None = None,
) -> PageRoute:
    source_rel = Path("page.pyxl")
    return PageRoute(
        path=path,
        source_relative_path=source_rel,
        source_absolute_path=Path("/project/pages") / source_rel,
        server_module_path=Path("/build/server/pages/page.py"),
        client_module_path=Path("/build/client/page.jsx"),
        metadata_path=Path("/build/metadata/pages/page.json"),
        module_key="pyxle.server.pages.page",
        client_asset_path="/page.jsx",
        server_asset_path="pages/page",
        content_hash="abc123",
        loader_name=None,
        loader_line=None,
        head_elements=(),
        head_is_dynamic=False,
        uses_suspense=uses_suspense,
        loading_boundary=loading_boundary,
    )


class _CapturingLogger(ConsoleLogger):
    def __init__(self) -> None:
        super().__init__()
        self.warnings: list[str] = []

    def warning(self, message: str) -> None:  # type: ignore[override]
        self.warnings.append(message)


# ---------------------------------------------------------------------------
# _has_streaming_eligible_routes
# ---------------------------------------------------------------------------


class TestHasStreamingEligibleRoutes:
    def test_suspense_route_is_eligible(self) -> None:
        table = RouteTable(pages=[_page_route(uses_suspense=True)], apis=[])
        assert _has_streaming_eligible_routes(table) is True

    def test_loading_boundary_on_route_is_eligible(self) -> None:
        boundary = _page_route("/loading")
        table = RouteTable(
            pages=[_page_route(loading_boundary=boundary)], apis=[]
        )
        assert _has_streaming_eligible_routes(table) is True

    def test_compiled_loading_pyxl_is_eligible(self) -> None:
        table = RouteTable(
            pages=[_page_route()],
            apis=[],
            loading_boundary_pages=[_page_route("/loading")],
        )
        assert _has_streaming_eligible_routes(table) is True

    def test_plain_routes_are_not_eligible(self) -> None:
        table = RouteTable(pages=[_page_route()], apis=[])
        assert _has_streaming_eligible_routes(table) is False


# ---------------------------------------------------------------------------
# _warn_base_http_middleware_with_streaming
# ---------------------------------------------------------------------------


class TestWarnBaseHttpMiddlewareWithStreaming:
    def _base_http_middleware(self):
        return load_custom_middlewares(
            ["tests.devserver.sample_middlewares:HeaderCaptureMiddleware"]
        )

    def _asgi_middleware(self):
        return load_custom_middlewares(
            ["tests.devserver.sample_middlewares:SimpleAsgiMiddleware"]
        )

    def test_warns_for_base_http_middleware_and_streaming(self) -> None:
        logger = _CapturingLogger()
        table = RouteTable(pages=[_page_route(uses_suspense=True)], apis=[])

        _warn_base_http_middleware_with_streaming(
            self._base_http_middleware(), table, logger=logger
        )

        assert len(logger.warnings) == 1
        message = logger.warnings[0]
        assert "HeaderCaptureMiddleware" in message
        assert "BaseHTTPMiddleware" in message
        assert "No response returned" in message
        assert "pure-ASGI" in message

    def test_no_warning_without_streaming_routes(self) -> None:
        logger = _CapturingLogger()
        table = RouteTable(pages=[_page_route()], apis=[])

        _warn_base_http_middleware_with_streaming(
            self._base_http_middleware(), table, logger=logger
        )

        assert logger.warnings == []

    def test_no_warning_for_pure_asgi_middleware(self) -> None:
        logger = _CapturingLogger()
        table = RouteTable(pages=[_page_route(uses_suspense=True)], apis=[])

        _warn_base_http_middleware_with_streaming(
            self._asgi_middleware(), table, logger=logger
        )

        assert logger.warnings == []

    def test_no_warning_without_any_middleware(self) -> None:
        logger = _CapturingLogger()
        table = RouteTable(pages=[_page_route(uses_suspense=True)], apis=[])

        _warn_base_http_middleware_with_streaming([], table, logger=logger)

        assert logger.warnings == []

    def test_plural_wording_for_multiple_offenders(self) -> None:
        logger = _CapturingLogger()
        table = RouteTable(pages=[_page_route(uses_suspense=True)], apis=[])
        middlewares = load_custom_middlewares(
            [
                "tests.devserver.sample_middlewares:HeaderCaptureMiddleware",
                "tests.devserver.sample_middlewares:create_rate_limit_middleware",
            ]
        )

        _warn_base_http_middleware_with_streaming(middlewares, table, logger=logger)

        assert len(logger.warnings) == 1
        assert "classes" in logger.warnings[0]
