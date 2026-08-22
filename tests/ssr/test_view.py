from __future__ import annotations

import json
import logging
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from starlette.requests import Request

from pyxle.devserver.routes import PageRoute
from pyxle.devserver.settings import DevServerSettings
from pyxle.ssr import view as ssr_view
from pyxle.ssr.renderer import (
    BrowserGlobalRenderError,
    CjsDependencyRenderError,
    ComponentRenderError,
    InlineStyleFragment,
    RenderResult,
)
from pyxle.ssr.view import (
    HeadEvaluationError,
    build_page_navigation_response,
    build_page_response,
    build_streaming_page_response,
)


def _stream_of(*frames):
    """Build a fake ``render_stream`` async-generator callable yielding *frames*."""

    async def _gen(
        component_path,
        props,
        *,
        request_pathname=None,
        csrf_token=None,
        fallback_path=None,
    ):
        for frame in frames:
            yield frame

    return _gen


def test_auth_seed_for_request_returns_scope_value() -> None:
    """An auth provider's scope blob is forwarded verbatim to the document."""
    seed = {"user": {"email": "a@b.c"}, "endpoints": {"me": "/auth/me"}}
    request = Request(
        {"type": "http", "method": "GET", "path": "/", "headers": [], "pyxle.auth": seed}
    )
    assert ssr_view._auth_seed_for_request(request) is seed


def test_auth_seed_for_request_absent_returns_sentinel() -> None:
    """No auth provider → the ABSENT sentinel, so the document emits no seed."""
    from pyxle.ssr.template import _AUTH_SEED_ABSENT

    request = Request({"type": "http", "method": "GET", "path": "/", "headers": []})
    assert ssr_view._auth_seed_for_request(request) is _AUTH_SEED_ABSENT


@pytest.fixture
def anyio_backend() -> str:  # pragma: no cover - fixture wiring
    return "asyncio"


class StubRenderer:
    def __init__(self) -> None:
        self.calls: list[tuple[Path, dict[str, object]]] = []
        self.request_pathnames: list[str | None] = []
        self.csrf_tokens: list[str | None] = []
        self.responses: list[RenderResult] = []

    async def render(
        self,
        component_path: Path,
        props: dict[str, object],
        *,
        request_pathname: str | None = None,
        csrf_token: str | None = None,
    ) -> RenderResult:
        self.calls.append((component_path, props))
        self.request_pathnames.append(request_pathname)
        self.csrf_tokens.append(csrf_token)
        if self.responses:
            return self.responses.pop(0)
        return RenderResult(html="<div></div>")


class StubOverlay:
    def __init__(self) -> None:
        self.events: list[tuple[str, str] | tuple[str, str, list[dict[str, str]]]] = []
        self.request_paths: list[str | None] = []

    async def notify_clear(self, route_path: str) -> None:
        self.events.append(("clear", route_path))

    async def notify_error(
        self,
        route_path: str,
        error: BaseException,
        *,
        breadcrumbs: list[dict[str, str]] | None = None,
        request_path: str | None = None,
    ) -> None:
        self.events.append(("error", route_path, breadcrumbs or []))
        self.request_paths.append(request_path)


async def _read_response_body(response) -> bytes:
    body_iterator = getattr(response, "body_iterator", None)
    if body_iterator is not None:
        chunks = bytearray()
        async for chunk in body_iterator:
            if isinstance(chunk, str):
                chunk = chunk.encode("utf-8")
            chunks.extend(chunk)
        return bytes(chunks)

    body = getattr(response, "body", b"")
    if isinstance(body, bytes):
        return body
    if isinstance(body, str):
        return body.encode("utf-8")
    return bytes(body or b"")


@pytest.fixture
def settings(tmp_path: Path) -> DevServerSettings:
    project = tmp_path / "project"
    (project / "pages").mkdir(parents=True)
    (project / "public").mkdir()
    return DevServerSettings.from_project_root(project)


def _page_route(tmp_path: Path, *, loader_name: str | None) -> PageRoute:
    return PageRoute(
        path="/",
        source_relative_path=Path("index.pyxl"),
        source_absolute_path=tmp_path / "pages" / "index.pyxl",
        server_module_path=tmp_path / "server" / "index.py",
        client_module_path=tmp_path / "client" / "index.jsx",
        metadata_path=tmp_path / "metadata" / "index.json",
        module_key="pyxle.server.pages.index",
        client_asset_path="/pages/index.jsx",
        server_asset_path="/pages/index.py",
        content_hash="hash",
        loader_name=loader_name,
        loader_line=1,
        head_elements=("<title>Home</title>",),
        head_is_dynamic=False,
    )


@pytest.mark.anyio
async def test_build_page_response_without_loader(settings: DevServerSettings, tmp_path: Path) -> None:
    renderer = StubRenderer()
    overlay = StubOverlay()
    overlay = StubOverlay()
    renderer.responses.append(RenderResult(html="<main>empty</main>"))

    page = _page_route(tmp_path, loader_name=None)

    request = Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "GET",
            "path": "/",
            "root_path": "",
            "headers": [],
        }
    )

    overlay = StubOverlay()

    response = await build_page_response(
        request=request,
        settings=settings,
        page=page,
        renderer=renderer,
        overlay=overlay,
    )

    body = (await _read_response_body(response)).decode()
    assert response.status_code == 200
    assert "<main>empty</main>" in body
    assert "<title>Home</title>" in body
    assert "nonce=\"" in body
    assert renderer.calls[-1][0] == page.client_module_path
    assert renderer.calls[-1][1] == {"data": {}}
    # The request path is forwarded to the renderer so SSR code (e.g.
    # usePathname) sees the real pathname and hydrates without a mismatch.
    assert renderer.request_pathnames[-1] == "/"
    assert overlay.events == [("clear", "/")]


def _request_from(host: str, *, path: str = "/") -> Request:
    """A request as it arrives from a browser that reached this server at *host*."""

    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "GET",
            "path": path,
            "root_path": "",
            "headers": [(b"host", host.encode())],
        }
    )


@pytest.mark.anyio
async def test_page_served_to_an_unreachable_origin_says_so(
    settings: DevServerSettings, tmp_path: Path, caplog
) -> None:
    """The framework's most silent failure gets exactly one line, and it is here.

    A browser whose origin Vite refuses receives a whole, correct document and
    no modules: the page renders and never becomes interactive. Vite answered
    ``200`` and logs nothing; the browser reports it to nothing a page can see.
    Pyxle holds both facts — the origin it served, and the allow-list it wrote —
    so it is the only party that can name the failure.
    """

    ssr_view._warn_once.cache_clear()
    renderer = StubRenderer()
    renderer.responses.append(RenderResult(html="<main>hi</main>"))
    dev_settings = replace(settings, starlette_host="0.0.0.0", starlette_port=3000)
    page = _page_route(tmp_path, loader_name=None)

    with caplog.at_level(logging.WARNING, logger="pyxle.ssr.view"):
        await build_page_response(
            request=_request_from("dev.internal:3000"),
            settings=dev_settings,
            page=page,
            renderer=renderer,
        )

    warnings = [record.getMessage() for record in caplog.records]
    assert any("http://dev.internal:3000" in message for message in warnings)
    assert any("never become interactive" in message for message in warnings)


@pytest.mark.anyio
async def test_page_served_to_a_reachable_origin_stays_quiet(
    settings: DevServerSettings, tmp_path: Path, caplog
) -> None:
    """The addresses the startup banner printed are not worth warning about."""

    ssr_view._warn_once.cache_clear()
    renderer = StubRenderer()
    renderer.responses.append(RenderResult(html="<main>hi</main>"))
    renderer.responses.append(RenderResult(html="<main>hi</main>"))
    dev_settings = replace(settings, starlette_host="0.0.0.0", starlette_port=3000)
    page = _page_route(tmp_path, loader_name=None)

    with caplog.at_level(logging.WARNING, logger="pyxle.ssr.view"):
        for host in ("192.168.1.11:3000", "localhost:3000"):
            await build_page_response(
                request=_request_from(host),
                settings=dev_settings,
                page=page,
                renderer=renderer,
            )

    assert [record.getMessage() for record in caplog.records] == []


@pytest.mark.anyio
async def test_unreachable_origin_warning_is_not_repeated(
    settings: DevServerSettings, tmp_path: Path, caplog
) -> None:
    """A browser reloading a dead page must not reprint the paragraph."""

    ssr_view._warn_once.cache_clear()
    renderer = StubRenderer()
    renderer.responses.extend(RenderResult(html="<main>hi</main>") for _ in range(3))
    dev_settings = replace(settings, starlette_host="0.0.0.0", starlette_port=3000)
    page = _page_route(tmp_path, loader_name=None)

    with caplog.at_level(logging.WARNING, logger="pyxle.ssr.view"):
        for host in ("dev.internal:3000", "dev.internal:3000", "other.internal:3000"):
            await build_page_response(
                request=_request_from(host),
                settings=dev_settings,
                page=page,
                renderer=renderer,
            )

    # Once per origin: repeats are silenced, a *different* origin is not.
    assert len(caplog.records) == 2


@pytest.mark.anyio
async def test_production_never_warns_about_origins(
    settings: DevServerSettings, tmp_path: Path, caplog
) -> None:
    ssr_view._warn_once.cache_clear()
    renderer = StubRenderer()
    renderer.responses.append(RenderResult(html="<main>hi</main>"))
    prod_settings = replace(
        settings,
        debug=False,
        starlette_host="0.0.0.0",
        starlette_port=3000,
        page_manifest={"pages/index.jsx": {"file": "assets/index.js"}},
    )
    page = _page_route(tmp_path, loader_name=None)

    with caplog.at_level(logging.WARNING, logger="pyxle.ssr.view"):
        await build_page_response(
            request=_request_from("app.example.com"),
            settings=prod_settings,
            page=page,
            renderer=renderer,
        )

    assert [record.getMessage() for record in caplog.records] == []


def test_document_origin_always_carries_an_explicit_port() -> None:
    """The allow-list is written with ports, so the comparison needs one."""

    assert (
        ssr_view._document_origin(_request_from("192.168.1.11:3000"))
        == "http://192.168.1.11:3000"
    )
    assert ssr_view._document_origin(_request_from("dev.internal")) == (
        "http://dev.internal:80"
    )
    # An IPv6 literal needs its brackets back before it meets a port separator.
    assert ssr_view._document_origin(_request_from("[::1]:3000")) == "http://[::1]:3000"
    assert ssr_view._document_origin(Request({
        "type": "http",
        "http_version": "1.1",
        "method": "GET",
        "path": "/",
        "root_path": "",
        "headers": [],
    })) is None


@pytest.mark.anyio
async def test_build_page_navigation_response_returns_payload(
    settings: DevServerSettings,
    tmp_path: Path,
) -> None:
    renderer = StubRenderer()
    renderer.responses.append(RenderResult(html="<main>empty</main>"))
    overlay = StubOverlay()
    page = _page_route(tmp_path, loader_name=None)

    request = Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "GET",
            "path": "/",
            "root_path": "",
            "headers": [],
        }
    )

    response = await build_page_navigation_response(
        request=request,
        settings=settings,
        page=page,
        renderer=renderer,
        overlay=overlay,
    )

    payload = json.loads(await _read_response_body(response))
    assert response.status_code == 200
    assert payload["ok"] is True
    assert payload["page"]["clientAssetPath"] == page.client_asset_path
    assert payload["props"] == {"data": {}}
    assert "<title>Home</title>" in payload["headMarkup"]
    # No cache config for "/" in the default settings → client default TTL.
    assert payload["navCacheTtlSeconds"] is None
    assert overlay.events == [("clear", "/")]


def test_resolve_nav_cache_ttl_matches_cache_config() -> None:
    from types import SimpleNamespace

    from pyxle.config import CacheConfig
    from pyxle.ssr.view import _resolve_nav_cache_ttl

    settings = SimpleNamespace(cache=CacheConfig(routes=(("/docs/*", 300), ("/", 60))))
    assert _resolve_nav_cache_ttl(settings, "/") == 60
    assert _resolve_nav_cache_ttl(settings, "/docs/intro") == 300
    # A path with no matching cache route falls back to the client default.
    assert _resolve_nav_cache_ttl(settings, "/playground") is None


def test_resolve_nav_cache_ttl_without_cache_config() -> None:
    from types import SimpleNamespace

    from pyxle.ssr.view import _resolve_nav_cache_ttl

    assert _resolve_nav_cache_ttl(SimpleNamespace(), "/") is None
    assert _resolve_nav_cache_ttl(SimpleNamespace(cache=None), "/") is None


def test_resolve_nav_cache_ttl_dynamic_loader_page_never_caches() -> None:
    """A loader page with no declared cache lifetime is dynamic → TTL 0, so a
    mutation is visible immediately on client back/forward (not stale)."""
    from types import SimpleNamespace

    from pyxle.ssr.view import _resolve_nav_cache_ttl

    settings = SimpleNamespace(cache=None)
    dynamic = SimpleNamespace(cache_revalidate=None, has_loader=True)
    assert _resolve_nav_cache_ttl(settings, "/incidents/x", page=dynamic) == 0

    # A CACHE directive on the page wins over the dynamic default.
    cached = SimpleNamespace(cache_revalidate=3600, has_loader=True)
    assert _resolve_nav_cache_ttl(settings, "/about", page=cached) == 3600

    # A static, loader-less page keeps the client default (None).
    static = SimpleNamespace(cache_revalidate=None, has_loader=False)
    assert _resolve_nav_cache_ttl(settings, "/static", page=static) is None


def test_resolve_nav_cache_ttl_cache_config_wins_over_page() -> None:
    """An explicit edge-cache entry takes priority over the per-page default."""
    from types import SimpleNamespace

    from pyxle.config import CacheConfig
    from pyxle.ssr.view import _resolve_nav_cache_ttl

    settings = SimpleNamespace(cache=CacheConfig(routes=(("/feed", 120),)))
    dynamic = SimpleNamespace(cache_revalidate=None, has_loader=True)
    assert _resolve_nav_cache_ttl(settings, "/feed", page=dynamic) == 120


@pytest.mark.anyio
async def test_build_page_response_with_loader(settings: DevServerSettings, tmp_path: Path) -> None:
    server_module = tmp_path / "server" / "index.py"
    server_module.parent.mkdir(parents=True, exist_ok=True)
    server_module.write_text(
        """
import json

async def load_home(request):
    return {"value": request.query_params.get("value", "0")}, 202
""",
        encoding="utf-8",
    )

    page = _page_route(tmp_path, loader_name="load_home")
    page = PageRoute(
        path=page.path,
        source_relative_path=page.source_relative_path,
        source_absolute_path=page.source_absolute_path,
        server_module_path=server_module,
        client_module_path=page.client_module_path,
        metadata_path=page.metadata_path,
        module_key=page.module_key,
        client_asset_path=page.client_asset_path,
        server_asset_path=page.server_asset_path,
        content_hash=page.content_hash,
        loader_name=page.loader_name,
        loader_line=page.loader_line,
        head_elements=page.head_elements,
        head_is_dynamic=page.head_is_dynamic,
    )

    renderer = StubRenderer()
    renderer.responses.append(RenderResult(html="<p>SSR</p>"))

    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "GET",
        "path": "/",
        "root_path": "",
        "headers": [],
        "query_string": b"value=9",
    }
    request = Request(scope)

    response = await build_page_response(
        request=request,
        settings=settings,
        page=page,
        renderer=renderer,
    )

    assert response.status_code == 202
    body_text = (await _read_response_body(response)).decode()
    assert "<p>SSR</p>" in body_text
    assert "<title>Home</title>" in body_text
    assert renderer.calls[-1][0] == page.client_module_path
    assert renderer.calls[-1][1]["data"]["value"] == "9"


# --------------------------------------------------------------------------- #
# Loader {data, revalidate} cache envelope (ROADMAP 2.1)
# --------------------------------------------------------------------------- #


def test_normalize_loader_result_plain_mapping_has_no_revalidate(tmp_path: Path) -> None:
    page = _page_route(tmp_path, loader_name="load")
    payload, status, revalidate = ssr_view._normalize_loader_result({"a": 1}, page)
    assert payload == {"a": 1}
    assert status == 200
    assert revalidate is None


def test_normalize_loader_result_unwraps_envelope(tmp_path: Path) -> None:
    page = _page_route(tmp_path, loader_name="load")
    payload, status, revalidate = ssr_view._normalize_loader_result(
        {"data": {"a": 1}, "revalidate": 60}, page
    )
    assert payload == {"a": 1}
    assert status == 200
    assert revalidate == 60.0


def test_normalize_loader_result_envelope_with_status_tuple(tmp_path: Path) -> None:
    page = _page_route(tmp_path, loader_name="load")
    payload, status, revalidate = ssr_view._normalize_loader_result(
        ({"data": {"a": 1}, "revalidate": 30}, 201), page
    )
    assert payload == {"a": 1}
    assert status == 201
    assert revalidate == 30.0


def test_normalize_loader_result_not_envelope_with_extra_keys(tmp_path: Path) -> None:
    # A page that genuinely exposes data/revalidate *plus* another key is not a
    # cache directive — it is returned verbatim as props.
    page = _page_route(tmp_path, loader_name="load")
    result = {"data": {"a": 1}, "revalidate": 60, "extra": True}
    payload, _status, revalidate = ssr_view._normalize_loader_result(result, page)
    assert payload == result
    assert revalidate is None


def test_normalize_loader_result_not_envelope_when_data_not_mapping(tmp_path: Path) -> None:
    page = _page_route(tmp_path, loader_name="load")
    result = {"data": [1, 2], "revalidate": 60}
    payload, _status, revalidate = ssr_view._normalize_loader_result(result, page)
    assert payload == result
    assert revalidate is None


@pytest.mark.parametrize("value,expected", [(None, None), (0, 0.0), (60, 60.0), (1.5, 1.5)])
def test_coerce_revalidate_accepts_valid(tmp_path: Path, value, expected) -> None:
    page = _page_route(tmp_path, loader_name="load")
    assert ssr_view._coerce_revalidate(value, page) == expected


@pytest.mark.parametrize("value", [-1, True, False, "60", [1]])
def test_coerce_revalidate_rejects_invalid(tmp_path: Path, value) -> None:
    page = _page_route(tmp_path, loader_name="load")
    with pytest.raises(ssr_view.LoaderExecutionError):
        ssr_view._coerce_revalidate(value, page)


@pytest.mark.anyio
async def test_build_page_response_sets_revalidate_header_from_envelope(
    settings: DevServerSettings, tmp_path: Path
) -> None:
    server_module = tmp_path / "server" / "index.py"
    server_module.parent.mkdir(parents=True, exist_ok=True)
    server_module.write_text(
        '\nasync def load_home(request):\n'
        '    return {"data": {"value": "x"}, "revalidate": 90}\n',
        encoding="utf-8",
    )
    page = replace(
        _page_route(tmp_path, loader_name="load_home"), server_module_path=server_module
    )

    renderer = StubRenderer()
    renderer.responses.append(RenderResult(html="<p>SSR</p>"))
    request = Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "GET",
            "path": "/",
            "root_path": "",
            "headers": [],
            "query_string": b"",
        }
    )

    response = await build_page_response(
        request=request, settings=settings, page=page, renderer=renderer
    )

    assert response.headers[ssr_view.REVALIDATE_HEADER] == "90"
    # The inner data became the component props; envelope keys did not leak.
    assert renderer.calls[-1][1]["data"] == {"value": "x"}


@pytest.mark.anyio
async def test_build_page_response_inlines_renderer_styles(
    settings: DevServerSettings,
    tmp_path: Path,
) -> None:
    renderer = StubRenderer()
    renderer.responses.append(
        RenderResult(
            html="<p>Styled</p>",
            inline_styles=(
                InlineStyleFragment(
                    identifier="style-one",
                    contents=".hero { color: red; }",
                    source="pages/index.css",
                ),
            ),
        )
    )

    page = _page_route(tmp_path, loader_name=None)
    request = Request({"type": "http", "http_version": "1.1", "method": "GET", "path": "/", "root_path": "", "headers": []})

    response = await build_page_response(
        request=request,
        settings=settings,
        page=page,
        renderer=renderer,
    )

    body_text = (await _read_response_body(response)).decode()
    assert 'data-pyxle-inline-style="style-one"' in body_text
    assert '.hero { color: red; }' in body_text


@pytest.mark.anyio
async def test_build_page_response_validates_loader_return(settings: DevServerSettings, tmp_path: Path) -> None:
    server_module = tmp_path / "server" / "bad.py"
    server_module.parent.mkdir(parents=True, exist_ok=True)
    server_module.write_text(
        """
async def load_home(request):
    return "oops"
""",
        encoding="utf-8",
    )

    page = _page_route(tmp_path, loader_name="load_home")
    page = PageRoute(
        path=page.path,
        source_relative_path=page.source_relative_path,
        source_absolute_path=page.source_absolute_path,
        server_module_path=server_module,
        client_module_path=page.client_module_path,
        metadata_path=page.metadata_path,
        module_key=page.module_key,
        client_asset_path=page.client_asset_path,
        server_asset_path=page.server_asset_path,
        content_hash=page.content_hash,
        loader_name=page.loader_name,
        loader_line=page.loader_line,
        head_elements=page.head_elements,
        head_is_dynamic=page.head_is_dynamic,
    )

    renderer = StubRenderer()
    overlay = StubOverlay()

    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "GET",
        "path": "/",
        "root_path": "",
        "headers": [],
    }
    request = Request(scope)

    response = await build_page_response(
        request=request,
        settings=settings,
        page=page,
        renderer=renderer,
        overlay=overlay,
    )

    body = (await _read_response_body(response)).decode()
    assert response.status_code == 500
    assert "Server Render Failed" in body
    assert "LoaderExecutionError" in body
    assert overlay.events and overlay.events[0][0] == "error"
    assert overlay.events[0][1] == "/"
    loader_breadcrumbs = overlay.events[0][2]
    assert loader_breadcrumbs[0]["status"] == "failed"
    assert loader_breadcrumbs[1]["status"] == "blocked"
    assert loader_breadcrumbs[2]["label"] == "Hydration"


@pytest.mark.anyio
async def test_build_page_navigation_response_reports_loader_error(
    settings: DevServerSettings,
    tmp_path: Path,
) -> None:
    server_module = tmp_path / "server" / "bad_nav.py"
    server_module.parent.mkdir(parents=True, exist_ok=True)
    server_module.write_text(
        """
async def load_home(request):
    return "oops"
""",
        encoding="utf-8",
    )

    page = _page_route(tmp_path, loader_name="load_home")
    page = PageRoute(
        path=page.path,
        source_relative_path=page.source_relative_path,
        source_absolute_path=page.source_absolute_path,
        server_module_path=server_module,
        client_module_path=page.client_module_path,
        metadata_path=page.metadata_path,
        module_key=page.module_key,
        client_asset_path=page.client_asset_path,
        server_asset_path=page.server_asset_path,
        content_hash=page.content_hash,
        loader_name=page.loader_name,
        loader_line=page.loader_line,
        head_elements=page.head_elements,
        head_is_dynamic=page.head_is_dynamic,
    )

    renderer = StubRenderer()
    overlay = StubOverlay()
    request = Request({"type": "http", "http_version": "1.1", "method": "GET", "path": "/", "root_path": "", "headers": []})

    response = await build_page_navigation_response(
        request=request,
        settings=settings,
        page=page,
        renderer=renderer,
        overlay=overlay,
    )

    payload = json.loads(await _read_response_body(response))
    assert response.status_code == 500
    assert payload["ok"] is False
    assert payload["stage"] == "loader"
    assert overlay.events and overlay.events[0][0] == "error"


@pytest.mark.anyio
async def test_build_page_response_missing_loader(settings: DevServerSettings, tmp_path: Path) -> None:
    server_module = tmp_path / "server" / "missing.py"
    server_module.parent.mkdir(parents=True, exist_ok=True)
    server_module.write_text("async def other(request):\n    return {}\n", encoding="utf-8")

    page = _page_route(tmp_path, loader_name="load_home")
    page = PageRoute(
        path=page.path,
        source_relative_path=page.source_relative_path,
        source_absolute_path=page.source_absolute_path,
        server_module_path=server_module,
        client_module_path=page.client_module_path,
        metadata_path=page.metadata_path,
        module_key=page.module_key,
        client_asset_path=page.client_asset_path,
        server_asset_path=page.server_asset_path,
        content_hash=page.content_hash,
        loader_name=page.loader_name,
        loader_line=page.loader_line,
        head_elements=page.head_elements,
        head_is_dynamic=page.head_is_dynamic,
    )

    renderer = StubRenderer()
    request = Request({"type": "http", "http_version": "1.1", "method": "GET", "path": "/", "root_path": "", "headers": []})

    response = await build_page_response(
        request=request,
        settings=settings,
        page=page,
        renderer=renderer,
    )

    body = (await _read_response_body(response)).decode()
    assert response.status_code == 500
    assert "LoaderExecutionError" in body


@pytest.mark.anyio
async def test_build_page_response_handles_renderer_error(settings: DevServerSettings, tmp_path: Path) -> None:
    server_module = tmp_path / "server" / "index.py"
    server_module.parent.mkdir(parents=True, exist_ok=True)
    server_module.write_text(
        """
async def load_home(request):
    return {}
""",
        encoding="utf-8",
    )

    page = _page_route(tmp_path, loader_name="load_home")
    page = PageRoute(
        path=page.path,
        source_relative_path=page.source_relative_path,
        source_absolute_path=page.source_absolute_path,
        server_module_path=server_module,
        client_module_path=page.client_module_path,
        metadata_path=page.metadata_path,
        module_key=page.module_key,
        client_asset_path=page.client_asset_path,
        server_asset_path=page.server_asset_path,
        content_hash=page.content_hash,
        loader_name=page.loader_name,
        loader_line=page.loader_line,
        head_elements=page.head_elements,
        head_is_dynamic=page.head_is_dynamic,
    )

    class FailingRenderer(StubRenderer):
        async def render(
            self,
            component_path: Path,
            props: dict[str, object],
            *,
            request_pathname: str | None = None,
            csrf_token: str | None = None,
        ) -> str:  # type: ignore[override]
            raise ComponentRenderError("render boom")

    renderer = FailingRenderer()
    overlay = StubOverlay()
    request = Request({"type": "http", "http_version": "1.1", "method": "GET", "path": "/", "root_path": "", "headers": []})

    response = await build_page_response(
        request=request,
        settings=settings,
        page=page,
        renderer=renderer,
        overlay=overlay,
    )

    body = (await _read_response_body(response)).decode()
    assert response.status_code == 500
    assert "render boom" in body
    assert overlay.events and overlay.events[0][0] == "error"
    renderer_breadcrumbs = overlay.events[0][2]
    assert renderer_breadcrumbs[0]["status"] == "passed"
    assert renderer_breadcrumbs[1]["status"] == "failed"


@pytest.mark.anyio
async def test_build_page_navigation_response_handles_renderer_error(
    settings: DevServerSettings,
    tmp_path: Path,
) -> None:
    server_module = tmp_path / "server" / "renderer_nav.py"
    server_module.parent.mkdir(parents=True, exist_ok=True)
    server_module.write_text(
        """
async def load_home(request):
    return {}
""",
        encoding="utf-8",
    )

    page = _page_route(tmp_path, loader_name="load_home")
    page = PageRoute(
        path=page.path,
        source_relative_path=page.source_relative_path,
        source_absolute_path=page.source_absolute_path,
        server_module_path=server_module,
        client_module_path=page.client_module_path,
        metadata_path=page.metadata_path,
        module_key=page.module_key,
        client_asset_path=page.client_asset_path,
        server_asset_path=page.server_asset_path,
        content_hash=page.content_hash,
        loader_name=page.loader_name,
        loader_line=page.loader_line,
        head_elements=page.head_elements,
        head_is_dynamic=page.head_is_dynamic,
    )

    class NavFailingRenderer(StubRenderer):
        async def render(
            self,
            component_path: Path,
            props: dict[str, object],
            *,
            request_pathname: str | None = None,
            csrf_token: str | None = None,
        ) -> str:  # type: ignore[override]
            raise ComponentRenderError("render boom")

    renderer = NavFailingRenderer()
    overlay = StubOverlay()
    request = Request({"type": "http", "http_version": "1.1", "method": "GET", "path": "/", "root_path": "", "headers": []})

    response = await build_page_navigation_response(
        request=request,
        settings=settings,
        page=page,
        renderer=renderer,
        overlay=overlay,
    )

    payload = json.loads(await _read_response_body(response))
    assert response.status_code == 500
    assert payload["ok"] is False
    assert payload["stage"] == "renderer"
    assert overlay.events and overlay.events[0][0] == "error"


@pytest.mark.anyio
async def test_build_page_response_uses_manifest_assets_in_production(
    settings: DevServerSettings,
    tmp_path: Path,
) -> None:
    renderer = StubRenderer()
    renderer.responses.append(RenderResult(html="<section>prod</section>"))

    prod_settings = replace(
        settings,
        debug=False,
        page_manifest={
            "/": {
                "client": {
                    "file": "assets/index.js",
                    "imports": [],
                    "css": ["assets/index.css"],
                }
            }
        },
    )

    page = _page_route(tmp_path, loader_name=None)
    request = Request({"type": "http", "http_version": "1.1", "method": "GET", "path": "/", "root_path": "", "headers": []})

    response = await build_page_response(
        request=request,
        settings=prod_settings,
        page=page,
        renderer=renderer,
    )

    body = (await _read_response_body(response)).decode()
    assert "/client/assets/index.js" in body
    assert 'rel="stylesheet" href="/client/assets/index.css"' in body
    assert "@vite/client" not in body


@pytest.mark.anyio
async def test_build_page_response_handles_missing_manifest_entry(
    settings: DevServerSettings,
    tmp_path: Path,
) -> None:
    renderer = StubRenderer()
    renderer.responses.append(RenderResult(html="<section>prod</section>"))

    prod_settings = replace(settings, debug=False, page_manifest={})

    page = _page_route(tmp_path, loader_name=None)
    request = Request({"type": "http", "http_version": "1.1", "method": "GET", "path": "/", "root_path": "", "headers": []})

    response = await build_page_response(
        request=request,
        settings=prod_settings,
        page=page,
        renderer=renderer,
    )

    body = (await _read_response_body(response)).decode()
    assert "Missing Manifest Entry" in body


@pytest.mark.anyio
async def test_build_page_response_handles_head_error(settings: DevServerSettings, tmp_path: Path) -> None:
    server_module = tmp_path / "server" / "head.py"
    server_module.parent.mkdir(parents=True, exist_ok=True)
    server_module.write_text(
        "HEAD = ['<title>Ok</title>', 123]\n",
        encoding="utf-8",
    )

    page = replace(
        _page_route(tmp_path, loader_name=None),
        server_module_path=server_module,
        head_elements=(),
        head_is_dynamic=True,
    )

    renderer = StubRenderer()
    overlay = StubOverlay()
    request = Request({"type": "http", "http_version": "1.1", "method": "GET", "path": "/", "root_path": "", "headers": []})

    response = await build_page_response(
        request=request,
        settings=settings,
        page=page,
        renderer=renderer,
        overlay=overlay,
    )

    body = (await _read_response_body(response)).decode()
    assert response.status_code == 500
    assert "HeadEvaluationError" in body
    assert overlay.events and overlay.events[0][0] == "error"
    breadcrumbs = overlay.events[0][2]
    assert breadcrumbs[0]["status"] == "skipped"
    assert breadcrumbs[1]["status"] == "unknown"


@pytest.mark.anyio
async def test_build_page_response_supports_callable_head(settings: DevServerSettings, tmp_path: Path) -> None:
    server_module = tmp_path / "server" / "head_callable.py"
    server_module.parent.mkdir(parents=True, exist_ok=True)
    server_module.write_text(
        """
def HEAD(data):
    product = data['product']
    return [
        f"<title>{product['name']} - Pyxle</title>",
        f'<meta name="description" content="{product["description"]}" />',
    ]

async def load_home(request):
    return {
        'product': {
            'name': 'Gizmo',
            'description': 'Callable heads reuse loader data',
        }
    }
""",
        encoding="utf-8",
    )

    page = replace(
        _page_route(tmp_path, loader_name="load_home"),
        server_module_path=server_module,
        head_elements=(),
        head_is_dynamic=True,
    )

    renderer = StubRenderer()
    renderer.responses.append(RenderResult(html="<main>callable</main>"))
    request = Request({"type": "http", "http_version": "1.1", "method": "GET", "path": "/", "root_path": "", "headers": []})

    response = await build_page_response(
        request=request,
        settings=settings,
        page=page,
        renderer=renderer,
    )

    body = (await _read_response_body(response)).decode()
    assert response.status_code == 200
    assert "<main>callable</main>" in body
    assert "<title>Gizmo - Pyxle</title>" in body
    assert 'content="Callable heads reuse loader data"' in body


@pytest.mark.anyio
async def test_build_page_navigation_response_handles_head_error(
    settings: DevServerSettings,
    tmp_path: Path,
) -> None:
    server_module = tmp_path / "server" / "head_nav.py"
    server_module.parent.mkdir(parents=True, exist_ok=True)
    server_module.write_text(
        "HEAD = ['<title>Ok</title>', 123]\n",
        encoding="utf-8",
    )

    page = replace(
        _page_route(tmp_path, loader_name=None),
        server_module_path=server_module,
        head_elements=(),
        head_is_dynamic=True,
    )

    renderer = StubRenderer()
    overlay = StubOverlay()
    request = Request({"type": "http", "http_version": "1.1", "method": "GET", "path": "/", "root_path": "", "headers": []})

    response = await build_page_navigation_response(
        request=request,
        settings=settings,
        page=page,
        renderer=renderer,
        overlay=overlay,
    )

    payload = json.loads(await _read_response_body(response))
    assert response.status_code == 500
    assert payload["ok"] is False
    assert payload["stage"] == "server"
    assert overlay.events and overlay.events[0][0] == "error"


@pytest.mark.anyio
async def test_build_page_response_refreshes_shared_python_modules(settings: DevServerSettings, tmp_path: Path) -> None:
    project_root = str(settings.project_root)
    added = False
    if project_root not in sys.path:
        sys.path.insert(0, project_root)
        added = True
    try:
        (settings.pages_dir / "components").mkdir(parents=True, exist_ok=True)
        (settings.pages_dir / "__init__.py").write_text("from .components import get_value\n", encoding="utf-8")
        (settings.pages_dir / "components" / "__init__.py").write_text(
            "from .head import get_value\n__all__ = ['get_value']\n",
            encoding="utf-8",
        )
        shared_module = settings.pages_dir / "components" / "head.py"
        shared_module.write_text(
            "def get_value():\n    return 'alpha'\n",
            encoding="utf-8",
        )

        server_module = tmp_path / "server" / "index.py"
        server_module.parent.mkdir(parents=True, exist_ok=True)
        server_module.write_text(
            "from pages.components import get_value\n\nasync def load_home(request):\n    return {'value': get_value()}\n",
            encoding="utf-8",
        )

        page = _page_route(tmp_path, loader_name="load_home")
        page = PageRoute(
            path=page.path,
            source_relative_path=page.source_relative_path,
            source_absolute_path=page.source_absolute_path,
            server_module_path=server_module,
            client_module_path=page.client_module_path,
            metadata_path=page.metadata_path,
            module_key=page.module_key,
            client_asset_path=page.client_asset_path,
            server_asset_path=page.server_asset_path,
            content_hash=page.content_hash,
            loader_name=page.loader_name,
            loader_line=page.loader_line,
            head_elements=page.head_elements,
            head_is_dynamic=page.head_is_dynamic,
        )

        renderer = StubRenderer()
        renderer.responses.extend(
            [
                RenderResult(html="<section>first</section>"),
                RenderResult(html="<section>second</section>"),
            ]
        )

        request = Request({
            "type": "http",
            "http_version": "1.1",
            "method": "GET",
            "path": "/",
            "root_path": "",
            "headers": [],
        })

        await build_page_response(
            request=request,
            settings=settings,
            page=page,
            renderer=renderer,
        )
        assert renderer.calls[-1][1]["data"]["value"] == "alpha"

        shared_module.write_text(
            "def get_value():\n    return 'beta'\n",
            encoding="utf-8",
        )

        # Simulate the watcher's rebuild on save: invalidate the changed helper
        # module and advance the reload generation. The next request then
        # re-imports the page — re-running its `from pages.components import ...`
        # against the fresh helper — exactly as `pyxle dev` does. Without a
        # rebuild the page (and its imported helper) stay cached, so module-level
        # state persists across requests like production.
        from pyxle.devserver.watcher import _invalidate_python_modules
        from pyxle.ssr import module_cache

        _invalidate_python_modules([shared_module], Path(project_root))
        module_cache.mark_rebuild()

        await build_page_response(
            request=request,
            settings=settings,
            page=page,
            renderer=renderer,
        )
        assert renderer.calls[-1][1]["data"]["value"] == "beta"
    finally:
        if added and project_root in sys.path:
            sys.path.remove(project_root)


def test_resolve_head_elements_returns_static(tmp_path: Path) -> None:
    page = replace(
        _page_route(tmp_path, loader_name=None),
        head_elements=("<title>Static</title>",),
        head_is_dynamic=False,
    )

    resolved = ssr_view._resolve_head_elements(page, module=None, loader_payload={})

    assert resolved == ("<title>Static</title>",)


def test_resolve_head_elements_reads_dynamic_module(tmp_path: Path) -> None:
    page = replace(
        _page_route(tmp_path, loader_name=None),
        head_elements=(),
        head_is_dynamic=True,
    )

    module = SimpleNamespace(HEAD=["<title>Dynamic</title>"])

    resolved = ssr_view._resolve_head_elements(page, module, loader_payload={})

    assert resolved == ("<title>Dynamic</title>",)


def test_resolve_head_elements_handles_missing_head(tmp_path: Path) -> None:
    page = replace(
        _page_route(tmp_path, loader_name=None),
        head_elements=(),
        head_is_dynamic=True,
    )

    module = SimpleNamespace()

    resolved = ssr_view._resolve_head_elements(page, module, loader_payload={})

    assert resolved == ()


def test_resolve_head_elements_validates_entries(tmp_path: Path) -> None:
    page = replace(
        _page_route(tmp_path, loader_name=None),
        head_elements=(),
        head_is_dynamic=True,
    )

    module = SimpleNamespace(HEAD=["<title>Ok</title>", 123])

    with pytest.raises(HeadEvaluationError):
        ssr_view._resolve_head_elements(page, module, loader_payload={})


def test_resolve_head_elements_invokes_callable_with_loader_data(tmp_path: Path) -> None:
    page = replace(
        _page_route(tmp_path, loader_name=None),
        head_elements=(),
        head_is_dynamic=True,
    )

    captured: dict[str, str] = {}

    def build_head(data: dict[str, object]) -> str:
        captured["title"] = f"{data['product']['name']}"
        return f"<title>{data['product']['name']}</title>"

    module = SimpleNamespace(HEAD=build_head)
    loader_payload = {"product": {"name": "Callables"}}

    resolved = ssr_view._resolve_head_elements(page, module, loader_payload)

    assert resolved == ("<title>Callables</title>",)
    assert captured["title"] == "Callables"


def test_resolve_head_elements_callable_requires_data_argument(tmp_path: Path) -> None:
    page = replace(
        _page_route(tmp_path, loader_name=None),
        head_elements=(),
        head_is_dynamic=True,
    )

    def build_head_without_args() -> str:
        return "<title>Invalid</title>"

    module = SimpleNamespace(HEAD=build_head_without_args)

    with pytest.raises(HeadEvaluationError):
        ssr_view._resolve_head_elements(page, module, loader_payload={})


@pytest.mark.anyio
async def test_build_page_response_merges_layout_head_blocks(settings: DevServerSettings, tmp_path: Path) -> None:
    """Test that layout head JSX blocks are merged with page head elements."""
    # Create layout.pyxl with head blocks
    layout_path = settings.pages_dir / "layout.pyxl"
    layout_path.write_text(
        """\n\nimport React from 'react';\n\nexport default function Layout({ children }) {\n    return <div>{children}</div>;\n}\n<Head>\n<meta name='layout-meta' content='from-layout'/>\n</Head>\n""",
        encoding="utf-8",
    )

    # Create index.pyxl with head elements and jsx blocks
    page_path = settings.pages_dir / "index.pyxl"
    page_path.write_text(
        """HEAD = "<title>Home</title>"\n\nimport React from 'react';\n\nexport default function Home({ data }) {\n    return <div>{data.message}</div>;\n}\n<Head>\n<meta name='page-meta' content='from-page'/>\n</Head>\n""",
        encoding="utf-8",
    )

    # Compile the pages
    from pyxle.devserver.builder import build_once
    build_once(settings)

    # Load route info
    from pyxle.devserver.registry import load_metadata_registry
    from pyxle.devserver.routes import build_route_table
    
    registry = load_metadata_registry(settings)
    routes = build_route_table(registry)
    page = routes.find_page("/")
    assert page is not None

    # Mock renderer
    renderer = StubRenderer()
    renderer.responses.append(RenderResult(html="<div>home</div>"))

    # Create mock request
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "query_string": b"",
        "headers": [],
        "server": ("localhost", 8000),
    }
    request = Request(scope)

    # Build response
    response = await build_page_response(
        request=request,
        settings=settings,
        page=page,
        renderer=renderer,
        overlay=None,
    )

    # Verify response is successful
    assert response.status_code == 200

    # Parse HTML to check head elements
    body_bytes = await _read_response_body(response)
    html = body_bytes.decode("utf-8")
    assert "<title>Home</title>" in html
    # Layout head block should be in the output
    assert "layout-meta" in html or "from-layout" in html or "from-page" in html


@pytest.mark.anyio
async def test_a_layouts_head_variable_reaches_the_document(
    settings: DevServerSettings, tmp_path: Path
) -> None:
    """A root layout's Python ``HEAD`` variable must survive the whole pipeline.

    This exercises the seam the unit tests each see only one side of: the
    registry splits a layout's contribution into two channels and the merger
    filters only one of them. Wire the evaluated ``HEAD`` channel into the
    filtered one and both sides still pass their own tests, while every page in
    the site silently loses its structured data — there is no rendered twin to
    restore it, because a Python ``HEAD`` variable is never rendered by React.

    JSON-LD and an inline ``<style>`` are the payloads that matter here: they
    are *all* braces, so they are exactly what a JSX-expression filter eats.
    """
    layout_path = settings.pages_dir / "layout.pyxl"
    layout_path.write_text(
        "HEAD = [\n"
        "    '<script type=\"application/ld+json\">"
        '{"@context":"https://schema.org","@type":"Organization","name":"Acme"}'
        "</script>',\n"
        "    '<style>.hero{color:red}</style>',\n"
        "]\n"
        "\n"
        "import React from 'react';\n"
        "\n"
        "export default function Layout({ children }) {\n"
        "    return <div>{children}</div>;\n"
        "}\n",
        encoding="utf-8",
    )

    page_path = settings.pages_dir / "index.pyxl"
    page_path.write_text(
        "import React from 'react';\n"
        "\n"
        "export default function Home() {\n"
        "    return <div>home</div>;\n"
        "}\n",
        encoding="utf-8",
    )

    from pyxle.devserver.builder import build_once

    build_once(settings)

    from pyxle.devserver.registry import load_metadata_registry
    from pyxle.devserver.routes import build_route_table

    registry = load_metadata_registry(settings)
    page = build_route_table(registry).find_page("/")
    assert page is not None

    renderer = StubRenderer()
    renderer.responses.append(RenderResult(html="<div>home</div>"))

    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/",
            "query_string": b"",
            "headers": [],
            "server": ("localhost", 8000),
        }
    )

    response = await build_page_response(
        request=request,
        settings=settings,
        page=page,
        renderer=renderer,
        overlay=None,
    )
    assert response.status_code == 200

    html = (await _read_response_body(response)).decode("utf-8")
    assert '{"@context":"https://schema.org","@type":"Organization","name":"Acme"}' in html
    assert ".hero{color:red}" in html


# ---------------------------------------------------------------------------
# Error boundary integration in build_page_response
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_build_page_response_loader_error_hits_error_boundary(
    settings: DevServerSettings, tmp_path: Path
) -> None:
    """LoaderError triggers _try_error_boundary and falls back to error doc."""
    server_module = tmp_path / "server" / "index.py"
    server_module.parent.mkdir(parents=True, exist_ok=True)
    server_module.write_text(
        "from pyxle.runtime import LoaderError\n"
        "async def my_loader(request):\n"
        "    raise LoaderError('Not allowed', status_code=403)\n",
        encoding="utf-8",
    )

    page = PageRoute(
        path="/",
        source_relative_path=Path("index.pyxl"),
        source_absolute_path=tmp_path / "pages" / "index.pyxl",
        server_module_path=server_module,
        client_module_path=tmp_path / "client" / "index.jsx",
        metadata_path=tmp_path / "metadata" / "index.json",
        module_key="pyxle.server.pages.index_lerr",
        client_asset_path="/pages/index.jsx",
        server_asset_path="/pages/index.py",
        content_hash="hash",
        loader_name="my_loader",
        loader_line=2,
        head_elements=(),
        head_is_dynamic=False,
    )

    renderer = StubRenderer()
    overlay = StubOverlay()
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "query_string": b"",
        "headers": [],
    }
    request = Request(scope)

    response = await build_page_response(
        request=request,
        settings=settings,
        page=page,
        renderer=renderer,
        overlay=overlay,
    )

    assert response.status_code == 403
    body = (await _read_response_body(response)).decode()
    assert "Not allowed" in body
    # A 403 the author raised on purpose is a response, not a crash, so the dev
    # overlay stays down and the boundary above is what the developer sees. The
    # route is cleared rather than left alone, so an earlier real fault on the
    # same route stops replaying once the author starts answering deliberately.
    assert not any(ev[0] == "error" for ev in overlay.events)
    assert ("clear", "/") in overlay.events


@pytest.mark.anyio
async def test_build_page_navigation_response_loader_error_uses_status_code(
    settings: DevServerSettings, tmp_path: Path
) -> None:
    """LoaderError in navigation mode returns the correct status code."""
    server_module = tmp_path / "server" / "nav_lerr.py"
    server_module.parent.mkdir(parents=True, exist_ok=True)
    server_module.write_text(
        "from pyxle.runtime import LoaderError\n"
        "async def my_loader(request):\n"
        "    raise LoaderError('Forbidden', status_code=403)\n",
        encoding="utf-8",
    )

    page = PageRoute(
        path="/nav",
        source_relative_path=Path("nav.pyxl"),
        source_absolute_path=tmp_path / "pages" / "nav.pyxl",
        server_module_path=server_module,
        client_module_path=tmp_path / "client" / "nav.jsx",
        metadata_path=tmp_path / "metadata" / "nav.json",
        module_key="pyxle.server.pages.nav_lerr",
        client_asset_path="/pages/nav.jsx",
        server_asset_path="/pages/nav.py",
        content_hash="hash",
        loader_name="my_loader",
        loader_line=2,
        head_elements=(),
        head_is_dynamic=False,
    )

    renderer = StubRenderer()
    overlay = StubOverlay()
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/nav",
        "query_string": b"",
        "headers": [],
    }
    request = Request(scope)

    response = await build_page_navigation_response(
        request=request,
        settings=settings,
        page=page,
        renderer=renderer,
        overlay=overlay,
    )

    payload = json.loads(await _read_response_body(response))
    assert response.status_code == 403
    assert payload["ok"] is False
    assert "Forbidden" in payload["error"]


def _nav_page_raising(tmp_path: Path, raise_source: str, module_stem: str) -> PageRoute:
    """A page whose loader raises whatever *raise_source* says."""
    server_module = tmp_path / "server" / f"{module_stem}.py"
    server_module.parent.mkdir(parents=True, exist_ok=True)
    server_module.write_text(
        "from pyxle.runtime import ActionError, LoaderError\n"
        "async def my_loader(request):\n"
        f"    {raise_source}\n",
        encoding="utf-8",
    )
    return PageRoute(
        path="/nav",
        source_relative_path=Path("nav.pyxl"),
        source_absolute_path=tmp_path / "pages" / "nav.pyxl",
        server_module_path=server_module,
        client_module_path=tmp_path / "client" / "nav.jsx",
        metadata_path=tmp_path / "metadata" / "nav.json",
        module_key=f"pyxle.server.pages.{module_stem}",
        client_asset_path="/pages/nav.jsx",
        server_asset_path="/pages/nav.py",
        content_hash="hash",
        loader_name="my_loader",
        loader_line=2,
        head_elements=(),
        head_is_dynamic=False,
    )


async def _nav_payload(page: PageRoute, settings: DevServerSettings) -> dict:
    response = await build_page_navigation_response(
        request=Request(
            {
                "type": "http",
                "method": "GET",
                "path": "/nav",
                "query_string": b"",
                "headers": [],
            }
        ),
        settings=settings,
        page=page,
        renderer=StubRenderer(),
        overlay=StubOverlay(),
    )
    return json.loads(await _read_response_body(response)), response.status_code


@pytest.mark.anyio
async def test_a_deliberate_404_does_not_raise_the_dev_overlay(
    settings: DevServerSettings, tmp_path: Path
) -> None:
    """A loader answering 404 on purpose must not be reported as a crash.

    The dev overlay is a crash reporter. A ``LoaderError(..., status_code=404)``
    is a response the author wrote — the error boundary renders it, which is the
    entire point of raising it — so covering the screen with
    *"Loader FAILED / Renderer BLOCKED"* and a traceback describes the developer's
    own deliberate code as a failure. ``_log_render_failure`` already draws this
    line for the server log; this is the same rule on the surface that missed it.
    """
    page = _nav_page_raising(
        tmp_path,
        "raise LoaderError('That post was deleted in 2019.', status_code=404)",
        "overlay_deliberate_404",
    )
    overlay = StubOverlay()

    await build_page_navigation_response(
        request=Request(
            {
                "type": "http",
                "method": "GET",
                "path": "/nav",
                "query_string": b"",
                "headers": [],
            }
        ),
        settings=settings,
        page=page,
        renderer=StubRenderer(),
        overlay=overlay,
    )

    assert not any(ev[0] == "error" for ev in overlay.events)
    assert ("clear", "/nav") in overlay.events


@pytest.mark.anyio
async def test_a_genuine_fault_still_raises_the_dev_overlay_and_names_its_url(
    settings: DevServerSettings, tmp_path: Path
) -> None:
    """The overlay must keep doing its job for real crashes.

    Two things at once: a 500 still reports, and it carries the URL that broke.
    The client shows the overlay only while it is on that URL, so without
    ``request_path`` one broken route covers every other page — a working page
    is then not merely obscured but unusable, because the full-screen overlay
    swallows its clicks.
    """
    page = _nav_page_raising(
        tmp_path,
        "raise RuntimeError('genuine server fault')",
        "overlay_real_fault",
    )
    overlay = StubOverlay()

    await build_page_navigation_response(
        request=Request(
            {
                "type": "http",
                "method": "GET",
                "path": "/nav",
                "query_string": b"",
                "headers": [],
            }
        ),
        settings=settings,
        page=page,
        renderer=StubRenderer(),
        overlay=overlay,
    )

    assert any(ev[0] == "error" for ev in overlay.events)
    assert overlay.request_paths == ["/nav"]


@pytest.mark.anyio
async def test_navigation_delivers_an_author_raised_message_in_production(
    settings: DevServerSettings, tmp_path: Path
) -> None:
    """Clicking a link must answer the same as pasting the URL.

    An author-raised ``LoaderError`` is deliberate, user-facing copy, and the
    error-handling guide promises it reaches the visitor *verbatim in every
    environment*. A full page load honoured that. A client-side navigation to
    the same page replaced it with ``"ServerError"`` and a generic string — so
    the identical 404 read one way when the URL was pasted and another when the
    link was clicked, and only the clicked one was wrong.
    """
    page = _nav_page_raising(
        tmp_path,
        "raise LoaderError('That post was deleted in 2019.', status_code=404)",
        "nav_author_prod",
    )

    payload, status = await _nav_payload(page, replace(settings, debug=False))

    assert status == 404
    assert payload["error"] == "That post was deleted in 2019."
    assert payload["errorType"] == "LoaderError"


@pytest.mark.anyio
async def test_navigation_carries_an_author_raised_data_payload(
    settings: DevServerSettings, tmp_path: Path
) -> None:
    """``LoaderError(data=...)`` is written for the boundary to render."""
    page = _nav_page_raising(
        tmp_path,
        "raise LoaderError('Gone', status_code=410, data={'slug': 'hello'})",
        "nav_author_data",
    )

    payload, _ = await _nav_payload(page, replace(settings, debug=False))

    assert payload["data"] == {"slug": "hello"}


@pytest.mark.anyio
async def test_navigation_still_hides_a_non_author_exception_in_production(
    settings: DevServerSettings, tmp_path: Path
) -> None:
    """The reason the sanitization exists, and it must survive the fix.

    A guard, not a proof — this passed before the change too. It is here so
    that widening the author-raised exemption can never quietly widen to
    everything (CLAUDE.md rule 18).
    """
    page = _nav_page_raising(
        tmp_path,
        "raise RuntimeError('row 4471 at /srv/secrets/app.db key=sk_live_ABC')",
        "nav_leak_prod",
    )

    payload, status = await _nav_payload(page, replace(settings, debug=False))
    raw = json.dumps(payload)

    assert status == 500
    assert payload["errorType"] == "ServerError"
    assert payload["error"] == "An unexpected error occurred."
    for secret in ("sk_live_ABC", "srv/secrets", "row 4471", "RuntimeError"):
        assert secret not in raw


def test_normalize_head_entries_none_returns_empty(tmp_path: Path) -> None:
    """_normalize_head_entries(page, None) returns an empty tuple."""
    from pyxle.ssr.view import _normalize_head_entries

    page = _page_route(tmp_path, loader_name=None)
    assert _normalize_head_entries(page, None) == ()


def test_normalize_head_entries_string_wraps_in_tuple(tmp_path: Path) -> None:
    from pyxle.ssr.view import _normalize_head_entries

    page = _page_route(tmp_path, loader_name=None)
    assert _normalize_head_entries(page, "<title>Hi</title>") == ("<title>Hi</title>",)


def test_normalize_head_entries_list_of_strings(tmp_path: Path) -> None:
    from pyxle.ssr.view import _normalize_head_entries

    page = _page_route(tmp_path, loader_name=None)
    result = _normalize_head_entries(page, ["<title>A</title>", "<meta name='x' />"])
    assert result == ("<title>A</title>", "<meta name='x' />")


def test_normalize_head_entries_bad_type_raises(tmp_path: Path) -> None:
    from pyxle.ssr.view import _normalize_head_entries

    page = _page_route(tmp_path, loader_name=None)
    with pytest.raises(HeadEvaluationError, match="must be a string"):
        _normalize_head_entries(page, 42)


def test_normalize_head_entries_non_string_item_raises(tmp_path: Path) -> None:
    from pyxle.ssr.view import _normalize_head_entries

    page = _page_route(tmp_path, loader_name=None)
    with pytest.raises(HeadEvaluationError, match="must be strings"):
        _normalize_head_entries(page, ["valid", 42])


def test_evaluate_head_callable_async_raises(tmp_path: Path) -> None:
    """Async HEAD callables are rejected."""
    from pyxle.ssr.view import _evaluate_head_callable

    page = _page_route(tmp_path, loader_name=None)

    async def async_head(data):
        return "<title>Async</title>"

    with pytest.raises(HeadEvaluationError, match="must return synchronously"):
        _evaluate_head_callable(page, async_head, {"key": "val"})


@pytest.mark.anyio
async def test_runtime_head_overrides_static_dynamic_title(
    settings: DevServerSettings, tmp_path: Path,
) -> None:
    """Regression: a dynamic ``<title>{expression}</title>`` inside a
    ``<Head>`` block must render the runtime-evaluated value, not the
    literal source text captured at compile time.

    The compiler stores ``<title>{pageTitle}</title>`` verbatim in
    ``page.head_jsx_blocks``. The Head component, when rendered, calls
    ``renderToStaticMarkup`` and produces ``<title>Installation</title>``,
    which is forwarded as a runtime head block. The merger must give the
    runtime version precedence so the literal ``{pageTitle}`` never
    leaks into the rendered HTML.
    """
    page = replace(
        _page_route(tmp_path, loader_name=None),
        head_elements=(),
        head_is_dynamic=False,
        head_jsx_blocks=("<title>{pageTitle}</title>",),
    )

    renderer = StubRenderer()
    renderer.responses.append(
        RenderResult(
            html="<main>doc</main>",
            head_elements=("<title>Installation - Pyxle Docs</title>",),
        )
    )
    request = Request({
        "type": "http",
        "http_version": "1.1",
        "method": "GET",
        "path": "/",
        "root_path": "",
        "headers": [],
    })

    response = await build_page_response(
        request=request,
        settings=settings,
        page=page,
        renderer=renderer,
    )

    body = (await _read_response_body(response)).decode()
    assert response.status_code == 200
    assert "<title>Installation - Pyxle Docs</title>" in body
    assert "{pageTitle}" not in body


def test_import_server_module_loads_and_registers(tmp_path: Path) -> None:
    """_import_server_module loads the module and registers it in sys.modules."""
    from pyxle.ssr.view import _import_server_module

    mod_path = tmp_path / "test_mod.py"
    mod_path.write_text("VALUE = 42\n", encoding="utf-8")
    key = "pyxle._test_import_module"

    module = _import_server_module(key, mod_path)
    assert module.VALUE == 42
    assert sys.modules[key] is module

    # Cleanup
    sys.modules.pop(key, None)


def test_import_server_module_caches_in_production(tmp_path: Path) -> None:
    """In production (debug=False), calling twice returns the cached module."""
    from pyxle.ssr.view import _import_server_module

    mod_path = tmp_path / "test_cached.py"
    mod_path.write_text("COUNTER = 1\n", encoding="utf-8")
    key = "pyxle._test_cached_module"

    first = _import_server_module(key, mod_path, debug=False)
    assert first.COUNTER == 1
    first.COUNTER = 99

    second = _import_server_module(key, mod_path, debug=False)
    assert second is first
    assert second.COUNTER == 99  # State preserved

    sys.modules.pop(key, None)


def test_import_server_module_persists_globals_until_rebuild(tmp_path: Path) -> None:
    """In dev (debug=True), the module is reused across calls so module-level
    globals persist across requests exactly like production, and is re-imported
    only after a rebuild advances the reload generation."""
    from pyxle.ssr import module_cache
    from pyxle.ssr.view import _import_server_module

    mod_path = tmp_path / "test_debug.py"
    mod_path.write_text("COUNTER = 0\n", encoding="utf-8")
    key = "pyxle._test_debug_module"

    try:
        first = _import_server_module(key, mod_path, debug=True)
        assert first.COUNTER == 0
        first.COUNTER = 42

        # No rebuild between requests: same module, globals persist.
        second = _import_server_module(key, mod_path, debug=True)
        assert second is first
        assert second.COUNTER == 42

        # A rebuild advances the generation → the next import re-executes the
        # module body from disk, resetting module-level globals.
        module_cache.mark_rebuild()
        third = _import_server_module(key, mod_path, debug=True)
        assert third is not first
        assert third.COUNTER == 0
    finally:
        sys.modules.pop(key, None)


@pytest.mark.anyio
async def test_build_page_response_forwards_csrf_token_from_scope(
    settings: DevServerSettings, tmp_path: Path
) -> None:
    """The CSRF middleware stashes the active token on
    ``scope['pyxle.csrf_token']`` so SSR can plumb it through to
    ``globalThis.__PYXLE_CSRF_TOKEN__`` — that's how ``<Form>`` learns
    the token at render time without doing a network round-trip.
    """
    renderer = StubRenderer()
    renderer.responses.append(RenderResult(html="<main>x</main>"))
    overlay = StubOverlay()
    page = _page_route(tmp_path, loader_name=None)

    request = Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "GET",
            "path": "/",
            "root_path": "",
            "headers": [],
            "pyxle.csrf_token": "tok-from-middleware",
        }
    )

    await build_page_response(
        request=request,
        settings=settings,
        page=page,
        renderer=renderer,
        overlay=overlay,
    )

    assert renderer.csrf_tokens[-1] == "tok-from-middleware"


@pytest.mark.anyio
async def test_build_page_response_omits_csrf_token_when_absent(
    settings: DevServerSettings, tmp_path: Path
) -> None:
    """If no CSRF middleware is in the stack, the renderer just sees
    ``None`` and ``<Form>`` drops back to its cookie-only path. The
    framework should never invent a token of its own."""
    renderer = StubRenderer()
    renderer.responses.append(RenderResult(html="<main>x</main>"))
    overlay = StubOverlay()
    page = _page_route(tmp_path, loader_name=None)

    request = Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "GET",
            "path": "/",
            "root_path": "",
            "headers": [],
            # No "state" key whatsoever — CSRF middleware not present.
        }
    )

    await build_page_response(
        request=request,
        settings=settings,
        page=page,
        renderer=renderer,
        overlay=overlay,
    )

    assert renderer.csrf_tokens[-1] is None


# ---------------------------------------------------------------------------
# Helpers for error / not-found boundary tests
# ---------------------------------------------------------------------------


def _boundary_page(tmp_path: Path, *, filename: str, module_key: str) -> PageRoute:
    """Build a PageRoute standing in for a compiled error/not-found boundary.

    The boundary has no loader and static head elements so that rendering it
    only depends on the stub renderer succeeding.
    """
    stem = filename.removesuffix(".pyxl")
    return PageRoute(
        path=f"/__{stem}",
        source_relative_path=Path(filename),
        source_absolute_path=tmp_path / "pages" / filename,
        server_module_path=tmp_path / "server" / f"{stem}.py",
        client_module_path=tmp_path / "client" / f"{stem}.jsx",
        metadata_path=tmp_path / "metadata" / f"{stem}.json",
        module_key=module_key,
        client_asset_path=f"/pages/{stem}.jsx",
        server_asset_path=f"/pages/{stem}.py",
        content_hash="hash",
        loader_name=None,
        loader_line=None,
        head_elements=("<title>Boundary</title>",),
        head_is_dynamic=False,
    )


@pytest.mark.anyio
async def test_build_page_response_loader_error_renders_error_boundary(
    settings: DevServerSettings, tmp_path: Path
) -> None:
    """A LoaderError with a registered error boundary returns the rendered
    boundary document (status from the error) rather than the default
    fallback. Exercises the ``return boundary_response`` path and the
    ``overlay is None`` branch in the LoaderError handler."""
    from pyxle.devserver.error_pages import ErrorBoundaryRegistry

    server_module = tmp_path / "server" / "le_boundary.py"
    server_module.parent.mkdir(parents=True, exist_ok=True)
    server_module.write_text(
        "from pyxle.runtime import LoaderError\n"
        "async def my_loader(request):\n"
        "    raise LoaderError('Teapot', status_code=418, data={'why': 'brew'})\n",
        encoding="utf-8",
    )

    page = replace(
        _page_route(tmp_path, loader_name="my_loader"),
        server_module_path=server_module,
        module_key="pyxle.server.pages.le_boundary",
        head_elements=(),
    )
    boundary = _boundary_page(tmp_path, filename="error.pyxl", module_key="pyxle.server.pages.le_boundary_err")
    registry = ErrorBoundaryRegistry(error_pages={".": boundary}, not_found_pages={})

    renderer = StubRenderer()
    renderer.responses.append(RenderResult(html="<aside>boundary rendered</aside>"))
    request = Request({"type": "http", "method": "GET", "path": "/", "query_string": b"", "headers": []})

    response = await build_page_response(
        request=request,
        settings=settings,
        page=page,
        renderer=renderer,
        error_boundaries=registry,
        overlay=None,
    )

    assert response.status_code == 418
    body = (await _read_response_body(response)).decode()
    assert "<aside>boundary rendered</aside>" in body
    # The boundary component receives the structured error context as props.
    assert renderer.calls[-1][0] == boundary.client_module_path
    error_props = renderer.calls[-1][1]["error"]
    assert error_props["message"] == "Teapot"
    assert error_props["statusCode"] == 418
    assert error_props["type"] == "LoaderError"
    assert error_props["data"] == {"why": "brew"}


@pytest.mark.anyio
async def test_build_page_response_loader_exec_error_renders_error_boundary(
    settings: DevServerSettings, tmp_path: Path
) -> None:
    """A LoaderExecutionError (loader returns a non-mapping) with a registered
    boundary returns the rendered boundary at status 500. Exercises the
    ``return boundary_response`` path in the LoaderExecutionError handler."""
    from pyxle.devserver.error_pages import ErrorBoundaryRegistry

    server_module = tmp_path / "server" / "lee_boundary.py"
    server_module.parent.mkdir(parents=True, exist_ok=True)
    server_module.write_text("async def my_loader(request):\n    return 'not a mapping'\n", encoding="utf-8")

    page = replace(
        _page_route(tmp_path, loader_name="my_loader"),
        server_module_path=server_module,
        module_key="pyxle.server.pages.lee_boundary",
        head_elements=(),
    )
    boundary = _boundary_page(tmp_path, filename="error.pyxl", module_key="pyxle.server.pages.lee_boundary_err")
    registry = ErrorBoundaryRegistry(error_pages={".": boundary}, not_found_pages={})

    renderer = StubRenderer()
    renderer.responses.append(RenderResult(html="<aside>exec boundary</aside>"))
    overlay = StubOverlay()
    request = Request({"type": "http", "method": "GET", "path": "/", "query_string": b"", "headers": []})

    response = await build_page_response(
        request=request,
        settings=settings,
        page=page,
        renderer=renderer,
        error_boundaries=registry,
        overlay=overlay,
    )

    assert response.status_code == 500
    body = (await _read_response_body(response)).decode()
    assert "<aside>exec boundary</aside>" in body
    assert renderer.calls[-1][1]["error"]["type"] == "LoaderExecutionError"


@pytest.mark.anyio
async def test_build_page_response_head_error_renders_error_boundary(
    settings: DevServerSettings, tmp_path: Path
) -> None:
    """A HeadEvaluationError with a registered boundary returns the rendered
    boundary at status 500, and skips overlay notification when overlay is
    None. Exercises the ``return boundary_response`` path and the
    ``overlay is None`` branch in the HeadEvaluationError handler."""
    from pyxle.devserver.error_pages import ErrorBoundaryRegistry

    server_module = tmp_path / "server" / "head_boundary.py"
    server_module.parent.mkdir(parents=True, exist_ok=True)
    server_module.write_text("HEAD = ['<title>Ok</title>', 123]\n", encoding="utf-8")

    page = replace(
        _page_route(tmp_path, loader_name=None),
        server_module_path=server_module,
        head_elements=(),
        head_is_dynamic=True,
    )
    boundary = _boundary_page(tmp_path, filename="error.pyxl", module_key="pyxle.server.pages.head_boundary_err")
    registry = ErrorBoundaryRegistry(error_pages={".": boundary}, not_found_pages={})

    renderer = StubRenderer()
    renderer.responses.append(RenderResult(html="<aside>head boundary</aside>"))
    request = Request({"type": "http", "method": "GET", "path": "/", "query_string": b"", "headers": []})

    response = await build_page_response(
        request=request,
        settings=settings,
        page=page,
        renderer=renderer,
        error_boundaries=registry,
        overlay=None,
    )

    assert response.status_code == 500
    body = (await _read_response_body(response)).decode()
    assert "<aside>head boundary</aside>" in body
    assert renderer.calls[-1][1]["error"]["type"] == "HeadEvaluationError"


@pytest.mark.anyio
async def test_build_page_response_renderer_error_renders_error_boundary(
    settings: DevServerSettings, tmp_path: Path
) -> None:
    """A ComponentRenderError with a registered boundary re-renders the
    boundary component (the first render raises, the second succeeds) and
    returns it at status 500. Exercises the ``return boundary_response`` path
    in the ComponentRenderError handler and the ``overlay is None`` branch."""
    from pyxle.devserver.error_pages import ErrorBoundaryRegistry

    server_module = tmp_path / "server" / "render_boundary.py"
    server_module.parent.mkdir(parents=True, exist_ok=True)
    server_module.write_text("async def my_loader(request):\n    return {}\n", encoding="utf-8")

    page = replace(
        _page_route(tmp_path, loader_name="my_loader"),
        server_module_path=server_module,
        module_key="pyxle.server.pages.render_boundary",
        head_elements=(),
    )
    boundary = _boundary_page(tmp_path, filename="error.pyxl", module_key="pyxle.server.pages.render_boundary_err")
    registry = ErrorBoundaryRegistry(error_pages={".": boundary}, not_found_pages={})

    class FailFirstRenderer(StubRenderer):
        async def render(
            self,
            component_path: Path,
            props: dict[str, object],
            *,
            request_pathname: str | None = None,
            csrf_token: str | None = None,
        ) -> RenderResult:
            self.calls.append((component_path, props))
            if len(self.calls) == 1:
                raise ComponentRenderError("render boom")
            return RenderResult(html="<aside>render boundary</aside>")

    renderer = FailFirstRenderer()
    request = Request({"type": "http", "method": "GET", "path": "/", "query_string": b"", "headers": []})

    response = await build_page_response(
        request=request,
        settings=settings,
        page=page,
        renderer=renderer,
        error_boundaries=registry,
        overlay=None,
    )

    assert response.status_code == 500
    body = (await _read_response_body(response)).decode()
    assert "<aside>render boundary</aside>" in body
    # Two render attempts: the page (which raised) and the boundary.
    assert len(renderer.calls) == 2
    assert renderer.calls[-1][0] == boundary.client_module_path
    assert renderer.calls[-1][1]["error"]["type"] == "ComponentRenderError"


@pytest.mark.anyio
async def test_build_page_response_clears_overlay_on_missing_manifest(
    settings: DevServerSettings, tmp_path: Path
) -> None:
    """When the manifest lookup fails (production with an empty manifest), the
    response falls back to a fully-rendered document and the overlay is still
    notified that the route is clear. Exercises the overlay ``notify_clear``
    inside the ManifestLookupError branch."""
    renderer = StubRenderer()
    renderer.responses.append(RenderResult(html="<section>prod</section>"))
    overlay = StubOverlay()

    prod_settings = replace(settings, debug=False, page_manifest={})
    page = _page_route(tmp_path, loader_name=None)
    request = Request({"type": "http", "http_version": "1.1", "method": "GET", "path": "/", "root_path": "", "headers": []})

    response = await build_page_response(
        request=request,
        settings=prod_settings,
        page=page,
        renderer=renderer,
        overlay=overlay,
    )

    body = (await _read_response_body(response)).decode()
    assert "Missing Manifest Entry" in body
    assert overlay.events == [("clear", "/")]


@pytest.mark.anyio
async def test_build_page_response_loader_error_without_boundary_or_overlay(
    settings: DevServerSettings, tmp_path: Path
) -> None:
    """A LoaderError with neither an overlay nor an error boundary falls back
    to the default error document. Exercises the ``overlay is None`` branch in
    the LoaderError handler together with the no-boundary fallback."""
    server_module = tmp_path / "server" / "le_plain.py"
    server_module.parent.mkdir(parents=True, exist_ok=True)
    server_module.write_text(
        "from pyxle.runtime import LoaderError\n"
        "async def my_loader(request):\n"
        "    raise LoaderError('Denied', status_code=403)\n",
        encoding="utf-8",
    )

    page = replace(
        _page_route(tmp_path, loader_name="my_loader"),
        server_module_path=server_module,
        module_key="pyxle.server.pages.le_plain",
        head_elements=(),
    )

    renderer = StubRenderer()
    request = Request({"type": "http", "method": "GET", "path": "/", "query_string": b"", "headers": []})

    response = await build_page_response(
        request=request,
        settings=settings,
        page=page,
        renderer=renderer,
        overlay=None,
    )

    body = (await _read_response_body(response)).decode()
    assert response.status_code == 403
    assert "Denied" in body


# ---------------------------------------------------------------------------
# build_not_found_response
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_build_not_found_response_returns_none_without_registry(
    settings: DevServerSettings, tmp_path: Path
) -> None:
    """With no error-boundary registry, build_not_found_response returns None
    so the caller falls back to the default 404."""
    from pyxle.ssr.view import build_not_found_response

    renderer = StubRenderer()
    request = Request({"type": "http", "method": "GET", "path": "/missing", "query_string": b"", "headers": []})

    result = await build_not_found_response(
        request=request,
        settings=settings,
        renderer=renderer,
        error_boundaries=None,
    )

    assert result is None


@pytest.mark.anyio
async def test_build_not_found_response_returns_none_without_boundary(
    settings: DevServerSettings, tmp_path: Path
) -> None:
    """With a registry that has no matching not-found boundary, the function
    returns None."""
    from pyxle.devserver.error_pages import ErrorBoundaryRegistry
    from pyxle.ssr.view import build_not_found_response

    registry = ErrorBoundaryRegistry(error_pages={}, not_found_pages={})
    renderer = StubRenderer()
    request = Request({"type": "http", "method": "GET", "path": "/missing", "query_string": b"", "headers": []})

    result = await build_not_found_response(
        request=request,
        settings=settings,
        renderer=renderer,
        error_boundaries=registry,
    )

    assert result is None


@pytest.mark.anyio
async def test_build_not_found_response_renders_boundary_document(
    settings: DevServerSettings, tmp_path: Path
) -> None:
    """A matching not-found boundary is rendered into a 404 document. Exercises
    the debug-mode document-rendering success path of build_not_found_response."""
    from pyxle.devserver.error_pages import ErrorBoundaryRegistry
    from pyxle.ssr.view import build_not_found_response

    boundary = _boundary_page(tmp_path, filename="not-found.pyxl", module_key="pyxle.server.pages.notfound")
    registry = ErrorBoundaryRegistry(error_pages={}, not_found_pages={".": boundary})

    renderer = StubRenderer()
    renderer.responses.append(RenderResult(html="<h1>Page not found</h1>"))
    request = Request({"type": "http", "method": "GET", "path": "/missing", "query_string": b"", "headers": []})

    response = await build_not_found_response(
        request=request,
        settings=replace(settings, debug=True),
        renderer=renderer,
        error_boundaries=registry,
    )

    assert response is not None
    assert response.status_code == 404
    body = (await _read_response_body(response)).decode()
    assert "<h1>Page not found</h1>" in body
    assert "<title>Boundary</title>" in body
    assert renderer.calls[-1][0] == boundary.client_module_path


@pytest.mark.anyio
async def test_build_not_found_response_returns_none_when_boundary_fails(
    settings: DevServerSettings, tmp_path: Path
) -> None:
    """If rendering the not-found boundary itself raises, the function returns
    None so the caller uses the default 404."""
    from pyxle.devserver.error_pages import ErrorBoundaryRegistry
    from pyxle.ssr.view import build_not_found_response

    boundary = _boundary_page(tmp_path, filename="not-found.pyxl", module_key="pyxle.server.pages.notfound_fail")
    registry = ErrorBoundaryRegistry(error_pages={}, not_found_pages={".": boundary})

    class BrokenRenderer(StubRenderer):
        async def render(
            self,
            component_path: Path,
            props: dict[str, object],
            *,
            request_pathname: str | None = None,
            csrf_token: str | None = None,
        ) -> RenderResult:
            raise ComponentRenderError("boundary boom")

    renderer = BrokenRenderer()
    request = Request({"type": "http", "method": "GET", "path": "/missing", "query_string": b"", "headers": []})

    result = await build_not_found_response(
        request=request,
        settings=settings,
        renderer=renderer,
        error_boundaries=registry,
    )

    assert result is None


# ---------------------------------------------------------------------------
# Navigation response edge branches
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_build_page_navigation_response_success_without_overlay(
    settings: DevServerSettings, tmp_path: Path
) -> None:
    """The navigation success path skips overlay notification when overlay is
    None and still returns the JSON payload. Exercises the production-mode path
    and the ``overlay is None`` branch of the success path."""
    renderer = StubRenderer()
    renderer.responses.append(RenderResult(html="<main>nav</main>"))

    prod_settings = replace(settings, debug=False)
    page = _page_route(tmp_path, loader_name=None)
    request = Request({"type": "http", "http_version": "1.1", "method": "GET", "path": "/", "root_path": "", "headers": []})

    response = await build_page_navigation_response(
        request=request,
        settings=prod_settings,
        page=page,
        renderer=renderer,
        overlay=None,
    )

    payload = json.loads(await _read_response_body(response))
    assert response.status_code == 200
    assert payload["ok"] is True
    assert payload["props"] == {"data": {}}
    assert "<title>Home</title>" in payload["headMarkup"]


@pytest.mark.anyio
async def test_build_page_navigation_response_loader_error_without_overlay(
    settings: DevServerSettings, tmp_path: Path
) -> None:
    """A LoaderError in navigation mode without an overlay still returns the
    structured error payload. Exercises the ``overlay is None`` branch in
    _navigation_error_response."""
    server_module = tmp_path / "server" / "nav_le_noov.py"
    server_module.parent.mkdir(parents=True, exist_ok=True)
    server_module.write_text(
        "from pyxle.runtime import LoaderError\n"
        "async def my_loader(request):\n"
        "    raise LoaderError('Nope', status_code=401)\n",
        encoding="utf-8",
    )

    page = replace(
        _page_route(tmp_path, loader_name="my_loader"),
        path="/nav",
        server_module_path=server_module,
        module_key="pyxle.server.pages.nav_le_noov",
        head_elements=(),
    )
    renderer = StubRenderer()
    request = Request({"type": "http", "method": "GET", "path": "/nav", "query_string": b"", "headers": []})

    response = await build_page_navigation_response(
        request=request,
        settings=settings,
        page=page,
        renderer=renderer,
        overlay=None,
    )

    payload = json.loads(await _read_response_body(response))
    assert response.status_code == 401
    assert payload["ok"] is False
    assert payload["stage"] == "loader"
    assert payload["errorType"] == "LoaderError"
    assert "Nope" in payload["error"]


# ---------------------------------------------------------------------------
# Loader result shapes and head resolution edge cases
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_build_page_response_sync_loader_single_tuple(
    settings: DevServerSettings, tmp_path: Path
) -> None:
    """A *synchronous* loader returning a one-element ``(mapping,)`` tuple is
    accepted with the default 200 status. Exercises the non-awaitable loader
    branch and the single-element tuple branch of _normalize_loader_result."""
    server_module = tmp_path / "server" / "sync_one.py"
    server_module.parent.mkdir(parents=True, exist_ok=True)
    # Sync def (no await) returning a 1-tuple — no explicit status code.
    server_module.write_text("def load_home(request):\n    return ({'value': 'solo'},)\n", encoding="utf-8")

    page = replace(
        _page_route(tmp_path, loader_name="load_home"),
        server_module_path=server_module,
        module_key="pyxle.server.pages.sync_one",
    )
    renderer = StubRenderer()
    renderer.responses.append(RenderResult(html="<p>solo</p>"))
    request = Request({"type": "http", "method": "GET", "path": "/", "query_string": b"", "headers": []})

    response = await build_page_response(
        request=request,
        settings=settings,
        page=page,
        renderer=renderer,
    )

    assert response.status_code == 200
    body = (await _read_response_body(response)).decode()
    assert "<p>solo</p>" in body
    assert renderer.calls[-1][1] == {"data": {"value": "solo"}}


# ---------------------------------------------------------------------------
# Production sanitization + server-side logging of SPA-navigation failures.
#
# Page (HTML) responses already sanitize in production via
# ``render_error_document``. The SPA-navigation channel returns JSON, so it
# needs the SAME treatment — an exception message can carry file paths, row
# IDs, or secrets, and must never reach the client (CLAUDE.md rule 18). And
# because production responses are deliberately opaque, the real error must be
# written to the server log so an operator can still diagnose a 500.
# ---------------------------------------------------------------------------


def _nav_render_fault(tmp_path: Path, *, message: str):
    """Return a ``(page, renderer, request)`` triple whose SSR render raises a
    ``ComponentRenderError`` carrying ``message``."""
    server_module = tmp_path / "server" / "nav_fault.py"
    server_module.parent.mkdir(parents=True, exist_ok=True)
    server_module.write_text(
        "async def load_home(request):\n    return {}\n", encoding="utf-8"
    )
    page = replace(
        _page_route(tmp_path, loader_name="load_home"),
        server_module_path=server_module,
        module_key="pyxle.server.pages.nav_fault",
    )

    class _FailingRenderer(StubRenderer):
        async def render(
            self,
            component_path: Path,
            props: dict[str, object],
            *,
            request_pathname: str | None = None,
            csrf_token: str | None = None,
        ) -> str:  # type: ignore[override]
            raise ComponentRenderError(message)

    request = Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "GET",
            "path": "/",
            "root_path": "",
            "headers": [],
        }
    )
    return page, _FailingRenderer(), request


@pytest.mark.anyio
async def test_navigation_error_payload_sanitized_in_production(
    settings: DevServerSettings, tmp_path: Path
) -> None:
    """In production the navigation-error JSON must not echo the exception
    message or its concrete type — that would leak internal state to any
    client that triggers a render error during SPA navigation."""
    page, renderer, request = _nav_render_fault(
        tmp_path, message="boom at /srv/app/secret_db.py row 42"
    )
    prod_settings = replace(settings, debug=False)

    response = await build_page_navigation_response(
        request=request,
        settings=prod_settings,
        page=page,
        renderer=renderer,
        overlay=None,
    )
    payload = json.loads(await _read_response_body(response))

    assert response.status_code == 500
    assert payload["ok"] is False
    assert payload["stage"] == "renderer"
    # The exception detail must NOT reach the client.
    assert "secret_db.py" not in payload["error"]
    assert "row 42" not in payload["error"]
    assert payload["errorType"] == "ServerError"


@pytest.mark.anyio
async def test_navigation_error_payload_detailed_in_dev(
    settings: DevServerSettings, tmp_path: Path
) -> None:
    """In development (the default fixture, ``debug=True``) the detail IS
    exposed — the developer needs it, and it mirrors the dev error overlay."""
    page, renderer, request = _nav_render_fault(tmp_path, message="render boom detail")

    response = await build_page_navigation_response(
        request=request,
        settings=settings,
        page=page,
        renderer=renderer,
        overlay=None,
    )
    payload = json.loads(await _read_response_body(response))

    assert response.status_code == 500
    assert payload["errorType"] == "ComponentRenderError"
    assert "render boom detail" in payload["error"]


@pytest.mark.anyio
async def test_render_failure_is_logged_server_side(
    settings: DevServerSettings, tmp_path: Path, caplog
) -> None:
    """Production responses are opaque, so the real error must land in the
    server log (with the route and full detail) for the operator to diagnose."""
    page, renderer, request = _nav_render_fault(tmp_path, message="loggable failure detail")
    prod_settings = replace(settings, debug=False)

    with caplog.at_level("ERROR", logger="pyxle.ssr.view"):
        await build_page_navigation_response(
            request=request,
            settings=prod_settings,
            page=page,
            renderer=renderer,
            overlay=None,
        )

    fault_logs = [
        r for r in caplog.records
        if r.name == "pyxle.ssr.view" and r.levelname == "ERROR"
    ]
    assert fault_logs, "a 500 render fault must be logged server-side"
    message = fault_logs[-1].getMessage()
    assert page.path in message  # the route
    assert "loggable failure detail" in message  # the real, unsanitized detail


def test_resolve_head_elements_imports_module_when_none(tmp_path: Path) -> None:
    """When ``head_is_dynamic`` is True and no module is passed, the resolver
    imports the server module itself to read ``HEAD``. Exercises the lazy
    module import inside _resolve_head_elements."""
    server_module = tmp_path / "server" / "head_lazy.py"
    server_module.parent.mkdir(parents=True, exist_ok=True)
    server_module.write_text("HEAD = ['<meta name=\"lazy\" content=\"yes\" />']\n", encoding="utf-8")

    page = replace(
        _page_route(tmp_path, loader_name=None),
        server_module_path=server_module,
        module_key="pyxle.server.pages.head_lazy",
        head_elements=(),
        head_is_dynamic=True,
    )

    resolved = ssr_view._resolve_head_elements(page, None, {}, debug=False)
    assert resolved == ('<meta name="lazy" content="yes" />',)

    sys.modules.pop("pyxle.server.pages.head_lazy", None)


def test_evaluate_head_callable_awaitable_without_close_raises(tmp_path: Path) -> None:
    """An awaitable HEAD return value that lacks a ``close`` method is still
    rejected. Exercises the ``hasattr(value, 'close')`` False branch in
    _evaluate_head_callable."""
    from pyxle.ssr.view import _evaluate_head_callable

    page = _page_route(tmp_path, loader_name=None)

    class AwaitableNoClose:
        def __await__(self):
            yield
            return "<title>x</title>"

    def head(data):
        return AwaitableNoClose()

    with pytest.raises(HeadEvaluationError, match="must return synchronously"):
        _evaluate_head_callable(page, head, {"k": "v"})


# ---------------------------------------------------------------------------
# Layout loaders (real compilation)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_build_page_response_executes_layout_loaders(
    settings: DevServerSettings, tmp_path: Path
) -> None:
    """A ``layout.pyxl`` declaring a ``@server`` loader contributes its data
    under ``layoutData`` in the rendered component props. Exercises the layout
    loader execution loop and the ``props['layoutData']`` assignment."""
    layout_path = settings.pages_dir / "layout.pyxl"
    layout_path.write_text(
        "from pyxle.runtime import server\n"
        "\n"
        "@server\n"
        "async def load_layout(request):\n"
        "    return {'banner': 'from-layout'}\n"
        "\n"
        "import React from 'react';\n"
        "\n"
        "export default function Layout({ children }) {\n"
        "    return <div>{children}</div>;\n"
        "}\n",
        encoding="utf-8",
    )

    page_path = settings.pages_dir / "index.pyxl"
    page_path.write_text(
        "import React from 'react';\n"
        "\n"
        "export default function Home({ data }) {\n"
        "    return <div>home</div>;\n"
        "}\n",
        encoding="utf-8",
    )

    from pyxle.devserver.builder import build_once
    from pyxle.devserver.registry import load_metadata_registry
    from pyxle.devserver.routes import build_route_table

    build_once(settings)
    registry = load_metadata_registry(settings)
    routes = build_route_table(registry)
    page = routes.find_page("/")
    assert page is not None

    renderer = StubRenderer()
    renderer.responses.append(RenderResult(html="<div>home</div>"))
    request = Request({"type": "http", "method": "GET", "path": "/", "query_string": b"", "headers": []})

    response = await build_page_response(
        request=request,
        settings=settings,
        page=page,
        renderer=renderer,
    )

    assert response.status_code == 200
    props = renderer.calls[-1][1]
    assert props["data"] == {}
    assert props["layoutData"] == {"banner": "from-layout"}


@pytest.mark.anyio
async def test_execute_layout_loaders_merges_tuple_and_skips_missing(
    settings: DevServerSettings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """_execute_layout_loaders walks every discovered layout loader, handling:

    * a layout whose module is missing the named loader (skipped),
    * a *synchronous* loader returning a ``(dict, ...)`` tuple (the leading
      dict is used),
    * a synchronous loader returning a non-mapping (ignored).

    The surviving mapping results are merged into a single dict.
    """
    from pyxle.devserver.registry import LayoutLoaderInfo
    from pyxle.ssr.view import _execute_layout_loaders

    layout_dir = tmp_path / "layouts"
    layout_dir.mkdir()

    # Module A: declares a loader name that does not exist on the module.
    missing_mod = layout_dir / "missing.py"
    missing_mod.write_text("OTHER = 1\n", encoding="utf-8")

    # Module B: synchronous loader returning a one-tuple of a dict.
    tuple_mod = layout_dir / "tuple_layout.py"
    tuple_mod.write_text(
        "def load_layout(request):\n    return ({'banner': 'tuple-data'},)\n",
        encoding="utf-8",
    )

    # Module C: synchronous loader returning a non-mapping (must be ignored).
    nonmap_mod = layout_dir / "nonmap_layout.py"
    nonmap_mod.write_text(
        "def load_layout(request):\n    return 'not-a-mapping'\n",
        encoding="utf-8",
    )

    infos = (
        LayoutLoaderInfo(
            relative_path=Path("missing.pyxl"),
            server_module_path=missing_mod,
            module_key="pyxle._test_layout_missing",
            loader_name="load_layout",
        ),
        LayoutLoaderInfo(
            relative_path=Path("tuple_layout.pyxl"),
            server_module_path=tuple_mod,
            module_key="pyxle._test_layout_tuple",
            loader_name="load_layout",
        ),
        LayoutLoaderInfo(
            relative_path=Path("nonmap_layout.pyxl"),
            server_module_path=nonmap_mod,
            module_key="pyxle._test_layout_nonmap",
            loader_name="load_layout",
        ),
    )

    monkeypatch.setattr(
        "pyxle.devserver.registry.find_layout_loaders",
        lambda _settings, _path: infos,
    )

    page = _page_route(tmp_path, loader_name=None)
    request = Request({"type": "http", "method": "GET", "path": "/", "query_string": b"", "headers": []})

    try:
        layout_results = await _execute_layout_loaders(
            settings=replace(settings, debug=True),
            page=page,
            request=request,
        )
    finally:
        for key in (
            "pyxle._test_layout_missing",
            "pyxle._test_layout_tuple",
            "pyxle._test_layout_nonmap",
        ):
            sys.modules.pop(key, None)

    # Only the tuple loader contributed mapping data; the missing-loader module
    # was skipped and the non-mapping return was ignored.
    assert layout_results.merged == {"banner": "tuple-data"}
    # ...and that data is also filed under its own layout, which is what a
    # callable HEAD in that layout is handed.
    assert layout_results.per_layout == {
        Path("tuple_layout.pyxl"): {"banner": "tuple-data"}
    }


# ---------------------------------------------------------------------------
# Server module importer + module purge edge cases
# ---------------------------------------------------------------------------


def test_ensure_app_root_importable_inserts_project_root(tmp_path: Path) -> None:
    """A compiled module under a ``.pyxle-build`` directory makes its project
    root (the directory containing ``.pyxle-build``) importable. Exercises the
    ``sys.path.insert`` line of _ensure_app_root_importable."""
    from pyxle.ssr.view import _ensure_app_root_importable

    module_path = tmp_path / ".pyxle-build" / "server" / "pages" / "deep.py"
    module_path.parent.mkdir(parents=True)
    module_path.write_text("X = 1\n", encoding="utf-8")

    project_root = str(tmp_path.resolve())
    if project_root in sys.path:
        sys.path.remove(project_root)

    try:
        _ensure_app_root_importable(module_path)
        assert project_root in sys.path
        # Calling again is idempotent — it must not duplicate the entry.
        _ensure_app_root_importable(module_path)
        assert sys.path.count(project_root) == 1
    finally:
        while project_root in sys.path:
            sys.path.remove(project_root)


def test_import_server_module_raises_when_spec_unavailable(tmp_path: Path) -> None:
    """A module path with an unrecognized extension yields no import spec, so
    the importer raises a LoaderExecutionError naming the path. Exercises the
    ``spec is None`` guard in _import_server_module."""
    from pyxle.ssr.view import LoaderExecutionError, _import_server_module

    bad_path = tmp_path / "module.unknownext"
    bad_path.write_text("X = 1\n", encoding="utf-8")

    with pytest.raises(LoaderExecutionError, match="Unable to load page module"):
        _import_server_module("pyxle._test_no_spec", bad_path)

    assert "pyxle._test_no_spec" not in sys.modules


@pytest.mark.anyio
async def test_build_streaming_page_response_streams_prefix_body_suffix(
    settings: DevServerSettings, tmp_path: Path
) -> None:
    page = replace(_page_route(tmp_path, loader_name=None), uses_suspense=True)
    renderer = StubRenderer()  # only used for an error-boundary fallback render
    overlay = StubOverlay()
    request = Request(
        {"type": "http", "http_version": "1.1", "method": "GET", "path": "/", "root_path": "", "headers": []}
    )

    response = await build_streaming_page_response(
        request=request,
        settings=settings,
        page=page,
        renderer=renderer,
        stream_render=_stream_of(
            {"type": "chunk", "html": "<main>shell</main>"},
            {"type": "end", "styles": [], "headElements": []},
        ),
        overlay=overlay,
    )

    body = (await _read_response_body(response)).decode()
    assert response.status_code == 200
    # Static head flushed in the prefix, the streamed chunk in the body, the
    # hydration props script in the suffix.
    assert "<title>Home</title>" in body
    assert "<main>shell</main>" in body
    assert "__PYXLE_PROPS__" in body
    # The buffered renderer was never invoked — the stream produced the body.
    assert renderer.calls == []
    assert overlay.events == [("clear", "/")]


@pytest.mark.anyio
async def test_build_streaming_page_response_shell_error_falls_back(
    settings: DevServerSettings, tmp_path: Path
) -> None:
    page = replace(_page_route(tmp_path, loader_name=None), uses_suspense=True)
    renderer = StubRenderer()
    overlay = StubOverlay()
    request = Request(
        {"type": "http", "http_version": "1.1", "method": "GET", "path": "/", "root_path": "", "headers": []}
    )

    # The very first frame is an error (renderToPipeableStream onShellError) —
    # no bytes were sent yet, so it maps to the sanitized error document.
    response = await build_streaming_page_response(
        request=request,
        settings=settings,
        page=page,
        renderer=renderer,
        stream_render=_stream_of({"type": "error", "error": "shell exploded"}),
        overlay=overlay,
    )

    assert response.status_code == 500
    # An error before the first byte must never emit a partial streamed body.
    body = (await _read_response_body(response)).decode()
    assert "<main>shell</main>" not in body
    assert any(event[0] == "error" for event in overlay.events)


@pytest.mark.anyio
async def test_build_streaming_page_response_without_manifest_falls_back_to_buffered(
    settings: DevServerSettings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # When the streaming shell can't be built (no client manifest to link the
    # hydration bundle), the request falls back to the buffered builder rather
    # than emitting a broken document.
    page = replace(_page_route(tmp_path, loader_name=None), uses_suspense=True)
    renderer = StubRenderer()
    renderer.responses.append(RenderResult(html="<main>buffered-fallback</main>"))
    overlay = StubOverlay()
    request = Request(
        {"type": "http", "http_version": "1.1", "method": "GET", "path": "/", "root_path": "", "headers": []}
    )

    def _raise_manifest(*args, **kwargs):
        raise ssr_view.ManifestLookupError

    monkeypatch.setattr(ssr_view, "build_document_shell", _raise_manifest)

    # stream_render must never be consumed once we've decided to buffer.
    async def _never(*args, **kwargs):  # pragma: no cover - must not be called
        yield {"type": "chunk", "html": "<should-not-appear/>"}

    response = await build_streaming_page_response(
        request=request,
        settings=settings,
        page=page,
        renderer=renderer,
        stream_render=_never,
        overlay=overlay,
    )

    body = (await _read_response_body(response)).decode()
    assert response.status_code == 200
    assert "<main>buffered-fallback</main>" in body
    assert "<should-not-appear/>" not in body


@pytest.mark.anyio
async def test_streaming_passes_loading_fallback_path(
    settings: DevServerSettings, tmp_path: Path
) -> None:
    loading_route = replace(
        _page_route(tmp_path, loader_name=None),
        client_module_path=tmp_path / "client" / "loading.jsx",
    )
    page = replace(
        _page_route(tmp_path, loader_name=None),
        uses_suspense=True,
        loading_boundary=loading_route,
    )
    captured: dict = {}

    async def _capturing(component_path, props, *, request_pathname=None, csrf_token=None, fallback_path=None):
        captured["fallback_path"] = fallback_path
        yield {"type": "chunk", "html": "<main>x</main>"}
        yield {"type": "end"}

    request = Request(
        {"type": "http", "http_version": "1.1", "method": "GET", "path": "/", "root_path": "", "headers": []}
    )
    await build_streaming_page_response(
        request=request,
        settings=settings,
        page=page,
        renderer=StubRenderer(),
        stream_render=_capturing,
        overlay=StubOverlay(),
    )
    # The page's nearest loading.pyxl is forwarded so the worker can wrap it.
    assert captured["fallback_path"] == loading_route.client_module_path


@pytest.mark.anyio
async def test_nav_payload_for_streaming_page_uses_static_head_and_carries_loading_asset(
    settings: DevServerSettings, tmp_path: Path
) -> None:
    # A streaming-eligible page's nav payload is built from the loader + static
    # head WITHOUT a buffered render (renderToString throws on suspension).
    loading_route = replace(
        _page_route(tmp_path, loader_name=None),
        client_asset_path="/pages/loading.jsx",
    )
    page = replace(
        _page_route(tmp_path, loader_name=None),
        uses_suspense=True,
        loading_boundary=loading_route,
    )
    renderer = StubRenderer()
    request = Request(
        {"type": "http", "http_version": "1.1", "method": "GET", "path": "/", "root_path": "", "headers": []}
    )

    response = await build_page_navigation_response(
        request=request,
        settings=settings,
        page=page,
        renderer=renderer,
        overlay=StubOverlay(),
    )

    body = json.loads((await _read_response_body(response)).decode())
    assert body["ok"] is True
    assert body["page"]["loadingAssetPath"] == "/pages/loading.jsx"
    assert "<title>Home</title>" in body["headMarkup"]  # static HEAD
    # No buffered render happened for the streaming nav payload.
    assert renderer.calls == []


@pytest.mark.anyio
async def test_nav_payload_for_plain_page_has_null_loading_asset(
    settings: DevServerSettings, tmp_path: Path
) -> None:
    page = _page_route(tmp_path, loader_name=None)  # no boundary, no suspense
    renderer = StubRenderer()
    renderer.responses.append(RenderResult(html="<main>plain</main>"))
    request = Request(
        {"type": "http", "http_version": "1.1", "method": "GET", "path": "/", "root_path": "", "headers": []}
    )

    response = await build_page_navigation_response(
        request=request,
        settings=settings,
        page=page,
        renderer=renderer,
        overlay=StubOverlay(),
    )
    body = json.loads((await _read_response_body(response)).decode())
    assert body["page"]["loadingAssetPath"] is None
    assert body["page"]["errorAssetPath"] is None
    # A plain page's nav payload still renders buffered for its runtime head.
    assert renderer.calls != []


@pytest.mark.anyio
async def test_nav_payload_carries_error_asset_when_boundary_present(
    settings: DevServerSettings, tmp_path: Path
) -> None:
    # A page with a nearest error.pyxl carries its client asset so the client
    # error boundary can render the same error.pyxl the server would.
    error_route = replace(
        _page_route(tmp_path, loader_name=None),
        client_asset_path="/pages/error.jsx",
    )
    page = replace(
        _page_route(tmp_path, loader_name=None),
        error_boundary=error_route,
    )
    renderer = StubRenderer()
    renderer.responses.append(RenderResult(html="<main>plain</main>"))
    request = Request(
        {"type": "http", "http_version": "1.1", "method": "GET", "path": "/", "root_path": "", "headers": []}
    )

    response = await build_page_navigation_response(
        request=request,
        settings=settings,
        page=page,
        renderer=renderer,
        overlay=StubOverlay(),
    )
    body = json.loads((await _read_response_body(response)).decode())
    assert body["page"]["errorAssetPath"] == "/pages/error.jsx"


# ---------------------------------------------------------------------------
# Missing request.state attribute guidance
# ---------------------------------------------------------------------------


def test_missing_state_attribute_matches_starlette_state_error() -> None:
    """Only Starlette's exact State AttributeError pattern is recognized."""
    from starlette.datastructures import State

    try:
        State().db
    except AttributeError as exc:
        state_error = exc

    assert ssr_view.missing_state_attribute(state_error) == "db"
    assert ssr_view.missing_state_attribute(AttributeError("boom")) is None
    assert (
        ssr_view.missing_state_attribute(
            ValueError("'State' object has no attribute 'db'")
        )
        is None
    )


def test_missing_request_state_error_messages() -> None:
    """'db' gets the pyxle-db pointer; other names get the generic guidance."""
    db_error = ssr_view.MissingRequestStateError("db")
    assert db_error.attribute == "db"
    assert "request.state.db is not set" in str(db_error)
    assert "pyxle-db" in str(db_error)
    assert '"plugins": ["pyxle-db"]' in str(db_error)

    generic = ssr_view.MissingRequestStateError("session")
    assert generic.attribute == "session"
    assert "request.state.session is not set" in str(generic)
    assert "plugins or middleware" in str(generic)
    assert "pyxle-db" in str(generic)  # the example still shows the pattern


def _state_page(tmp_path: Path, loader_body: str) -> PageRoute:
    server_module = tmp_path / "server" / "state_page.py"
    server_module.parent.mkdir(parents=True, exist_ok=True)
    server_module.write_text(
        f"async def load_home(request):\n    {loader_body}\n",
        encoding="utf-8",
    )
    page = _page_route(tmp_path, loader_name="load_home")
    return replace(page, server_module_path=server_module, module_key="pyxle.server.pages.state_page")


@pytest.mark.anyio
async def test_execute_loader_wraps_missing_state_with_guidance(tmp_path: Path) -> None:
    """request.state.db without a provider → MissingRequestStateError, chained."""
    page = _state_page(tmp_path, "return {'rows': request.state.db}")
    request = Request({"type": "http", "method": "GET", "path": "/", "headers": []})

    with pytest.raises(ssr_view.MissingRequestStateError) as excinfo:
        await ssr_view._execute_loader(page, request, module=None, debug=True)

    assert excinfo.value.attribute == "db"
    assert isinstance(excinfo.value.__cause__, AttributeError)
    assert "'State' object has no attribute 'db'" in str(excinfo.value.__cause__)


@pytest.mark.anyio
async def test_execute_loader_other_attribute_error_gets_no_state_guidance(
    tmp_path: Path,
) -> None:
    """An unrelated AttributeError must not get the request.state guidance.

    It is still a loader crash (so ``error.pyxl`` renders), but it is *not* a
    :class:`MissingRequestStateError` — telling a developer to install the
    pyxle-db plugin because their own object lacks an attribute would send them
    somewhere with nothing to fix.
    """
    page = _state_page(tmp_path, "raise AttributeError('boom')")
    request = Request({"type": "http", "method": "GET", "path": "/", "headers": []})

    with pytest.raises(ssr_view.LoaderCrashError) as excinfo:
        await ssr_view._execute_loader(page, request, module=None, debug=True)

    assert not isinstance(excinfo.value, ssr_view.MissingRequestStateError)
    assert isinstance(excinfo.value.__cause__, AttributeError)
    assert str(excinfo.value.__cause__) == "boom"
    assert "AttributeError: boom" in str(excinfo.value)


@pytest.mark.anyio
async def test_build_page_response_missing_state_shows_guidance_in_dev(
    settings: DevServerSettings, tmp_path: Path
) -> None:
    page = _state_page(tmp_path, "return {'rows': request.state.db}")
    renderer = StubRenderer()
    overlay = StubOverlay()
    request = Request(
        {"type": "http", "http_version": "1.1", "method": "GET", "path": "/", "root_path": "", "headers": []}
    )

    response = await build_page_response(
        request=request,
        settings=settings,
        page=page,
        renderer=renderer,
        overlay=overlay,
    )

    body = (await _read_response_body(response)).decode()
    assert response.status_code == 500
    assert "MissingRequestStateError" in body
    assert "pyxle-db" in body

    # The overlay breadcrumb carries the same guidance for the dev overlay.
    assert overlay.events and overlay.events[0][0] == "error"
    breadcrumbs = overlay.events[0][2]
    assert breadcrumbs[0]["status"] == "failed"
    assert "request.state.db is not set" in breadcrumbs[0]["detail"]


@pytest.mark.anyio
async def test_build_page_response_missing_state_stays_generic_in_production(
    settings: DevServerSettings, tmp_path: Path
) -> None:
    prod_settings = replace(settings, debug=False)
    page = _state_page(tmp_path, "return {'rows': request.state.db}")
    renderer = StubRenderer()
    request = Request(
        {"type": "http", "http_version": "1.1", "method": "GET", "path": "/", "root_path": "", "headers": []}
    )

    response = await build_page_response(
        request=request,
        settings=prod_settings,
        page=page,
        renderer=renderer,
    )

    body = (await _read_response_body(response)).decode()
    assert response.status_code == 500
    assert "pyxle-db" not in body
    assert "request.state" not in body
    assert "MissingRequestStateError" not in body


# ---------------------------------------------------------------------------
# A loader whose own body raises — the ordinary bug reaches error.pyxl
# ---------------------------------------------------------------------------


def _boundary_registry(tmp_path: Path):
    """A registry whose root error.pyxl is a renderable stub boundary page."""
    from pyxle.devserver.error_pages import ErrorBoundaryRegistry

    boundary = _boundary_page(
        tmp_path, filename="error.pyxl", module_key="pyxle.server.pages.crash_boundary"
    )
    return ErrorBoundaryRegistry(error_pages={".": boundary}, not_found_pages={})


def _http_request(path: str = "/") -> Request:
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "GET",
            "path": path,
            "root_path": "",
            "headers": [],
        }
    )


@pytest.mark.anyio
async def test_execute_loader_wraps_body_exception_as_loader_crash(tmp_path: Path) -> None:
    """A plain KeyError in a loader body becomes a classified loader failure.

    This is the regression: it used to propagate bare, so the render pipeline
    could not tell it from an unexpected framework fault and never consulted
    the application's error.pyxl.
    """
    page = _state_page(tmp_path, "return {'id': {'name': 'ada'}['user_id']}")
    request = _http_request()

    with pytest.raises(ssr_view.LoaderCrashError) as excinfo:
        await ssr_view._execute_loader(page, request, module=None, debug=True)

    # A loader-stage failure, so every page pipeline recognises it...
    assert isinstance(excinfo.value, ssr_view.LoaderExecutionError)
    # ...the original exception survives for the traceback...
    assert isinstance(excinfo.value.__cause__, KeyError)
    # ...and the message names the loader, the route, and the real exception.
    assert "load_home" in str(excinfo.value)
    assert "/" in str(excinfo.value)
    assert "KeyError" in str(excinfo.value)
    assert "user_id" in str(excinfo.value)


@pytest.mark.anyio
async def test_execute_loader_wraps_exception_with_empty_message(tmp_path: Path) -> None:
    """An exception with no message still yields a readable classification."""
    page = _state_page(tmp_path, "raise ValueError()")

    with pytest.raises(ssr_view.LoaderCrashError) as excinfo:
        await ssr_view._execute_loader(page, _http_request(), module=None, debug=True)

    assert str(excinfo.value).endswith("raised ValueError")


@pytest.mark.anyio
async def test_execute_loader_passes_author_loader_error_through(tmp_path: Path) -> None:
    """LoaderError is the author's deliberate signal and is never wrapped.

    Wrapping it would cost the status code and replace intentional, user-facing
    copy with a generic message in production.
    """
    from pyxle.runtime import LoaderError

    page = _state_page(
        tmp_path,
        "from pyxle.runtime import LoaderError; raise LoaderError('Gone', status_code=410)",
    )

    with pytest.raises(LoaderError) as excinfo:
        await ssr_view._execute_loader(page, _http_request(), module=None, debug=True)

    assert not isinstance(excinfo.value, ssr_view.LoaderExecutionError)
    assert excinfo.value.status_code == 410
    assert excinfo.value.message == "Gone"


@pytest.mark.anyio
async def test_build_page_response_loader_crash_renders_error_boundary(
    settings: DevServerSettings, tmp_path: Path
) -> None:
    """The headline fix: an ordinary loader bug renders the app's error.pyxl."""
    page = _state_page(tmp_path, "return {'id': {'name': 'ada'}['user_id']}")
    renderer = StubRenderer()
    renderer.responses.append(RenderResult(html="<main>our own error page</main>"))
    overlay = StubOverlay()
    boundaries = _boundary_registry(tmp_path)

    response = await build_page_response(
        request=_http_request(),
        settings=settings,
        page=page,
        renderer=renderer,
        overlay=overlay,
        error_boundaries=boundaries,
    )

    assert response.status_code == 500
    body = (await _read_response_body(response)).decode()
    assert "our own error page" in body

    # The boundary component — not the page — was rendered, with error props.
    component_path, props = renderer.calls[-1]
    assert component_path == boundaries.error_pages["."].client_module_path
    assert props["error"]["statusCode"] == 500

    # The overlay reports it as a loader-stage failure, so the dev breadcrumbs
    # say the renderer was blocked rather than "outcome unknown".
    assert overlay.events and overlay.events[0][0] == "error"
    breadcrumbs = overlay.events[0][2]
    assert breadcrumbs[0]["status"] == "failed"
    assert breadcrumbs[1]["status"] == "blocked"


@pytest.mark.anyio
async def test_build_page_response_loader_crash_shows_detail_in_dev(
    settings: DevServerSettings, tmp_path: Path
) -> None:
    """In dev the boundary receives the real exception detail."""
    page = _state_page(tmp_path, "return {'id': {'name': 'ada'}['user_id']}")
    renderer = StubRenderer()
    renderer.responses.append(RenderResult(html="<main>err</main>"))

    await build_page_response(
        request=_http_request(),
        settings=settings,
        page=page,
        renderer=renderer,
        error_boundaries=_boundary_registry(tmp_path),
    )

    message = renderer.calls[-1][1]["error"]["message"]
    assert "KeyError" in message
    assert "user_id" in message


@pytest.mark.anyio
async def test_build_page_response_loader_crash_hides_internals_in_production(
    settings: DevServerSettings, tmp_path: Path
) -> None:
    """In production the visitor gets the app's error page, not the internals.

    The boundary renders (the visitor stays inside the application's design),
    but the exception message and class name never reach it — CLAUDE.md rule 18.
    """
    prod_settings = replace(settings, debug=False)
    page = _state_page(
        tmp_path,
        "raise RuntimeError('connect failed: postgres://u:hunter2@db/app row 4711')",
    )
    renderer = StubRenderer()
    renderer.responses.append(RenderResult(html="<main>our own error page</main>"))

    response = await build_page_response(
        request=_http_request(),
        settings=prod_settings,
        page=page,
        renderer=renderer,
        error_boundaries=_boundary_registry(tmp_path),
    )

    assert response.status_code == 500
    body = (await _read_response_body(response)).decode()
    assert "our own error page" in body
    for secret in ("hunter2", "4711", "postgres://", "RuntimeError", "LoaderCrashError"):
        assert secret not in body

    error_props = renderer.calls[-1][1]["error"]
    assert error_props["message"] == "An unexpected error occurred."
    assert error_props["type"] == "ServerError"


@pytest.mark.anyio
async def test_loader_crash_is_logged_even_when_the_boundary_renders(
    settings: DevServerSettings, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A rendered error.pyxl must not make the failure vanish from the logs.

    The production response is sanitized on purpose, so the server log is the
    only record of what broke. Logging only on the fallback path would mean
    every application that ships an error.pyxl silently swallows its own
    500s — the boundary would hide exactly what it is there to survive.
    """
    prod_settings = replace(settings, debug=False)
    page = _state_page(tmp_path, "return {'id': {'name': 'ada'}['user_id']}")
    renderer = StubRenderer()
    renderer.responses.append(RenderResult(html="<main>our own error page</main>"))

    with caplog.at_level(logging.ERROR, logger="pyxle.ssr.view"):
        response = await build_page_response(
            request=_http_request(),
            settings=prod_settings,
            page=page,
            renderer=renderer,
            error_boundaries=_boundary_registry(tmp_path),
        )

    assert "our own error page" in (await _read_response_body(response)).decode()
    records = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert len(records) == 1, "the failure must be logged exactly once"
    assert "KeyError" in records[0].exc_text
    assert "user_id" in records[0].exc_text


@pytest.mark.anyio
async def test_loader_crash_without_boundary_is_logged_once(
    settings: DevServerSettings, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The fallback path logs the same failure exactly once, not twice."""
    prod_settings = replace(settings, debug=False)
    page = _state_page(tmp_path, "return {'id': {'name': 'ada'}['user_id']}")

    with caplog.at_level(logging.ERROR, logger="pyxle.ssr.view"):
        await build_page_response(
            request=_http_request(),
            settings=prod_settings,
            page=page,
            renderer=StubRenderer(),
        )

    assert len([r for r in caplog.records if r.levelno >= logging.ERROR]) == 1


@pytest.mark.anyio
async def test_build_page_navigation_response_loader_crash_reports_loader_stage(
    settings: DevServerSettings, tmp_path: Path
) -> None:
    """A client-side navigation reports the crash as a loader-stage failure."""
    page = _state_page(tmp_path, "return {'id': {'name': 'ada'}['user_id']}")

    response = await build_page_navigation_response(
        request=_http_request(),
        settings=settings,
        page=page,
        renderer=StubRenderer(),
    )

    assert response.status_code == 500
    payload = json.loads(await _read_response_body(response))
    assert payload["ok"] is False
    assert payload["stage"] == "loader"
    assert "KeyError" in payload["error"]


@pytest.mark.anyio
async def test_layout_loader_crash_renders_error_boundary(
    settings: DevServerSettings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A crash in a *layout* loader is classified the same way."""
    from pyxle.devserver.registry import LayoutLoaderInfo

    layout_module = tmp_path / "server" / "layout.py"
    layout_module.parent.mkdir(parents=True, exist_ok=True)
    layout_module.write_text(
        "async def load_shell(request):\n    raise TypeError('layout blew up')\n",
        encoding="utf-8",
    )
    info = LayoutLoaderInfo(
        relative_path=Path("layout.pyxl"),
        server_module_path=layout_module,
        module_key="pyxle.server.pages.layout_crash",
        loader_name="load_shell",
    )
    monkeypatch.setattr(
        "pyxle.devserver.registry.find_layout_loaders", lambda settings, path: (info,)
    )

    renderer = StubRenderer()
    renderer.responses.append(RenderResult(html="<main>our own error page</main>"))

    response = await build_page_response(
        request=_http_request(),
        settings=settings,
        page=_page_route(tmp_path, loader_name=None),
        renderer=renderer,
        error_boundaries=_boundary_registry(tmp_path),
    )

    assert response.status_code == 500
    assert "our own error page" in (await _read_response_body(response)).decode()
    message = renderer.calls[-1][1]["error"]["message"]
    assert "Layout loader 'load_shell' in layout.pyxl" in message
    assert "TypeError: layout blew up" in message


@pytest.mark.anyio
async def test_framework_fault_does_not_run_the_error_boundary(
    settings: DevServerSettings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fault in Pyxle's own pipeline returns the fallback, not error.pyxl.

    This is the deliberate other half of the contract: application failures are
    classified where they happen and reach the boundary; anything left is a
    framework fault, and running more application code to handle it can
    compound the failure.
    """
    monkeypatch.setattr(
        ssr_view,
        "build_document_shell",
        MagicMock(side_effect=ValueError("document assembly is broken")),
    )
    renderer = StubRenderer()
    overlay = StubOverlay()

    response = await build_page_response(
        request=_http_request(),
        settings=settings,
        page=_page_route(tmp_path, loader_name=None),
        renderer=renderer,
        overlay=overlay,
        error_boundaries=_boundary_registry(tmp_path),
    )

    assert response.status_code == 500
    # Only the page render happened — the boundary component was never rendered.
    assert [call[0] for call in renderer.calls] == [
        _page_route(tmp_path, loader_name=None).client_module_path
    ]
    body = (await _read_response_body(response)).decode()
    assert "Server Render Failed" in body
    # Reported as a server-stage fault, not a loader or renderer one.
    assert overlay.events[-1][2][1]["status"] == "unknown"


@pytest.mark.anyio
async def test_navigation_framework_fault_reports_server_stage(
    settings: DevServerSettings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The JSON navigation path has the same framework-fault guardrail."""
    monkeypatch.setattr(
        ssr_view,
        "_resolve_nav_cache_ttl",
        MagicMock(side_effect=ValueError("cache lookup is broken")),
    )

    response = await build_page_navigation_response(
        request=_http_request(),
        settings=settings,
        page=_page_route(tmp_path, loader_name=None),
        renderer=StubRenderer(),
    )

    assert response.status_code == 500
    payload = json.loads(await _read_response_body(response))
    assert payload["ok"] is False
    assert payload["stage"] == "server"


# ---------------------------------------------------------------------------
# Browser-global render errors — enrichment, prod hygiene, regression
# ---------------------------------------------------------------------------


def _failing_renderer(message: str) -> StubRenderer:
    """A renderer whose render always fails with *message*."""

    class _Failing(StubRenderer):
        async def render(
            self,
            component_path: Path,
            props: dict[str, object],
            *,
            request_pathname: str | None = None,
            csrf_token: str | None = None,
        ) -> RenderResult:  # type: ignore[override]
            raise ComponentRenderError(message)

    return _Failing()


def _plain_request() -> Request:
    return Request(
        {"type": "http", "http_version": "1.1", "method": "GET", "path": "/", "root_path": "", "headers": []}
    )


def test_enrich_render_error_upgrades_browser_global(tmp_path: Path) -> None:
    """A browser-global ReferenceError becomes a BrowserGlobalRenderError."""
    page = _page_route(tmp_path, loader_name=None)
    original = ComponentRenderError("document is not defined")

    enriched = ssr_view._enrich_render_error(original, page)

    assert isinstance(enriched, BrowserGlobalRenderError)
    assert enriched.global_name == "document"
    assert enriched.source_file == "pages/index.pyxl"
    assert enriched.original_message == "document is not defined"
    assert enriched.__cause__ is original
    # Already-enriched errors pass through untouched (no double wrapping).
    assert ssr_view._enrich_render_error(enriched, page) is enriched


def test_enrich_render_error_leaves_unrelated_errors_untouched(tmp_path: Path) -> None:
    """Non-browser-global render errors flow through as the same object."""
    page = _page_route(tmp_path, loader_name=None)
    for message in ("render boom", "someHelper is not defined"):
        original = ComponentRenderError(message)
        assert ssr_view._enrich_render_error(original, page) is original


@pytest.mark.anyio
async def test_build_page_response_enriches_browser_global_error_in_dev(
    settings: DevServerSettings, tmp_path: Path
) -> None:
    """Dev error page + overlay explain the browser-global failure and the fix."""
    page = _page_route(tmp_path, loader_name=None)
    renderer = _failing_renderer("window is not defined")
    overlay = StubOverlay()

    response = await build_page_response(
        request=_plain_request(),
        settings=settings,
        page=page,
        renderer=renderer,
        overlay=overlay,
    )

    body = (await _read_response_body(response)).decode()
    assert response.status_code == 500
    assert "window is not defined" in body
    assert "pages/index.pyxl" in body
    assert "browser global" in body
    assert "useEffect" in body
    assert "BrowserGlobalRenderError" in body
    # The overlay's renderer breadcrumb carries the enriched detail too.
    error_events = [event for event in overlay.events if event[0] == "error"]
    assert error_events
    renderer_breadcrumb = error_events[0][2][1]
    assert renderer_breadcrumb["status"] == "failed"
    assert "pages/index.pyxl" in renderer_breadcrumb["detail"]
    assert "useEffect" in renderer_breadcrumb["detail"]


@pytest.mark.anyio
async def test_build_page_response_browser_global_error_is_generic_in_production(
    settings: DevServerSettings,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Production body stays generic; the enriched message goes to the log only."""
    prod_settings = replace(settings, debug=False)
    page = _page_route(tmp_path, loader_name=None)
    renderer = _failing_renderer("window is not defined")

    with caplog.at_level(logging.ERROR, logger="pyxle.ssr.view"):
        response = await build_page_response(
            request=_plain_request(),
            settings=prod_settings,
            page=page,
            renderer=renderer,
        )

    body = (await _read_response_body(response)).decode()
    assert response.status_code == 500
    assert "Server Error" in body
    # No internals in the HTTP body (security rule 18): no global name, no
    # source file, no remedy text, no exception type.
    assert "window" not in body
    assert "index.pyxl" not in body
    assert "pages/" not in body
    assert "useEffect" not in body
    assert "BrowserGlobalRenderError" not in body
    # The enriched explanation is preserved server-side for the operator.
    assert "pages/index.pyxl" in caplog.text
    assert "browser global" in caplog.text
    assert "useEffect" in caplog.text


@pytest.mark.anyio
async def test_build_page_response_unrelated_reference_error_not_rewritten(
    settings: DevServerSettings, tmp_path: Path
) -> None:
    """A ReferenceError on a non-browser name renders exactly as before."""
    page = _page_route(tmp_path, loader_name=None)
    renderer = _failing_renderer("someHelper is not defined")
    overlay = StubOverlay()

    response = await build_page_response(
        request=_plain_request(),
        settings=settings,
        page=page,
        renderer=renderer,
        overlay=overlay,
    )

    body = (await _read_response_body(response)).decode()
    assert response.status_code == 500
    assert "someHelper is not defined" in body
    assert "ComponentRenderError" in body
    assert "browser global" not in body
    assert "useEffect" not in body
    assert "BrowserGlobalRenderError" not in body


@pytest.mark.anyio
async def test_build_page_navigation_response_enriches_browser_global_error(
    settings: DevServerSettings, tmp_path: Path
) -> None:
    """The dev nav-error JSON carries the enriched message and error type."""
    page = _page_route(tmp_path, loader_name=None)
    renderer = _failing_renderer("document is not defined")

    response = await build_page_navigation_response(
        request=_plain_request(),
        settings=settings,
        page=page,
        renderer=renderer,
        overlay=StubOverlay(),
    )

    payload = json.loads(await _read_response_body(response))
    assert response.status_code == 500
    assert payload["ok"] is False
    assert payload["stage"] == "renderer"
    assert payload["errorType"] == "BrowserGlobalRenderError"
    assert "pages/index.pyxl" in payload["error"]
    assert "useEffect" in payload["error"]


@pytest.mark.anyio
async def test_build_page_navigation_response_browser_global_error_generic_in_production(
    settings: DevServerSettings, tmp_path: Path
) -> None:
    """The production nav-error JSON leaks neither the global nor the file.

    The expected string changed when the nav path started building its payload
    with ``_build_error_context``, the same function the HTML boundary uses.
    This path had its own generic wording; the shared one is what the
    error-handling guide documents as the production value. What this test is
    actually for — that nothing internal escapes — is unchanged, and still
    asserted below.
    """
    prod_settings = replace(settings, debug=False)
    page = _page_route(tmp_path, loader_name=None)
    renderer = _failing_renderer("localStorage is not defined")

    response = await build_page_navigation_response(
        request=_plain_request(),
        settings=prod_settings,
        page=page,
        renderer=renderer,
        overlay=StubOverlay(),
    )

    raw = (await _read_response_body(response)).decode()
    payload = json.loads(raw)
    assert response.status_code == 500
    assert payload["errorType"] == "ServerError"
    assert payload["error"] == "An unexpected error occurred."
    assert "localStorage" not in raw
    assert "pyxl" not in raw
    assert "useEffect" not in raw


@pytest.mark.anyio
async def test_build_streaming_page_response_enriches_browser_global_shell_error(
    settings: DevServerSettings, tmp_path: Path
) -> None:
    """A browser-global shell error on the streaming path gets the same enrichment."""
    page = replace(_page_route(tmp_path, loader_name=None), uses_suspense=True)
    renderer = StubRenderer()
    overlay = StubOverlay()

    response = await build_streaming_page_response(
        request=_plain_request(),
        settings=settings,
        page=page,
        renderer=renderer,
        stream_render=_stream_of({"type": "error", "error": "matchMedia is not defined"}),
        overlay=overlay,
    )

    body = (await _read_response_body(response)).decode()
    assert response.status_code == 500
    assert "matchMedia is not defined" in body
    assert "pages/index.pyxl" in body
    assert "useEffect" in body


DYNAMIC_REQUIRE_MSG = 'Dynamic require of "react" is not supported'


def test_enrich_render_error_upgrades_dynamic_require(tmp_path: Path) -> None:
    """A "Dynamic require of X" error becomes a CjsDependencyRenderError."""
    page = _page_route(tmp_path, loader_name=None)
    original = ComponentRenderError(DYNAMIC_REQUIRE_MSG)

    enriched = ssr_view._enrich_render_error(original, page)

    assert isinstance(enriched, CjsDependencyRenderError)
    assert enriched.module_name == "react"
    assert enriched.source_file == "pages/index.pyxl"
    assert enriched.__cause__ is original
    # Already-enriched errors pass through untouched.
    assert ssr_view._enrich_render_error(enriched, page) is enriched


@pytest.mark.anyio
async def test_build_page_response_enriches_cjs_require_error_in_dev(
    settings: DevServerSettings, tmp_path: Path
) -> None:
    page = _page_route(tmp_path, loader_name=None)
    renderer = _failing_renderer(DYNAMIC_REQUIRE_MSG)
    overlay = StubOverlay()

    response = await build_page_response(
        request=_plain_request(),
        settings=settings,
        page=page,
        renderer=renderer,
        overlay=overlay,
    )

    body = (await _read_response_body(response)).decode()
    assert response.status_code == 500
    assert "pages/index.pyxl" in body
    # 'require(...)' is HTML-escaped in the dev page; assert on plain-text spans.
    assert "CommonJS dependency" in body
    assert "ES module" in body
    assert "CjsDependencyRenderError" in body


@pytest.mark.anyio
async def test_build_page_response_cjs_require_error_is_generic_in_production(
    settings: DevServerSettings, tmp_path: Path, caplog
) -> None:
    prod_settings = replace(settings, debug=False)
    page = _page_route(tmp_path, loader_name=None)
    renderer = _failing_renderer(DYNAMIC_REQUIRE_MSG)

    with caplog.at_level("ERROR"):
        response = await build_page_response(
            request=_plain_request(),
            settings=prod_settings,
            page=page,
            renderer=renderer,
            overlay=None,
        )

    body = (await _read_response_body(response)).decode()
    assert response.status_code == 500
    assert "Server Error" in body
    assert "react" not in body
    assert "pages/" not in body
    assert "CjsDependencyRenderError" not in body
    # The enriched detail reaches the server log only.
    assert "require('react')" in caplog.text


# --------------------------------------------------------------------------- #
# Error boundary + ancestor layout loaders (F6 regression)                     #
# --------------------------------------------------------------------------- #


def _write_layout_with_loader(settings) -> None:
    (settings.pages_dir / "layout.pyxl").write_text(
        "from pyxle.runtime import server\n"
        "\n"
        "@server\n"
        "async def load_layout(request):\n"
        "    return {'site': 'Pyxle Blog'}\n"
        "\n"
        "import React from 'react';\n"
        "\n"
        "export default function Layout({ children, data }) {\n"
        "    return <div>{data.site}{children}</div>;\n"
        "}\n",
        encoding="utf-8",
    )


@pytest.mark.anyio
async def test_error_boundary_receives_layout_loader_data(
    settings: DevServerSettings, tmp_path: Path
) -> None:
    """The error boundary render includes ancestor layout loader data.

    The compiled error.pyxl is wrapped in the layout chain like any page; a
    layout with a @server loader used to get NO layoutData during the boundary
    render, crash on the missing props, and silently fall back to the default
    error document (in dev AND prod)."""
    _write_layout_with_loader(settings)
    (settings.pages_dir / "error.pyxl").write_text(
        "import React from 'react';\n"
        "export default function ErrorPage({ error }) {\n"
        "    return <h1>{error.message}</h1>;\n"
        "}\n",
        encoding="utf-8",
    )
    (settings.pages_dir / "index.pyxl").write_text(
        "from pyxle.runtime import server\n"
        "\n"
        "@server\n"
        "async def load(request):\n"
        "    raise LoaderError('Teapot', status_code=418)\n"
        "\n"
        "import React from 'react';\n"
        "export default function Home({ data }) {\n"
        "    return <div>home</div>;\n"
        "}\n",
        encoding="utf-8",
    )

    from pyxle.devserver.builder import build_once
    from pyxle.devserver.error_pages import build_error_boundary_registry
    from pyxle.devserver.registry import load_metadata_registry
    from pyxle.devserver.routes import build_route_table

    build_once(settings)
    registry = load_metadata_registry(settings)
    routes = build_route_table(registry)
    page = routes.find_page("/")
    assert page is not None
    boundaries = build_error_boundary_registry(list(routes.error_boundary_pages))

    renderer = StubRenderer()
    renderer.responses.append(RenderResult(html="<h1>Teapot</h1>"))
    request = Request({"type": "http", "method": "GET", "path": "/", "query_string": b"", "headers": []})

    response = await build_page_response(
        request=request,
        settings=settings,
        page=page,
        renderer=renderer,
        error_boundaries=boundaries,
    )

    assert response.status_code == 418
    body = (await _read_response_body(response)).decode()
    assert "<h1>Teapot</h1>" in body  # the BOUNDARY rendered, not the default doc
    boundary_props = renderer.calls[-1][1]
    assert boundary_props["error"]["message"] == "Teapot"
    assert boundary_props["layoutData"] == {"site": "Pyxle Blog"}


@pytest.mark.anyio
async def test_error_boundary_survives_failing_layout_loader(
    settings: DevServerSettings, tmp_path: Path, caplog
) -> None:
    """A layout loader that raises during the boundary render must not mask
    the boundary: it renders with error-only props and the failure is logged."""
    (settings.pages_dir / "layout.pyxl").write_text(
        "from pyxle.runtime import server\n"
        "\n"
        "@server\n"
        "async def load_layout(request):\n"
        "    raise RuntimeError('layout db down')\n"
        "\n"
        "import React from 'react';\n"
        "export default function Layout({ children }) {\n"
        "    return <div>{children}</div>;\n"
        "}\n",
        encoding="utf-8",
    )
    (settings.pages_dir / "error.pyxl").write_text(
        "import React from 'react';\n"
        "export default function ErrorPage({ error }) {\n"
        "    return <h1>{error.message}</h1>;\n"
        "}\n",
        encoding="utf-8",
    )
    (settings.pages_dir / "index.pyxl").write_text(
        "from pyxle.runtime import server\n"
        "\n"
        "@server\n"
        "async def load(request):\n"
        "    raise LoaderError('nope', status_code=500)\n"
        "\n"
        "import React from 'react';\n"
        "export default function Home({ data }) {\n"
        "    return <div>home</div>;\n"
        "}\n",
        encoding="utf-8",
    )

    from pyxle.devserver.builder import build_once
    from pyxle.devserver.error_pages import build_error_boundary_registry
    from pyxle.devserver.registry import load_metadata_registry
    from pyxle.devserver.routes import build_route_table

    build_once(settings)
    registry = load_metadata_registry(settings)
    routes = build_route_table(registry)
    page = routes.find_page("/")
    assert page is not None
    boundaries = build_error_boundary_registry(list(routes.error_boundary_pages))

    renderer = StubRenderer()
    renderer.responses.append(RenderResult(html="<h1>nope</h1>"))
    request = Request({"type": "http", "method": "GET", "path": "/", "query_string": b"", "headers": []})

    with caplog.at_level("WARNING"):
        response = await build_page_response(
            request=request,
            settings=settings,
            page=page,
            renderer=renderer,
            error_boundaries=boundaries,
        )

    assert response.status_code == 500
    body = (await _read_response_body(response)).decode()
    assert "<h1>nope</h1>" in body
    boundary_props = renderer.calls[-1][1]
    assert "layoutData" not in boundary_props
    assert "Layout loader failed while rendering error boundary" in caplog.text


@pytest.mark.anyio
async def test_error_boundary_failure_is_logged(
    settings: DevServerSettings, tmp_path: Path, caplog
) -> None:
    """A boundary that itself fails falls back to the default error document —
    and says so in the log instead of swapping silently."""
    page = _page_route(tmp_path, loader_name=None)
    boundary = _boundary_page(tmp_path, filename="error.pyxl", module_key="pyxle.server.pages.errlog")
    from pyxle.devserver.error_pages import ErrorBoundaryRegistry

    registry = ErrorBoundaryRegistry(error_pages={".": boundary}, not_found_pages={})

    renderer = _failing_renderer("render boom")

    with caplog.at_level("WARNING"):
        response = await build_page_response(
            request=_plain_request(),
            settings=settings,
            page=page,
            renderer=renderer,
            error_boundaries=registry,
        )

    assert response.status_code == 500
    assert "failed to render; serving the default error document" in caplog.text


@pytest.mark.anyio
async def test_error_boundary_head_merges_every_source(
    settings: DevServerSettings, tmp_path: Path
) -> None:
    """A page rendered through an error boundary gets a fully merged head.

    The boundary used to take its ``HEAD`` variable verbatim and nothing else,
    so the error page — the page a confused visitor is most likely to see —
    was served without the site's stylesheet, favicon, description or title,
    and without its own ``<Head>``. It now goes through the same merge every
    other page uses, over all four sources: the layout's two channels
    (``<Head>`` JSX and its already-evaluated ``HEAD`` variable), the
    boundary's own ``HEAD`` variable, and the ``<Head>`` elements the render
    produced.
    """
    (settings.pages_dir / "layout.pyxl").write_text(
        "HEAD = [\n"
        "    '<link rel=\"stylesheet\" href=\"/site.css\" />',\n"
        "    '<style>body{margin:0}</style>',\n"
        "]\n"
        "\n"
        "import React from 'react';\n"
        "import { Head } from 'pyxle/client';\n"
        "export default function Layout({ children }) {\n"
        "    return (<><Head><title>Site</title>"
        "<link rel=\"icon\" href=\"/favicon.svg\" /></Head>{children}</>);\n"
        "}\n",
        encoding="utf-8",
    )
    (settings.pages_dir / "error.pyxl").write_text(
        "HEAD = ['<meta name=\"robots\" content=\"noindex\" />']\n"
        "\n"
        "import React from 'react';\n"
        "export default function ErrorPage({ error }) {\n"
        "    return <h1>{error.message}</h1>;\n"
        "}\n",
        encoding="utf-8",
    )
    (settings.pages_dir / "index.pyxl").write_text(
        "from pyxle.runtime import server\n"
        "\n"
        "@server\n"
        "async def load(request):\n"
        "    raise LoaderError('gone', status_code=410)\n"
        "\n"
        "import React from 'react';\n"
        "export default function Home({ data }) {\n"
        "    return <div>home</div>;\n"
        "}\n",
        encoding="utf-8",
    )

    from pyxle.devserver.builder import build_once
    from pyxle.devserver.error_pages import build_error_boundary_registry
    from pyxle.devserver.registry import load_metadata_registry
    from pyxle.devserver.routes import build_route_table

    build_once(settings)
    registry = load_metadata_registry(settings)
    routes = build_route_table(registry)
    page = routes.find_page("/")
    assert page is not None
    boundaries = build_error_boundary_registry(list(routes.error_boundary_pages))

    renderer = StubRenderer()
    renderer.responses.append(
        RenderResult(
            html="<h1>gone</h1>",
            head_elements=("<title>Something went wrong</title>",),
        )
    )
    request = Request(
        {"type": "http", "method": "GET", "path": "/", "query_string": b"", "headers": []}
    )

    response = await build_page_response(
        request=request,
        settings=settings,
        page=page,
        renderer=renderer,
        error_boundaries=boundaries,
    )

    assert response.status_code == 410
    body = (await _read_response_body(response)).decode()
    assert "<h1>gone</h1>" in body  # the boundary rendered, not the default doc
    # The layout's <Head> JSX channel.
    assert '<link rel="icon" href="/favicon.svg"' in body
    # The layout's HEAD variable channel — brace-heavy content that the JSX
    # expression filter would drop if the two channels were merged into one.
    assert '<link rel="stylesheet" href="/site.css"' in body
    assert "<style>body{margin:0}</style>" in body
    # The boundary's own HEAD variable.
    assert '<meta name="robots" content="noindex"' in body
    # The render's <Head> wins the title over the layout's.
    assert "<title>Something went wrong</title>" in body
    assert "<title>Site</title>" not in body
    # The framework's placeholder title only appears when there is no title.
    assert "<title>Pyxle</title>" not in body


@pytest.mark.anyio
async def test_error_boundary_survives_failing_head_evaluation(
    settings: DevServerSettings, tmp_path: Path, caplog
) -> None:
    """A boundary ``HEAD`` callable that raises costs the head, not the page.

    A callable ``HEAD`` is handed the error context rather than loader data —
    a shape it has never run against — so it can raise exactly here. The
    boundary must still reach the visitor.
    """
    (settings.pages_dir / "error.pyxl").write_text(
        "def HEAD(data):\n"
        "    raise RuntimeError('head boom')\n"
        "\n"
        "import React from 'react';\n"
        "export default function ErrorPage({ error }) {\n"
        "    return <h1>{error.message}</h1>;\n"
        "}\n",
        encoding="utf-8",
    )
    (settings.pages_dir / "index.pyxl").write_text(
        "from pyxle.runtime import server\n"
        "\n"
        "@server\n"
        "async def load(request):\n"
        "    raise LoaderError('nope', status_code=500)\n"
        "\n"
        "import React from 'react';\n"
        "export default function Home({ data }) {\n"
        "    return <div>home</div>;\n"
        "}\n",
        encoding="utf-8",
    )

    from pyxle.devserver.builder import build_once
    from pyxle.devserver.error_pages import build_error_boundary_registry
    from pyxle.devserver.registry import load_metadata_registry
    from pyxle.devserver.routes import build_route_table

    build_once(settings)
    registry = load_metadata_registry(settings)
    routes = build_route_table(registry)
    page = routes.find_page("/")
    assert page is not None
    boundaries = build_error_boundary_registry(list(routes.error_boundary_pages))

    renderer = StubRenderer()
    renderer.responses.append(RenderResult(html="<h1>nope</h1>"))
    request = Request(
        {"type": "http", "method": "GET", "path": "/", "query_string": b"", "headers": []}
    )

    with caplog.at_level("WARNING"):
        response = await build_page_response(
            request=request,
            settings=settings,
            page=page,
            renderer=renderer,
            error_boundaries=boundaries,
        )

    assert response.status_code == 500
    body = (await _read_response_body(response)).decode()
    assert "<h1>nope</h1>" in body
    assert "HEAD evaluation failed for error boundary error.pyxl" in caplog.text
