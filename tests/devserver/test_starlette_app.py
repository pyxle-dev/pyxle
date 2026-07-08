from __future__ import annotations

import json
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from starlette.applications import Starlette
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse
from starlette.staticfiles import StaticFiles
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from pyxle.cli.logger import ConsoleLogger
from pyxle.devserver.builder import build_once
from pyxle.devserver.registry import load_metadata_registry
from pyxle.devserver.routes import build_route_table
from pyxle.devserver.settings import DevServerSettings
from pyxle.devserver.starlette_app import (
    ApiRouteError,
    PageRouteError,
    build_api_router,
    build_page_router,
    build_static_files_mount,
    create_starlette_app,
)


@pytest.fixture
def project(tmp_path: Path) -> DevServerSettings:
    root = tmp_path / "project"
    (root / "pages").mkdir(parents=True)
    (root / "public").mkdir()
    settings = DevServerSettings.from_project_root(root)

    write_file(
        settings.pages_dir / "api/pulse.py",
        """from starlette.responses import JSONResponse\n\nasync def endpoint(request):\n    name = request.query_params.get(\"name\", \"World\")\n    return JSONResponse({\"message\": f\"Hello, {name}!\"})\n""",
    )

    write_file(
        settings.pages_dir / "api/posts/[id].py",
        """from starlette.endpoints import HTTPEndpoint\nfrom starlette.responses import JSONResponse\n\nclass PostEndpoint(HTTPEndpoint):\n    async def get(self, request):\n        return JSONResponse({\"id\": request.path_params[\"id\"]})\n""",
    )

    write_file(
        settings.pages_dir / "index.pyxl",
        """

@server
async def load_home(request):
    return {"message": "hi"}

# --- JavaScript/PSX (Client + Server) ---

import React from 'react';

export default function Home({ data }) {
    return <div>{data.message}</div>;
}
""",
    )

    write_file(
        settings.pages_dir / "posts/[id].pyxl",
        """import React from 'react';

export default function Post({ data }) {
    return <article>{data.title}</article>;
}
""",
    )

    return settings


def write_file(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_build_api_router_registers_function_and_class(project: DevServerSettings) -> None:
    build_once(project)
    registry = load_metadata_registry(project)
    table = build_route_table(registry)

    router = build_api_router(table.apis)

    app = Starlette()
    app.router.routes.extend(router.routes)

    client = TestClient(app)

    response = client.get("/api/pulse", params={"name": "Alice"})
    assert response.status_code == 200
    assert response.json() == {"message": "Hello, Alice!"}

    response = client.get("/api/posts/42")
    assert response.status_code == 200
    assert response.json() == {"id": "42"}


def test_build_api_router_raises_for_invalid_module(project: DevServerSettings) -> None:
    write_file(project.pages_dir / "api/bad.py", "value = 123\n")

    build_once(project)
    registry = load_metadata_registry(project)
    table = build_route_table(registry)

    bad_route = next(route for route in table.apis if route.path == "/api/bad")

    with pytest.raises(ApiRouteError):
        build_api_router([bad_route])


def test_build_api_router_registers_websocket(project: DevServerSettings) -> None:
    """An API module that exports ``async def websocket(ws)`` is wired
    up as a :class:`WebSocketRoute`. Exists because previously Pyxle had
    no user-facing WS support — every app that wanted live updates had
    to hand-roll an ASGI middleware."""
    write_file(
        project.pages_dir / "api/echo.py",
        "async def websocket(ws):\n"
        "    await ws.accept()\n"
        "    try:\n"
        "        while True:\n"
        "            msg = await ws.receive_text()\n"
        "            await ws.send_text(f'echo:{msg}')\n"
        "    except Exception:\n"
        "        pass\n",
    )

    build_once(project)
    registry = load_metadata_registry(project)
    table = build_route_table(registry)

    router = build_api_router(table.apis)
    app = Starlette()
    app.router.routes.extend(router.routes)

    with TestClient(app) as client:
        with client.websocket_connect("/api/echo") as ws:
            ws.send_text("hello")
            assert ws.receive_text() == "echo:hello"
            ws.send_text("world")
            assert ws.receive_text() == "echo:world"


def test_build_api_router_supports_http_and_ws_in_same_module(
    project: DevServerSettings,
) -> None:
    """A module can export both ``endpoint`` and ``websocket`` to serve
    the same path over both protocols — e.g. a REST GET alongside a
    live-updates WS channel."""
    write_file(
        project.pages_dir / "api/dual.py",
        "from starlette.responses import JSONResponse\n"
        "\n"
        "async def endpoint(request):\n"
        "    return JSONResponse({'ok': True})\n"
        "\n"
        "async def websocket(ws):\n"
        "    await ws.accept()\n"
        "    await ws.send_text('ws-hello')\n"
        "    await ws.close()\n",
    )

    build_once(project)
    registry = load_metadata_registry(project)
    table = build_route_table(registry)

    router = build_api_router(table.apis)
    app = Starlette()
    app.router.routes.extend(router.routes)

    with TestClient(app) as client:
        assert client.get("/api/dual").json() == {"ok": True}
        with client.websocket_connect("/api/dual") as ws:
            assert ws.receive_text() == "ws-hello"


def test_build_page_router_registers_page_websocket(
    project: DevServerSettings, monkeypatch
) -> None:
    """A page that declares ``async def websocket(ws)`` serves a WebSocket
    route at its path, ALONGSIDE its HTTP GET — both on the same dynamic path,
    with path params resolved for the WS upgrade too."""
    write_file(
        project.pages_dir / "chat/[room].pyxl",
        "async def websocket(ws):\n"
        "    await ws.accept()\n"
        "    room = ws.path_params['room']\n"
        "    msg = await ws.receive_text()\n"
        "    await ws.send_text(f'{room}:{msg}')\n"
        "    await ws.close()\n"
        "\n"
        "import React from 'react';\n"
        "export default function Chat() { return <div>chat</div>; }\n",
    )

    build_once(project)
    registry = load_metadata_registry(project)
    table = build_route_table(registry)

    async def fake_build_page_response(*, request, settings, page, renderer, overlay=None, **_kw):
        return PlainTextResponse(f"SSR:{page.path}")

    monkeypatch.setattr(
        "pyxle.devserver.starlette_app.build_page_response",
        fake_build_page_response,
    )

    router = build_page_router(table.pages, settings=project, renderer=object())
    app = Starlette()
    app.router.routes.extend(router.routes)

    with TestClient(app) as client:
        # The HTTP GET still renders on the same path…
        assert client.get("/chat/lobby").text == "SSR:/chat/{room}"
        # …and a WS upgrade to the same path resolves the [room] param.
        with client.websocket_connect("/chat/lobby") as ws:
            ws.send_text("hi")
            assert ws.receive_text() == "lobby:hi"


def test_page_without_websocket_has_no_ws_route(project: DevServerSettings) -> None:
    """A page with no ``websocket`` handler exposes only its HTTP route — a WS
    upgrade to its path is rejected."""
    build_once(project)  # index.pyxl declares only a loader
    registry = load_metadata_registry(project)
    table = build_route_table(registry)

    router = build_page_router(table.pages, settings=project, renderer=object())
    app = Starlette()
    app.router.routes.extend(router.routes)

    with TestClient(app) as client:
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect("/"):
                pass


def test_page_websocket_stale_metadata_raises(project: DevServerSettings) -> None:
    """Metadata that names a websocket handler the server module doesn't expose
    (a stale build) fails loudly at router build, not with a 500 on connect."""
    build_once(project)
    registry = load_metadata_registry(project)
    table = build_route_table(registry)
    index = next(page for page in table.pages if page.path == "/")
    broken = replace(index, websocket_name="not_a_real_handler")

    with pytest.raises(PageRouteError, match="no such callable"):
        build_page_router([broken], settings=project, renderer=object())


def test_build_page_router_invokes_build_page_response(project: DevServerSettings, monkeypatch) -> None:
    build_once(project)
    registry = load_metadata_registry(project)
    table = build_route_table(registry)

    captured: list[str] = []

    async def fake_build_page_response(*, request, settings, page, renderer, overlay=None, **_kw):
        captured.append((page.path, overlay))
        return PlainTextResponse(f"SSR:{page.path}")

    monkeypatch.setattr(
        "pyxle.devserver.starlette_app.build_page_response",
        fake_build_page_response,
    )

    router = build_page_router(
        table.pages,
        settings=project,
        renderer=object(),  # type: ignore[arg-type]
    )

    app = Starlette()
    app.router.routes.extend(router.routes)

    client = TestClient(app)

    response = client.get("/")
    assert response.status_code == 200
    assert response.text == "SSR:/"

    dynamic_response = client.get("/posts/123")
    assert dynamic_response.status_code == 200
    assert dynamic_response.text == "SSR:/posts/{id}"
    assert captured == [("/", None), ("/posts/{id}", None)]


def test_page_handler_sets_vary_and_cache_control_headers(
    project: DevServerSettings, monkeypatch
) -> None:
    """Page handlers set ``Vary: x-pyxle-navigation`` on both HTML
    and JSON responses so the browser's HTTP cache stores them as
    separate entries for the same URL. Without this, a browser that
    served cached navigation JSON during a tab-restore would show
    raw JSON to the user instead of the HTML page.

    HTML responses also get ``Cache-Control: private, no-cache``.
    JSON nav responses get ``Cache-Control: no-store``."""
    build_once(project)
    registry = load_metadata_registry(project)
    table = build_route_table(registry)

    async def fake_html(*, request, settings, page, renderer, overlay=None, **_kw):
        return PlainTextResponse(f"HTML:{page.path}")

    async def fake_json(*, request, settings, page, renderer, overlay=None, **_kw):
        from starlette.responses import JSONResponse

        return JSONResponse({"ok": True, "routePath": page.path})

    monkeypatch.setattr(
        "pyxle.devserver.starlette_app.build_page_response",
        fake_html,
    )
    monkeypatch.setattr(
        "pyxle.devserver.starlette_app.build_page_navigation_response",
        fake_json,
    )

    router = build_page_router(
        table.pages, settings=project, renderer=object()  # type: ignore[arg-type]
    )
    app = Starlette()
    app.router.routes.extend(router.routes)
    client = TestClient(app)

    # HTML response (no nav header)
    html_resp = client.get("/")
    assert html_resp.status_code == 200
    assert html_resp.headers["vary"] == "x-pyxle-navigation"
    assert "private" in html_resp.headers.get("cache-control", "")
    assert "no-cache" in html_resp.headers.get("cache-control", "")

    # JSON nav response (with nav header)
    json_resp = client.get("/", headers={"x-pyxle-navigation": "1"})
    assert json_resp.status_code == 200
    assert json_resp.headers["vary"] == "x-pyxle-navigation"
    assert "no-store" in json_resp.headers.get("cache-control", "")


def test_build_static_files_mount_serves_public_directory(project: DevServerSettings) -> None:
    mount = build_static_files_mount(project)

    assert mount.path in {"", "/"}
    assert mount.name == "pyxle-public"
    assert isinstance(mount.app, StaticFiles)
    assert Path(mount.app.directory) == project.public_dir


def test_build_static_files_mount_rejects_websocket_scope(project: DevServerSettings) -> None:
    mount = build_static_files_mount(project)

    app = Starlette()
    app.router.routes.append(mount)

    client = TestClient(app)

    with pytest.raises(WebSocketDisconnect) as excinfo:
        with client.websocket_connect("/__pyxle__/overlay"):
            pass

    assert getattr(excinfo.value, "code", None) == 4404


def test_create_starlette_app_combines_routes(project: DevServerSettings, monkeypatch) -> None:
    static_file = project.public_dir / "robots.txt"
    static_file.write_text("User-agent: *\nAllow: /\n", encoding="utf-8")

    build_once(project)
    registry = load_metadata_registry(project)
    table = build_route_table(registry)

    renderer = object()

    monkeypatch.setattr(
        "pyxle.devserver.starlette_app.ComponentRenderer",
        lambda: renderer,
    )

    async def fake_build_page_response(*, request, settings, page, renderer, overlay=None, **_kw):
        assert overlay is not None
        return HTMLResponse(f"<div>{page.path}</div>")

    monkeypatch.setattr(
        "pyxle.devserver.starlette_app.build_page_response",
        fake_build_page_response,
    )

    app = create_starlette_app(project, table)
    client = TestClient(app)

    response = client.get("/api/pulse")
    assert response.status_code == 200
    assert response.json()["message"] == "Hello, World!"

    page_response = client.get("/posts/5")
    assert page_response.status_code == 200
    assert "<div>/posts/{id}</div>" in page_response.text

    asset_response = client.get("/robots.txt")
    assert asset_response.status_code == 200
    assert "User-agent" in asset_response.text

    assert app.state.ssr_renderer is renderer
    assert app.state.overlay is not None

    with client.websocket_connect("/__pyxle__/overlay") as websocket:
        websocket.close()


def test_static_assets_middleware_handles_catchall_routes(project: DevServerSettings, monkeypatch, tmp_path: Path) -> None:
    write_file(
        project.pages_dir / "[...slug].pyxl",
        """
import React from 'react';

export default function Fallback() {
    return <div>fallback</div>;
}
""",
    )

    public_styles = project.public_dir / "styles"
    public_styles.mkdir(parents=True, exist_ok=True)
    (public_styles / "site.css").write_text("body { color: red; }", encoding="utf-8")

    client_assets = tmp_path / "dist-client"
    (client_assets / "assets").mkdir(parents=True)
    (client_assets / "assets" / "bundle.js").write_text("console.log('hi')", encoding="utf-8")

    build_once(project)
    registry = load_metadata_registry(project)
    table = build_route_table(registry)

    async def fake_build_page_response(*_, **__):  # pragma: no cover - deterministic HTML
        return HTMLResponse("<div>page</div>")

    monkeypatch.setattr(
        "pyxle.devserver.starlette_app.build_page_response",
        fake_build_page_response,
    )

    prod_settings = replace(project, debug=False, page_manifest={})

    app = create_starlette_app(
        prod_settings,
        table,
        serve_static=True,
        client_static_dir=client_assets,
    )

    client = TestClient(app)

    css_response = client.get("/styles/site.css")
    assert css_response.status_code == 200
    assert "color: red" in css_response.text

    bundle_response = client.get("/client/assets/bundle.js")
    assert bundle_response.status_code == 200
    assert "console.log" in bundle_response.text

    fallback_response = client.get("/unknown/path")
    assert fallback_response.status_code == 200
    assert "page" in fallback_response.text


def test_create_starlette_app_uses_vite_proxy(project: DevServerSettings, monkeypatch) -> None:
    build_once(project)
    registry = load_metadata_registry(project)
    table = build_route_table(registry)

    renderer = object()

    monkeypatch.setattr(
        "pyxle.devserver.starlette_app.ComponentRenderer",
        lambda: renderer,
    )

    async def fake_build_page_response(*, request, settings, page, renderer, overlay=None, **_kw):
        assert overlay is not None
        return PlainTextResponse("page")

    monkeypatch.setattr(
        "pyxle.devserver.starlette_app.build_page_response",
        fake_build_page_response,
    )

    app = create_starlette_app(project, table)
    proxy = app.state.vite_proxy

    captured: list[str] = []
    shutdown_flag: list[bool] = []

    async def fake_handle(request):
        captured.append(request.url.path)
        return PlainTextResponse("ok")

    async def fake_close():
        shutdown_flag.append(True)

    proxy.handle = fake_handle  # type: ignore[assignment]
    proxy.should_proxy = lambda request: request.url.path.startswith("/@vite")  # type: ignore[assignment]
    proxy.close = fake_close  # type: ignore[assignment]

    with TestClient(app) as client:
        response = client.get("/@vite/client")
        assert response.status_code == 200
        assert response.text == "ok"

    assert captured == ["/@vite/client"]
    assert shutdown_flag == [True]


def test_create_starlette_app_serves_client_assets_in_production(
    project: DevServerSettings,
    monkeypatch,
) -> None:
    build_once(project)
    registry = load_metadata_registry(project)
    table = build_route_table(registry)

    renderer = object()
    monkeypatch.setattr(
        "pyxle.devserver.starlette_app.ComponentRenderer",
        lambda: renderer,
    )

    dist_root = project.project_root / "dist"
    client_dir = dist_root / "client"
    public_dir = dist_root / "public"
    client_dir.mkdir(parents=True, exist_ok=True)
    public_dir.mkdir(parents=True, exist_ok=True)
    (client_dir / "assets").mkdir(exist_ok=True)
    (client_dir / "assets" / "bundle.js").write_text("console.log('prod');", encoding="utf-8")
    (public_dir / "robots.txt").write_text("Prod robots", encoding="utf-8")

    prod_settings = replace(project, debug=False, page_manifest={})

    app = create_starlette_app(
        prod_settings,
        table,
        public_static_dir=public_dir,
        client_static_dir=client_dir,
    )

    assert getattr(app.state, "vite_proxy", None) is None
    assert getattr(app.state, "overlay", None) is None

    with TestClient(app) as client:
        asset = client.get("/client/assets/bundle.js")
        assert asset.status_code == 200
        assert "prod" in asset.text

        robots = client.get("/robots.txt")
        assert robots.status_code == 200
        assert "Prod robots" in robots.text


def test_create_starlette_app_can_disable_static_mounts(
    project: DevServerSettings,
    monkeypatch,
) -> None:
    build_once(project)
    registry = load_metadata_registry(project)
    table = build_route_table(registry)

    renderer = object()
    monkeypatch.setattr(
        "pyxle.devserver.starlette_app.ComponentRenderer",
        lambda: renderer,
    )

    async def fake_build_page_response(*, request, settings, page, renderer, overlay=None, **_kw):
        return PlainTextResponse("page")

    monkeypatch.setattr(
        "pyxle.devserver.starlette_app.build_page_response",
        fake_build_page_response,
    )

    (project.public_dir / "robots.txt").write_text("ok", encoding="utf-8")

    app = create_starlette_app(project, table, serve_static=False)
    client = TestClient(app)

    response = client.get("/robots.txt")
    assert response.status_code == 404

def test_health_endpoints_reflect_readiness(project: DevServerSettings, monkeypatch) -> None:
    build_once(project)
    registry = load_metadata_registry(project)
    table = build_route_table(registry)

    renderer = object()

    monkeypatch.setattr(
        "pyxle.devserver.starlette_app.ComponentRenderer",
        lambda: renderer,
    )

    async def fake_build_page_response(*, request, settings, page, renderer, overlay=None, **_kw):
        assert overlay is not None
        return PlainTextResponse("page")

    monkeypatch.setattr(
        "pyxle.devserver.starlette_app.build_page_response",
        fake_build_page_response,
    )

    app = create_starlette_app(project, table)
    client = TestClient(app)

    health = client.get("/healthz")
    assert health.status_code == 200
    payload = health.json()
    assert payload["status"] == "ok"
    assert payload["ready"] is False
    assert payload["uptime"] >= 0

    ready = client.get("/readyz")
    assert ready.status_code == 503
    assert ready.json()["ready"] is False

    app.state.pyxle_ready = True
    ready_after = client.get("/readyz")
    assert ready_after.status_code == 200
    assert ready_after.json()["ready"] is True


def test_create_starlette_app_applies_custom_middleware(project: DevServerSettings, monkeypatch) -> None:
    build_once(project)
    registry = load_metadata_registry(project)
    table = build_route_table(registry)

    renderer = object()

    monkeypatch.setattr(
        "pyxle.devserver.starlette_app.ComponentRenderer",
        lambda: renderer,
    )

    async def fake_build_page_response(*, request, settings, page, renderer, overlay=None, **_kw):
        return PlainTextResponse("page")

    monkeypatch.setattr(
        "pyxle.devserver.starlette_app.build_page_response",
        fake_build_page_response,
    )

    custom = replace(
        project,
        custom_middlewares=("tests.devserver.sample_middlewares:HeaderCaptureMiddleware",),
    )

    app = create_starlette_app(custom, table)
    client = TestClient(app)

    response = client.get("/api/pulse", headers={"x-auth-token": "secret"})

    assert response.status_code == 200
    assert response.headers["x-auth-token"] == "secret"


def test_create_starlette_app_injects_project_root_into_sys_path(
    project: DevServerSettings,
    monkeypatch,
) -> None:
    build_once(project)
    registry = load_metadata_registry(project)
    table = build_route_table(registry)

    renderer = object()

    monkeypatch.setattr(
        "pyxle.devserver.starlette_app.ComponentRenderer",
        lambda: renderer,
    )

    async def fake_build_page_response(*, request, settings, page, renderer, overlay=None, **_kw):
        return PlainTextResponse("page")

    monkeypatch.setattr(
        "pyxle.devserver.starlette_app.build_page_response",
        fake_build_page_response,
    )

    project_root = str(project.project_root)
    sanitized_path = [entry for entry in sys.path if entry != project_root]
    monkeypatch.setattr(sys, "path", sanitized_path)

    create_starlette_app(project, table)

    assert sys.path[0] == project_root


def test_create_starlette_app_loads_page_manifest(project: DevServerSettings, monkeypatch) -> None:
    dist_dir = project.project_root / "dist"
    dist_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = dist_dir / "page-manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "/": {
                    "client": {
                        "file": "assets/index.js",
                        "imports": [],
                        "css": [],
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    build_once(project)
    registry = load_metadata_registry(project)
    table = build_route_table(registry)

    captured: dict[str, object] = {}

    async def fake_build_page_response(*, settings, **_kw):
        captured["manifest"] = settings.page_manifest
        return PlainTextResponse("ok")

    monkeypatch.setattr(
        "pyxle.devserver.starlette_app.build_page_response",
        fake_build_page_response,
    )

    prod_settings = replace(project, debug=False)
    app = create_starlette_app(prod_settings, table)
    client = TestClient(app)

    response = client.get("/")
    assert response.status_code == 200
    assert captured["manifest"] is not None
    assert captured["manifest"]["/"]["client"]["file"] == "assets/index.js"


def test_create_starlette_app_warns_when_manifest_missing(project: DevServerSettings, monkeypatch) -> None:
    build_once(project)
    registry = load_metadata_registry(project)
    table = build_route_table(registry)

    warnings: list[str] = []

    class StubLogger(ConsoleLogger):
        def warning(self, message: str) -> None:  # type: ignore[override]
            warnings.append(message)

    async def fake_build_page_response(*args, **kwargs):
        return PlainTextResponse("ok")

    monkeypatch.setattr(
        "pyxle.devserver.starlette_app.build_page_response",
        fake_build_page_response,
    )

    prod_settings = replace(project, debug=False)
    create_starlette_app(prod_settings, table, logger=StubLogger())

    assert warnings


def test_route_hooks_attach_metadata_and_custom_policies(
    project: DevServerSettings,
    monkeypatch,
) -> None:
    write_file(
        project.pages_dir / "api/hook_check.py",
        """from starlette.responses import JSONResponse\n\nasync def endpoint(request):\n    route = request.scope.get(\"pyxle\", {}).get(\"route\", {})\n    targets = getattr(request.state, \"route_targets\", [])\n    return JSONResponse({\"route\": route, \"targets\": targets})\n""",
    )

    build_once(project)
    registry = load_metadata_registry(project)
    table = build_route_table(registry)

    renderer = object()

    monkeypatch.setattr(
        "pyxle.devserver.starlette_app.ComponentRenderer",
        lambda: renderer,
    )

    async def capture_page_response(*, request, **_):
        metadata = request.scope.get("pyxle", {}).get("route")
        payload = {
            "recorded": getattr(request.state, "recorded_route", None),
            "metadata": metadata,
        }
        return JSONResponse(payload)

    monkeypatch.setattr(
        "pyxle.devserver.starlette_app.build_page_response",
        capture_page_response,
    )

    custom = replace(
        project,
        page_route_hooks=("tests.devserver.sample_middlewares:record_route_hook",),
        api_route_hooks=("tests.devserver.sample_middlewares:build_target_hook",),
    )

    app = create_starlette_app(custom, table)
    client = TestClient(app)

    page_response = client.get("/")
    assert page_response.status_code == 200
    page_json = page_response.json()
    assert page_json["recorded"] == "/"
    assert page_json["metadata"]["path"] == "/"
    assert page_json["metadata"]["target"] == "page"

    api_response = client.get("/api/hook_check")
    assert api_response.status_code == 200
    assert api_response.json()["targets"] == ["api"]


def test_dev_mode_adds_vite_cors_origin_automatically(
    project: DevServerSettings,
    monkeypatch,
) -> None:
    """In debug mode the Vite dev server origin should be allowed even without
    explicit CORS configuration, so that HMR and asset requests succeed."""
    build_once(project)
    registry = load_metadata_registry(project)
    table = build_route_table(registry)

    monkeypatch.setattr(
        "pyxle.devserver.starlette_app.ComponentRenderer",
        lambda: object(),
    )

    async def fake_build_page_response(*, request, settings, page, renderer, overlay=None, **_kw):
        return PlainTextResponse("page")

    monkeypatch.setattr(
        "pyxle.devserver.starlette_app.build_page_response",
        fake_build_page_response,
    )

    # debug=True is the default from the fixture
    assert project.debug is True
    # Default vite_host is 127.0.0.1
    assert project.vite_host == "127.0.0.1"

    app = create_starlette_app(project, table)
    client = TestClient(app)

    vite_port = project.vite_port

    # 127.0.0.1 origin should be allowed
    response = client.get(
        "/api/pulse",
        headers={"Origin": f"http://127.0.0.1:{vite_port}"},
    )
    assert response.status_code == 200
    assert response.headers.get("access-control-allow-origin") == f"http://127.0.0.1:{vite_port}"

    # localhost should also be allowed (browsers treat them as different origins)
    response = client.get(
        "/api/pulse",
        headers={"Origin": f"http://localhost:{vite_port}"},
    )
    assert response.status_code == 200
    assert response.headers.get("access-control-allow-origin") == f"http://localhost:{vite_port}"


def test_dev_mode_cors_merges_with_user_config(
    project: DevServerSettings,
    monkeypatch,
) -> None:
    """When the user configures CORS origins, the Vite origin should be merged
    in during debug mode without duplicating it."""
    from pyxle.config import CorsConfig

    build_once(project)
    registry = load_metadata_registry(project)
    table = build_route_table(registry)

    monkeypatch.setattr(
        "pyxle.devserver.starlette_app.ComponentRenderer",
        lambda: object(),
    )

    async def fake_build_page_response(*, request, settings, page, renderer, overlay=None, **_kw):
        return PlainTextResponse("page")

    monkeypatch.setattr(
        "pyxle.devserver.starlette_app.build_page_response",
        fake_build_page_response,
    )

    user_origin = "https://example.com"
    settings_with_cors = replace(
        project,
        cors=CorsConfig(origins=(user_origin,)),
    )

    app = create_starlette_app(settings_with_cors, table)
    client = TestClient(app)

    # User-configured origin should work
    response = client.get("/api/pulse", headers={"Origin": user_origin})
    assert response.status_code == 200
    assert response.headers.get("access-control-allow-origin") == user_origin

    # Vite origin should also work (auto-merged)
    vite_origin = f"http://{project.vite_host}:{project.vite_port}"
    response = client.get("/api/pulse", headers={"Origin": vite_origin})
    assert response.status_code == 200
    assert response.headers.get("access-control-allow-origin") == vite_origin


def test_production_mode_does_not_add_vite_cors(
    project: DevServerSettings,
    monkeypatch,
) -> None:
    """In production mode, no automatic Vite CORS origin should be injected."""
    build_once(project)
    registry = load_metadata_registry(project)
    table = build_route_table(registry)

    async def fake_build_page_response(*, request, settings, page, renderer, overlay=None, **_kw):
        return PlainTextResponse("page")

    monkeypatch.setattr(
        "pyxle.devserver.starlette_app.build_page_response",
        fake_build_page_response,
    )

    prod_settings = replace(project, debug=False)
    app = create_starlette_app(prod_settings, table)
    client = TestClient(app)

    vite_origin = f"http://{prod_settings.vite_host}:{prod_settings.vite_port}"
    response = client.get("/api/pulse", headers={"Origin": vite_origin})
    assert response.status_code == 200
    # No CORS header should be present — no CORS middleware in prod without config
    assert response.headers.get("access-control-allow-origin") is None


def test_dev_mode_cors_allows_localhost_when_bound_to_all_interfaces(
    project: DevServerSettings,
    monkeypatch,
) -> None:
    """When vite_host is 0.0.0.0, browsers send Origin as localhost or
    127.0.0.1 — never the literal 0.0.0.0.  Both must be allowed."""
    build_once(project)
    registry = load_metadata_registry(project)
    table = build_route_table(registry)

    monkeypatch.setattr(
        "pyxle.devserver.starlette_app.ComponentRenderer",
        lambda: object(),
    )

    async def fake_build_page_response(*, request, settings, page, renderer, overlay=None, **_kw):
        return PlainTextResponse("page")

    monkeypatch.setattr(
        "pyxle.devserver.starlette_app.build_page_response",
        fake_build_page_response,
    )

    wildcard_settings = replace(project, vite_host="0.0.0.0")
    app = create_starlette_app(wildcard_settings, table)
    client = TestClient(app)

    vite_port = wildcard_settings.vite_port

    # localhost origin should be allowed
    resp_localhost = client.get(
        "/api/pulse",
        headers={"Origin": f"http://localhost:{vite_port}"},
    )
    assert resp_localhost.status_code == 200
    assert resp_localhost.headers.get("access-control-allow-origin") == f"http://localhost:{vite_port}"

    # 127.0.0.1 origin should also be allowed
    resp_loopback = client.get(
        "/api/pulse",
        headers={"Origin": f"http://127.0.0.1:{vite_port}"},
    )
    assert resp_loopback.status_code == 200
    assert resp_loopback.headers.get("access-control-allow-origin") == f"http://127.0.0.1:{vite_port}"

    # LAN IP origin should also be allowed (regex match on port)
    resp_lan = client.get(
        "/api/pulse",
        headers={"Origin": f"http://192.168.1.42:{vite_port}"},
    )
    assert resp_lan.status_code == 200
    assert resp_lan.headers.get("access-control-allow-origin") == f"http://192.168.1.42:{vite_port}"

    # Wrong port should NOT match
    resp_wrong_port = client.get(
        "/api/pulse",
        headers={"Origin": f"http://localhost:{vite_port + 1}"},
    )
    assert resp_wrong_port.headers.get("access-control-allow-origin") is None


# ---------------------------------------------------------------------------
# _import_middleware_class — plugin-contributed middleware spec resolver
# ---------------------------------------------------------------------------


def test_import_middleware_class_colon_form() -> None:
    """``package.module:Attribute`` resolves to the named class."""
    from pyxle.devserver.starlette_app import _import_middleware_class
    from tests.devserver.sample_middlewares import HeaderCaptureMiddleware

    resolved = _import_middleware_class(
        "tests.devserver.sample_middlewares:HeaderCaptureMiddleware"
    )
    assert resolved is HeaderCaptureMiddleware


def test_import_middleware_class_dotted_form() -> None:
    """``package.module.Attribute`` (no colon) also resolves the class."""
    from pyxle.devserver.starlette_app import _import_middleware_class
    from tests.devserver.sample_middlewares import SimpleAsgiMiddleware

    resolved = _import_middleware_class(
        "tests.devserver.sample_middlewares.SimpleAsgiMiddleware"
    )
    assert resolved is SimpleAsgiMiddleware


def test_import_middleware_class_rejects_bare_name() -> None:
    """A spec with neither ':' nor '.' is unresolvable and must raise."""
    from pyxle.devserver.starlette_app import _import_middleware_class

    with pytest.raises(ValueError) as excinfo:
        _import_middleware_class("notamodulespec")
    assert "package.module:Class" in str(excinfo.value)


def test_import_middleware_class_missing_attribute() -> None:
    """A valid module but missing attribute reports the attribute name."""
    from pyxle.devserver.starlette_app import _import_middleware_class

    with pytest.raises(AttributeError) as excinfo:
        _import_middleware_class(
            "tests.devserver.sample_middlewares:DoesNotExist"
        )
    assert "DoesNotExist" in str(excinfo.value)


def test_import_middleware_class_non_class_target() -> None:
    """A spec resolving to a non-class value is rejected with a clear type error."""
    from pyxle.devserver.starlette_app import _import_middleware_class

    # ``invalid_factory`` is a function, not a class.
    with pytest.raises(TypeError) as excinfo:
        _import_middleware_class(
            "tests.devserver.sample_middlewares:invalid_factory"
        )
    assert "non-class" in str(excinfo.value)


# ---------------------------------------------------------------------------
# HttpOnlyStaticFiles — non-HTTP scope handling
# ---------------------------------------------------------------------------


def test_http_only_static_ignores_lifespan_scope(project: DevServerSettings) -> None:
    """A non-http, non-websocket scope (e.g. ``lifespan``) is silently
    ignored — the static app neither serves nor closes a socket."""
    import asyncio

    from pyxle.devserver.starlette_app import HttpOnlyStaticFiles

    static_app = HttpOnlyStaticFiles(directory=project.public_dir, check_dir=False)

    sent: list[dict] = []

    async def receive() -> dict:
        return {"type": "lifespan.startup"}

    async def send(message: dict) -> None:
        sent.append(message)

    asyncio.run(static_app({"type": "lifespan"}, receive, send))

    # Bare ``return`` — nothing is sent for an unsupported scope type.
    assert sent == []


# ---------------------------------------------------------------------------
# StaticAssetsMiddleware — method gating and _try_static edge branches
# ---------------------------------------------------------------------------


def _static_assets_app(
    *, public_directory: Path | None = None, client_directory: Path | None = None
) -> Starlette:
    """Build a Starlette app whose only middleware is StaticAssetsMiddleware,
    falling through to a sentinel handler when no static file matches."""
    from starlette.middleware import Middleware

    from pyxle.devserver.starlette_app import StaticAssetsMiddleware

    async def fallthrough(request):  # noqa: ANN001
        return PlainTextResponse("FELL-THROUGH")

    app = Starlette(
        middleware=[
            Middleware(
                StaticAssetsMiddleware,
                public_directory=public_directory,
                client_directory=client_directory,
            )
        ],
    )
    app.router.add_route("/{path:path}", fallthrough, methods=["GET", "POST"])
    return app


def test_static_assets_middleware_passes_through_non_get_methods(
    tmp_path: Path,
) -> None:
    """Only GET/HEAD are served from static dirs; a POST to a path that
    matches a real public file must bypass the static layer entirely."""
    public_dir = tmp_path / "public"
    public_dir.mkdir()
    (public_dir / "robots.txt").write_text("User-agent: *", encoding="utf-8")

    app = _static_assets_app(public_directory=public_dir)
    client = TestClient(app)

    # GET serves the static file.
    got = client.get("/robots.txt")
    assert got.status_code == 200
    assert "User-agent" in got.text

    # POST is not a static method — falls through to the app handler.
    posted = client.post("/robots.txt")
    assert posted.status_code == 200
    assert posted.text == "FELL-THROUGH"


def test_static_assets_middleware_public_branch_when_client_dir_present(
    tmp_path: Path,
) -> None:
    """With a client dir configured, a non-/client path skips the client
    branch (181->190) and is served from the public dir instead."""
    public_dir = tmp_path / "public"
    public_dir.mkdir()
    (public_dir / "favicon.ico").write_text("ICON", encoding="utf-8")
    client_dir = tmp_path / "client"
    client_dir.mkdir()

    app = _static_assets_app(public_directory=public_dir, client_directory=client_dir)
    client = TestClient(app)

    resp = client.get("/favicon.ico")
    assert resp.status_code == 200
    assert resp.text == "ICON"
    # public assets get the short-lived cache header (not the immutable one).
    assert resp.headers["cache-control"] == "public, max-age=3600"


def test_static_assets_middleware_serves_hashed_client_asset_immutable(
    tmp_path: Path,
) -> None:
    """Vite hashed assets under /client/dist/assets/ are immutable and get a
    one-year ``immutable`` cache header (line 230)."""
    client_dir = tmp_path / "client"
    hashed_dir = client_dir / "dist" / "assets"
    hashed_dir.mkdir(parents=True)
    (hashed_dir / "index-a1b2c3d4.js").write_text("export const x = 1;", encoding="utf-8")

    app = _static_assets_app(client_directory=client_dir)
    client = TestClient(app)

    resp = client.get("/client/dist/assets/index-a1b2c3d4.js")
    assert resp.status_code == 200
    assert "export const x" in resp.text
    assert resp.headers["cache-control"] == "public, max-age=31536000, immutable"


def test_static_assets_middleware_unmatched_client_path_falls_through(
    tmp_path: Path,
) -> None:
    """A /client request with no matching file returns False from
    _try_static and falls through to the app (404 from static is swallowed)."""
    client_dir = tmp_path / "client"
    client_dir.mkdir()

    app = _static_assets_app(client_directory=client_dir)
    client = TestClient(app)

    resp = client.get("/client/assets/missing.js")
    assert resp.status_code == 200
    assert resp.text == "FELL-THROUGH"


def test_try_static_returns_false_on_prefix_mismatch(tmp_path: Path) -> None:
    """Calling _try_static with a prefix the path does not start with is a
    no-op that returns False (defensive guard, line 209)."""
    import asyncio

    from pyxle.devserver.starlette_app import HttpOnlyStaticFiles, StaticAssetsMiddleware

    client_dir = tmp_path / "client"
    client_dir.mkdir()
    static = HttpOnlyStaticFiles(directory=client_dir, check_dir=False)

    scope = {"type": "http", "method": "GET", "path": "/not-client/foo.js"}

    async def receive() -> dict:
        return {"type": "http.request"}

    send = AsyncMock()

    result = asyncio.run(
        StaticAssetsMiddleware._try_static(
            static, scope, receive, send, prefix="/client"
        )
    )
    assert result is False
    # The guard short-circuits before touching the static app, so nothing
    # is ever sent on the wire.
    send.assert_not_awaited()


def test_try_static_handles_scope_without_bytes_raw_path(tmp_path: Path) -> None:
    """When the scope's raw_path is absent/non-bytes, the prefix-stripping
    path skips the raw_path re-encode (214->216) and still serves the file."""
    import asyncio

    from pyxle.devserver.starlette_app import HttpOnlyStaticFiles, StaticAssetsMiddleware

    client_dir = tmp_path / "client"
    client_dir.mkdir()
    (client_dir / "app.js").write_text("CLIENTJS", encoding="utf-8")
    static = HttpOnlyStaticFiles(directory=client_dir, check_dir=False)

    # No "raw_path" key at all → the isinstance(raw_path, bytes) guard is False.
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/client/app.js",
        "headers": [],
    }

    async def receive() -> dict:
        return {"type": "http.request"}

    started: list[dict] = []
    body = bytearray()

    async def send(message: dict) -> None:
        if message["type"] == "http.response.start":
            started.append(message)
        elif message["type"] == "http.response.body":
            body.extend(message.get("body", b""))

    result = asyncio.run(
        StaticAssetsMiddleware._try_static(
            static, scope, receive, send, prefix="/client"
        )
    )
    assert result is True
    assert started and started[0]["status"] == 200
    assert bytes(body) == b"CLIENTJS"


def test_try_static_reraises_non_404_http_exception(tmp_path: Path) -> None:
    """A non-404 HTTPException from the static app (e.g. 405 from a method
    StaticFiles rejects) propagates rather than being treated as a miss."""
    import asyncio

    from starlette.exceptions import HTTPException

    from pyxle.devserver.starlette_app import StaticAssetsMiddleware

    class _BoomStatic:
        async def __call__(self, scope, receive, send):  # noqa: ANN001
            raise HTTPException(status_code=405)

    scope = {"type": "http", "method": "GET", "path": "/whatever.js"}

    async def receive() -> dict:
        return {"type": "http.request"}

    send = AsyncMock()

    with pytest.raises(HTTPException) as excinfo:
        asyncio.run(
            StaticAssetsMiddleware._try_static(_BoomStatic(), scope, receive, send)
        )
    assert excinfo.value.status_code == 405


# ---------------------------------------------------------------------------
# build_api_router / _import_module / _resolve_api_handlers — error paths
# ---------------------------------------------------------------------------


def test_build_api_router_raises_for_unloadable_module_path(project: DevServerSettings) -> None:
    """A module path with no recognised Python suffix yields a None import
    spec, surfaced as ApiRouteError (line 322)."""
    from pyxle.devserver.routes import ApiRoute

    bogus = ApiRoute(
        path="/api/bogus",
        source_relative_path=Path("api/bogus"),
        source_absolute_path=project.pages_dir / "api/bogus",
        # No ``.py`` suffix → importlib returns spec=None.
        server_module_path=project.server_build_dir / "api" / "bogus",
        module_key="pyxle.server.api.bogus_unloadable",
        content_hash="deadbeef",
    )

    with pytest.raises(ApiRouteError) as excinfo:
        build_api_router([bogus])
    assert "Unable to load API module" in str(excinfo.value)


def test_resolve_api_handlers_rejects_non_callable_endpoint(project: DevServerSettings) -> None:
    """An API module whose ``endpoint`` is not callable is rejected with a
    descriptive ApiRouteError (line 358)."""
    write_file(
        project.pages_dir / "api/bad_endpoint.py",
        "endpoint = 42  # not callable\n",
    )
    build_once(project)
    registry = load_metadata_registry(project)
    table = build_route_table(registry)
    bad = next(r for r in table.apis if r.path == "/api/bad_endpoint")

    with pytest.raises(ApiRouteError) as excinfo:
        build_api_router([bad])
    assert "endpoint" in str(excinfo.value)
    assert "not callable" in str(excinfo.value)


def test_resolve_api_handlers_rejects_non_callable_websocket(project: DevServerSettings) -> None:
    """An API module whose ``websocket`` is not callable is rejected (line 366)."""
    write_file(
        project.pages_dir / "api/bad_ws.py",
        "from starlette.responses import JSONResponse\n"
        "\n"
        "async def endpoint(request):\n"
        "    return JSONResponse({'ok': True})\n"
        "\n"
        "websocket = 'not callable'\n",
    )
    build_once(project)
    registry = load_metadata_registry(project)
    table = build_route_table(registry)
    bad = next(r for r in table.apis if r.path == "/api/bad_ws")

    with pytest.raises(ApiRouteError) as excinfo:
        build_api_router([bad])
    assert "websocket" in str(excinfo.value)
    assert "not callable" in str(excinfo.value)


def test_resolve_api_handler_shim_returns_http_handler(project: DevServerSettings) -> None:
    """The compatibility shim ``_resolve_api_handler`` returns the HTTP
    handler for a module that has one (lines 401-407 happy path)."""
    import importlib.util
    import sys as _sys

    from pyxle.devserver.starlette_app import _resolve_api_handler

    mod_path = project.server_build_dir / "shim_http.py"
    write_file(
        mod_path,
        "from starlette.responses import JSONResponse\n"
        "\n"
        "async def endpoint(request):\n"
        "    return JSONResponse({'ok': True})\n",
    )
    spec = importlib.util.spec_from_file_location("pyxle._shim_http_mod", mod_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    try:
        handler = _resolve_api_handler(module)
        assert handler is module.endpoint
    finally:
        _sys.modules.pop("pyxle._shim_http_mod", None)


def test_resolve_api_handler_shim_raises_for_ws_only_module(
    project: DevServerSettings,
) -> None:
    """The shim raises when a module exposes only a WebSocket handler, telling
    the caller to use _resolve_api_handlers instead (lines 402-406)."""
    import importlib.util
    import sys as _sys

    from pyxle.devserver.starlette_app import _resolve_api_handler

    mod_path = project.server_build_dir / "shim_ws.py"
    write_file(
        mod_path,
        "async def websocket(ws):\n"
        "    await ws.accept()\n"
        "    await ws.close()\n",
    )
    spec = importlib.util.spec_from_file_location("pyxle._shim_ws_mod", mod_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    try:
        with pytest.raises(ApiRouteError) as excinfo:
            _resolve_api_handler(module)
        assert "WebSocket" in str(excinfo.value)
    finally:
        _sys.modules.pop("pyxle._shim_ws_mod", None)


# ---------------------------------------------------------------------------
# _dispatch_action — validation and error envelopes not exercised elsewhere
# ---------------------------------------------------------------------------


def _action_app(module_path: Path, module_key: str, action_name: str) -> TestClient:
    """Build a TestClient over a single specific (non-catchall) action route."""
    from pyxle.devserver.routes import ActionRoute
    from pyxle.devserver.starlette_app import build_action_router

    route = ActionRoute(
        path=f"/api/__actions/page/{action_name}",
        page_path="/page",
        action_name=action_name,
        server_module_path=module_path,
        module_key=module_key,
    )
    router = build_action_router([route])
    app = Starlette()
    app.router.routes.extend(router.routes)
    return TestClient(app, raise_server_exceptions=False)


def test_dispatch_action_rejects_invalid_action_name(tmp_path: Path) -> None:
    """A route whose action name fails the SAFE_IDENTIFIER_RE check returns a
    400 'Invalid action name' before any module import (line 589)."""
    from pyxle.devserver.routes import ActionRoute
    from pyxle.devserver.starlette_app import build_action_router

    module_path = tmp_path / "server" / "page.py"
    write_file(module_path, "from pyxle.runtime import action\n")

    # Register a route whose path carries an illegal action segment. The route
    # path itself is a literal so Starlette matches it; the handler then
    # validates the (invalid) action name.
    route = ActionRoute(
        path="/api/__actions/page/bad-name",
        page_path="/page",
        action_name="bad-name",  # hyphen is not a valid identifier
        server_module_path=module_path,
        module_key="pyxle.server.pages.badname",
    )
    router = build_action_router([route])
    app = Starlette()
    app.router.routes.extend(router.routes)
    client = TestClient(app, raise_server_exceptions=False)

    resp = client.post("/api/__actions/page/bad-name", json={})
    assert resp.status_code == 400
    assert resp.json() == {"ok": False, "error": "Invalid action name"}


def test_dispatch_action_rejects_oversized_body(tmp_path: Path) -> None:
    """A Content-Length exceeding the 10 MB action body cap is rejected with
    413 before the body is read (lines 596-600)."""
    module_path = tmp_path / "server" / "page.py"
    write_file(
        module_path,
        "from pyxle.runtime import action\n"
        "\n"
        "@action\n"
        "async def upload(request):\n"
        "    return {'ok': True}\n",
    )
    client = _action_app(module_path, "pyxle.server.pages.oversized", "upload")

    too_big = str(11 * 1024 * 1024)
    resp = client.post(
        "/api/__actions/page/upload",
        headers={"content-length": too_big, "content-type": "application/json"},
        content=b"{}",
    )
    assert resp.status_code == 413
    assert resp.json() == {"ok": False, "error": "Request body too large"}


def test_dispatch_action_warns_on_synchronous_action(tmp_path: Path) -> None:
    """A function decorated @action but defined with ``def`` (not ``async
    def``) is flagged with a warning before dispatch (lines 617-625).

    The dispatcher always ``await``\\s the action, so a synchronous function
    that returns a plain dict cannot complete (you can't await a dict) and is
    caught by the generic error envelope as a 500 — the warning is the
    actionable signal that tells the developer to make the action ``async``.
    """
    import logging

    module_path = tmp_path / "server" / "page.py"
    write_file(
        module_path,
        "from pyxle.runtime import action\n"
        "\n"
        "@action\n"
        "def sync_action(request):\n"
        "    return {'ran': 'sync'}\n",
    )
    # debug=True so the generic-exception path surfaces the real error string.
    from pyxle.devserver.routes import ActionRoute
    from pyxle.devserver.starlette_app import build_action_router

    route = ActionRoute(
        path="/api/__actions/page/sync_action",
        page_path="/page",
        action_name="sync_action",
        server_module_path=module_path,
        module_key="pyxle.server.pages.syncaction",
    )
    router = build_action_router([route], debug=True)
    app = Starlette()
    app.router.routes.extend(router.routes)
    client = TestClient(app, raise_server_exceptions=False)

    # Attach a handler directly to the module logger so the assertion does
    # not depend on caplog's root-propagation behaviour across the
    # TestClient worker thread.
    records: list[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    logger = logging.getLogger("pyxle.devserver.starlette_app")
    handler = _Capture()
    previous_level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.WARNING)
    try:
        resp = client.post("/api/__actions/page/sync_action", json={})
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous_level)

    # The warning fired (line 618-620) regardless of the eventual outcome.
    assert any("synchronous" in rec.getMessage() for rec in records)
    # Awaiting the returned dict fails → generic 500 envelope.
    assert resp.status_code == 500
    assert resp.json()["ok"] is False


def test_dispatch_action_invalidate_hint_all_empty_skips_header(tmp_path: Path) -> None:
    """When the invalidate hint is truthy but every URL is empty, the join
    yields an empty string and no header is emitted (branch 662->664)."""
    module_path = tmp_path / "server" / "page.py"
    write_file(
        module_path,
        "from pyxle.runtime import action\n"
        "\n"
        "@action\n"
        "async def noop_invalidate(request):\n"
        "    # Non-empty list, but all entries are empty → joined == ''.\n"
        "    return {'ok': True, '__pyxle_invalidate__': ['', '']}\n",
    )
    client = _action_app(module_path, "pyxle.server.pages.invempty", "noop_invalidate")

    resp = client.post("/api/__actions/page/noop_invalidate", json={})
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    assert "x-pyxle-invalidate" not in resp.headers


def test_dispatch_action_generic_exception_envelope_debug(tmp_path: Path) -> None:
    """A non-ActionError exception is wrapped in a 500 envelope; in debug the
    real message is surfaced (lines 642-644)."""
    from pyxle.devserver.routes import ActionRoute
    from pyxle.devserver.starlette_app import build_action_router

    module_path = tmp_path / "server" / "page.py"
    write_file(
        module_path,
        "from pyxle.runtime import action\n"
        "\n"
        "@action\n"
        "async def boom(request):\n"
        "    raise RuntimeError('kaboom detail')\n",
    )
    route = ActionRoute(
        path="/api/__actions/page/boom",
        page_path="/page",
        action_name="boom",
        server_module_path=module_path,
        module_key="pyxle.server.pages.boomdebug",
    )
    router = build_action_router([route], debug=True)
    app = Starlette()
    app.router.routes.extend(router.routes)
    client = TestClient(app, raise_server_exceptions=False)

    resp = client.post("/api/__actions/page/boom", json={})
    assert resp.status_code == 500
    body = resp.json()
    assert body["ok"] is False
    assert body["error"] == "kaboom detail"


def test_dispatch_action_generic_exception_envelope_production(tmp_path: Path) -> None:
    """In production (debug=False) the same exception is masked behind a
    generic 'Internal server error' message (line 643 false branch)."""
    from pyxle.devserver.routes import ActionRoute
    from pyxle.devserver.starlette_app import build_action_router

    module_path = tmp_path / "server" / "page.py"
    write_file(
        module_path,
        "from pyxle.runtime import action\n"
        "\n"
        "@action\n"
        "async def boom(request):\n"
        "    raise RuntimeError('leaky internal detail')\n",
    )
    route = ActionRoute(
        path="/api/__actions/page/boom",
        page_path="/page",
        action_name="boom",
        server_module_path=module_path,
        module_key="pyxle.server.pages.boomprod",
    )
    router = build_action_router([route], debug=False)
    app = Starlette()
    app.router.routes.extend(router.routes)
    client = TestClient(app, raise_server_exceptions=False)

    resp = client.post("/api/__actions/page/boom", json={})
    assert resp.status_code == 500
    body = resp.json()
    assert body["ok"] is False
    assert body["error"] == "Internal server error"
    assert "leaky internal detail" not in resp.text


def test_dispatch_action_emits_invalidate_header_for_list(tmp_path: Path) -> None:
    """``invalidate_routes(dict, *urls)`` stashes hints the dispatcher lifts
    into an ``x-pyxle-invalidate`` header, stripping the sentinel (659-663)."""
    module_path = tmp_path / "server" / "page.py"
    write_file(
        module_path,
        "from pyxle.runtime import action, invalidate_routes\n"
        "\n"
        "@action\n"
        "async def delete_post(request):\n"
        "    result = {'deleted': True}\n"
        "    return invalidate_routes(result, '/posts', '/feed')\n",
    )
    client = _action_app(module_path, "pyxle.server.pages.invlist", "delete_post")

    resp = client.post("/api/__actions/page/delete_post", json={})
    assert resp.status_code == 200
    body = resp.json()
    assert body == {"ok": True, "deleted": True}
    # Sentinel key must not leak into the JSON body.
    assert "__pyxle_invalidate__" not in body
    assert resp.headers["x-pyxle-invalidate"] == "/posts, /feed"


def test_dispatch_action_invalidate_header_from_string_hint(tmp_path: Path) -> None:
    """When the stashed hint is a bare string (not a list) it is normalised to
    a single-entry header (lines 659-660)."""
    module_path = tmp_path / "server" / "page.py"
    write_file(
        module_path,
        "from pyxle.runtime import action\n"
        "\n"
        "@action\n"
        "async def touch(request):\n"
        "    # Hand-craft a string sentinel to exercise the str-normalisation.\n"
        "    return {'ok': True, '__pyxle_invalidate__': '/dashboard'}\n",
    )
    client = _action_app(module_path, "pyxle.server.pages.invstr", "touch")

    resp = client.post("/api/__actions/page/touch", json={})
    assert resp.status_code == 200
    assert resp.headers["x-pyxle-invalidate"] == "/dashboard"
    assert "__pyxle_invalidate__" not in resp.json()


def test_catchall_action_missing_name_returns_400(tmp_path: Path) -> None:
    """A catch-all action request whose captured path is empty yields a 400
    'Action name missing from request path' (line 696)."""
    from pyxle.devserver.routes import ActionRoute
    from pyxle.devserver.starlette_app import build_action_router

    module_path = tmp_path / "server" / "docs.py"
    write_file(
        module_path,
        "from pyxle.runtime import action\n"
        "\n"
        "@action\n"
        "async def search(request):\n"
        "    return {'ok': True}\n",
    )
    route = ActionRoute(
        path="/api/__actions/docs/{_pyxle_action_path:path}",
        page_path="/docs",
        action_name="",
        server_module_path=module_path,
        module_key="pyxle.server.pages.docsempty",
        is_catchall=True,
    )
    router = build_action_router([route])
    app = Starlette()
    app.router.routes.extend(router.routes)
    client = TestClient(app, raise_server_exceptions=False)

    # Trailing slash → captured ``_pyxle_action_path`` is empty.
    resp = client.post("/api/__actions/docs/", json={})
    assert resp.status_code == 400
    assert resp.json() == {
        "ok": False,
        "error": "Action name missing from request path",
    }


# ---------------------------------------------------------------------------
# build_client_assets_mount — direct constructor coverage
# ---------------------------------------------------------------------------


def test_build_client_assets_mount_serves_directory(tmp_path: Path) -> None:
    """``build_client_assets_mount`` mounts a StaticFiles app under /client
    (lines 726-727)."""
    from pyxle.devserver.starlette_app import build_client_assets_mount

    client_dir = tmp_path / "dist-client"
    (client_dir / "assets").mkdir(parents=True)
    (client_dir / "assets" / "main.js").write_text("MAINJS", encoding="utf-8")

    mount = build_client_assets_mount(client_dir)
    assert mount.path == "/client"
    assert mount.name == "pyxle-client-assets"

    app = Starlette()
    app.router.routes.append(mount)
    client = TestClient(app)

    resp = client.get("/client/assets/main.js")
    assert resp.status_code == 200
    assert resp.text == "MAINJS"


# ---------------------------------------------------------------------------
# create_starlette_app — middleware / hook loading failures + boot wiring
# ---------------------------------------------------------------------------


def test_create_starlette_app_raises_on_bad_custom_middleware(
    project: DevServerSettings,
) -> None:
    """A custom middleware spec that cannot be loaded raises (and is logged)
    at app-assembly time (lines 789-791)."""
    from pyxle.devserver.middleware import MiddlewareHookError

    build_once(project)
    registry = load_metadata_registry(project)
    table = build_route_table(registry)

    errors: list[str] = []

    class StubLogger(ConsoleLogger):
        def error(self, message: str) -> None:  # type: ignore[override]
            errors.append(message)

    broken = replace(
        project,
        custom_middlewares=("tests.devserver.sample_middlewares:NoSuchMiddleware",),
    )

    with pytest.raises(MiddlewareHookError):
        create_starlette_app(broken, table, logger=StubLogger())
    assert errors  # the failure was surfaced through the logger


def test_create_starlette_app_warns_base_http_middleware_with_streaming(
    project: DevServerSettings,
    monkeypatch,
) -> None:
    """A BaseHTTPMiddleware paired with a streaming-eligible route warns at boot.

    Exercises the warning wiring in ``create_starlette_app`` (F28): the build
    has a ``<Suspense>`` route and a configured ``BaseHTTPMiddleware``, so the
    incompatibility is flagged.
    """
    build_once(project)
    registry = load_metadata_registry(project)
    table = build_route_table(registry)
    # Mark a real page as streaming-eligible without authoring a Suspense page.
    table.pages[0] = replace(table.pages[0], uses_suspense=True)

    warnings: list[str] = []

    class StubLogger(ConsoleLogger):
        def warning(self, message: str) -> None:  # type: ignore[override]
            warnings.append(message)

    with_middleware = replace(
        project,
        custom_middlewares=("tests.devserver.sample_middlewares:HeaderCaptureMiddleware",),
    )

    create_starlette_app(with_middleware, table, logger=StubLogger())

    assert any("BaseHTTPMiddleware" in w and "streaming" in w for w in warnings)


def test_create_starlette_app_raises_on_bad_route_hook(
    project: DevServerSettings,
) -> None:
    """A page route-hook spec that fails to resolve raises a RouteHookError,
    logged via the console logger (lines 898-900)."""
    from pyxle.devserver.route_hooks import RouteHookError

    build_once(project)
    registry = load_metadata_registry(project)
    table = build_route_table(registry)

    errors: list[str] = []

    class StubLogger(ConsoleLogger):
        def error(self, message: str) -> None:  # type: ignore[override]
            errors.append(message)

    broken = replace(
        project,
        page_route_hooks=("tests.devserver.sample_middlewares:no_such_hook",),
    )

    with pytest.raises(RouteHookError):
        create_starlette_app(broken, table, logger=StubLogger())
    assert errors


def test_ensure_project_root_on_sys_path_is_idempotent(project: DevServerSettings) -> None:
    """When the project root is already on sys.path, no duplicate entry is
    inserted (branch 59->exit)."""
    from pyxle.devserver.starlette_app import _ensure_project_root_on_sys_path

    root = str(project.project_root)
    original = list(sys.path)
    try:
        sys.path.insert(0, root)
        before = sys.path.count(root)
        _ensure_project_root_on_sys_path(project.project_root)
        # No additional copy added.
        assert sys.path.count(root) == before
    finally:
        sys.path[:] = original


# ---------------------------------------------------------------------------
# CORS — non-loopback host and user-config merge branches
# ---------------------------------------------------------------------------


def test_dev_mode_cors_for_named_host_origin(
    project: DevServerSettings,
    monkeypatch,
) -> None:
    """When vite_host is a concrete non-loopback host, exactly that origin is
    allowed (line 828)."""
    monkeypatch.setattr(
        "pyxle.devserver.starlette_app.ComponentRenderer",
        lambda: object(),
    )

    async def fake_build_page_response(*, request, settings, page, renderer, overlay=None, **_kw):
        return PlainTextResponse("page")

    monkeypatch.setattr(
        "pyxle.devserver.starlette_app.build_page_response",
        fake_build_page_response,
    )

    build_once(project)
    registry = load_metadata_registry(project)
    table = build_route_table(registry)

    named = replace(project, vite_host="dev.internal")
    app = create_starlette_app(named, table)
    client = TestClient(app)

    origin = f"http://dev.internal:{named.vite_port}"
    resp = client.get("/api/pulse", headers={"Origin": origin})
    assert resp.status_code == 200
    assert resp.headers.get("access-control-allow-origin") == origin

    # A different host on the same port must NOT be allowed.
    other = client.get(
        "/api/pulse", headers={"Origin": f"http://evil.example:{named.vite_port}"}
    )
    assert other.headers.get("access-control-allow-origin") is None


def test_dev_mode_user_cors_already_contains_vite_origin(
    project: DevServerSettings,
    monkeypatch,
) -> None:
    """When the user's CORS origins already include the Vite origin, the merge
    does not duplicate it (branch 840->839 continues the loop)."""
    from pyxle.config import CorsConfig

    monkeypatch.setattr(
        "pyxle.devserver.starlette_app.ComponentRenderer",
        lambda: object(),
    )

    async def fake_build_page_response(*, request, settings, page, renderer, overlay=None, **_kw):
        return PlainTextResponse("page")

    monkeypatch.setattr(
        "pyxle.devserver.starlette_app.build_page_response",
        fake_build_page_response,
    )

    build_once(project)
    registry = load_metadata_registry(project)
    table = build_route_table(registry)

    # Pre-seed BOTH loopback vite origins so the merge loop finds them present.
    vite_origins = (
        f"http://localhost:{project.vite_port}",
        f"http://127.0.0.1:{project.vite_port}",
        "https://app.example.com",
    )
    with_cors = replace(project, cors=CorsConfig(origins=vite_origins))
    app = create_starlette_app(with_cors, table)
    client = TestClient(app)

    # The user origin works.
    user_resp = client.get(
        "/api/pulse", headers={"Origin": "https://app.example.com"}
    )
    assert user_resp.headers.get("access-control-allow-origin") == "https://app.example.com"

    # The (already-present) vite origin still works and is not duplicated.
    vite_resp = client.get(
        "/api/pulse", headers={"Origin": f"http://127.0.0.1:{project.vite_port}"}
    )
    assert (
        vite_resp.headers.get("access-control-allow-origin")
        == f"http://127.0.0.1:{project.vite_port}"
    )


def test_dev_mode_user_cors_merges_regex_when_bound_to_all_interfaces(
    project: DevServerSettings,
    monkeypatch,
) -> None:
    """User CORS + vite_host=0.0.0.0 merges the private-network regex into the
    CORS middleware kwargs (line 843)."""
    from pyxle.config import CorsConfig

    monkeypatch.setattr(
        "pyxle.devserver.starlette_app.ComponentRenderer",
        lambda: object(),
    )

    async def fake_build_page_response(*, request, settings, page, renderer, overlay=None, **_kw):
        return PlainTextResponse("page")

    monkeypatch.setattr(
        "pyxle.devserver.starlette_app.build_page_response",
        fake_build_page_response,
    )

    build_once(project)
    registry = load_metadata_registry(project)
    table = build_route_table(registry)

    wildcard = replace(
        project,
        vite_host="0.0.0.0",
        cors=CorsConfig(origins=("https://prod.example.com",)),
    )
    app = create_starlette_app(wildcard, table)
    client = TestClient(app)

    # Explicit user origin allowed.
    user_resp = client.get(
        "/api/pulse", headers={"Origin": "https://prod.example.com"}
    )
    assert (
        user_resp.headers.get("access-control-allow-origin")
        == "https://prod.example.com"
    )

    # A private-network origin matches the merged regex.
    lan_origin = f"http://192.168.0.5:{wildcard.vite_port}"
    lan_resp = client.get("/api/pulse", headers={"Origin": lan_origin})
    assert lan_resp.headers.get("access-control-allow-origin") == lan_origin


def test_production_user_cors_does_not_merge_vite_origin(
    project: DevServerSettings,
    monkeypatch,
) -> None:
    """With user CORS but debug=False, the Vite origin is NOT auto-merged
    (branch 837->845 skips the debug merge block)."""
    from pyxle.config import CorsConfig

    monkeypatch.setattr(
        "pyxle.devserver.starlette_app.ComponentRenderer",
        lambda: object(),
    )

    async def fake_build_page_response(*, request, settings, page, renderer, overlay=None, **_kw):
        return PlainTextResponse("page")

    monkeypatch.setattr(
        "pyxle.devserver.starlette_app.build_page_response",
        fake_build_page_response,
    )

    build_once(project)
    registry = load_metadata_registry(project)
    table = build_route_table(registry)

    prod = replace(
        project,
        debug=False,
        cors=CorsConfig(origins=("https://prod.example.com",)),
    )
    app = create_starlette_app(prod, table)
    client = TestClient(app)

    # User origin works.
    user_resp = client.get(
        "/api/pulse", headers={"Origin": "https://prod.example.com"}
    )
    assert (
        user_resp.headers.get("access-control-allow-origin")
        == "https://prod.example.com"
    )

    # The Vite origin is NOT in the allowed set in production.
    vite_origin = f"http://{prod.vite_host}:{prod.vite_port}"
    vite_resp = client.get("/api/pulse", headers={"Origin": vite_origin})
    assert vite_resp.headers.get("access-control-allow-origin") is None


# ---------------------------------------------------------------------------
# CSRF middleware wiring
# ---------------------------------------------------------------------------


def test_create_starlette_app_installs_csrf_middleware(
    project: DevServerSettings,
    monkeypatch,
) -> None:
    """When ``settings.csrf`` is enabled the CSRF middleware is added to the
    stack: a GET seeds the double-submit cookie and an unprotected POST is
    rejected with 403 (lines 875-885, 978)."""
    from pyxle.config import CsrfConfig

    monkeypatch.setattr(
        "pyxle.devserver.starlette_app.ComponentRenderer",
        lambda: object(),
    )

    async def fake_build_page_response(*, request, settings, page, renderer, overlay=None, **_kw):
        return PlainTextResponse("page")

    monkeypatch.setattr(
        "pyxle.devserver.starlette_app.build_page_response",
        fake_build_page_response,
    )

    build_once(project)
    registry = load_metadata_registry(project)
    table = build_route_table(registry)

    with_csrf = replace(project, csrf=CsrfConfig(enabled=True))
    app = create_starlette_app(with_csrf, table)
    client = TestClient(app, raise_server_exceptions=False)

    # A GET is a safe method: the CSRF cookie is seeded on the response.
    # The default cookie name is port-namespaced (``pyxle-csrf-<bind port>``)
    # so two Pyxle apps on one host never stomp each other's token.
    got = client.get("/api/pulse")
    assert got.status_code == 200
    assert any(
        cookie.startswith("pyxle-csrf-") for cookie in got.headers.get_list("set-cookie")
    )

    # A POST without the matching header/token is rejected by the middleware.
    posted = client.post("/api/__actions/index/noop", json={})
    assert posted.status_code == 403


def test_create_starlette_app_csrf_secure_cookie_in_production(
    project: DevServerSettings,
    monkeypatch,
) -> None:
    """In production with CSRF enabled and cookie_secure unset, the cookie is
    forced Secure (lines 881-883)."""
    from pyxle.config import CsrfConfig

    monkeypatch.setattr(
        "pyxle.devserver.starlette_app.ComponentRenderer",
        lambda: object(),
    )

    async def fake_build_page_response(*, request, settings, page, renderer, overlay=None, **_kw):
        return PlainTextResponse("page")

    monkeypatch.setattr(
        "pyxle.devserver.starlette_app.build_page_response",
        fake_build_page_response,
    )

    build_once(project)
    registry = load_metadata_registry(project)
    table = build_route_table(registry)

    prod_csrf = replace(
        project,
        debug=False,
        csrf=CsrfConfig(enabled=True, cookie_secure=False),
    )
    app = create_starlette_app(prod_csrf, table)
    client = TestClient(app, raise_server_exceptions=False)

    got = client.get("/api/pulse")
    csrf_cookie = next(
        c for c in got.headers.get_list("set-cookie") if c.startswith("pyxle-csrf-")
    )
    assert "Secure" in csrf_cookie


def test_create_starlette_app_installs_rate_limit_middleware(
    project: DevServerSettings,
    monkeypatch,
) -> None:
    """When ``settings.rate_limit`` is enabled the limiter is wired into the
    stack: requests up to the capacity pass, the next is rejected with 429, and
    because observability sits outside the limiter the 429 still carries the
    correlation id header."""
    from pyxle.config import RateLimitConfig

    monkeypatch.setattr(
        "pyxle.devserver.starlette_app.ComponentRenderer",
        lambda: object(),
    )

    build_once(project)
    registry = load_metadata_registry(project)
    table = build_route_table(registry)

    with_rl = replace(
        project, rate_limit=RateLimitConfig(requests=2, window_seconds=60.0)
    )
    app = create_starlette_app(with_rl, table)
    client = TestClient(app, raise_server_exceptions=False)

    # The first two requests drain the bucket (real clock barely advances, so
    # the ~0.03 tokens/sec refill is negligible across three rapid calls).
    assert client.get("/api/pulse").status_code == 200
    assert client.get("/api/pulse").status_code == 200

    throttled = client.get("/api/pulse")
    assert throttled.status_code == 429
    assert int(throttled.headers["retry-after"]) >= 1
    assert throttled.json() == {"ok": False, "error": "Too Many Requests"}
    # Observability wraps the limiter: the rejected request is still tagged.
    assert throttled.headers.get("x-request-id")


def test_create_starlette_app_skips_rate_limit_when_disabled(
    project: DevServerSettings,
    monkeypatch,
) -> None:
    """A disabled ``RateLimitConfig`` (requests == 0) installs no limiter, so
    far more requests than any bucket capacity all succeed."""
    from pyxle.config import RateLimitConfig

    monkeypatch.setattr(
        "pyxle.devserver.starlette_app.ComponentRenderer",
        lambda: object(),
    )

    build_once(project)
    registry = load_metadata_registry(project)
    table = build_route_table(registry)

    with_rl = replace(project, rate_limit=RateLimitConfig(requests=0))
    app = create_starlette_app(with_rl, table)
    client = TestClient(app, raise_server_exceptions=False)

    for _ in range(10):
        assert client.get("/api/pulse").status_code == 200


# ---------------------------------------------------------------------------
# Plugin-contributed middleware
# ---------------------------------------------------------------------------


def _write_plugin_package(tmp_path: Path, *, body: str, package: str) -> Path:
    """Write a tiny importable plugin package onto a fresh directory and return
    that directory (to be inserted on sys.path by the caller)."""
    pkg_root = tmp_path / "plugin_src"
    (pkg_root / package).mkdir(parents=True, exist_ok=True)
    (pkg_root / package / "__init__.py").write_text("", encoding="utf-8")
    (pkg_root / package / "plugin.py").write_text(body, encoding="utf-8")
    return pkg_root


def test_plugin_contributed_middleware_is_applied(
    project: DevServerSettings,
    monkeypatch,
) -> None:
    """A plugin returning ``(import_string, options)`` from ``middleware()``
    has that middleware instantiated and added to the stack (lines 986-1007)."""
    monkeypatch.setattr(
        "pyxle.devserver.starlette_app.ComponentRenderer",
        lambda: object(),
    )

    async def fake_build_page_response(*, request, settings, page, renderer, overlay=None, **_kw):
        return PlainTextResponse("page")

    monkeypatch.setattr(
        "pyxle.devserver.starlette_app.build_page_response",
        fake_build_page_response,
    )

    pkg_root = _write_plugin_package(
        tmp_path=project.project_root,
        package="pyxle_stamp",
        body=(
            "from starlette.middleware.base import BaseHTTPMiddleware\n"
            "from pyxle.plugins import PyxlePlugin\n"
            "\n"
            "class StampMiddleware(BaseHTTPMiddleware):\n"
            "    async def dispatch(self, request, call_next):\n"
            "        response = await call_next(request)\n"
            "        response.headers['x-plugin-stamp'] = 'on'\n"
            "        return response\n"
            "\n"
            "class _StampPlugin(PyxlePlugin):\n"
            "    name = 'pyxle-stamp'\n"
            "    def middleware(self):\n"
            "        return ((\n"
            "            'pyxle_stamp.plugin:StampMiddleware', {},\n"
            "        ),)\n"
            "\n"
            "plugin = _StampPlugin()\n"
        ),
    )
    monkeypatch.syspath_prepend(str(pkg_root))

    build_once(project)
    registry = load_metadata_registry(project)
    table = build_route_table(registry)

    with_plugin = replace(project, plugins=("pyxle-stamp",))
    app = create_starlette_app(with_plugin, table)
    client = TestClient(app)

    resp = client.get("/api/pulse")
    assert resp.status_code == 200
    assert resp.headers.get("x-plugin-stamp") == "on"


def test_plugin_middleware_skips_malformed_entry(
    project: DevServerSettings,
    monkeypatch,
) -> None:
    """A plugin middleware entry that is not a 2-tuple is skipped with a
    warning, and the app still boots (lines 989-996)."""
    monkeypatch.setattr(
        "pyxle.devserver.starlette_app.ComponentRenderer",
        lambda: object(),
    )

    async def fake_build_page_response(*, request, settings, page, renderer, overlay=None, **_kw):
        return PlainTextResponse("page")

    monkeypatch.setattr(
        "pyxle.devserver.starlette_app.build_page_response",
        fake_build_page_response,
    )

    pkg_root = _write_plugin_package(
        tmp_path=project.project_root,
        package="pyxle_bad_mw",
        body=(
            "from pyxle.plugins import PyxlePlugin\n"
            "\n"
            "class _BadMwPlugin(PyxlePlugin):\n"
            "    name = 'pyxle-bad-mw'\n"
            "    def middleware(self):\n"
            "        # Not an (import_string, options) pair.\n"
            "        return ('this-is-not-a-tuple-pair',)\n"
            "\n"
            "plugin = _BadMwPlugin()\n"
        ),
    )
    monkeypatch.syspath_prepend(str(pkg_root))

    build_once(project)
    registry = load_metadata_registry(project)
    table = build_route_table(registry)

    warnings: list[str] = []

    class StubLogger(ConsoleLogger):
        def warning(self, message, *args) -> None:  # type: ignore[override]
            warnings.append(message % args if args else message)

    with_plugin = replace(project, plugins=("pyxle-bad-mw",))
    app = create_starlette_app(with_plugin, table, logger=StubLogger())
    client = TestClient(app)

    resp = client.get("/api/pulse")
    assert resp.status_code == 200
    assert any("isn't" in w or "skipping" in w for w in warnings)


def test_plugin_middleware_import_failure_raises(
    project: DevServerSettings,
    monkeypatch,
) -> None:
    """If a plugin middleware import_string cannot be loaded, app assembly
    logs an error and re-raises (lines 999-1006)."""
    monkeypatch.setattr(
        "pyxle.devserver.starlette_app.ComponentRenderer",
        lambda: object(),
    )

    async def fake_build_page_response(*, request, settings, page, renderer, overlay=None, **_kw):
        return PlainTextResponse("page")

    monkeypatch.setattr(
        "pyxle.devserver.starlette_app.build_page_response",
        fake_build_page_response,
    )

    pkg_root = _write_plugin_package(
        tmp_path=project.project_root,
        package="pyxle_brokenmw",
        body=(
            "from pyxle.plugins import PyxlePlugin\n"
            "\n"
            "class _BrokenMwPlugin(PyxlePlugin):\n"
            "    name = 'pyxle-brokenmw'\n"
            "    def middleware(self):\n"
            "        return (('pyxle_brokenmw.plugin:DoesNotExist', {}),)\n"
            "\n"
            "plugin = _BrokenMwPlugin()\n"
        ),
    )
    monkeypatch.syspath_prepend(str(pkg_root))

    build_once(project)
    registry = load_metadata_registry(project)
    table = build_route_table(registry)

    errors: list[str] = []

    class StubLogger(ConsoleLogger):
        def error(self, message, *args) -> None:  # type: ignore[override]
            errors.append(message % args if args else message)

    with_plugin = replace(project, plugins=("pyxle-brokenmw",))
    with pytest.raises(AttributeError):
        create_starlette_app(with_plugin, table, logger=StubLogger())
    assert errors


# ---------------------------------------------------------------------------
# Catch-all not-found handler (not-found.pyxl boundary)
# ---------------------------------------------------------------------------


def _project_with_not_found(project: DevServerSettings) -> None:
    """Add a root ``not-found.pyxl`` to the project's pages so the route table
    registers a not-found boundary."""
    write_file(
        project.pages_dir / "not-found.pyxl",
        """import React from 'react';

export default function NotFound() {
    return <div>Custom 404</div>;
}
""",
    )


def test_not_found_handler_renders_boundary_response(
    project: DevServerSettings,
    monkeypatch,
) -> None:
    """With a not-found.pyxl present, an unknown path is routed to the catch-all
    handler which returns the rendered boundary response (lines 1049-1055,
    1077-1085)."""
    monkeypatch.setattr(
        "pyxle.devserver.starlette_app.ComponentRenderer",
        lambda: object(),
    )

    async def fake_build_page_response(*, request, settings, page, renderer, overlay=None, **_kw):
        return PlainTextResponse("page")

    monkeypatch.setattr(
        "pyxle.devserver.starlette_app.build_page_response",
        fake_build_page_response,
    )

    async def fake_not_found(*, request, settings, renderer, error_boundaries, overlay=None):
        return HTMLResponse("<div>Custom 404</div>", status_code=404)

    monkeypatch.setattr(
        "pyxle.devserver.starlette_app.build_not_found_response",
        fake_not_found,
    )

    _project_with_not_found(project)
    build_once(project)
    registry = load_metadata_registry(project)
    table = build_route_table(registry)
    assert table.error_boundary_pages  # boundary compiled

    app = create_starlette_app(project, table)
    client = TestClient(app)

    resp = client.get("/this/does/not/exist")
    assert resp.status_code == 404
    assert "Custom 404" in resp.text


def test_not_found_handler_falls_back_to_plain_404(
    project: DevServerSettings,
    monkeypatch,
) -> None:
    """When the not-found boundary renderer returns None, the catch-all handler
    emits a plain 'Not Found' 404 (lines 1086-1087)."""
    monkeypatch.setattr(
        "pyxle.devserver.starlette_app.ComponentRenderer",
        lambda: object(),
    )

    async def fake_build_page_response(*, request, settings, page, renderer, overlay=None, **_kw):
        return PlainTextResponse("page")

    monkeypatch.setattr(
        "pyxle.devserver.starlette_app.build_page_response",
        fake_build_page_response,
    )

    async def fake_not_found(*, request, settings, renderer, error_boundaries, overlay=None):
        return None

    monkeypatch.setattr(
        "pyxle.devserver.starlette_app.build_not_found_response",
        fake_not_found,
    )

    _project_with_not_found(project)
    build_once(project)
    registry = load_metadata_registry(project)
    table = build_route_table(registry)

    app = create_starlette_app(project, table)
    client = TestClient(app)

    resp = client.get("/missing/page")
    assert resp.status_code == 404
    assert resp.text == "Not Found"


# ---------------------------------------------------------------------------
# _health_payload — missing start time
# ---------------------------------------------------------------------------


def test_healthz_payload_without_start_time_uptime_zero(
    project: DevServerSettings,
) -> None:
    """When ``app.state.pyxle_started_at`` is absent/non-numeric, uptime stays
    0.0 (branch 1119->1122)."""
    import asyncio

    from pyxle.devserver.starlette_app import _healthz_endpoint

    app = Starlette()
    # Deliberately do NOT set pyxle_started_at.

    class _Req:
        def __init__(self, application):
            self.app = application

    response = asyncio.run(_healthz_endpoint(_Req(app)))  # type: ignore[arg-type]
    assert response.status_code == 200
    payload = json.loads(response.body)
    assert payload["status"] == "ok"
    assert payload["ready"] is False
    assert payload["uptime"] == 0.0


# ---------------------------------------------------------------------------
# Sync API endpoints — threadpool dispatch through the route-hook chain
# ---------------------------------------------------------------------------


def test_sync_function_endpoint_runs_in_threadpool_with_default_policies(
    project: DevServerSettings,
    monkeypatch,
) -> None:
    """A plain ``def endpoint(request)`` API module must work through the
    real app assembly (default API policies installed) and must execute off
    the event loop. Regression: this used to 500 with ``TypeError`` because
    the hook chain awaited the sync return value."""

    write_file(
        project.pages_dir / "api/sync_info.py",
        """import asyncio\nfrom starlette.responses import JSONResponse\n\ndef endpoint(request):\n    try:\n        asyncio.get_running_loop()\n        on_loop = True\n    except RuntimeError:\n        on_loop = False\n    route = request.scope.get(\"pyxle\", {}).get(\"route\", {})\n    return JSONResponse({\"onLoop\": on_loop, \"target\": route.get(\"target\")})\n""",
    )

    build_once(project)
    registry = load_metadata_registry(project)
    table = build_route_table(registry)

    monkeypatch.setattr(
        "pyxle.devserver.starlette_app.ComponentRenderer",
        lambda: object(),
    )

    app = create_starlette_app(project, table)
    client = TestClient(app)

    response = client.get("/api/sync_info")
    assert response.status_code == 200
    payload = response.json()
    # Ran in a worker thread (no running loop there) …
    assert payload["onLoop"] is False
    # … and the default attach_route_metadata policy still ran around it.
    assert payload["target"] == "api"


def test_http_endpoint_class_dispatches_through_default_api_policies(
    project: DevServerSettings,
) -> None:
    """HTTPEndpoint-class API modules must dispatch natively even when route
    hooks are installed. Regression: the class used to be wrapped into the
    request→response chain and crashed with ``TypeError`` on every request."""

    from pyxle.devserver.route_hooks import DEFAULT_API_POLICIES

    build_once(project)
    registry = load_metadata_registry(project)
    table = build_route_table(registry)

    router = build_api_router(table.apis, route_hooks=list(DEFAULT_API_POLICIES))

    app = Starlette()
    app.router.routes.extend(router.routes)
    client = TestClient(app)

    response = client.get("/api/posts/42")
    assert response.status_code == 200
    assert response.json() == {"id": "42"}

    # Starlette's native HTTPEndpoint dispatch supplies the 405 handling.
    response = client.post("/api/posts/42")
    assert response.status_code == 405


def test_sync_http_endpoint_method_runs_in_threadpool(
    project: DevServerSettings,
) -> None:
    """Sync methods on HTTPEndpoint classes are threadpooled by Starlette's
    own dispatch once the class is routed natively."""

    from pyxle.devserver.route_hooks import DEFAULT_API_POLICIES

    write_file(
        project.pages_dir / "api/sync_class.py",
        """import asyncio\nfrom starlette.endpoints import HTTPEndpoint\nfrom starlette.responses import JSONResponse\n\nclass SyncEndpoint(HTTPEndpoint):\n    def get(self, request):\n        try:\n            asyncio.get_running_loop()\n            on_loop = True\n        except RuntimeError:\n            on_loop = False\n        return JSONResponse({\"onLoop\": on_loop})\n""",
    )

    build_once(project)
    registry = load_metadata_registry(project)
    table = build_route_table(registry)

    router = build_api_router(table.apis, route_hooks=list(DEFAULT_API_POLICIES))

    app = Starlette()
    app.router.routes.extend(router.routes)
    client = TestClient(app)

    response = client.get("/api/sync_class")
    assert response.status_code == 200
    assert response.json() == {"onLoop": False}


# ---------------------------------------------------------------------------
# StaticAssetsMiddleware — in-memory cache (production serve)
# ---------------------------------------------------------------------------


def _cached_static_app(
    *,
    public_directory: Path | None = None,
    client_directory: Path | None = None,
    **cache_kwargs,
) -> Starlette:
    """Like _static_assets_app but with the in-memory cache enabled."""
    from starlette.middleware import Middleware

    from pyxle.devserver.starlette_app import StaticAssetsMiddleware

    async def fallthrough(request):  # noqa: ANN001
        return PlainTextResponse("FELL-THROUGH")

    app = Starlette(
        middleware=[
            Middleware(
                StaticAssetsMiddleware,
                public_directory=public_directory,
                client_directory=client_directory,
                cache_in_memory=True,
                **cache_kwargs,
            )
        ],
    )
    app.router.add_route("/{path:path}", fallthrough, methods=["GET", "POST"])
    return app


def test_static_memory_cache_serves_after_file_deleted(tmp_path: Path) -> None:
    """Cached assets are served entirely from memory: deleting the file on
    disk after startup must not affect responses."""
    public_dir = tmp_path / "public"
    public_dir.mkdir()
    asset = public_dir / "benchmark.json"
    asset.write_text('{"hello": "world"}', encoding="utf-8")

    app = _cached_static_app(public_directory=public_dir)
    client = TestClient(app)

    first = client.get("/benchmark.json")
    assert first.status_code == 200
    assert first.json() == {"hello": "world"}
    assert first.headers["content-type"] == "application/json"
    assert first.headers["cache-control"] == "public, max-age=3600"
    assert first.headers["etag"].startswith('"')
    assert "last-modified" in first.headers

    asset.unlink()

    second = client.get("/benchmark.json")
    assert second.status_code == 200
    assert second.json() == {"hello": "world"}


def test_static_memory_cache_conditional_requests_return_304(tmp_path: Path) -> None:
    public_dir = tmp_path / "public"
    public_dir.mkdir()
    (public_dir / "styles.css").write_text("body { margin: 0 }", encoding="utf-8")

    app = _cached_static_app(public_directory=public_dir)
    client = TestClient(app)

    base = client.get("/styles.css")
    assert base.status_code == 200
    assert base.headers["content-type"] == "text/css; charset=utf-8"
    etag = base.headers["etag"]
    last_modified = base.headers["last-modified"]

    not_modified = client.get("/styles.css", headers={"if-none-match": etag})
    assert not_modified.status_code == 304
    assert not_modified.content == b""
    assert not_modified.headers["etag"] == etag

    by_date = client.get("/styles.css", headers={"if-modified-since": last_modified})
    assert by_date.status_code == 304


def test_static_memory_cache_head_preserves_content_length(tmp_path: Path) -> None:
    public_dir = tmp_path / "public"
    public_dir.mkdir()
    body = b'{"k": 1}'
    (public_dir / "data.json").write_bytes(body)

    app = _cached_static_app(public_directory=public_dir)
    client = TestClient(app)

    response = client.head("/data.json")
    assert response.status_code == 200
    assert response.content == b""
    assert response.headers["content-length"] == str(len(body))


def test_static_memory_cache_skips_oversized_files(tmp_path: Path) -> None:
    """Files above the per-file budget keep streaming from disk."""
    public_dir = tmp_path / "public"
    public_dir.mkdir()
    big = public_dir / "big.txt"
    big.write_text("X" * 64, encoding="utf-8")

    app = _cached_static_app(public_directory=public_dir, cache_max_file_bytes=8)
    client = TestClient(app)

    served = client.get("/big.txt")
    assert served.status_code == 200
    assert served.text == "X" * 64

    # Not cached: once the file is gone the request falls through.
    big.unlink()
    fallen = client.get("/big.txt")
    assert fallen.text == "FELL-THROUGH"


def test_static_memory_cache_respects_total_budget(tmp_path: Path) -> None:
    """The startup walk stops caching once the total budget is consumed
    (sorted order, so a.txt wins the budget over b.txt)."""
    public_dir = tmp_path / "public"
    public_dir.mkdir()
    (public_dir / "a.txt").write_text("AAAA", encoding="utf-8")
    (public_dir / "b.txt").write_text("BBBB", encoding="utf-8")

    app = _cached_static_app(public_directory=public_dir, cache_max_total_bytes=4)
    client = TestClient(app)

    # Starlette builds the middleware stack lazily — issue a request first so
    # the startup walk runs while the files still exist on disk.
    assert client.get("/a.txt").text == "AAAA"
    assert client.get("/b.txt").text == "BBBB"

    (public_dir / "a.txt").unlink()
    (public_dir / "b.txt").unlink()

    cached = client.get("/a.txt")
    assert cached.status_code == 200
    assert cached.text == "AAAA"

    uncached = client.get("/b.txt")
    assert uncached.text == "FELL-THROUGH"


def test_static_memory_cache_hashed_client_assets_stay_immutable(tmp_path: Path) -> None:
    client_dir = tmp_path / "client"
    hashed_dir = client_dir / "dist" / "assets"
    hashed_dir.mkdir(parents=True)
    (hashed_dir / "index-a1b2c3d4.js").write_text("export const x = 1;", encoding="utf-8")

    app = _cached_static_app(client_directory=client_dir)
    client = TestClient(app)

    response = client.get("/client/dist/assets/index-a1b2c3d4.js")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "public, max-age=31536000, immutable"
    assert response.headers["content-type"] == "text/javascript; charset=utf-8"

    (hashed_dir / "index-a1b2c3d4.js").unlink()
    assert client.get("/client/dist/assets/index-a1b2c3d4.js").status_code == 200


def test_static_cache_disabled_reads_live_from_disk(tmp_path: Path) -> None:
    """Without cache_in_memory (dev), edits to public files are visible."""
    public_dir = tmp_path / "public"
    public_dir.mkdir()
    (public_dir / "live.txt").write_text("before", encoding="utf-8")

    app = _static_assets_app(public_directory=public_dir)
    client = TestClient(app)

    assert client.get("/live.txt").text == "before"
    (public_dir / "live.txt").write_text("after", encoding="utf-8")
    assert client.get("/live.txt").text == "after"


def test_static_assets_middleware_dev_public_uses_no_cache(tmp_path: Path) -> None:
    """In dev (debug=True), a public asset is served with a revalidating
    ``no-cache`` header so a browser refresh reflects an edit — not the
    hour-long production cache."""
    from starlette.middleware import Middleware

    from pyxle.devserver.starlette_app import StaticAssetsMiddleware

    public_dir = tmp_path / "public"
    public_dir.mkdir()
    (public_dir / "logo.svg").write_text("<svg/>", encoding="utf-8")

    app = Starlette(
        middleware=[
            Middleware(StaticAssetsMiddleware, public_directory=public_dir, debug=True)
        ]
    )
    app.router.add_route(
        "/{path:path}", lambda r: PlainTextResponse("X"), methods=["GET"]
    )
    client = TestClient(app)

    resp = client.get("/logo.svg")
    assert resp.status_code == 200
    assert resp.headers["cache-control"] == "no-cache"


def test_static_assets_middleware_dev_client_hashed_stays_immutable(
    tmp_path: Path,
) -> None:
    """Debug mode must not weaken hashed client-bundle caching — those stay
    immutable regardless of mode."""
    from starlette.middleware import Middleware

    from pyxle.devserver.starlette_app import StaticAssetsMiddleware

    client_dir = tmp_path / "client"
    hashed_dir = client_dir / "dist" / "assets"
    hashed_dir.mkdir(parents=True)
    (hashed_dir / "index-a1b2c3d4.js").write_text("export const x = 1;", encoding="utf-8")

    app = Starlette(
        middleware=[
            Middleware(StaticAssetsMiddleware, client_directory=client_dir, debug=True)
        ]
    )
    app.router.add_route(
        "/{path:path}", lambda r: PlainTextResponse("X"), methods=["GET"]
    )
    client = TestClient(app)

    resp = client.get("/client/dist/assets/index-a1b2c3d4.js")
    assert resp.status_code == 200
    assert resp.headers["cache-control"] == "public, max-age=31536000, immutable"


def test_static_file_index_resync_discovers_new_file(tmp_path: Path) -> None:
    """A file added after startup is not served until the shared index is
    resync'd — then it becomes discoverable without rebuilding the app."""
    from starlette.middleware import Middleware

    from pyxle.devserver.starlette_app import StaticAssetsMiddleware, StaticFileIndex

    public_dir = tmp_path / "public"
    public_dir.mkdir()

    index = StaticFileIndex(public_dir)
    assert "/late.txt" not in index

    async def fallthrough(request):  # noqa: ANN001
        return PlainTextResponse("FELL-THROUGH")

    app = Starlette(
        middleware=[
            Middleware(
                StaticAssetsMiddleware,
                public_directory=public_dir,
                debug=True,
                public_index=index,
            )
        ]
    )
    app.router.add_route("/{path:path}", fallthrough, methods=["GET"])
    client = TestClient(app)

    # Not yet indexed → the request falls through to the app.
    assert client.get("/late.txt").text == "FELL-THROUGH"

    # Create the file and refresh the shared index (what the dev watcher does).
    (public_dir / "late.txt").write_text("HELLO", encoding="utf-8")
    index.resync()
    assert "/late.txt" in index

    resp = client.get("/late.txt")
    assert resp.status_code == 200
    assert resp.text == "HELLO"


def test_create_starlette_app_enables_static_cache_only_in_production(
    project: DevServerSettings,
    monkeypatch,
) -> None:
    """The app assembly memory-caches static assets only when not in debug
    mode — dev keeps serving public/ straight from disk — and threads the
    debug flag + a shared static index the dev watcher can refresh."""

    from pyxle.devserver.starlette_app import StaticAssetsMiddleware

    build_once(project)
    registry = load_metadata_registry(project)
    table = build_route_table(registry)

    monkeypatch.setattr(
        "pyxle.devserver.starlette_app.ComponentRenderer",
        lambda: object(),
    )

    def _static_kwargs(app):
        for mw in app.user_middleware:
            if mw.cls is StaticAssetsMiddleware:
                return mw.kwargs
        raise AssertionError("StaticAssetsMiddleware not installed")

    dev_app = create_starlette_app(project, table)
    dev_kwargs = _static_kwargs(dev_app)
    assert dev_kwargs["cache_in_memory"] is False
    assert dev_kwargs["debug"] is True
    # The shared index is exposed for the dev watcher and passed to the middleware.
    assert dev_app.state.pyxle_static_index is not None
    assert dev_kwargs["public_index"] is dev_app.state.pyxle_static_index

    prod_app = create_starlette_app(replace(project, debug=False), table)
    prod_kwargs = _static_kwargs(prod_app)
    assert prod_kwargs["cache_in_memory"] is True
    assert prod_kwargs["debug"] is False


def test_hot_route_refresh_keeps_streaming_wired(
    project: DevServerSettings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: the dev-server hot route refresh must re-thread the pool's
    render_stream. Without this, a hot reload rebuilt routes without it and every
    <Suspense> page silently fell back from renderToPipeableStream to the
    buffered renderToString (which can't stream Suspense)."""
    import pyxle.devserver as devserver_pkg

    build_once(project)
    routes = build_route_table(load_metadata_registry(project))

    def _render_stream(*args, **kwargs):  # pragma: no cover - identity sentinel
        raise AssertionError("not invoked in this test")

    pool = SimpleNamespace(render_stream=_render_stream)
    app = create_starlette_app(project, routes, pool=pool)

    # create_starlette_app stashes the pool's render_stream for later refreshes.
    assert app.state.pyxle_stream_render is _render_stream

    # ...and the hot route refresh threads it back into the rebuilt routes.
    captured: dict = {}

    def _spy_build_app_routes(**kwargs):
        captured.update(kwargs)
        return ([], object())

    monkeypatch.setattr(
        "pyxle.devserver.starlette_app._build_app_routes", _spy_build_app_routes
    )
    devserver_pkg._rebuild_app_routes(app, project)
    assert captured["stream_render"] is _render_stream


@pytest.mark.anyio
async def test_loading_boundary_route_takes_streaming_path(
    anyio_backend, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A page with a loading.pyxl boundary but NO in-page <Suspense> must still
    enter the streaming path — otherwise the route-level loading shell is dead."""
    from starlette.requests import Request
    from starlette.responses import HTMLResponse

    from pyxle.devserver.routes import PageRoute
    from pyxle.devserver.starlette_app import ComponentRenderer, _build_cached_page_response

    root = tmp_path / "project"
    (root / "pages").mkdir(parents=True)
    (root / "public").mkdir()
    settings = DevServerSettings.from_project_root(root)

    loading = PageRoute(
        path="/dashboard/loading",
        source_relative_path=Path("dashboard/loading.pyxl"),
        source_absolute_path=root / "pages" / "dashboard" / "loading.pyxl",
        server_module_path=root / "server" / "dashboard" / "loading.py",
        client_module_path=root / "client" / "dashboard" / "loading.jsx",
        metadata_path=root / "meta" / "dashboard" / "loading.json",
        module_key="pyxle.server.pages.dashboard.loading",
        client_asset_path="/pages/dashboard/loading.jsx",
        server_asset_path="/pages/dashboard/loading.py",
        content_hash="h",
        loader_name=None,
        loader_line=None,
        head_elements=(),
        head_is_dynamic=False,
    )
    route = replace(
        loading,
        path="/dashboard",
        source_relative_path=Path("dashboard/index.pyxl"),
        client_asset_path="/pages/dashboard/index.jsx",
        uses_suspense=False,  # NO in-page Suspense — only the loading boundary
        loading_boundary=loading,
    )

    called: dict = {}

    async def _fake_streaming(**kwargs):
        called["yes"] = True
        return HTMLResponse("streamed")

    monkeypatch.setattr(
        "pyxle.devserver.starlette_app.build_streaming_page_response", _fake_streaming
    )

    async def _stream_render(*args, **kwargs):  # pragma: no cover - sentinel
        yield {"type": "end"}

    request = Request(
        {"type": "http", "http_version": "1.1", "method": "GET", "path": "/dashboard",
         "query_string": b"", "root_path": "", "headers": []}
    )
    response = await _build_cached_page_response(
        request=request,
        route=route,
        settings=settings,
        renderer=ComponentRenderer(),
        overlay=None,
        error_boundaries=None,
        page_cache=None,
        stream_render=_stream_render,
    )
    assert called.get("yes") is True
    assert response.headers["Cache-Control"] == "private, no-cache"


def test_build_app_routes_registers_llms_routes_when_enabled(
    project: DevServerSettings,
) -> None:
    from dataclasses import replace

    from pyxle.config import LlmsConfig
    from pyxle.devserver.starlette_app import _build_app_routes

    build_once(project)
    registry = load_metadata_registry(project)
    table = build_route_table(registry)

    enabled = replace(project, llms=LlmsConfig(enabled=True))
    built, _eb = _build_app_routes(
        settings=enabled,
        routes=table,
        renderer=object(),  # type: ignore[arg-type]
        overlay=None,
        api_route_hooks=[],
        page_route_hooks=[],
    )
    paths = {getattr(route, "path", None) for route in built}
    assert "/index.md" in paths
    assert "/posts/{id}.md" in paths
    assert "/llms.txt" in paths

    # Off by default: no markdown routes or index.
    built_off, _ = _build_app_routes(
        settings=project,
        routes=table,
        renderer=object(),  # type: ignore[arg-type]
        overlay=None,
        api_route_hooks=[],
        page_route_hooks=[],
    )
    paths_off = {getattr(route, "path", None) for route in built_off}
    assert "/index.md" not in paths_off
    assert "/llms.txt" not in paths_off
