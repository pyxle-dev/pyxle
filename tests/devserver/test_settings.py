from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from pyxle.cli.logger import ConsoleLogger
from pyxle.devserver import DevServer
from pyxle.devserver.builder import BuildSummary
from pyxle.devserver.registry import MetadataRegistry
from pyxle.devserver.routes import RouteTable
from pyxle.devserver.scripts import resolve_global_scripts
from pyxle.devserver.settings import DevServerSettings
from pyxle.devserver.styles import resolve_global_stylesheets
from pyxle.devserver.watcher import WatcherStatistics


def test_settings_from_project_root_resolves_paths(tmp_path: Path) -> None:
    project = tmp_path / "my-app"
    project.mkdir()

    settings = DevServerSettings.from_project_root(project)

    assert settings.project_root == project.resolve()
    assert settings.pages_dir == project / "pages"
    assert settings.public_dir == project / "public"
    assert settings.build_root == project / ".pyxle-build"
    assert settings.client_build_dir == settings.build_root / "client"
    assert settings.server_build_dir == settings.build_root / "server"
    assert settings.metadata_build_dir == settings.build_root / "metadata"
    assert settings.starlette_port == 8000
    assert settings.vite_port == 5173
    assert settings.debug is True
    assert settings.custom_middlewares == ()
    assert settings.page_route_hooks == ()
    assert settings.api_route_hooks == ()


def test_settings_support_custom_directory_names(tmp_path: Path) -> None:
    project = tmp_path / "custom"
    project.mkdir()

    settings = DevServerSettings.from_project_root(
        project,
        pages_dir="src/pages",
        public_dir="static",
        build_dir=".cache",
        starlette_port=9000,
        vite_port=6000,
        debug=False,
        custom_middlewares=["tests.devserver.sample_middlewares:HeaderCaptureMiddleware"],
        page_route_hooks=("tests.devserver.sample_middlewares:record_route_hook",),
        api_route_hooks=("tests.devserver.sample_middlewares:build_target_hook",),
    )

    assert settings.pages_dir == (project / "src/pages").resolve()
    assert settings.public_dir == (project / "static").resolve()
    assert settings.build_root == project / ".cache"
    assert settings.client_build_dir == settings.build_root / "client"
    assert settings.starlette_port == 9000
    assert settings.vite_port == 6000
    assert settings.debug is False
    assert settings.custom_middlewares == ("tests.devserver.sample_middlewares:HeaderCaptureMiddleware",)
    assert settings.page_route_hooks == ("tests.devserver.sample_middlewares:record_route_hook",)
    assert settings.api_route_hooks == ("tests.devserver.sample_middlewares:build_target_hook",)


def test_settings_to_dict_round_trip(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    settings = DevServerSettings.from_project_root(project)

    payload = settings.to_dict()
    assert payload["project_root"] == str(project.resolve())
    assert payload["starlette_port"] == 8000
    assert payload["debug"] is True
    assert payload["client_build_dir"].endswith("client")
    assert payload["custom_middlewares"] == []
    assert payload["page_route_hooks"] == []
    assert payload["api_route_hooks"] == []


def test_settings_accept_pre_resolved_global_assets(tmp_path: Path) -> None:
    root = tmp_path / "assets-project"
    (root / "pages").mkdir(parents=True)
    (root / "public").mkdir()
    style_path = root / "styles" / "global.css"
    style_path.parent.mkdir(parents=True, exist_ok=True)
    style_path.write_text("body { color: black; }\n", encoding="utf-8")
    script_path = root / "scripts" / "analytics.js"
    script_path.parent.mkdir(parents=True, exist_ok=True)
    script_path.write_text("console.log('analytics');\n", encoding="utf-8")

    styles = resolve_global_stylesheets(root, ("styles/global.css",))
    scripts = resolve_global_scripts(root, ("scripts/analytics.js",))

    settings = DevServerSettings.from_project_root(
        root,
        global_stylesheets=styles,
        global_scripts=scripts,
    )

    assert settings.global_stylesheets == styles
    assert settings.global_scripts == scripts


@pytest.mark.parametrize("project_root", [".", Path(".")])
def test_from_project_root_accepts_str_and_path(project_root: Path | str) -> None:
    settings = DevServerSettings.from_project_root(project_root)
    assert isinstance(settings.project_root, Path)
    assert settings.project_root.exists()


def test_devserver_start_runs_with_stubbed_uvicorn(monkeypatch, tmp_path: Path) -> None:
    settings = DevServerSettings.from_project_root(tmp_path)
    capture: list[str] = []
    logger = ConsoleLogger(secho=lambda message, fg=None, bold=False: capture.append(message))

    def fake_build_once(config: DevServerSettings, *, force_rebuild: bool = False) -> BuildSummary:
        return BuildSummary(compiled_pages=["pages/index.pyxl"], copied_api_modules=[], removed=[])

    monkeypatch.setattr("pyxle.devserver.build_once", fake_build_once)
    monkeypatch.setattr(
        "pyxle.devserver.build_metadata_registry",
        lambda cfg: MetadataRegistry(pages=[], apis=[]),
    )
    monkeypatch.setattr(
        "pyxle.devserver.build_route_table",
        lambda registry: RouteTable(pages=[], apis=[]),
    )

    overlay_calls: list[list[str]] = []

    class StubOverlay:
        async def notify_reload(self, *, changed_paths: list[str]) -> None:
            overlay_calls.append(changed_paths)

    sentinel_app = SimpleNamespace(state=SimpleNamespace(overlay=StubOverlay()))
    monkeypatch.setattr(
        "pyxle.devserver.create_starlette_app",
        lambda cfg, routes, **_: sentinel_app,
    )

    watcher_instances: list[object] = []

    class StubWatcher:
        def __init__(
            self,
            cfg: DevServerSettings,
            *,
            logger: ConsoleLogger,
            on_rebuild,
            **_: object,
        ) -> None:
            self.started = False
            self.closed = False
            self._on_rebuild = on_rebuild
            self._cfg = cfg
            watcher_instances.append(self)

        def start(self) -> None:
            self.started = True
            stats = BuildSummary(compiled_pages=["pages/index.pyxl"], copied_api_modules=[], removed=[])
            self._on_rebuild(
                WatcherStatistics(
                    elapsed_seconds=0.1,
                    summary=stats,
                    error=None,
                    changed_paths=[self._cfg.pages_dir / "pages" / "index.pyxl"],
                )
            )

        def close(self) -> None:
            self.closed = True

    monkeypatch.setattr("pyxle.devserver.ProjectWatcher", StubWatcher)

    class StubConfig:
        def __init__(self, app: object, **kwargs: object) -> None:
            self.app = app
            self.kwargs = kwargs

    class StubServer:
        def __init__(self, config: StubConfig) -> None:
            self.config = config

        async def serve(self) -> None:
            return None

    monkeypatch.setattr("pyxle.devserver.uvicorn.Config", StubConfig)
    monkeypatch.setattr("pyxle.devserver.uvicorn.Server", StubServer)

    class StubVite:
        def __init__(self, cfg: DevServerSettings, *, logger: ConsoleLogger, **_: object) -> None:
            self.started = False
            self.ready = False
            self.stopped = False

        async def start(self) -> None:
            self.started = True

        async def wait_until_ready(self) -> None:
            self.ready = True

        async def stop(self) -> None:
            self.stopped = True

    monkeypatch.setattr("pyxle.devserver.ViteProcess", StubVite)

    monkeypatch.setattr(
        "pyxle.devserver.asyncio.run_coroutine_threadsafe",
        lambda coro, loop: loop.create_task(coro),
    )

    asyncio.run(DevServer(settings=settings, logger=logger).start())

    assert watcher_instances and watcher_instances[0].started is True
    assert watcher_instances[0].closed is True
    # The curated startup summary is always visible; the "Starting Starlette"
    # line moved to debug (verbose-only).
    assert any("Pyxle dev server ready" in message for message in capture)
    assert overlay_calls and overlay_calls[0]
    assert overlay_calls[0][0].endswith("pages/index.pyxl")
