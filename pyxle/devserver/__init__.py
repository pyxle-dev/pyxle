"""Dev server orchestration components."""

from __future__ import annotations

import asyncio
import socket
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Optional

import uvicorn

from .builder import BuildSummary, build_once
from .client_files import write_client_bootstrap_files
from .registry import build_metadata_registry
from .routes import build_route_table
from .settings import DevServerSettings
from .starlette_app import create_starlette_app
from .tailwind import TailwindProcess, detect_postcss_config, detect_tailwind_config
from .vite import ViteProcess
from .watcher import ProjectWatcher, WatcherStatistics

if TYPE_CHECKING:  # pragma: no cover - import cycle guard
    from pyxle.cli.logger import ConsoleLogger


def _default_logger() -> "ConsoleLogger":
    from pyxle.cli.logger import ConsoleLogger as _ConsoleLogger

    return _ConsoleLogger()


@dataclass(slots=True)
class DevServer:
    """High-level orchestrator coordinating Pyxle's development workflow."""

    settings: DevServerSettings
    logger: "ConsoleLogger" = field(default_factory=_default_logger)
    _watcher: Optional[ProjectWatcher] = field(default=None, init=False, repr=False)
    vite_port_search_limit: int = 10
    tailwind: bool = True

    async def start(self) -> None:
        """Run the development server until the underlying uvicorn server exits."""

        logger = self.logger
        settings = self._ensure_vite_port_available(self.settings)
        self.settings = settings

        logger.info("Preparing Pyxle development server")

        await self._ensure_node_modules(settings)

        summary = self._run_initial_build(settings)
        self._log_initial_build(summary)

        write_client_bootstrap_files(settings)

        registry = build_metadata_registry(settings)
        route_table = build_route_table(registry)
        logger.info(
            f"Discovered {len(route_table.pages)} page route(s) and {len(route_table.apis)} API route(s)"
        )

        _pool = None
        if settings.ssr_workers > 0:
            from pyxle.ssr.worker_pool import SsrWorkerPool  # noqa: PLC0415

            _pool = SsrWorkerPool(
                size=settings.ssr_workers,
                project_root=settings.project_root,
                client_root=settings.client_build_dir,
            )

        app = create_starlette_app(settings, route_table, logger=logger, pool=_pool)
        overlay = _resolve_overlay(app)
        loop = asyncio.get_running_loop()

        def _handle_rebuild(stats: WatcherStatistics) -> None:
            _maybe_schedule_reload(overlay, loop, stats)
            if stats.summary is None or not stats.summary.any_changes():
                return
            # Invalidate SSR bundle caches in the worker pool when files change.
            if _pool is not None:
                try:
                    asyncio.run_coroutine_threadsafe(_pool.invalidate(), loop)
                except RuntimeError:
                    pass
            # Refresh the live route table so route-*shape* changes (a renamed/
            # added/removed loader or @action, a new or deleted page, head
            # changes, a layout wrapping a page) take effect without restarting
            # ``pyxle dev``. The build runs here on the watcher thread and may
            # raise on a mid-edit syntax error — never crash the watcher; the
            # single atomic swap is marshaled onto the event loop so it never
            # races in-flight request routing.
            if settings.debug:
                try:
                    new_routes, error_boundaries = _rebuild_app_routes(app, settings)
                except Exception as exc:
                    logger.warning(
                        f"Live route refresh failed; restart `pyxle dev` to apply changes: {exc}"
                    )
                else:
                    loop.call_soon_threadsafe(
                        _apply_refreshed_routes, app, new_routes, error_boundaries
                    )
        config = uvicorn.Config(
            app,
            host=settings.starlette_host,
            port=settings.starlette_port,
            loop="asyncio",
            reload=False,
            lifespan="auto",
            log_config=None,
        )
        server = uvicorn.Server(config)

        watcher: ProjectWatcher | None = None
        vite_process: ViteProcess | None = None
        tailwind_process: TailwindProcess | None = None

        try:
            vite_process = ViteProcess(settings, logger=logger)
            await vite_process.start()
            await vite_process.wait_until_ready()

            if self.tailwind and detect_tailwind_config(settings.project_root) is not None:
                postcss_config = detect_postcss_config(settings.project_root)
                if postcss_config is not None:
                    logger.info(
                        f"Detected {postcss_config.name} \u2014 skipping standalone "
                        "Tailwind watcher; CSS will be processed and hashed by "
                        "Vite via PostCSS."
                    )
                else:
                    tailwind_process = TailwindProcess(settings, logger=logger)
                    await tailwind_process.start()

            watcher = ProjectWatcher(settings, logger=logger, on_rebuild=_handle_rebuild)
            self._watcher = watcher

            logger.info(
                "Starting Starlette on http://"
                f"{settings.starlette_host}:{settings.starlette_port} "
                f"(Vite proxy at http://{settings.vite_host}:{settings.vite_port})"
            )

            watcher.start()
            _set_app_ready_flag(app, True)
            try:
                await server.serve()
            except asyncio.CancelledError:
                logger.warning("Dev server cancellation requested; shutting down")
                server.should_exit = True
                raise
        finally:
            _set_app_ready_flag(app, False)
            if watcher is not None:
                watcher.close()
                self._watcher = None
            if tailwind_process is not None:
                await tailwind_process.stop()
            if vite_process is not None:
                await vite_process.stop()
            logger.info("Dev server stopped")

    async def _ensure_node_modules(self, settings: DevServerSettings) -> None:
        """Run ``npm install`` if ``node_modules/`` is missing and ``package.json`` exists."""

        project_root = settings.project_root
        node_modules = project_root / "node_modules"
        package_json = project_root / "package.json"

        if node_modules.is_dir() or not package_json.is_file():
            return

        import shutil  # noqa: PLC0415

        npm_exec = shutil.which("npm")
        if npm_exec is None:
            self.logger.warning(
                "node_modules/ not found and 'npm' is not available; skipping auto-install"
            )
            return

        self.logger.info("node_modules/ not found — running 'npm install'")
        try:
            process = await asyncio.create_subprocess_exec(
                npm_exec,
                "install",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(project_root),
            )
            stdout_bytes, stderr_bytes = await process.communicate()

            if process.returncode == 0:
                self.logger.success("npm install completed")
            else:
                stderr_text = stderr_bytes.decode(errors="ignore").strip() if stderr_bytes else ""
                self.logger.warning(
                    f"npm install exited with code {process.returncode}"
                    + (f": {stderr_text[:200]}" if stderr_text else "")
                )
        except FileNotFoundError:
            self.logger.warning("Failed to execute 'npm install'")
        except Exception as exc:
            self.logger.warning(f"npm install failed: {exc}")

    # Internal helpers -------------------------------------------------

    def _run_initial_build(self, settings: DevServerSettings) -> BuildSummary:
        try:
            summary = build_once(settings, force_rebuild=True)
        except Exception as exc:
            self.logger.error(f"Initial build failed: {exc}")
            raise
        return summary

    def _log_initial_build(self, summary: BuildSummary) -> None:
        total_compiled = len(summary.compiled_pages)
        total_api_copied = len(summary.copied_api_modules)
        total_assets = len(summary.copied_client_assets)
        total_styles = len(summary.synced_stylesheets)
        total_scripts = len(summary.synced_scripts)
        total_removed = len(summary.removed)

        if summary.any_changes():
            parts = [
                f"{total_compiled} page(s) compiled",
                f"{total_api_copied} API module(s) copied",
                f"{total_assets} client asset(s) copied",
                f"{total_styles} global stylesheet(s) synced",
                f"{total_scripts} global script(s) synced",
                f"{total_removed} artifact(s) removed",
            ]
            message = "; ".join(parts)
            self.logger.success(f"Initial build completed — {message}")
        else:
            self.logger.info("Initial build completed with no changes detected")

    def _ensure_vite_port_available(self, settings: DevServerSettings) -> DevServerSettings:
        host = settings.vite_host
        base_port = settings.vite_port

        for offset in range(self.vite_port_search_limit):
            candidate = base_port + offset
            if self._is_port_available(host, candidate):
                if candidate != base_port:
                    self.logger.warning(
                        f"Vite port {base_port} in use; retrying on {candidate}"
                    )
                    return replace(settings, vite_port=candidate)
                return settings

        raise RuntimeError(
            f"Unable to find available Vite port after {self.vite_port_search_limit} attempts"
        )

    @staticmethod
    def _is_port_available(host: str, port: int) -> bool:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.1)
            result = sock.connect_ex((host, port))
            return result != 0


def _set_app_ready_flag(app: object, ready: bool) -> None:
    state = getattr(app, "state", None)
    if state is None:
        return
    setattr(state, "pyxle_ready", ready)


def _resolve_overlay(app: object):
    state = getattr(app, "state", None)
    if state is None:
        return None
    return getattr(state, "overlay", None)


def _rebuild_app_routes(app, settings: DevServerSettings):
    """Build a fresh Starlette route list + error-boundary registry from the
    current on-disk metadata, reusing the live app's renderer, overlay, and
    config-derived route hooks.

    Powers the dev-server hot route-table refresh — route-*shape* changes apply
    without a restart. File I/O + object construction only, so it is safe to
    call off the event loop (e.g. on the watcher thread). Returns
    ``(routes_list, error_boundaries)``.
    """
    from pyxle.devserver.starlette_app import _build_app_routes  # noqa: PLC0415

    new_table = build_route_table(build_metadata_registry(settings))
    return _build_app_routes(
        settings=settings,
        routes=new_table,
        renderer=app.state.ssr_renderer,
        overlay=getattr(app.state, "overlay", None),
        api_route_hooks=app.state.pyxle_route_hooks[0],
        page_route_hooks=app.state.pyxle_route_hooks[1],
        stream_render=getattr(app.state, "pyxle_stream_render", None),
    )


def _apply_refreshed_routes(app, new_routes, error_boundaries) -> None:
    """Swap a freshly built route list into the live app and drop the SSR
    render cache. Must run on the event-loop thread — the list swap is a single
    atomic assignment relative to (synchronous) request route-matching.
    """
    app.router.routes[:] = new_routes
    app.state.error_boundaries = error_boundaries
    renderer = getattr(app.state, "ssr_renderer", None)
    if renderer is not None:
        renderer.clear()


def _maybe_schedule_reload(overlay, loop, stats: WatcherStatistics) -> bool:
    if overlay is None:
        return False
    if stats.error is not None or stats.summary is None:
        return False
    summary = stats.summary
    changed_paths: list[str] = [
        *summary.compiled_pages,
        *summary.copied_api_modules,
        *summary.removed,
    ]
    if not changed_paths and stats.changed_paths:
        changed_paths = [
            path.as_posix() if isinstance(path, Path) else str(path)
            for path in stats.changed_paths
        ]
    if not changed_paths:
        return False
    coroutine = overlay.notify_reload(changed_paths=changed_paths)
    try:
        asyncio.run_coroutine_threadsafe(coroutine, loop)
    except RuntimeError:
        if hasattr(coroutine, "close"):
            coroutine.close()
        return False
    return True


__all__ = ["DevServer", "DevServerSettings", "ProjectWatcher"]
