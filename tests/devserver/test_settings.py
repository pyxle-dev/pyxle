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


def test_settings_resolve_dev_watch_dirs(tmp_path: Path) -> None:
    """dev_watch entries resolve to absolute dirs under the root; dev_ignore
    globs are stored verbatim."""
    root = tmp_path / "app"
    (root / "pages").mkdir(parents=True)
    (root / "public").mkdir()

    settings = DevServerSettings.from_project_root(
        root,
        dev_watch=("lib", "components"),
        dev_ignore=("pages/generated/*",),
    )

    assert settings.dev_watch_dirs == ((root / "lib").resolve(), (root / "components").resolve())
    assert settings.dev_ignore_globs == ("pages/generated/*",)


def test_settings_dev_watch_drops_out_of_bounds_and_dedupes(tmp_path: Path) -> None:
    """Defence in depth: a direct caller passing a traversal path has it dropped,
    and duplicate entries collapse to one."""
    root = tmp_path / "app"
    (root / "pages").mkdir(parents=True)
    (root / "public").mkdir()

    settings = DevServerSettings.from_project_root(
        root,
        dev_watch=("lib", "lib", "../escape"),
    )

    # Only the in-bounds directory survives, and it appears once.
    assert settings.dev_watch_dirs == ((root / "lib").resolve(),)


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

        async def notify_clear(self, *, route_path: str) -> None:
            """A clean rebuild retracts the sticky build-failure overlay."""

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
            self.should_exit = False
            self.started = False

        async def serve(self) -> None:
            # Real uvicorn flips `started` at the end of its startup sequence
            # and then serves; the readiness banner waits for exactly that, so
            # a stub returning instantly would be torn down before it fired.
            self.started = True
            for _ in range(10):
                await asyncio.sleep(0)

    monkeypatch.setattr("pyxle.devserver.uvicorn.Config", StubConfig)
    monkeypatch.setattr("pyxle.devserver.uvicorn.Server", StubServer)

    class StubVite:
        # A Vite process that started and became ready is running.
        running = True
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


def _run_dev_server_with_stats(
    monkeypatch, tmp_path: Path, stats: WatcherStatistics, *, build_app_routes=None
):
    """Boot ``DevServer.start`` against stubs and feed it one rebuild result.

    Returns ``(refresh_calls, messages)`` — how many times the live route-table
    refresh ran, and everything the logger was asked to print. The refresh is
    what re-imports endpoint modules, so "did it run?" is the whole question
    for a change that produces no build artifact.
    """
    settings = DevServerSettings.from_project_root(tmp_path)
    messages: list[str] = []
    logger = ConsoleLogger(secho=lambda message, fg=None, bold=False: messages.append(message))

    monkeypatch.setattr(
        "pyxle.devserver.build_once",
        lambda cfg, *, force_rebuild=False: BuildSummary(),
    )
    monkeypatch.setattr(
        "pyxle.devserver.build_metadata_registry",
        lambda cfg: MetadataRegistry(pages=[], apis=[]),
    )
    monkeypatch.setattr(
        "pyxle.devserver.build_route_table",
        lambda registry: RouteTable(pages=[], apis=[]),
    )

    refresh_calls: list[object] = []

    def fake_build_app_routes(**kwargs):
        refresh_calls.append(kwargs)
        if build_app_routes is not None:
            return build_app_routes(**kwargs)
        return [], None

    monkeypatch.setattr(
        "pyxle.devserver.starlette_app._build_app_routes", fake_build_app_routes
    )

    class StubOverlay:
        async def notify_reload(self, *, changed_paths: list[str]) -> None:
            """Connected browsers are told to reload."""

        async def notify_clear(self, *, route_path: str) -> None:
            """A clean pass retracts the sticky build-failure overlay."""

    class StubRenderer:
        def clear(self) -> None:
            """The SSR bundle cache is dropped when routes are swapped."""

    sentinel_app = SimpleNamespace(
        state=SimpleNamespace(
            overlay=StubOverlay(),
            ssr_renderer=StubRenderer(),
            pyxle_route_hooks=((), (), ()),
            pyxle_build_failures=None,
        ),
        router=SimpleNamespace(routes=[]),
    )
    monkeypatch.setattr(
        "pyxle.devserver.create_starlette_app",
        lambda cfg, routes, **_: sentinel_app,
    )

    class StubWatcher:
        def __init__(self, cfg, *, logger, on_rebuild, **_: object) -> None:
            self._on_rebuild = on_rebuild

        def start(self) -> None:
            self._on_rebuild(stats)

        def close(self) -> None:
            """Nothing to tear down."""

    monkeypatch.setattr("pyxle.devserver.ProjectWatcher", StubWatcher)

    class StubConfig:
        def __init__(self, app: object, **kwargs: object) -> None:
            self.app = app

    class StubServer:
        def __init__(self, config: StubConfig) -> None:
            self.should_exit = False
            self.started = False

        async def serve(self) -> None:
            self.started = True
            for _ in range(10):
                await asyncio.sleep(0)

    monkeypatch.setattr("pyxle.devserver.uvicorn.Config", StubConfig)
    monkeypatch.setattr("pyxle.devserver.uvicorn.Server", StubServer)

    class StubVite:
        running = True

        def __init__(self, cfg, *, logger, **_: object) -> None:
            pass

        async def start(self) -> None:
            """Vite is already up."""

        async def wait_until_ready(self) -> None:
            """Vite is already ready."""

        async def stop(self) -> None:
            """Nothing to stop."""

    monkeypatch.setattr("pyxle.devserver.ViteProcess", StubVite)
    monkeypatch.setattr(
        "pyxle.devserver.asyncio.run_coroutine_threadsafe",
        lambda coro, loop: loop.create_task(coro),
    )

    asyncio.run(DevServer(settings=settings, logger=logger).start())
    return refresh_calls, messages


def test_helper_module_edit_refreshes_the_route_table(monkeypatch, tmp_path: Path) -> None:
    """A helper edit must re-import the endpoints that import it.

    ``pages/api/_shared.py`` is not a build artifact — the scanner skips it, so
    the pass reports no changes at all. The listener used to read that as
    "nothing happened" and skip the route-table refresh, which is the step that
    re-imports endpoint modules. The endpoint then served the helper's old
    values indefinitely, with no rebuild line and nothing to restart.
    """
    stats = WatcherStatistics(
        elapsed_seconds=0.01,
        summary=BuildSummary(),
        error=None,
        changed_paths=[tmp_path / "pages" / "api" / "_shared.py"],
        purged_modules=("pages.api._shared",),
    )

    refresh_calls, _ = _run_dev_server_with_stats(monkeypatch, tmp_path, stats)

    assert len(refresh_calls) == 1


def test_a_pass_that_changed_nothing_skips_the_route_table_refresh(
    monkeypatch, tmp_path: Path
) -> None:
    """The refresh is not free — a genuinely empty pass must not trigger it."""
    stats = WatcherStatistics(
        elapsed_seconds=0.01,
        summary=BuildSummary(),
        error=None,
        changed_paths=[tmp_path / "pages" / "styles" / "app.css"],
    )

    refresh_calls, _ = _run_dev_server_with_stats(monkeypatch, tmp_path, stats)

    assert refresh_calls == []


def test_a_failed_route_refresh_says_what_is_still_serving(
    monkeypatch, tmp_path: Path
) -> None:
    """Telling a developer to restart overstates it: the next change recovers."""

    def explode(**kwargs):
        raise RuntimeError("Failed to import API module: unexpected indent")

    stats = WatcherStatistics(
        elapsed_seconds=0.01,
        summary=BuildSummary(compiled_pages=["index.pyxl"]),
        error=None,
        changed_paths=[tmp_path / "pages" / "index.pyxl"],
    )

    _, messages = _run_dev_server_with_stats(
        monkeypatch, tmp_path, stats, build_app_routes=explode
    )

    warning = next(m for m in messages if "Route table not refreshed" in m)
    assert "still serving" in warning
    assert "unexpected indent" in warning
    assert "restart" not in warning.lower()
