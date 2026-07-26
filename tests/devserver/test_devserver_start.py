from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest
from starlette.applications import Starlette
from starlette.responses import HTMLResponse
from starlette.testclient import TestClient

from pyxle.cli.logger import ConsoleLogger, Verbosity
from pyxle.devserver import (
    DevServer,
    DevServerSettings,
    _maybe_schedule_reload,
    _notify_rebuild_error,
    _set_app_ready_flag,
)
from pyxle.devserver.builder import BuildSummary
from pyxle.devserver.registry import MetadataRegistry
from pyxle.devserver.routes import RouteTable
from pyxle.devserver.watcher import WatcherStatistics

pytestmark = pytest.mark.anyio("asyncio")


@pytest.fixture
def anyio_backend() -> str:  # pragma: no cover - fixture wiring
    return "asyncio"


class LogCapture:
    def __init__(self) -> None:
        self.messages: List[str] = []

    def __call__(self, message: str, fg: str | None = None, bold: bool = False) -> None:  # pragma: no cover - formatting only
        self.messages.append(message)


async def test_devserver_start_configures_uvicorn_and_watcher(anyio_backend, monkeypatch, tmp_path: Path) -> None:
    settings = DevServerSettings.from_project_root(tmp_path)
    build_calls: List[Dict[str, Any]] = []
    watcher_instances: List["StubWatcher"] = []
    captured_config: Dict[str, Any] = {}
    server_state: Dict[str, Any] = {}
    capture = LogCapture()
    logger = ConsoleLogger(secho=capture)
    bootstrap_calls: List[DevServerSettings] = []
    vite_instances: List["StubVite"] = []

    def fake_build_once(config: DevServerSettings, *, force_rebuild: bool = False) -> BuildSummary:
        build_calls.append({"settings": config, "force": force_rebuild})
        return BuildSummary(compiled_pages=["pages/index.pyxl"], copied_api_modules=["api/pulse.py"], removed=[])

    monkeypatch.setattr("pyxle.devserver.build_once", fake_build_once)
    monkeypatch.setattr(
        "pyxle.devserver.build_metadata_registry",
        lambda cfg: MetadataRegistry(pages=[], apis=[]),
    )
    monkeypatch.setattr(
        "pyxle.devserver.build_route_table",
        lambda registry: RouteTable(pages=[], apis=[]),
    )

    class DummyApp:
        def __init__(self) -> None:
            self.state = SimpleNamespace()

    dummy_app = DummyApp()
    monkeypatch.setattr(
        "pyxle.devserver.create_starlette_app",
        lambda cfg, routes, **_: dummy_app,
    )
    monkeypatch.setattr(
        "pyxle.devserver.write_client_bootstrap_files",
        lambda cfg: bootstrap_calls.append(cfg),
    )

    class StubVite:
        # A Vite process that started and became ready is running.
        running = True
        def __init__(self, cfg: DevServerSettings, *, logger: ConsoleLogger, **_: Any) -> None:
            self.settings = cfg
            self.logger = logger
            self.started = False
            self.ready = False
            self.stopped = False
            vite_instances.append(self)

        async def start(self) -> None:
            self.started = True

        async def wait_until_ready(self) -> None:
            self.ready = True

        async def stop(self) -> None:
            self.stopped = True

    monkeypatch.setattr("pyxle.devserver.ViteProcess", StubVite)

    class StubWatcher:
        def __init__(self, cfg: DevServerSettings, *, logger: ConsoleLogger, **_: Any) -> None:
            self.settings = cfg
            self.logger = logger
            self.started = False
            self.closed = False
            watcher_instances.append(self)

        def start(self) -> None:
            self.started = True

        def close(self) -> None:
            self.closed = True

    monkeypatch.setattr("pyxle.devserver.ProjectWatcher", StubWatcher)

    class StubConfig:
        def __init__(self, app: object, **kwargs: Any) -> None:
            captured_config["app"] = app
            captured_config["kwargs"] = kwargs

    class StubServer:
        def __init__(self, config: StubConfig) -> None:
            server_state["config"] = config
            self.should_exit = False

        async def serve(self) -> None:
            server_state["served"] = True
            server_state["ready_during_serve"] = getattr(dummy_app.state, "pyxle_ready", None)

    monkeypatch.setattr("pyxle.devserver.uvicorn.Config", StubConfig)
    monkeypatch.setattr("pyxle.devserver.uvicorn.Server", StubServer)

    server = DevServer(settings=settings, logger=logger)

    await server.start()

    assert build_calls == [{"settings": server.settings, "force": True}]
    assert watcher_instances and watcher_instances[0].started is True
    assert watcher_instances[0].closed is True
    assert captured_config["app"] is dummy_app
    assert captured_config["kwargs"]["host"] == server.settings.starlette_host
    assert captured_config["kwargs"]["port"] == server.settings.starlette_port
    assert captured_config["kwargs"]["loop"] == "asyncio"
    assert captured_config["kwargs"]["reload"] is False
    assert server_state.get("served") is True
    assert server_state.get("ready_during_serve") is True
    assert server._watcher is None
    # The detailed initial-build breakdown is verbose-only now; the curated
    # startup summary is what stays visible by default.
    assert any("Pyxle dev server ready" in message for message in capture.messages)
    assert bootstrap_calls == [server.settings]
    assert vite_instances and vite_instances[0].started is True
    assert vite_instances[0].ready is True
    assert vite_instances[0].stopped is True
    assert getattr(dummy_app.state, "pyxle_ready", False) is False


async def test_devserver_start_builds_routes_and_creates_app(anyio_backend, monkeypatch, tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    settings = DevServerSettings.from_project_root(project_root)
    (settings.pages_dir / "api").mkdir(parents=True, exist_ok=True)
    (settings.pages_dir / "api" / "pulse.py").write_text(
        """from starlette.responses import JSONResponse\n\nasync def endpoint(request):\n    return JSONResponse({\"message\": \"Hello\"})\n""",
        encoding="utf-8",
    )
    (settings.pages_dir / "index.pyxl").write_text(
        """\n\n@server\nasync def load_home(request):\n    return {\"message\": \"hi\"}\n\n# --- JavaScript/PSX (Client + Server) ---\n\nimport React from 'react';\n\nexport default function Home({ data }) {\n    return <div>{data.message}</div>;\n}\n""",
        encoding="utf-8",
    )

    watcher_instances: List["StubWatcher"] = []
    bootstrap_calls: List[DevServerSettings] = []
    vite_instances: List["StubVite"] = []

    class StubWatcher:
        def __init__(self, cfg: DevServerSettings, *, logger: ConsoleLogger, **_: Any) -> None:
            self.started = False
            self.closed = False
            watcher_instances.append(self)

        def start(self) -> None:
            self.started = True

        def close(self) -> None:
            self.closed = True

    monkeypatch.setattr("pyxle.devserver.ProjectWatcher", StubWatcher)

    served_config: Dict[str, Any] = {}

    class StubConfig:
        def __init__(self, app: Starlette, **kwargs: Any) -> None:
            served_config["app"] = app
            served_config["kwargs"] = kwargs

    class StubServer:
        def __init__(self, config: StubConfig) -> None:
            self.config = config

        async def serve(self) -> None:
            served_config["served"] = True

    monkeypatch.setattr("pyxle.devserver.uvicorn.Config", StubConfig)
    monkeypatch.setattr("pyxle.devserver.uvicorn.Server", StubServer)
    monkeypatch.setattr(
    "pyxle.devserver.write_client_bootstrap_files",
    lambda cfg: bootstrap_calls.append(cfg),
    )
    renderer = object()

    monkeypatch.setattr(
        "pyxle.devserver.starlette_app.ComponentRenderer",
        lambda **_: renderer,
    )

    async def fake_build_page_response(*, request, settings, page, renderer, overlay=None, **_kw):
        assert overlay is not None
        return HTMLResponse(f"<h1>{page.path}</h1>")

    monkeypatch.setattr(
        "pyxle.devserver.starlette_app.build_page_response",
        fake_build_page_response,
    )

    class StubVite:
        # A Vite process that started and became ready is running.
        running = True
        def __init__(self, cfg: DevServerSettings, *, logger: ConsoleLogger, **_: Any) -> None:
            self.started = False
            self.ready = False
            self.stopped = False
            vite_instances.append(self)

        async def start(self) -> None:
            self.started = True

        async def wait_until_ready(self) -> None:
            self.ready = True

        async def stop(self) -> None:
            self.stopped = True

    monkeypatch.setattr("pyxle.devserver.ViteProcess", StubVite)

    class StubWorkerPool:
        def __init__(self, **_: Any) -> None:
            pass

        async def start(self) -> None:
            pass

        async def stop(self) -> None:
            pass

    monkeypatch.setattr("pyxle.ssr.worker_pool.SsrWorkerPool", StubWorkerPool)

    capture = LogCapture()
    logger = ConsoleLogger(secho=capture)

    server = DevServer(settings=settings, logger=logger)

    await server.start()

    assert watcher_instances and watcher_instances[0].started is True
    assert watcher_instances[0].closed is True
    assert served_config.get("served") is True

    app = served_config["app"]
    assert isinstance(app, Starlette)

    with TestClient(app) as client:
        response = client.get("/")
        assert response.status_code == 200
        assert "<h1>/</h1>" in response.text

        api_response = client.get("/api/pulse")
        assert api_response.status_code == 200
        assert api_response.json()["message"] == "Hello"

    assert bootstrap_calls == [server.settings]
    assert vite_instances and vite_instances[0].started is True
    assert vite_instances[0].ready is True
    assert vite_instances[0].stopped is True


async def test_devserver_retries_vite_port(monkeypatch, tmp_path: Path) -> None:
    settings = DevServerSettings.from_project_root(tmp_path)
    capture = LogCapture()
    logger = ConsoleLogger(secho=capture)

    bootstrap_calls: List[DevServerSettings] = []
    vite_instances: List["StubVite"] = []

    monkeypatch.setattr(
        "pyxle.devserver.build_once",
        lambda cfg, **_: BuildSummary(compiled_pages=[], copied_api_modules=[], removed=[]),
    )
    monkeypatch.setattr(
        "pyxle.devserver.build_metadata_registry",
        lambda cfg: MetadataRegistry(pages=[], apis=[]),
    )
    monkeypatch.setattr(
        "pyxle.devserver.build_route_table",
        lambda registry: RouteTable(pages=[], apis=[]),
    )
    monkeypatch.setattr(
        "pyxle.devserver.write_client_bootstrap_files",
        lambda cfg: bootstrap_calls.append(cfg),
    )

    monkeypatch.setattr(
        "pyxle.devserver.starlette_app.ComponentRenderer",
        lambda **_: object(),
    )

    async def fake_build_page_response(*, request, settings, page, renderer, overlay=None, **_kw):
        return HTMLResponse("<div></div>")

    monkeypatch.setattr(
        "pyxle.devserver.starlette_app.build_page_response",
        fake_build_page_response,
    )

    class StubVite:
        # A Vite process that started and became ready is running.
        running = True
        def __init__(self, cfg: DevServerSettings, *, logger: ConsoleLogger, **_: Any) -> None:
            self.settings = cfg
            self.started = False
            self.ready = False
            self.stopped = False
            vite_instances.append(self)

        async def start(self) -> None:
            self.started = True

        async def wait_until_ready(self) -> None:
            self.ready = True

        async def stop(self) -> None:
            self.stopped = True

    monkeypatch.setattr("pyxle.devserver.ViteProcess", StubVite)

    class StubWatcher:
        def __init__(self, cfg: DevServerSettings, *, logger: ConsoleLogger, **_: Any) -> None:
            pass

        def start(self) -> None:  # pragma: no cover - no-op stub
            pass

        def close(self) -> None:
            pass

    monkeypatch.setattr("pyxle.devserver.ProjectWatcher", StubWatcher)

    class StubConfig:
        def __init__(self, app: object, **kwargs: Any) -> None:
            pass

    class StubServer:
        def __init__(self, config: StubConfig) -> None:
            self.should_exit = False

        async def serve(self) -> None:
            return None

    monkeypatch.setattr("pyxle.devserver.uvicorn.Config", StubConfig)
    monkeypatch.setattr("pyxle.devserver.uvicorn.Server", StubServer)

    attempts: List[int] = []

    def fake_is_port_available(host: str, port: int) -> bool:
        attempts.append(port)
        base = settings.vite_port
        return port == base + 2

    monkeypatch.setattr(
        DevServer,
        "_is_port_available",
        staticmethod(fake_is_port_available),
    )

    server = DevServer(settings=settings, logger=logger)
    server.vite_port_search_limit = 5

    await server.start()

    assert attempts[:3] == [settings.vite_port, settings.vite_port + 1, settings.vite_port + 2]
    assert bootstrap_calls and bootstrap_calls[0].vite_port == settings.vite_port + 2
    assert vite_instances and vite_instances[0].settings.vite_port == settings.vite_port + 2
    assert vite_instances[0].ready is True
    assert any("retrying" in message for message in capture.messages)


def test_devserver_logs_initial_build_without_changes(tmp_path: Path) -> None:
    settings = DevServerSettings.from_project_root(tmp_path)
    capture = LogCapture()
    logger = ConsoleLogger(secho=capture)
    # The initial-build breakdown is verbose-only now.
    logger.set_verbosity(Verbosity.VERBOSE)
    server = DevServer(settings=settings, logger=logger)

    summary = BuildSummary(compiled_pages=[], copied_api_modules=[], removed=[])
    server._log_initial_build(summary)

    assert any("no changes" in message.lower() for message in capture.messages)


def test_devserver_ensure_vite_port_available_exhausted(monkeypatch, tmp_path: Path) -> None:
    settings = DevServerSettings.from_project_root(tmp_path)
    server = DevServer(settings=settings)
    server.vite_port_search_limit = 3

    monkeypatch.setattr(
        DevServer,
        "_is_port_available",
        staticmethod(lambda host, port: False),
    )

    with pytest.raises(RuntimeError):
        server._ensure_vite_port_available(settings)


async def test_maybe_schedule_reload_dispatches(monkeypatch) -> None:
    loop = asyncio.get_running_loop()
    captured: list[list[str]] = []

    class StubOverlay:
        async def notify_reload(self, *, changed_paths: list[str]) -> None:
            captured.append(changed_paths)

    summary = BuildSummary(compiled_pages=["pages/index.pyxl"], copied_api_modules=[], removed=[])
    stats = WatcherStatistics(elapsed_seconds=0.01, summary=summary, error=None, changed_paths=[])

    monkeypatch.setattr(
        "pyxle.devserver.asyncio.run_coroutine_threadsafe",
        lambda coro, loop: loop.create_task(coro),
    )

    overlay = StubOverlay()
    assert _maybe_schedule_reload(overlay, loop, stats) is True
    await asyncio.sleep(0)
    assert captured == [["pages/index.pyxl"]]


async def test_maybe_schedule_reload_handles_guard_paths(monkeypatch) -> None:
    loop = asyncio.get_running_loop()

    summary_with_change = BuildSummary(compiled_pages=["pages/about.pyxl"], copied_api_modules=[], removed=[])
    stats_with_change = WatcherStatistics(
        elapsed_seconds=0.01,
        summary=summary_with_change,
        error=None,
        changed_paths=[Path("pages/about.pyxl")],
    )

    summary_empty = BuildSummary()
    stats_empty = WatcherStatistics(
        elapsed_seconds=0.01,
        summary=summary_empty,
        error=None,
        changed_paths=[],
    )

    class StubOverlay:
        async def notify_reload(self, *, changed_paths: list[str]) -> None:  # pragma: no cover - not called
            raise AssertionError("should not be invoked")

    overlay = StubOverlay()

    assert _maybe_schedule_reload(None, loop, stats_with_change) is False
    stats_error = WatcherStatistics(
        elapsed_seconds=0.01,
        summary=summary_with_change,
        error=RuntimeError("boom"),
        changed_paths=[],
    )
    assert _maybe_schedule_reload(overlay, loop, stats_error) is False
    assert _maybe_schedule_reload(overlay, loop, stats_empty) is False


async def test_maybe_schedule_reload_handles_runtime_error(monkeypatch) -> None:
    loop = asyncio.get_running_loop()

    class StubOverlay:
        async def notify_reload(self, *, changed_paths: list[str]) -> None:
            pass

    summary = BuildSummary(compiled_pages=["pages/index.pyxl"], copied_api_modules=[], removed=[])
    stats = WatcherStatistics(elapsed_seconds=0.01, summary=summary, error=None, changed_paths=[])

    def raise_runtime(coro, loop):
        raise RuntimeError("boom")

    monkeypatch.setattr("pyxle.devserver.asyncio.run_coroutine_threadsafe", raise_runtime)

    assert _maybe_schedule_reload(StubOverlay(), loop, stats) is False


async def test_maybe_schedule_reload_uses_changed_paths_fallback(monkeypatch) -> None:
    loop = asyncio.get_running_loop()
    captured: list[list[str]] = []

    class StubOverlay:
        async def notify_reload(self, *, changed_paths: list[str]) -> None:
            captured.append(changed_paths)

    stats = WatcherStatistics(
        elapsed_seconds=0.02,
        summary=BuildSummary(),
        error=None,
        changed_paths=[Path("foo/bar.py"), "api/pulse.py"],
    )

    monkeypatch.setattr(
        "pyxle.devserver.asyncio.run_coroutine_threadsafe",
        lambda coro, loop: loop.create_task(coro),
    )

    overlay = StubOverlay()
    assert _maybe_schedule_reload(overlay, loop, stats) is True
    await asyncio.sleep(0)
    assert captured == [["foo/bar.py", "api/pulse.py"]]


def test_set_app_ready_flag_handles_missing_state() -> None:
    class Dummy:
        pass

    dummy = Dummy()

    _set_app_ready_flag(dummy, True)  # should be a no-op without errors


def test_set_app_ready_flag_sets_and_unsets_flag() -> None:
    class Dummy:
        def __init__(self) -> None:
            self.state = SimpleNamespace()

    dummy = Dummy()

    _set_app_ready_flag(dummy, True)
    assert getattr(dummy.state, "pyxle_ready", False) is True

    _set_app_ready_flag(dummy, False)
    assert getattr(dummy.state, "pyxle_ready", True) is False


async def test_ensure_node_modules_runs_npm_install(monkeypatch, tmp_path: Path) -> None:
    """When node_modules/ is missing but package.json exists, run npm install."""

    (tmp_path / "package.json").write_text('{"name":"test"}')
    settings = DevServerSettings.from_project_root(tmp_path)
    capture = LogCapture()
    logger = ConsoleLogger(secho=capture)
    server = DevServer(settings=settings, logger=logger)

    npm_calls: list[tuple[str, ...]] = []

    class StubProcess:
        def __init__(self) -> None:
            self.returncode = 0

        async def communicate(self) -> tuple[bytes, bytes]:
            return b"installed\n", b""

    async def fake_subprocess_exec(*args: Any, **kwargs: Any) -> StubProcess:
        npm_calls.append(args)
        return StubProcess()

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_subprocess_exec)
    monkeypatch.setattr("shutil.which", lambda cmd: f"/usr/bin/{cmd}" if cmd == "npm" else None)

    await server._ensure_node_modules(settings)

    assert len(npm_calls) == 1
    assert npm_calls[0][1] == "install"
    assert any("npm install completed" in m for m in capture.messages)


async def test_ensure_node_modules_skips_when_present(tmp_path: Path) -> None:
    """When node_modules/ already exists, skip npm install."""

    (tmp_path / "package.json").write_text('{"name":"test"}')
    (tmp_path / "node_modules").mkdir()
    settings = DevServerSettings.from_project_root(tmp_path)
    capture = LogCapture()
    logger = ConsoleLogger(secho=capture)
    server = DevServer(settings=settings, logger=logger)

    await server._ensure_node_modules(settings)

    assert not any("npm install" in m for m in capture.messages)


async def test_ensure_node_modules_skips_without_package_json(tmp_path: Path) -> None:
    """When package.json doesn't exist, skip npm install."""

    settings = DevServerSettings.from_project_root(tmp_path)
    capture = LogCapture()
    logger = ConsoleLogger(secho=capture)
    server = DevServer(settings=settings, logger=logger)

    await server._ensure_node_modules(settings)

    assert not any("npm install" in m for m in capture.messages)


async def test_ensure_node_modules_warns_when_npm_missing(monkeypatch, tmp_path: Path) -> None:
    """When npm is not available, warn and skip."""

    (tmp_path / "package.json").write_text('{"name":"test"}')
    settings = DevServerSettings.from_project_root(tmp_path)
    capture = LogCapture()
    logger = ConsoleLogger(secho=capture)
    server = DevServer(settings=settings, logger=logger)

    monkeypatch.setattr("shutil.which", lambda cmd: None)

    await server._ensure_node_modules(settings)

    assert any("not available" in m for m in capture.messages)


async def test_ensure_node_modules_warns_on_failure(monkeypatch, tmp_path: Path) -> None:
    """When npm install fails, warn and continue."""

    (tmp_path / "package.json").write_text('{"name":"test"}')
    settings = DevServerSettings.from_project_root(tmp_path)
    capture = LogCapture()
    logger = ConsoleLogger(secho=capture)
    server = DevServer(settings=settings, logger=logger)

    class StubProcess:
        def __init__(self) -> None:
            self.returncode = 1

        async def communicate(self) -> tuple[bytes, bytes]:
            return b"", b"ERR! something failed"

    async def fake_subprocess_exec(*args: Any, **kwargs: Any) -> StubProcess:
        return StubProcess()

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_subprocess_exec)
    monkeypatch.setattr("shutil.which", lambda cmd: f"/usr/bin/{cmd}" if cmd == "npm" else None)

    await server._ensure_node_modules(settings)

    assert any("exited with code 1" in m for m in capture.messages)


async def test_devserver_starts_tailwind_when_configured(monkeypatch, tmp_path: Path) -> None:
    """DevServer.start() launches TailwindProcess when tailwind config is present."""

    (tmp_path / "tailwind.config.cjs").write_text("module.exports = {}")
    styles_dir = tmp_path / "pages" / "styles"
    styles_dir.mkdir(parents=True)
    (styles_dir / "tailwind.css").write_text("@tailwind base;")

    settings = DevServerSettings.from_project_root(tmp_path)
    capture = LogCapture()
    logger = ConsoleLogger(secho=capture)
    tailwind_instances: list[Any] = []

    monkeypatch.setattr(
        "pyxle.devserver.build_once",
        lambda cfg, **_: BuildSummary(compiled_pages=[], copied_api_modules=[], removed=[]),
    )
    monkeypatch.setattr(
        "pyxle.devserver.build_metadata_registry",
        lambda cfg: MetadataRegistry(pages=[], apis=[]),
    )
    monkeypatch.setattr(
        "pyxle.devserver.build_route_table",
        lambda registry: RouteTable(pages=[], apis=[]),
    )
    monkeypatch.setattr(
        "pyxle.devserver.write_client_bootstrap_files",
        lambda cfg: None,
    )

    class DummyApp:
        def __init__(self) -> None:
            self.state = SimpleNamespace()

    monkeypatch.setattr(
        "pyxle.devserver.create_starlette_app",
        lambda cfg, routes, **_: DummyApp(),
    )

    class StubVite:
        # A Vite process that started and became ready is running.
        running = True
        def __init__(self, *a: Any, **kw: Any) -> None:
            pass

        async def start(self) -> None:
            pass

        async def wait_until_ready(self) -> None:
            pass

        async def stop(self) -> None:
            pass

    monkeypatch.setattr("pyxle.devserver.ViteProcess", StubVite)

    class StubWatcher:
        def __init__(self, *a: Any, **kw: Any) -> None:
            pass

        def start(self) -> None:
            pass

        def close(self) -> None:
            pass

    monkeypatch.setattr("pyxle.devserver.ProjectWatcher", StubWatcher)

    class StubTailwind:
        def __init__(self, *a: Any, **kw: Any) -> None:
            self.started = False
            self.stopped = False
            tailwind_instances.append(self)

        async def start(self) -> None:
            self.started = True

        async def stop(self) -> None:
            self.stopped = True

    monkeypatch.setattr("pyxle.devserver.TailwindProcess", StubTailwind)

    class StubConfig:
        def __init__(self, *a: Any, **kw: Any) -> None:
            pass

    class StubServer:
        def __init__(self, *a: Any) -> None:
            self.should_exit = False

        async def serve(self) -> None:
            pass

    monkeypatch.setattr("pyxle.devserver.uvicorn.Config", StubConfig)
    monkeypatch.setattr("pyxle.devserver.uvicorn.Server", StubServer)

    server = DevServer(settings=settings, logger=logger, tailwind=True)
    await server.start()

    assert len(tailwind_instances) == 1
    assert tailwind_instances[0].started is True
    assert tailwind_instances[0].stopped is True


async def test_devserver_skips_tailwind_when_disabled(monkeypatch, tmp_path: Path) -> None:
    """DevServer.start() skips TailwindProcess when tailwind=False."""

    (tmp_path / "tailwind.config.cjs").write_text("module.exports = {}")

    settings = DevServerSettings.from_project_root(tmp_path)
    capture = LogCapture()
    logger = ConsoleLogger(secho=capture)
    tailwind_instances: list[Any] = []

    monkeypatch.setattr(
        "pyxle.devserver.build_once",
        lambda cfg, **_: BuildSummary(compiled_pages=[], copied_api_modules=[], removed=[]),
    )
    monkeypatch.setattr(
        "pyxle.devserver.build_metadata_registry",
        lambda cfg: MetadataRegistry(pages=[], apis=[]),
    )
    monkeypatch.setattr(
        "pyxle.devserver.build_route_table",
        lambda registry: RouteTable(pages=[], apis=[]),
    )
    monkeypatch.setattr(
        "pyxle.devserver.write_client_bootstrap_files",
        lambda cfg: None,
    )

    class DummyApp:
        def __init__(self) -> None:
            self.state = SimpleNamespace()

    monkeypatch.setattr(
        "pyxle.devserver.create_starlette_app",
        lambda cfg, routes, **_: DummyApp(),
    )

    class StubVite:
        # A Vite process that started and became ready is running.
        running = True
        def __init__(self, *a: Any, **kw: Any) -> None:
            pass

        async def start(self) -> None:
            pass

        async def wait_until_ready(self) -> None:
            pass

        async def stop(self) -> None:
            pass

    monkeypatch.setattr("pyxle.devserver.ViteProcess", StubVite)

    class StubWatcher:
        def __init__(self, *a: Any, **kw: Any) -> None:
            pass

        def start(self) -> None:
            pass

        def close(self) -> None:
            pass

    monkeypatch.setattr("pyxle.devserver.ProjectWatcher", StubWatcher)

    class StubTailwind:
        def __init__(self, *a: Any, **kw: Any) -> None:
            tailwind_instances.append(self)

        async def start(self) -> None:
            pass

        async def stop(self) -> None:
            pass

    monkeypatch.setattr("pyxle.devserver.TailwindProcess", StubTailwind)

    class StubConfig:
        def __init__(self, *a: Any, **kw: Any) -> None:
            pass

    class StubServer:
        def __init__(self, *a: Any) -> None:
            self.should_exit = False

        async def serve(self) -> None:
            pass

    monkeypatch.setattr("pyxle.devserver.uvicorn.Config", StubConfig)
    monkeypatch.setattr("pyxle.devserver.uvicorn.Server", StubServer)

    server = DevServer(settings=settings, logger=logger, tailwind=False)
    await server.start()

    assert len(tailwind_instances) == 0


async def test_devserver_skips_tailwind_when_postcss_config_present(monkeypatch, tmp_path: Path) -> None:
    """When ``postcss.config.*`` is present alongside ``tailwind.config.*``, the
    standalone Tailwind watcher is skipped because Vite will process CSS
    through PostCSS instead. The user gets a clear info log explaining the
    decision so the behaviour is never silent.
    """

    (tmp_path / "tailwind.config.cjs").write_text("module.exports = {}")
    (tmp_path / "postcss.config.cjs").write_text(
        "module.exports = { plugins: { tailwindcss: {}, autoprefixer: {} } }"
    )
    styles_dir = tmp_path / "pages" / "styles"
    styles_dir.mkdir(parents=True)
    (styles_dir / "tailwind.css").write_text("@tailwind base;")

    settings = DevServerSettings.from_project_root(tmp_path)
    capture = LogCapture()
    logger = ConsoleLogger(secho=capture)
    tailwind_instances: list[Any] = []

    monkeypatch.setattr(
        "pyxle.devserver.build_once",
        lambda cfg, **_: BuildSummary(compiled_pages=[], copied_api_modules=[], removed=[]),
    )
    monkeypatch.setattr(
        "pyxle.devserver.build_metadata_registry",
        lambda cfg: MetadataRegistry(pages=[], apis=[]),
    )
    monkeypatch.setattr(
        "pyxle.devserver.build_route_table",
        lambda registry: RouteTable(pages=[], apis=[]),
    )
    monkeypatch.setattr(
        "pyxle.devserver.write_client_bootstrap_files",
        lambda cfg: None,
    )

    class DummyApp:
        def __init__(self) -> None:
            self.state = SimpleNamespace()

    monkeypatch.setattr(
        "pyxle.devserver.create_starlette_app",
        lambda cfg, routes, **_: DummyApp(),
    )

    class StubVite:
        # A Vite process that started and became ready is running.
        running = True
        def __init__(self, *a: Any, **kw: Any) -> None:
            pass

        async def start(self) -> None:
            pass

        async def wait_until_ready(self) -> None:
            pass

        async def stop(self) -> None:
            pass

    monkeypatch.setattr("pyxle.devserver.ViteProcess", StubVite)

    class StubWatcher:
        def __init__(self, *a: Any, **kw: Any) -> None:
            pass

        def start(self) -> None:
            pass

        def close(self) -> None:
            pass

    monkeypatch.setattr("pyxle.devserver.ProjectWatcher", StubWatcher)

    class StubTailwind:
        def __init__(self, *a: Any, **kw: Any) -> None:
            tailwind_instances.append(self)

        async def start(self) -> None:  # pragma: no cover - skip path means start is never reached
            pass

        async def stop(self) -> None:  # pragma: no cover - skip path means stop is never reached
            pass

    monkeypatch.setattr("pyxle.devserver.TailwindProcess", StubTailwind)

    class StubConfig:
        def __init__(self, *a: Any, **kw: Any) -> None:
            pass

    class StubServer:
        def __init__(self, *a: Any) -> None:
            self.should_exit = False

        async def serve(self) -> None:
            pass

    monkeypatch.setattr("pyxle.devserver.uvicorn.Config", StubConfig)
    monkeypatch.setattr("pyxle.devserver.uvicorn.Server", StubServer)

    server = DevServer(settings=settings, logger=logger, tailwind=True)
    await server.start()

    # The standalone watcher must NOT have been instantiated.
    assert tailwind_instances == []
    # And the user must have been told why.
    assert any(
        "postcss.config.cjs" in m and "skipping standalone Tailwind watcher" in m
        for m in capture.messages
    ), f"Expected skip log line, got: {capture.messages}"

async def test_notify_rebuild_error_broadcasts_to_overlay(monkeypatch) -> None:
    """A failed rebuild reaches the overlay (as the architecture docs promise)."""
    loop = asyncio.get_running_loop()
    captured: list[dict] = []

    class StubOverlay:
        async def notify_error(self, *, route_path, error, stack=None, breadcrumbs=None):
            captured.append({"route": route_path, "error": str(error), "breadcrumbs": breadcrumbs})

    stats = WatcherStatistics(
        elapsed_seconds=0.02,
        summary=None,
        error=ValueError("Unexpected token at line 3"),
        changed_paths=[Path("pages/index.pyxl")],
    )
    monkeypatch.setattr(
        "pyxle.devserver.asyncio.run_coroutine_threadsafe",
        lambda coro, loop: loop.create_task(coro),
    )

    assert _notify_rebuild_error(StubOverlay(), loop, stats) is True
    await asyncio.sleep(0)
    assert captured and "Unexpected token" in captured[0]["error"]
    assert captured[0]["breadcrumbs"][0]["status"] == "failed"
    assert "pages/index.pyxl" in captured[0]["breadcrumbs"][0]["detail"]


async def test_notify_rebuild_error_ignores_success_and_missing_overlay() -> None:
    loop = asyncio.get_running_loop()
    ok = WatcherStatistics(elapsed_seconds=0.01, summary=BuildSummary(), error=None, changed_paths=[])
    assert _notify_rebuild_error(None, loop, ok) is False
    failed = WatcherStatistics(
        elapsed_seconds=0.01, summary=None, error=RuntimeError("boom"), changed_paths=[]
    )
    # No overlay connected: still reports failure handled (True), never raises.
    assert _notify_rebuild_error(None, loop, failed) is True


async def test_devserver_skips_ready_banner_when_vite_not_running(
    anyio_backend, monkeypatch, tmp_path: Path
) -> None:
    """If Vite dies right after the readiness probe, don't advertise "ready"."""
    settings = DevServerSettings.from_project_root(tmp_path)
    capture = LogCapture()
    logger = ConsoleLogger(secho=capture)

    monkeypatch.setattr(
        "pyxle.devserver.build_once",
        lambda cfg, *, force_rebuild=False: BuildSummary(
            compiled_pages=[], copied_api_modules=[], removed=[]
        ),
    )
    monkeypatch.setattr(
        "pyxle.devserver.build_metadata_registry",
        lambda cfg: MetadataRegistry(pages=[], apis=[]),
    )
    monkeypatch.setattr(
        "pyxle.devserver.build_route_table",
        lambda registry: RouteTable(pages=[], apis=[]),
    )
    dummy_app = SimpleNamespace(state=SimpleNamespace())
    monkeypatch.setattr(
        "pyxle.devserver.create_starlette_app", lambda cfg, routes, **_: dummy_app
    )
    monkeypatch.setattr(
        "pyxle.devserver.write_client_bootstrap_files", lambda cfg: None
    )

    class DeadVite:
        # Passed the readiness probe, then exited (e.g. unsupported Node.js).
        running = False

        def __init__(self, cfg: DevServerSettings, *, logger: ConsoleLogger, **_: Any) -> None:
            pass

        async def start(self) -> None:
            pass

        async def wait_until_ready(self) -> None:
            pass

        async def stop(self) -> None:
            pass

    monkeypatch.setattr("pyxle.devserver.ViteProcess", DeadVite)

    class StubWatcher:
        def __init__(self, cfg: DevServerSettings, *, logger: ConsoleLogger, **_: Any) -> None:
            pass

        def start(self) -> None:
            pass

        def close(self) -> None:
            pass

    monkeypatch.setattr("pyxle.devserver.ProjectWatcher", StubWatcher)

    class StubConfig:
        def __init__(self, app: object, **kwargs: Any) -> None:
            pass

    class StubServer:
        def __init__(self, config: StubConfig) -> None:
            self.should_exit = False

        async def serve(self) -> None:
            pass

    monkeypatch.setattr("pyxle.devserver.uvicorn.Config", StubConfig)
    monkeypatch.setattr("pyxle.devserver.uvicorn.Server", StubServer)

    server = DevServer(settings=settings, logger=logger)
    await server.start()

    assert not any("dev server ready" in m for m in capture.messages)
    assert any("not ready" in m for m in capture.messages)


# ---------------------------------------------------------------------------
# dev-server.json — the editor-tooling discovery file
# ---------------------------------------------------------------------------


def _read_discovery(settings: DevServerSettings) -> Dict[str, Any]:
    return json.loads(
        (settings.build_root / "dev-server.json").read_text(encoding="utf-8")
    )


def test_write_discovery_file_payload(tmp_path: Path) -> None:
    import pyxle

    settings = DevServerSettings.from_project_root(tmp_path)
    server = DevServer(
        settings=settings,
        logger=ConsoleLogger(secho=LogCapture()),
        inspect_endpoint=("127.0.0.1", 5679),
    )

    server._write_discovery_file(settings)

    payload = _read_discovery(settings)
    assert payload["pid"] == os.getpid()
    assert payload["version"] == pyxle.__version__
    assert payload["startedAt"] > 0
    assert payload["projectRoot"] == str(settings.project_root)
    assert payload["server"] == {
        "host": settings.starlette_host,
        "port": settings.starlette_port,
    }
    assert payload["vite"] == {"host": settings.vite_host, "port": settings.vite_port}
    assert payload["url"] == f"http://127.0.0.1:{settings.starlette_port}"
    # Debug default + no studio config block = Studio enabled.
    assert payload["studio"] == (
        f"http://127.0.0.1:{settings.starlette_port}/__pyxle/studio"
    )
    assert payload["debugpy"] == {"host": "127.0.0.1", "port": 5679}
    # Written atomically — the temp file never survives a successful write.
    assert not (settings.build_root / "dev-server.json.tmp").exists()


def test_write_discovery_file_normalises_bind_all_host(tmp_path: Path) -> None:
    settings = DevServerSettings.from_project_root(tmp_path, starlette_host="0.0.0.0")
    server = DevServer(settings=settings, logger=ConsoleLogger(secho=LogCapture()))

    server._write_discovery_file(settings)

    payload = _read_discovery(settings)
    # The raw bind host is preserved for information…
    assert payload["server"]["host"] == "0.0.0.0"
    # …but URLs are browser-connectable.
    assert payload["url"] == f"http://127.0.0.1:{settings.starlette_port}"
    assert payload["studio"].startswith("http://127.0.0.1:")


def test_write_discovery_file_studio_none_when_unavailable(tmp_path: Path) -> None:
    logger = ConsoleLogger(secho=LogCapture())

    # Studio explicitly disabled in the config block.
    disabled = DevServerSettings.from_project_root(
        tmp_path / "a", studio=SimpleNamespace(enabled=False)
    )
    DevServer(settings=disabled, logger=logger)._write_discovery_file(disabled)
    assert _read_discovery(disabled)["studio"] is None

    # Studio is dev-only: no debug, no dashboard URL.
    production_like = DevServerSettings.from_project_root(tmp_path / "b", debug=False)
    DevServer(settings=production_like, logger=logger)._write_discovery_file(
        production_like
    )
    assert _read_discovery(production_like)["studio"] is None


def test_write_discovery_file_debugpy_none_without_endpoint(tmp_path: Path) -> None:
    settings = DevServerSettings.from_project_root(tmp_path)
    server = DevServer(settings=settings, logger=ConsoleLogger(secho=LogCapture()))

    server._write_discovery_file(settings)

    assert _read_discovery(settings)["debugpy"] is None


def test_write_discovery_file_swallows_oserror(monkeypatch, tmp_path: Path) -> None:
    """A failed discovery write must never take down the dev server."""
    settings = DevServerSettings.from_project_root(tmp_path)
    capture = LogCapture()
    logger = ConsoleLogger(secho=capture)
    logger.set_verbosity(Verbosity.VERBOSE)
    server = DevServer(settings=settings, logger=logger)

    def broken_write_text(self: Path, *args: Any, **kwargs: Any) -> int:
        raise OSError("disk full")

    monkeypatch.setattr(Path, "write_text", broken_write_text)

    server._write_discovery_file(settings)  # must not raise

    assert not (settings.build_root / "dev-server.json").exists()
    assert any("Could not write dev-server.json" in m for m in capture.messages)


def test_remove_discovery_file(tmp_path: Path) -> None:
    settings = DevServerSettings.from_project_root(tmp_path)
    server = DevServer(settings=settings, logger=ConsoleLogger(secho=LogCapture()))
    target = settings.build_root / "dev-server.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("{}", encoding="utf-8")

    server._remove_discovery_file(settings)
    assert not target.exists()

    # Removing an already-absent file is a no-op, not an error.
    server._remove_discovery_file(settings)
    assert not target.exists()


def test_remove_discovery_file_keeps_another_process_file(tmp_path: Path) -> None:
    """A second ``pyxle dev`` may have overwritten the file with its own pid;
    this process must NOT delete it and strand the still-running instance."""
    settings = DevServerSettings.from_project_root(tmp_path)
    server = DevServer(settings=settings, logger=ConsoleLogger(secho=LogCapture()))
    target = settings.build_root / "dev-server.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    # A foreign pid that is not ours (and is virtually never live in CI).
    foreign_pid = os.getpid() + 987654
    target.write_text(json.dumps({"pid": foreign_pid}), encoding="utf-8")

    server._remove_discovery_file(settings)
    assert target.exists()

    # Our own pid (or no pid) is removable.
    target.write_text(json.dumps({"pid": os.getpid()}), encoding="utf-8")
    server._remove_discovery_file(settings)
    assert not target.exists()


@pytest.mark.parametrize("payload", ["null", "42", "[]", '"x"', "{ not json", ""])
def test_remove_discovery_file_tolerates_non_object_json(
    tmp_path: Path, payload: str
) -> None:
    """A corrupt/hand-edited discovery file (valid non-object JSON, or malformed)
    must fall through to unlink, never raise — otherwise the exception escapes the
    shutdown ``finally`` and leaks the Vite subprocess."""
    settings = DevServerSettings.from_project_root(tmp_path)
    server = DevServer(settings=settings, logger=ConsoleLogger(secho=LogCapture()))
    target = settings.build_root / "dev-server.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(payload, encoding="utf-8")

    server._remove_discovery_file(settings)  # must not raise
    assert not target.exists()


async def test_devserver_start_manages_discovery_file_lifecycle(
    anyio_backend, monkeypatch, tmp_path: Path
) -> None:
    """``start()`` writes the discovery file after the initial build,
    re-asserts it on every rebuild (a build pass may rmtree the build root),
    and removes it on shutdown."""
    settings = DevServerSettings.from_project_root(tmp_path)
    logger = ConsoleLogger(secho=LogCapture())
    observed: Dict[str, Any] = {}
    watcher_instances: List[Any] = []

    monkeypatch.setattr(
        "pyxle.devserver.build_once",
        lambda cfg, *, force_rebuild=False: BuildSummary(
            compiled_pages=[], copied_api_modules=[], removed=[]
        ),
    )
    monkeypatch.setattr(
        "pyxle.devserver.build_metadata_registry",
        lambda cfg: MetadataRegistry(pages=[], apis=[]),
    )
    monkeypatch.setattr(
        "pyxle.devserver.build_route_table",
        lambda registry: RouteTable(pages=[], apis=[]),
    )
    monkeypatch.setattr("pyxle.devserver.write_client_bootstrap_files", lambda cfg: None)

    class DummyApp:
        def __init__(self) -> None:
            self.state = SimpleNamespace()

    dummy_app = DummyApp()
    monkeypatch.setattr(
        "pyxle.devserver.create_starlette_app",
        lambda cfg, routes, **_: dummy_app,
    )

    class StubVite:
        running = True

        def __init__(self, cfg: DevServerSettings, *, logger: ConsoleLogger, **_: Any) -> None:
            pass

        async def start(self) -> None:
            pass

        async def wait_until_ready(self) -> None:
            pass

        async def stop(self) -> None:
            pass

    monkeypatch.setattr("pyxle.devserver.ViteProcess", StubVite)

    class StubWatcher:
        def __init__(self, cfg: DevServerSettings, *, logger: ConsoleLogger, **kwargs: Any) -> None:
            self.on_rebuild = kwargs.get("on_rebuild")
            watcher_instances.append(self)

        def start(self) -> None:
            pass

        def close(self) -> None:
            pass

    monkeypatch.setattr("pyxle.devserver.ProjectWatcher", StubWatcher)

    class StubConfig:
        def __init__(self, app: object, **kwargs: Any) -> None:
            pass

    class StubServer:
        def __init__(self, config: StubConfig) -> None:
            self.should_exit = False

        async def serve(self) -> None:
            target = settings.build_root / "dev-server.json"
            observed["written_during_serve"] = target.exists()
            observed["payload"] = json.loads(target.read_text(encoding="utf-8"))

            # A rebuild pass can rmtree the build root; the rebuild hook must
            # re-assert the discovery file even for a failed build.
            target.unlink()
            stats = WatcherStatistics(
                elapsed_seconds=0.01,
                summary=None,
                error=RuntimeError("mid-edit syntax error"),
                changed_paths=[],
            )
            watcher_instances[0].on_rebuild(stats)
            observed["rewritten_after_rebuild"] = target.exists()

    monkeypatch.setattr("pyxle.devserver.uvicorn.Config", StubConfig)
    monkeypatch.setattr("pyxle.devserver.uvicorn.Server", StubServer)

    server = DevServer(
        settings=settings, logger=logger, inspect_endpoint=("127.0.0.1", 5680)
    )
    await server.start()

    assert observed["written_during_serve"] is True
    assert observed["payload"]["debugpy"] == {"host": "127.0.0.1", "port": 5680}
    assert observed["rewritten_after_rebuild"] is True
    # Gone after shutdown — stale coordinates must not linger for editors.
    assert not (settings.build_root / "dev-server.json").exists()


async def test_devserver_start_inspect_wait_blocks_after_discovery_file(
    anyio_backend, monkeypatch, tmp_path: Path
) -> None:
    """With ``inspect_wait`` set, ``start()`` calls ``debugpy.wait_for_client``
    only AFTER the discovery file is written — an editor needs the endpoint on
    disk before it can attach, and boot-time breakpoints must still bind."""
    settings = DevServerSettings.from_project_root(tmp_path)
    logger = ConsoleLogger(secho=LogCapture())
    observed: Dict[str, Any] = {}

    monkeypatch.setattr(
        "pyxle.devserver.build_once",
        lambda cfg, *, force_rebuild=False: BuildSummary(
            compiled_pages=[], copied_api_modules=[], removed=[]
        ),
    )
    monkeypatch.setattr(
        "pyxle.devserver.build_metadata_registry",
        lambda cfg: MetadataRegistry(pages=[], apis=[]),
    )
    monkeypatch.setattr(
        "pyxle.devserver.build_route_table",
        lambda registry: RouteTable(pages=[], apis=[]),
    )
    monkeypatch.setattr("pyxle.devserver.write_client_bootstrap_files", lambda cfg: None)

    class DummyApp:
        def __init__(self) -> None:
            self.state = SimpleNamespace()

    monkeypatch.setattr(
        "pyxle.devserver.create_starlette_app",
        lambda cfg, routes, **_: DummyApp(),
    )

    class StubVite:
        running = True

        def __init__(self, cfg: DevServerSettings, *, logger: ConsoleLogger, **_: Any) -> None:
            pass

        async def start(self) -> None:
            pass

        async def wait_until_ready(self) -> None:
            pass

        async def stop(self) -> None:
            pass

    monkeypatch.setattr("pyxle.devserver.ViteProcess", StubVite)

    class StubWatcher:
        def __init__(self, cfg: DevServerSettings, *, logger: ConsoleLogger, **kwargs: Any) -> None:
            pass

        def start(self) -> None:
            pass

        def close(self) -> None:
            pass

    monkeypatch.setattr("pyxle.devserver.ProjectWatcher", StubWatcher)

    class StubConfig:
        def __init__(self, app: object, **kwargs: Any) -> None:
            pass

    class StubServer:
        def __init__(self, config: StubConfig) -> None:
            self.should_exit = False

        async def serve(self) -> None:
            observed["served"] = True

    monkeypatch.setattr("pyxle.devserver.uvicorn.Config", StubConfig)
    monkeypatch.setattr("pyxle.devserver.uvicorn.Server", StubServer)

    class FakeDebugpy:
        def __init__(self) -> None:
            self.wait_calls = 0

        def wait_for_client(self) -> None:
            self.wait_calls += 1
            # The discovery file must already exist so an editor can read the
            # endpoint and attach before this blocks.
            observed["discovery_present_at_wait"] = (
                settings.build_root / "dev-server.json"
            ).exists()
            observed["served_before_wait"] = observed.get("served", False)

    fake_debugpy = FakeDebugpy()
    monkeypatch.setitem(sys.modules, "debugpy", fake_debugpy)

    server = DevServer(
        settings=settings,
        logger=logger,
        inspect_wait=True,
        inspect_endpoint=("127.0.0.1", 5681),
    )
    await server.start()

    assert fake_debugpy.wait_calls == 1
    assert observed["discovery_present_at_wait"] is True
    # The wait runs before the uvicorn server starts serving.
    assert observed["served_before_wait"] is False
    assert observed.get("served") is True


async def test_devserver_start_no_inspect_wait_never_blocks(
    anyio_backend, monkeypatch, tmp_path: Path
) -> None:
    """Without ``inspect_wait`` (or with an endpoint but the flag off), the
    server never imports debugpy or blocks for an attach."""
    settings = DevServerSettings.from_project_root(tmp_path)
    logger = ConsoleLogger(secho=LogCapture())

    monkeypatch.setattr(
        "pyxle.devserver.build_once",
        lambda cfg, *, force_rebuild=False: BuildSummary(
            compiled_pages=[], copied_api_modules=[], removed=[]
        ),
    )
    monkeypatch.setattr(
        "pyxle.devserver.build_metadata_registry",
        lambda cfg: MetadataRegistry(pages=[], apis=[]),
    )
    monkeypatch.setattr(
        "pyxle.devserver.build_route_table",
        lambda registry: RouteTable(pages=[], apis=[]),
    )
    monkeypatch.setattr("pyxle.devserver.write_client_bootstrap_files", lambda cfg: None)
    monkeypatch.setattr(
        "pyxle.devserver.create_starlette_app",
        lambda cfg, routes, **_: SimpleNamespace(state=SimpleNamespace()),
    )

    class StubVite:
        running = True

        def __init__(self, cfg: DevServerSettings, *, logger: ConsoleLogger, **_: Any) -> None:
            pass

        async def start(self) -> None:
            pass

        async def wait_until_ready(self) -> None:
            pass

        async def stop(self) -> None:
            pass

    monkeypatch.setattr("pyxle.devserver.ViteProcess", StubVite)

    class StubWatcher:
        def __init__(self, cfg: DevServerSettings, *, logger: ConsoleLogger, **kwargs: Any) -> None:
            pass

        def start(self) -> None:
            pass

        def close(self) -> None:
            pass

    monkeypatch.setattr("pyxle.devserver.ProjectWatcher", StubWatcher)

    class StubConfig:
        def __init__(self, app: object, **kwargs: Any) -> None:
            pass

    class StubServer:
        def __init__(self, config: StubConfig) -> None:
            self.should_exit = False

        async def serve(self) -> None:
            pass

    monkeypatch.setattr("pyxle.devserver.uvicorn.Config", StubConfig)
    monkeypatch.setattr("pyxle.devserver.uvicorn.Server", StubServer)

    class ExplodingDebugpy:
        def wait_for_client(self) -> None:  # pragma: no cover - must never run
            raise AssertionError("wait_for_client must not be called")

    monkeypatch.setitem(sys.modules, "debugpy", ExplodingDebugpy())

    # inspect_wait defaults False even though an endpoint is advertised.
    server = DevServer(
        settings=settings, logger=logger, inspect_endpoint=("127.0.0.1", 5682)
    )
    await server.start()  # must complete without touching debugpy
