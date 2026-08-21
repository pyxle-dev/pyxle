"""Tests for the production app factory (``pyxle.build.production``).

Covers the single-process assembly helper, the asset/dist resolution mirrors,
the worker-subprocess env contract, and the importable ``create_app`` factory
used by ``pyxle serve --workers N``.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from starlette.testclient import TestClient

from pyxle.build import production
from pyxle.build.pipeline import run_build
from pyxle.build.production import (
    ENV_CONFIG,
    ENV_DIST,
    ENV_HOST,
    ENV_PORT,
    ENV_PROJECT_ROOT,
    ENV_SERVE_STATIC,
    ENV_SSR_WORKERS,
    FACTORY_IMPORT_STRING,
    ProductionServeError,
    _resolve_dist_directory,
    _resolve_global_script_entries,
    _resolve_global_style_entries,
    _resolve_pool_size,
    build_production_app,
    build_settings,
    create_app,
    serve_worker_env,
)
from pyxle.config import PyxleConfig
from pyxle.devserver.settings import DevServerSettings


def _make_project(tmp_path: Path, *, with_manifest: bool = True) -> tuple[Path, Path]:
    """Create a minimal built project; return ``(project_root, dist)``."""
    project = tmp_path / "app"
    (project / "pages").mkdir(parents=True)
    (project / "public").mkdir(parents=True)
    dist = project / "dist"
    (dist / "public").mkdir(parents=True)
    # A real build puts Vite's bundle in dist/client/dist; the tree above it is
    # build input and is deliberately never mounted.
    (dist / "client" / "dist").mkdir(parents=True)
    if with_manifest:
        (dist / "page-manifest.json").write_text(
            '{"pages": {}, "generated_at": "2024-01-01"}', encoding="utf-8"
        )
    return project, dist


def _stub_assembly(monkeypatch, captured: dict) -> None:
    """Replace the heavy assembly seams so tests need no Node/compilation.

    Note this stubs out *which directory* the registry reads, so tests using it
    cannot say anything about that; the dist-rooting tests below deliberately
    run the real registry against a real dist tree instead.
    """
    monkeypatch.setattr(
        production, "build_metadata_registry", lambda settings, metadata=None: object()
    )
    monkeypatch.setattr(production, "build_route_table", lambda registry: [])

    def fake_create_app(settings, routes, **kwargs):
        captured["settings"] = settings
        captured["routes"] = routes
        captured.update(kwargs)
        return SimpleNamespace(state=SimpleNamespace(pyxle_ready=False))

    monkeypatch.setattr(production, "create_starlette_app", fake_create_app)


# ── asset / dist resolution mirrors ──────────────────────────────────────────


def test_factory_import_string_is_stable() -> None:
    assert FACTORY_IMPORT_STRING == "pyxle.build.production:create_app"


def test_resolve_dist_directory_default_absolute_and_relative(tmp_path: Path) -> None:
    assert _resolve_dist_directory(tmp_path, None) == (tmp_path / "dist").resolve()
    absolute = (tmp_path / "out").resolve()
    assert _resolve_dist_directory(tmp_path, absolute) == absolute
    assert _resolve_dist_directory(tmp_path, Path("rel")) == (tmp_path / "rel").resolve()


def test_resolve_global_script_entries_dedupe(tmp_path: Path) -> None:
    config = PyxleConfig(global_scripts=(" scripts/a.js ", "", "scripts/a.js", "scripts/b.js"))
    assert _resolve_global_script_entries(tmp_path, config) == ("scripts/a.js", "scripts/b.js")


def test_resolve_global_style_entries_auto_detects_global_css(tmp_path: Path) -> None:
    (tmp_path / "styles").mkdir()
    (tmp_path / "styles" / "global.css").write_text("body{}", encoding="utf-8")
    assert _resolve_global_style_entries(tmp_path, PyxleConfig()) == ("styles/global.css",)


def test_resolve_global_style_entries_explicit_skips_autodetect(tmp_path: Path) -> None:
    (tmp_path / "styles").mkdir()
    (tmp_path / "styles" / "global.css").write_text("body{}", encoding="utf-8")
    config = PyxleConfig(global_styles=("styles/theme.css", "styles/theme.css"))
    assert _resolve_global_style_entries(tmp_path, config) == ("styles/theme.css",)


@pytest.mark.parametrize(
    "requested,expected",
    [(1, 1), (3, 3), (0, min(os.cpu_count() or 2, 4))],
)
def test_resolve_pool_size(requested: int, expected: int) -> None:
    assert _resolve_pool_size(requested) == expected


@pytest.mark.parametrize(
    "requested,expected",
    [(1, 1), (4, 4), (0, max(1, os.cpu_count() or 1))],
)
def test_resolve_server_workers(requested: int, expected: int) -> None:
    from pyxle.build.production import resolve_server_workers

    assert resolve_server_workers(requested) == expected


def test_resolve_server_workers_clamps_to_one_when_no_cores(monkeypatch) -> None:
    from pyxle.build import production

    monkeypatch.setattr(production.os, "cpu_count", lambda: None)
    assert production.resolve_server_workers(0) == 1


# ── build_settings ───────────────────────────────────────────────────────────


def test_build_settings_applies_overrides_and_styles(tmp_path: Path) -> None:
    project, _ = _make_project(tmp_path)
    (project / "styles").mkdir()
    (project / "styles" / "global.css").write_text("body{}", encoding="utf-8")

    settings = build_settings(project, host="0.0.0.0", port=9001, ssr_workers=2)

    assert settings.starlette_host == "0.0.0.0"
    assert settings.starlette_port == 9001
    assert settings.ssr_workers == 2
    assert settings.debug is False
    assert any("global.css" in str(s.source if hasattr(s, "source") else s) for s in settings.global_stylesheets)


# ── build_production_app ─────────────────────────────────────────────────────


def test_build_production_app_missing_manifest_raises(tmp_path: Path) -> None:
    project, dist = _make_project(tmp_path, with_manifest=False)
    settings = build_settings(project)
    with pytest.raises(ProductionServeError) as excinfo:
        build_production_app(settings, dist)
    assert "page-manifest.json not found" in str(excinfo.value)


def test_build_production_app_assembles_and_sizes_pool(tmp_path: Path, monkeypatch) -> None:
    project, dist = _make_project(tmp_path)
    settings = build_settings(project, ssr_workers=2)
    captured: dict = {}
    _stub_assembly(monkeypatch, captured)

    pool_args: dict = {}

    def fake_pool(**kwargs):
        pool_args.update(kwargs)
        return SimpleNamespace()

    monkeypatch.setattr("pyxle.ssr.worker_pool.SsrWorkerPool", fake_pool)

    app, pool_size = build_production_app(settings, dist, serve_static=True)

    assert app.state.pyxle_ready is True
    assert pool_size == 2
    assert pool_args["size"] == 2
    assert captured["public_static_dir"] == dist / "public"
    # The bundle, not the build-input tree one level up.
    assert captured["client_static_dir"] == dist / "client" / "dist"
    assert captured["serve_static"] is True
    assert captured["pool"] is not None


def test_build_production_app_does_not_mount_the_build_input_tree(
    tmp_path: Path, monkeypatch
) -> None:
    """A ``dist/client`` that predates the bundle must not be mounted wholesale.

    ``dist/client/`` is the build input — page JSX, ``vite.config.js``,
    ``tsconfig.json``. Only ``dist/client/dist/`` is public, and when Vite left
    nothing there the mount is disabled rather than falling back to the parent.
    """
    project, dist = _make_project(tmp_path)
    (dist / "client" / "vite.config.js").write_text("export default {};", encoding="utf-8")
    shutil.rmtree(dist / "client" / "dist")
    settings = build_settings(project, ssr_workers=1)
    captured: dict = {}
    _stub_assembly(monkeypatch, captured)
    monkeypatch.setattr("pyxle.ssr.worker_pool.SsrWorkerPool", lambda **kw: SimpleNamespace())

    build_production_app(settings, dist)

    assert captured["client_static_dir"] is None


def test_build_production_app_serve_static_false_disables_mounts(tmp_path: Path, monkeypatch) -> None:
    project, dist = _make_project(tmp_path)
    settings = build_settings(project, ssr_workers=1)
    captured: dict = {}
    _stub_assembly(monkeypatch, captured)
    monkeypatch.setattr("pyxle.ssr.worker_pool.SsrWorkerPool", lambda **kw: SimpleNamespace())

    build_production_app(settings, dist, serve_static=False)

    assert captured["public_static_dir"] is None
    assert captured["client_static_dir"] is None
    assert captured["serve_static"] is False


def test_build_production_app_falls_back_when_dist_assets_missing(tmp_path: Path, monkeypatch) -> None:
    project, dist = _make_project(tmp_path)
    # Remove the built asset dirs to exercise the fallback/None branches.
    (dist / "public").rmdir()
    shutil.rmtree(dist / "client")
    settings = build_settings(project, ssr_workers=1)
    captured: dict = {}
    _stub_assembly(monkeypatch, captured)
    monkeypatch.setattr("pyxle.ssr.worker_pool.SsrWorkerPool", lambda **kw: SimpleNamespace())

    build_production_app(settings, dist)

    assert captured["public_static_dir"] == settings.public_dir  # fell back to source
    assert captured["client_static_dir"] is None  # 404s for /client


def test_build_production_app_zero_ssr_workers_builds_no_pool_when_cpu_zero(
    tmp_path: Path, monkeypatch
) -> None:
    # When the resolved pool size is non-positive, no pool is constructed.
    project, dist = _make_project(tmp_path)
    settings = build_settings(project, ssr_workers=1)
    captured: dict = {}
    _stub_assembly(monkeypatch, captured)
    monkeypatch.setattr(production, "_resolve_pool_size", lambda _n: 0)

    def explode(**_kwargs):  # pragma: no cover - must not be called
        raise AssertionError("pool must not be built when size <= 0")

    monkeypatch.setattr("pyxle.ssr.worker_pool.SsrWorkerPool", explode)

    _, pool_size = build_production_app(settings, dist)

    assert pool_size == 0
    assert captured["pool"] is None


# ── the production app is rooted in dist, not in .pyxle-build ────────────────
#
# These run the REAL metadata registry and route table against a real (small)
# artifact tree, because the thing under test is *which directory* the
# production server reads. Stubbing the registry — as the tests above do — is
# exactly what let a rooted-in-.pyxle-build bug ship: every line ran, and the
# only fact that mattered was mocked out.


def _write_built_project(
    tmp_path: Path,
    *,
    dist_client_path: str = "/routes/index.jsx",
    stale_client_path: str | None = None,
    dist_build_cache: bool = True,
) -> tuple[Path, Path]:
    """Create a project whose ``dist`` holds one layout-wrapped page.

    ``stale_client_path`` (when given) also writes an intermediate
    ``.pyxle-build`` tree whose copy of the page metadata disagrees with
    ``dist`` — the state any tool that recompiles a page after the build leaves
    behind, since ``client_path``/``wrappers`` are written by the layout
    composition pass and reset by the compiler.
    """
    project = tmp_path / "app"
    (project / "pages").mkdir(parents=True)
    (project / "pages" / "index.pyxl").write_text("", encoding="utf-8")

    build_cache = json.dumps(
        {"schema_version": "1", "sources": {"index.pyxl": {"kind": "page", "hash": "h1"}}}
    )

    def _write_artifacts(root: Path, client_path: str, wrappers: list[dict] | None) -> None:
        page_metadata: dict[str, object] = {
            "route_path": "/",
            "client_path": client_path,
            "server_path": "/pages/index.py",
            "loader_name": "load_home",
        }
        if wrappers is not None:
            page_metadata["wrappers"] = wrappers
        metadata_file = root / "metadata" / "pages" / "index.json"
        metadata_file.parent.mkdir(parents=True, exist_ok=True)
        metadata_file.write_text(json.dumps(page_metadata), encoding="utf-8")

        server_file = root / "server" / "pages" / "index.py"
        server_file.parent.mkdir(parents=True, exist_ok=True)
        server_file.write_text("", encoding="utf-8")

        for relative in ("pages/index.jsx", "routes/index.jsx"):
            client_file = root / "client" / relative
            client_file.parent.mkdir(parents=True, exist_ok=True)
            client_file.write_text("", encoding="utf-8")

    dist = project / "dist"
    _write_artifacts(
        dist,
        dist_client_path,
        [{"kind": "layout", "client_path": "/pages/layout.jsx"}],
    )
    (dist / "public").mkdir(parents=True, exist_ok=True)
    (dist / "page-manifest.json").write_text("{}", encoding="utf-8")
    if dist_build_cache:
        (dist / "meta.json").write_text(build_cache, encoding="utf-8")

    if stale_client_path is not None:
        intermediate = project / ".pyxle-build"
        _write_artifacts(intermediate, stale_client_path, None)
        (intermediate / "meta.json").write_text(build_cache, encoding="utf-8")

    return project, dist


def _routes_from(monkeypatch, settings, dist) -> object:
    """Assemble the production app, returning the real route table it built."""
    captured: dict = {}

    def fake_create_app(_settings, routes, **_kwargs):
        captured["routes"] = routes
        return SimpleNamespace(state=SimpleNamespace(pyxle_ready=False))

    monkeypatch.setattr(production, "create_starlette_app", fake_create_app)
    monkeypatch.setattr("pyxle.ssr.worker_pool.SsrWorkerPool", lambda **kw: SimpleNamespace())
    build_production_app(settings, dist, serve_static=False)
    return captured["routes"]


def test_production_routes_use_dist_metadata_not_stale_intermediate_build(
    tmp_path: Path, monkeypatch
) -> None:
    """A rewritten ``.pyxle-build`` must not strip layouts from a built dist.

    The intermediate tree here claims the page's client module is the bare
    ``/pages/index.jsx``; ``dist`` — what was actually built and shipped — says
    it is the layout-composed ``/routes/index.jsx``. Reading the intermediate
    tree drops the layout from every rendered page while leaving the layout
    loader's data in the hydration payload, so the failure is invisible except
    in the markup.
    """
    project, dist = _write_built_project(
        tmp_path, stale_client_path="/pages/index.jsx"
    )
    settings = build_settings(project, ssr_workers=1)

    routes = _routes_from(monkeypatch, settings, dist)

    (page,) = routes.pages
    assert page.client_asset_path == "/routes/index.jsx"
    assert page.client_module_path == dist / "client" / "routes" / "index.jsx"
    assert page.server_module_path == dist / "server" / "pages" / "index.py"
    assert page.metadata_path == dist / "metadata" / "pages" / "index.json"


def test_production_serves_a_dist_without_any_intermediate_build_dir(
    tmp_path: Path, monkeypatch
) -> None:
    """``dist`` alone is a complete deployment.

    A Docker image or CI artifact that ships only ``dist`` has no
    ``.pyxle-build``; if routing is derived from that directory the server
    starts cleanly and 404s every single page.
    """
    project, dist = _write_built_project(tmp_path)
    assert not (project / ".pyxle-build").exists()
    settings = build_settings(project, ssr_workers=1)

    routes = _routes_from(monkeypatch, settings, dist)

    assert [page.path for page in routes.pages] == ["/"]


def test_production_falls_back_to_build_root_when_dist_predates_build_cache_copy(
    tmp_path: Path, monkeypatch
) -> None:
    """An older ``dist`` has no ``meta.json``; serve it, but say it is stale."""
    project, dist = _write_built_project(
        tmp_path, stale_client_path="/pages/index.jsx", dist_build_cache=False
    )
    settings = build_settings(project, ssr_workers=1)
    warnings: list[str] = []
    monkeypatch.setattr(production.ConsoleLogger, "warning", lambda _self, msg: warnings.append(msg))

    routes = _routes_from(monkeypatch, settings, dist)

    # Routing still comes from dist — only the source *list* is borrowed.
    (page,) = routes.pages
    assert page.client_asset_path == "/routes/index.jsx"
    assert any("meta.json" in message for message in warnings)


def test_rebase_settings_onto_dist_leaves_an_incomplete_dist_alone(tmp_path: Path) -> None:
    """Nothing to re-root onto when the dist has no compiled artifacts."""
    project, dist = _make_project(tmp_path)
    settings = build_settings(project)

    assert production.rebase_settings_onto_dist(settings, dist) is settings


# ── dist alone is a deployment: the app's own source ships inside it ─────────
#
# These build a *real* project and then delete the source tree, leaving only
# ``dist`` — the shape of a Docker ``COPY --from=build /app/dist ./dist``, a CI
# artifact download, or an rsync of the build output. Only Vite is stubbed (no
# Node in the test environment); everything else — the compiler, the build
# cache, the registry, the route table, the Starlette app — is real, because the
# fact under test is *which files a running server still needs*. The failure
# mode is not a 404 on one endpoint: the import happens while the route table is
# assembled, so the server does not start at all.

_SHARED_HELPER = 'GREETING = "hello from a colocated helper"\n'

_ENDPOINT = """from starlette.responses import JSONResponse

from pages.api._internal.store import ITEMS
from pages.api._shared import GREETING


async def endpoint(request):
    return JSONResponse({"greeting": GREETING, "items": ITEMS})
"""

_PAGE = """from pages.lib.data import TAGLINE


@server
async def load_home(request):
    return {"tagline": TAGLINE}

import React from 'react';

export default function Home({ data }) {
    return <p>{data.tagline}</p>;
}
"""


@pytest.fixture
def isolated_app_imports():
    """Undo the ``sys.path`` / ``sys.modules`` marks a served app leaves behind.

    Serving inserts the deployment's import roots and caches the app's modules
    under stable keys (``pyxle.server.api.hello``, ``pages.api._shared``); a
    later test in the same process would otherwise get another test's copy.
    """
    original_path = list(sys.path)
    yield
    sys.path[:] = original_path
    for name in list(sys.modules):
        if name == "pages" or name.startswith(("pages.", "pyxle.server.")):
            del sys.modules[name]


def _ship_dist_only(tmp_path: Path, monkeypatch, *, config: dict | None = None) -> Path:
    """Build a real project, then hand back a root holding *only* its ``dist``.

    The source tree is deleted afterwards, so anything the server still reads
    from it fails loudly instead of being silently found next door.
    """
    project = tmp_path / "project"
    (project / "pages" / "api" / "_internal").mkdir(parents=True)
    (project / "pages" / "lib").mkdir(parents=True)
    (project / "public").mkdir(parents=True)
    (project / "pages" / "api" / "_shared.py").write_text(_SHARED_HELPER, encoding="utf-8")
    (project / "pages" / "api" / "_internal" / "store.py").write_text(
        'ITEMS = ["a", "b"]\n', encoding="utf-8"
    )
    (project / "pages" / "api" / "hello.py").write_text(_ENDPOINT, encoding="utf-8")
    (project / "pages" / "lib" / "data.py").write_text(
        'TAGLINE = "from a helper beside the pages"\n', encoding="utf-8"
    )
    (project / "pages" / "index.pyxl").write_text(_PAGE, encoding="utf-8")

    style_entries = ((config or {}).get("styling") or {}).get("globalStyles", ())
    for entry in style_entries:
        source = project / entry
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text("body{--shipped:1}", encoding="utf-8")

    settings = DevServerSettings.from_project_root(
        project, debug=False, global_stylesheets=list(style_entries)
    )

    def fake_vite_build(*_args, **_kwargs) -> None:
        """Stand in for the bundler: drop the manifest Vite would have written.

        The only stub in this file's dist-only tests — a Node toolchain is not
        the subject. Everything that decides what lands in ``dist`` stays real.
        """
        manifest = settings.client_build_dir / "dist" / ".vite" / "manifest.json"
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text(
            json.dumps(
                {"pages/index.jsx": {"file": "assets/index-TEST.js", "imports": [], "css": []}}
            ),
            encoding="utf-8",
        )

    monkeypatch.setattr("pyxle.build.pipeline._run_npm_build", fake_vite_build)
    run_build(settings, dist_dir=project / "dist")

    deploy = tmp_path / "deploy"
    deploy.mkdir()
    shutil.copytree(project / "dist", deploy / "dist")
    (deploy / "pyxle.config.json").write_text(json.dumps(config or {}), encoding="utf-8")
    shutil.rmtree(project)
    return deploy


def _serve_dist_only(deploy: Path, monkeypatch) -> TestClient:
    """Assemble the production app for a deployment that is nothing but ``dist``."""
    monkeypatch.setattr("pyxle.ssr.worker_pool.SsrWorkerPool", lambda **kw: SimpleNamespace())
    settings = build_settings(deploy, ssr_workers=1)
    app, _ = build_production_app(settings, deploy / "dist", serve_static=False)
    # No ``with``: the lifespan would start the (stubbed) SSR pool, and these
    # assertions are about routing and imports, not rendering.
    return TestClient(app)


def test_dist_only_deployment_serves_an_endpoint_that_imports_a_colocated_helper(
    tmp_path: Path, monkeypatch, isolated_app_imports
) -> None:
    """The documented private-module pattern has to survive deployment.

    ``pages/api/_shared.py`` is deliberately not a route, so the compiler never
    emits it into ``dist/server`` — while ``dist/server/api/hello.py``, which
    says ``from pages.api._shared import GREETING``, is emitted. Ship ``dist``
    on its own and that import has nothing to resolve against.
    """
    deploy = _ship_dist_only(tmp_path, monkeypatch)

    response = _serve_dist_only(deploy, monkeypatch).get("/api/hello")

    assert response.status_code == 200
    assert response.json() == {"greeting": "hello from a colocated helper", "items": ["a", "b"]}


def test_dist_only_deployment_imports_a_helper_beside_a_page(
    tmp_path: Path, monkeypatch, isolated_app_imports
) -> None:
    """Same guarantee for pages: a ``@server`` loader importing a neighbour.

    Rendering needs Node, but the import that used to break happens earlier —
    when the compiled page module is executed.
    """
    from pyxle.ssr.view import _import_server_module

    deploy = _ship_dist_only(tmp_path, monkeypatch)
    _serve_dist_only(deploy, monkeypatch)  # puts the deployment's roots on sys.path

    module = _import_server_module(
        "pyxle.server.pages.index", deploy / "dist" / "server" / "pages" / "index.py"
    )

    assert module.TAGLINE == "from a helper beside the pages"


def test_dist_only_deployment_resolves_a_configured_global_stylesheet(
    tmp_path: Path, monkeypatch, isolated_app_imports
) -> None:
    """A configured global stylesheet is read on every render and inlined into
    the head — with only ``dist`` shipped, resolving it against the project root
    raises ``GlobalStyleConfigError`` and ``pyxle serve`` exits before binding."""
    deploy = _ship_dist_only(
        tmp_path, monkeypatch, config={"styling": {"globalStyles": ["styles/site.css"]}}
    )

    settings = build_settings(deploy, ssr_workers=1)

    (sheet,) = settings.global_stylesheets
    assert sheet.source_path == deploy / "dist" / "app" / "styles" / "site.css"
    assert sheet.source_path.read_text(encoding="utf-8") == "body{--shipped:1}"


def test_rebase_points_pages_dir_at_the_mirror_only_when_the_source_is_gone(
    tmp_path: Path, monkeypatch
) -> None:
    """``pages_dir`` is read at request time (``llms.py`` handlers, colocated
    ``.md``). The deployed source tree stays authoritative when it exists; the
    mirror stands in when it does not."""
    deploy = _ship_dist_only(tmp_path, monkeypatch)
    dist = deploy / "dist"
    settings = build_settings(deploy)

    rebased = production.rebase_settings_onto_dist(settings, dist)
    assert rebased.pages_dir == dist / "app" / "pages"

    # With the source deployed alongside dist, nothing is re-rooted.
    (deploy / "pages").mkdir()
    assert production.rebase_settings_onto_dist(settings, dist).pages_dir == settings.pages_dir


def test_app_source_mirror_is_none_for_a_dist_without_one(tmp_path: Path) -> None:
    """A dist built before the mirror existed keeps the old behaviour."""
    _, dist = _make_project(tmp_path)
    assert production.app_source_mirror(dist) is None


def test_resolve_global_assets_reads_a_script_from_the_mirror(tmp_path: Path) -> None:
    """Global scripts follow the same project-first, mirror-second rule."""
    project, dist = _make_project(tmp_path)
    shipped = dist / "app" / "scripts" / "analytics.js"
    shipped.parent.mkdir(parents=True)
    shipped.write_text("//noop", encoding="utf-8")

    _, scripts = production.resolve_global_assets(
        project,
        PyxleConfig(global_scripts=("scripts/analytics.js",)),
        app_mirror=production.app_source_mirror(dist),
    )

    assert [script.source_path for script in scripts] == [shipped]


def test_mirrored_pages_dir_is_none_without_a_mirror_or_inside_the_root(
    tmp_path: Path,
) -> None:
    """Both give the caller nothing to re-root onto."""
    project, dist = _make_project(tmp_path)
    settings = build_settings(project)
    settings = replace(settings, pages_dir=project / "missing")

    # No dist/app at all (an older build).
    assert production._mirrored_pages_dir(settings, dist) is None

    # A pagesDir outside the project root was never importable, so it was
    # never mirrored either.
    (dist / "app").mkdir()
    outside = replace(settings, pages_dir=tmp_path / "elsewhere" / "pages")
    assert production._mirrored_pages_dir(outside, dist) is None


# ── serve_worker_env ─────────────────────────────────────────────────────────


def test_serve_worker_env_minimal(tmp_path: Path) -> None:
    env = serve_worker_env(
        tmp_path,
        config_path=None,
        dist_dir=None,
        host=None,
        port=None,
        ssr_workers=None,
        serve_static=False,
    )
    assert env == {ENV_PROJECT_ROOT: str(tmp_path), ENV_SERVE_STATIC: "0"}


def test_serve_worker_env_full(tmp_path: Path) -> None:
    env = serve_worker_env(
        tmp_path,
        config_path=Path("custom.json"),
        dist_dir=Path("out"),
        host="0.0.0.0",
        port=9000,
        ssr_workers=4,
        serve_static=True,
    )
    assert env[ENV_PROJECT_ROOT] == str(tmp_path)
    assert env[ENV_CONFIG] == "custom.json"
    assert env[ENV_DIST] == "out"
    assert env[ENV_HOST] == "0.0.0.0"
    assert env[ENV_PORT] == "9000"
    assert env[ENV_SSR_WORKERS] == "4"
    assert env[ENV_SERVE_STATIC] == "1"


# ── create_app (the uvicorn factory) ─────────────────────────────────────────


def test_create_app_rebuilds_from_env(tmp_path: Path, monkeypatch) -> None:
    project, dist = _make_project(tmp_path)
    captured: dict = {}
    _stub_assembly(monkeypatch, captured)
    monkeypatch.setattr("pyxle.ssr.worker_pool.SsrWorkerPool", lambda **kw: SimpleNamespace())

    monkeypatch.setenv(ENV_PROJECT_ROOT, str(project))
    monkeypatch.setenv(ENV_DIST, str(dist))
    monkeypatch.setenv(ENV_HOST, "127.0.0.1")
    monkeypatch.setenv(ENV_PORT, "8123")
    monkeypatch.setenv(ENV_SSR_WORKERS, "1")
    monkeypatch.setenv(ENV_SERVE_STATIC, "1")

    app = create_app()

    assert app.state.pyxle_ready is True
    assert captured["settings"].starlette_host == "127.0.0.1"
    assert captured["settings"].starlette_port == 8123
    assert captured["settings"].ssr_workers == 1
    assert captured["serve_static"] is True


def test_create_app_honours_serve_static_env(tmp_path: Path, monkeypatch) -> None:
    project, dist = _make_project(tmp_path)
    captured: dict = {}
    _stub_assembly(monkeypatch, captured)
    monkeypatch.setattr("pyxle.ssr.worker_pool.SsrWorkerPool", lambda **kw: SimpleNamespace())

    # Only the required project-root var; everything else defaults.
    monkeypatch.setenv(ENV_PROJECT_ROOT, str(project))
    monkeypatch.delenv(ENV_DIST, raising=False)
    monkeypatch.setenv(ENV_SERVE_STATIC, "0")

    create_app()

    assert captured["serve_static"] is False
    assert captured["public_static_dir"] is None
