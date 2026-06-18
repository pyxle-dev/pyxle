import json
from pathlib import Path

from pyxle.build.pipeline import run_build
from pyxle.cli.logger import ConsoleLogger
from pyxle.devserver.builder import BuildSummary
from pyxle.devserver.registry import MetadataRegistry, PageRegistryEntry
from pyxle.devserver.settings import DevServerSettings


def silent_logger() -> ConsoleLogger:
    return ConsoleLogger(secho=lambda *args, **kwargs: None)


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
