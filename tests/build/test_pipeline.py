import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from pyxle.build.pipeline import _prepare_dist, run_build
from pyxle.cli.logger import ConsoleLogger
from pyxle.compiler.writers import CLIENT_SOURCEMAP_SIDECAR
from pyxle.devserver.builder import BuildSummary
from pyxle.devserver.registry import MetadataRegistry, PageRegistryEntry
from pyxle.devserver.settings import DevServerSettings


def silent_logger() -> ConsoleLogger:
    return ConsoleLogger(secho=lambda *args, **kwargs: None)


def test_prepare_dist_excludes_dev_only_sourcemap_sidecar(tmp_path: Path) -> None:
    """The debugger source-map sidecar is a dev-only artifact and must never be
    copied into the production ``dist/client`` (Studio + debugger are dev-only)."""
    project = tmp_path / "project"
    settings = DevServerSettings.from_project_root(project)
    client = settings.client_build_dir
    (client / "pages").mkdir(parents=True)
    # A real client asset alongside the dev-only sidecar.
    (client / "pages" / "index.jsx").write_text("export default 1;\n", encoding="utf-8")
    (client / CLIENT_SOURCEMAP_SIDECAR).write_text('{"pages/index.jsx": {}}', encoding="utf-8")

    dist = project / "dist"
    _prepare_dist(settings, dist)

    # Real client assets are copied…
    assert (dist / "client" / "pages" / "index.jsx").exists()
    # …but the dev-only sidecar is not.
    assert not (dist / "client" / CLIENT_SOURCEMAP_SIDECAR).exists()


def test_prepare_dist_copies_the_build_cache_index(tmp_path: Path) -> None:
    """``dist`` must carry ``meta.json``, the list of compiled sources.

    It is what ``pyxle serve`` iterates to rebuild its route registry, so
    without it a deployment shipping only ``dist`` has no routes at all.
    """
    project = tmp_path / "project"
    settings = DevServerSettings.from_project_root(project)
    settings.build_root.mkdir(parents=True)
    index = '{"schema_version": "1", "sources": {}}'
    (settings.build_root / "meta.json").write_text(index, encoding="utf-8")

    dist = project / "dist"
    _prepare_dist(settings, dist)

    assert (dist / "meta.json").read_text(encoding="utf-8") == index


def test_prepare_dist_mirrors_colocated_python_helpers(tmp_path: Path) -> None:
    """Helpers under ``pages/`` are not routes, so nothing compiles them into
    ``dist/server`` — but the compiled routes import them. They must ship."""
    project = tmp_path / "project"
    settings = DevServerSettings.from_project_root(project)
    api = settings.pages_dir / "api" / "_internal"
    api.mkdir(parents=True)
    (settings.pages_dir / "api" / "_shared.py").write_text("LIMIT = 50\n", encoding="utf-8")
    (settings.pages_dir / "api" / "__init__.py").write_text("", encoding="utf-8")
    (api / "db.py").write_text("DSN = 'x'\n", encoding="utf-8")
    (settings.pages_dir / "llms.py").write_text("def to_markdown(ctx): ...\n", encoding="utf-8")
    (settings.pages_dir / "index.md").write_text("# Home\n", encoding="utf-8")
    # Bytecode caches are build noise, never part of a deployment.
    cache = settings.pages_dir / "api" / "__pycache__"
    cache.mkdir()
    (cache / "_shared.cpython-312.pyc").write_bytes(b"\x00")

    dist = project / "dist"
    _prepare_dist(settings, dist)

    app_pages = dist / "app" / "pages"
    assert (app_pages / "api" / "_shared.py").read_text(encoding="utf-8") == "LIMIT = 50\n"
    assert (app_pages / "api" / "__init__.py").exists()
    assert (app_pages / "api" / "_internal" / "db.py").exists()
    assert (app_pages / "llms.py").exists()
    assert (app_pages / "index.md").exists()
    assert not (app_pages / "api" / "__pycache__").exists()


def test_prepare_dist_mirrors_configured_global_styles(tmp_path: Path) -> None:
    """A configured global stylesheet is read per render and inlined into the
    document head, so its *source* has to travel with the build."""
    project = tmp_path / "project"
    (project / "styles").mkdir(parents=True)
    (project / "styles" / "site.css").write_text("body{color:red}", encoding="utf-8")
    (project / "scripts").mkdir()
    (project / "scripts" / "analytics.js").write_text("//noop", encoding="utf-8")
    settings = DevServerSettings.from_project_root(
        project,
        global_stylesheets=["styles/site.css"],
        global_scripts=["scripts/analytics.js"],
    )

    dist = project / "dist"
    _prepare_dist(settings, dist)

    assert (dist / "app" / "styles" / "site.css").read_text(encoding="utf-8") == "body{color:red}"
    assert (dist / "app" / "scripts" / "analytics.js").exists()


def test_prepare_dist_skips_what_it_cannot_mirror(tmp_path: Path) -> None:
    """Two things are silently skipped rather than failing the build: a
    ``pagesDir`` outside the project root (never importable as ``pages.…``
    anyway, in development either), and a configured asset whose file is gone."""
    project = tmp_path / "project"
    (project / "styles").mkdir(parents=True)
    stylesheet = project / "styles" / "site.css"
    stylesheet.write_text("body{}", encoding="utf-8")
    outside = tmp_path / "outside-pages"
    outside.mkdir()
    (outside / "helper.py").write_text("", encoding="utf-8")
    settings = DevServerSettings.from_project_root(
        project, pages_dir="../outside-pages", global_stylesheets=["styles/site.css"]
    )
    stylesheet.unlink()

    dist = project / "dist"
    _prepare_dist(settings, dist)

    assert not (dist / "app" / "styles" / "site.css").exists()
    assert list((dist / "app").rglob("helper.py")) == []


def test_prepare_dist_rewrites_the_mirror_from_scratch(tmp_path: Path) -> None:
    """A helper deleted from ``pages/`` must not linger in a rebuilt dist."""
    project = tmp_path / "project"
    settings = DevServerSettings.from_project_root(project)
    settings.pages_dir.mkdir(parents=True)
    (settings.pages_dir / "keep.py").write_text("", encoding="utf-8")
    dist = project / "dist"
    stale = dist / "app" / "pages" / "gone.py"
    stale.parent.mkdir(parents=True)
    stale.write_text("", encoding="utf-8")

    _prepare_dist(settings, dist)

    assert (dist / "app" / "pages" / "keep.py").exists()
    assert not stale.exists()


def _page_entry(tmp_path: Path, **overrides) -> PageRegistryEntry:
    base = dict(
        route_path="/chat/{room}",
        alternate_route_paths=(),
        source_relative_path=Path("pages/chat/[room].pyxl"),
        source_absolute_path=tmp_path / "pages" / "chat" / "[room].pyxl",
        server_module_path=tmp_path / "server" / "chat" / "[room].py",
        client_module_path=tmp_path / "client" / "chat" / "[room].jsx",
        metadata_path=tmp_path / "metadata" / "chat" / "[room].json",
        client_asset_path="/pages/chat/[room].jsx",
        server_asset_path="server/pages/chat/[room].py",
        module_key="pyxle.server.pages.chat.room",
        content_hash="h",
        loader_name=None,
        loader_line=None,
        head_elements=(),
        head_is_dynamic=False,
    )
    base.update(overrides)
    return PageRegistryEntry(**base)


def test_build_page_manifest_emits_websocket(tmp_path) -> None:
    """A page with a websocket handler gets a ``websocket`` entry in the
    page-manifest, so production tooling/parity sees it on disk."""
    from pyxle.build.pipeline import _build_page_manifest

    settings = DevServerSettings.from_project_root(tmp_path)
    registry = MetadataRegistry(
        pages=[_page_entry(tmp_path, websocket_name="websocket", websocket_line=3)],
        apis=[],
    )
    manifest = _build_page_manifest(settings, registry)
    assert manifest["/chat/{room}"]["websocket"] == {"name": "websocket", "line": 3}


def test_build_page_manifest_omits_websocket_when_absent(tmp_path) -> None:
    from pyxle.build.pipeline import _build_page_manifest

    settings = DevServerSettings.from_project_root(tmp_path)
    registry = MetadataRegistry(pages=[_page_entry(tmp_path)], apis=[])
    manifest = _build_page_manifest(settings, registry)
    assert "websocket" not in manifest["/chat/{room}"]


def _write_vite_manifest(settings: DevServerSettings, payload: dict) -> Path:
    """Place a Vite manifest where ``_load_vite_manifest`` / ``_copy_client_manifest``
    expect it (``<client_build_dir>/dist/.vite/manifest.json``)."""

    manifest_path = settings.client_build_dir / "dist" / ".vite" / "manifest.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")
    return manifest_path


def test_run_build_invokes_vite_and_copies_artifacts(monkeypatch, tmp_path):
    project = tmp_path / "project"
    pages_dir = project / "pages"
    public_dir = project / "public"
    build_root = project / ".pyxle-build"
    server_build = build_root / "server" / "pages"
    metadata_build = build_root / "metadata" / "pages"

    for path in (pages_dir, public_dir, server_build, metadata_build):
        path.mkdir(parents=True, exist_ok=True)

    (server_build / "index.py").write_text("print('server')\n", encoding="utf-8")
    (metadata_build / "index.json").write_text("{}", encoding="utf-8")
    (public_dir / "robots.txt").write_text("User-agent: *\n", encoding="utf-8")

    settings = DevServerSettings.from_project_root(project)

    # The Vite step is mocked out (no Node toolchain in tests); instead we drop
    # the manifest it would have produced so the real ``_load_vite_manifest``
    # and ``_build_page_manifest`` run against it.
    _write_vite_manifest(
        settings,
        {"pages/index.jsx": {"file": "assets/index-DEADBEEF.js", "imports": []}},
    )

    summary = BuildSummary(compiled_pages=["pages/index.pyxl"])
    registry = MetadataRegistry(
        pages=[
            PageRegistryEntry(
                route_path="/",
                alternate_route_paths=(),
                source_relative_path=Path("pages/index.pyxl"),
                source_absolute_path=pages_dir / "index.pyxl",
                server_module_path=server_build / "index.py",
                client_module_path=settings.client_build_dir / "pages" / "index.jsx",
                metadata_path=metadata_build / "index.json",
                client_asset_path="/pages/index.jsx",
                server_asset_path="server/pages/index.py",
                module_key="pyxle.server.pages.index",
                content_hash="hash123",
                loader_name=None,
                loader_line=None,
                head_elements=(),
                head_is_dynamic=False,
            )
        ],
        apis=[],
    )

    captured: dict[str, object] = {}

    def fake_build_once(settings_arg, *, force_rebuild):
        captured["force_rebuild"] = force_rebuild
        return summary

    def fake_build_metadata_registry(settings_arg):
        captured["registry_settings"] = settings_arg
        return registry

    def fake_run_npm_build(project_root, logger, *, settings):
        captured["npm_project_root"] = project_root

    monkeypatch.setattr("pyxle.build.pipeline.build_once", fake_build_once)
    monkeypatch.setattr(
        "pyxle.build.pipeline.build_metadata_registry", fake_build_metadata_registry
    )
    monkeypatch.setattr("pyxle.build.pipeline._run_npm_build", fake_run_npm_build)

    result = run_build(settings, logger=silent_logger())

    assert captured["force_rebuild"] is True
    assert captured["registry_settings"] == settings
    assert captured["npm_project_root"] == project

    dist_root = result.dist_dir
    assert (dist_root / "server" / "pages" / "index.py").exists()
    assert (dist_root / "metadata" / "pages" / "index.json").exists()
    assert (dist_root / "public" / "robots.txt").exists()
    assert (dist_root / "client" / "manifest.json").exists()

    assert result.page_manifest == {
        "/": {
            "client": {
                "file": "dist/assets/index-DEADBEEF.js",
                "imports": [],
                "css": [],
            },
            "server": {
                "file": "server/pages/index.py",
                "module_key": "pyxle.server.pages.index",
            },
        }
    }
    assert result.page_manifest_path == dist_root / "page-manifest.json"
    assert (
        json.loads(result.page_manifest_path.read_text(encoding="utf-8"))
        == result.page_manifest
    )


def test_run_build_supports_incremental_mode(monkeypatch, tmp_path):
    project = tmp_path / "project"
    pages_dir = project / "pages"
    public_dir = project / "public"
    build_root = project / ".pyxle-build"
    server_build = build_root / "server" / "pages"
    metadata_build = build_root / "metadata" / "pages"

    for path in (pages_dir, public_dir, server_build, metadata_build):
        path.mkdir(parents=True, exist_ok=True)

    (server_build / "index.py").write_text("print('server')\n", encoding="utf-8")
    (metadata_build / "index.json").write_text("{}", encoding="utf-8")

    settings = DevServerSettings.from_project_root(project)
    _write_vite_manifest(settings, {})

    summary = BuildSummary()
    registry = MetadataRegistry(pages=[], apis=[])

    captured: dict[str, object] = {}

    def fake_build_once(settings_arg, *, force_rebuild):
        captured["force_rebuild"] = force_rebuild
        return summary

    monkeypatch.setattr("pyxle.build.pipeline.build_once", fake_build_once)
    monkeypatch.setattr(
        "pyxle.build.pipeline.build_metadata_registry", lambda settings_arg: registry
    )
    monkeypatch.setattr(
        "pyxle.build.pipeline._run_npm_build",
        lambda project_root, logger, *, settings: None,
    )

    result = run_build(settings, logger=silent_logger(), force_rebuild=False)

    assert captured["force_rebuild"] is False
    assert result.page_manifest == {}
    assert result.client_manifest_path == result.client_dir / "manifest.json"


def test_collect_js_imports_walks_import_chain() -> None:
    from pyxle.build.pipeline import _collect_js_imports_from_vite_entry

    manifest = {
        "pages/index.jsx": {
            "file": "assets/index-A.js",
            "imports": ["_vendor-B.js", "_shared-C.js"],
        },
        "_vendor-B.js": {"file": "assets/vendor-B.js", "imports": ["_react-D.js"]},
        "_shared-C.js": {"file": "assets/shared-C.js", "imports": []},
        "_react-D.js": {"file": "assets/react-D.js"},
    }
    result = _collect_js_imports_from_vite_entry(manifest, "pages/index.jsx")
    # The entry's own file is excluded; imported chunks (transitive) included once.
    assert "assets/index-A.js" not in result
    assert result == ["assets/vendor-B.js", "assets/shared-C.js", "assets/react-D.js"]


def test_collect_js_imports_empty_for_no_imports_or_unknown() -> None:
    from pyxle.build.pipeline import _collect_js_imports_from_vite_entry

    assert _collect_js_imports_from_vite_entry({}, "missing") == []
    assert _collect_js_imports_from_vite_entry({"e": {"file": "e.js"}}, "e") == []


def test_build_page_manifest_populates_js_imports(tmp_path) -> None:
    from pyxle.build.pipeline import _build_page_manifest

    settings = DevServerSettings.from_project_root(tmp_path)
    page = _page_entry(
        tmp_path, route_path="/", client_asset_path="/pages/index.jsx"
    )
    registry = MetadataRegistry(pages=[page], apis=[])
    vite_manifest = {
        "pages/index.jsx": {
            "file": "assets/index-A.js",
            "imports": ["_vendor-B.js"],
            "css": [],
        },
        "_vendor-B.js": {"file": "assets/vendor-B.js"},
    }
    manifest = _build_page_manifest(settings, registry, vite_manifest=vite_manifest)
    client = manifest["/"]["client"]
    assert client["file"] == "dist/assets/index-A.js"
    assert client["imports"] == ["dist/assets/vendor-B.js"]


def _npm_build_project(tmp_path: Path, scripts: dict) -> tuple[Path, DevServerSettings]:
    project_root = tmp_path / "project"
    client_build_dir = project_root / ".pyxle-build" / "client"
    client_build_dir.mkdir(parents=True)
    (client_build_dir / "vite.config.js").write_text("export default {}\n", encoding="utf-8")
    (project_root / "package.json").write_text(
        json.dumps({"scripts": scripts}), encoding="utf-8"
    )
    return project_root, DevServerSettings.from_project_root(project_root)


def test_run_npm_build_skips_build_css_when_not_declared(monkeypatch, tmp_path):
    """A modern scaffold (Vite-managed CSS, no ``build:css`` script) must not
    invoke ``build:css`` — doing so exits non-zero and would log a misleading
    'script failed' warning on every build."""
    from pyxle.build import pipeline

    project_root, settings = _npm_build_project(tmp_path, {"build": "vite build"})

    called: list[str] = []
    monkeypatch.setattr(
        pipeline,
        "_run_npm_script",
        lambda root, script, logger, *, required=True: called.append(script),
    )
    monkeypatch.setattr(
        pipeline.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(returncode=0, stdout="", stderr=""),
    )

    messages: list[str] = []
    logger = ConsoleLogger(secho=lambda msg, **_: messages.append(str(msg)))
    pipeline._run_npm_build(project_root, logger, settings=settings)

    assert "build:css" not in called
    assert not any("build:css" in m for m in messages)


def test_run_npm_build_runs_build_css_when_declared(monkeypatch, tmp_path):
    """The legacy Tailwind-v3 path (a declared ``build:css`` script, no PostCSS)
    still runs ``build:css`` before the Vite build."""
    from pyxle.build import pipeline

    project_root, settings = _npm_build_project(
        tmp_path, {"build:css": "tailwindcss -i in.css -o out.css"}
    )

    called: list[str] = []
    monkeypatch.setattr(
        pipeline,
        "_run_npm_script",
        lambda root, script, logger, *, required=True: called.append(script),
    )
    monkeypatch.setattr(
        pipeline.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(returncode=0, stdout="", stderr=""),
    )

    pipeline._run_npm_build(project_root, silent_logger(), settings=settings)

    assert "build:css" in called


def test_run_npm_build_fails_when_npx_is_missing(monkeypatch, tmp_path):
    """A build that cannot reach npx must **stop**, not warn.

    Warning and carrying on produced a green "Build completed" banner over a
    ``dist/`` with no browser bundle, whose page manifest ``pyxle serve`` then
    rejected as an unsafe path — so the real problem (no Node toolchain)
    surfaced at deploy time, described as a path-safety error.
    """
    from pyxle.build import pipeline

    project_root, settings = _npm_build_project(tmp_path, {"build": "vite build"})

    def _no_npx(*args, **kwargs):
        raise FileNotFoundError(2, "No such file or directory", "npx")

    monkeypatch.setattr(pipeline.subprocess, "run", _no_npx)

    with pytest.raises(pipeline.ClientBuildError) as excinfo:
        pipeline._run_npm_build(project_root, silent_logger(), settings=settings)

    message = str(excinfo.value)
    # Names the missing prerequisite, the consequence, and the exact remedy.
    assert "npx was not found on your PATH" in message
    assert "`pyxle serve` refuses to start" in message
    assert "install npm alongside Node.js" in message
    assert "apt install npm" in message


def test_run_npm_build_fails_when_package_json_is_missing(tmp_path):
    """The other door to a bundle-less build: no ``package.json`` means Vite is
    never invoked, which used to warn-and-continue into the same broken dist."""
    from pyxle.build import pipeline

    project_root = tmp_path / "project"
    project_root.mkdir()
    settings = DevServerSettings.from_project_root(project_root)

    with pytest.raises(pipeline.ClientBuildError) as excinfo:
        pipeline._run_npm_build(project_root, silent_logger(), settings=settings)

    message = str(excinfo.value)
    assert "No package.json" in message
    assert str(project_root) in message
    assert "npm install" in message


def test_run_build_refuses_to_write_a_dist_that_cannot_be_served(monkeypatch, tmp_path):
    """The backstop, independent of *why* the bundle is missing.

    A page with no entry in the Vite manifest keeps its leading-slash dev asset
    path, which ``load_manifest`` rejects. Applying that same rule at build time
    — before ``dist/`` is touched — is what makes "the build succeeded" and "the
    build is servable" the same statement.
    """
    from pyxle.build import pipeline

    project = tmp_path / "project"
    (project / "pages").mkdir(parents=True)
    settings = DevServerSettings.from_project_root(project)

    registry = MetadataRegistry(
        pages=[
            PageRegistryEntry(
                route_path="/",
                alternate_route_paths=(),
                source_relative_path=Path("pages/index.pyxl"),
                source_absolute_path=project / "pages" / "index.pyxl",
                server_module_path=settings.server_build_dir / "index.py",
                client_module_path=settings.client_build_dir / "pages" / "index.jsx",
                metadata_path=settings.metadata_build_dir / "index.json",
                client_asset_path="/pages/index.jsx",
                server_asset_path="server/pages/index.py",
                module_key="pyxle.server.pages.index",
                content_hash="hash123",
                loader_name=None,
                loader_line=None,
                head_elements=(),
                head_is_dynamic=False,
            )
        ],
        apis=[],
    )

    # Vite "ran" but left no manifest — the shape a silently-skipped client
    # build produces. No ``_write_vite_manifest`` call here, deliberately.
    monkeypatch.setattr(
        "pyxle.build.pipeline.build_once", lambda s, *, force_rebuild: BuildSummary()
    )
    monkeypatch.setattr(
        "pyxle.build.pipeline.build_metadata_registry", lambda s: registry
    )
    monkeypatch.setattr(
        "pyxle.build.pipeline._run_npm_build", lambda root, logger, *, settings: None
    )

    with pytest.raises(pipeline.ClientBuildError) as excinfo:
        run_build(settings, logger=silent_logger())

    message = str(excinfo.value)
    assert "produced no browser bundle" in message
    assert "/pages/index.jsx" in message

    # Nothing unservable was written: the previous deployment survives a failed
    # rebuild rather than being replaced by a broken one.
    assert not (project / "dist").exists()
