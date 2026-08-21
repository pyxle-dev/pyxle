"""Dev server orchestration components."""

from __future__ import annotations

import asyncio
import json
import os
import socket
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Optional

import uvicorn

from pyxle.ssr.paths import clear_resolved_paths

from .builder import BuildFailed, BuildSummary, build_once
from .client_files import write_client_bootstrap_files
from .dev_origins import (
    is_wildcard_host,
    local_ipv4_addresses,
    vite_reachability_warning,
)
from .registry import build_metadata_registry
from .routes import RouteTable, build_route_table
from .settings import DevServerSettings
from .starlette_app import create_starlette_app
from .tailwind import TailwindProcess, detect_postcss_config, detect_tailwind_config
from .vite import ViteProcess
from .watcher import ProjectWatcher, WatcherStatistics

if TYPE_CHECKING:  # pragma: no cover - import cycle guard
    from pyxle.cli.logger import ConsoleLogger

    from .log_forwarding import BrowserConsoleLogHandler
    from .overlay import OverlayManager

#: How often to re-check ``uvicorn.Server.started`` while waiting to announce
#: readiness. Small enough that the startup time the banner reports stays
#: honest, large enough not to spin the loop while Vite and the lifespan work.
READY_POLL_INTERVAL_S = 0.005


def _attach_log_forwarding(
    overlay: "OverlayManager",
    loop: asyncio.AbstractEventLoop,
    logger: "ConsoleLogger",
    project_root: Optional[Path] = None,
    build_root: Optional[Path] = None,
) -> "BrowserConsoleLogHandler":
    """Attach the dev-only server-log → browser-console forwarding handler.

    Forwards INFO+ records to connected overlay clients; in verbose mode it also
    forwards DEBUG and the framework's own internal loggers. *project_root* and
    *build_root* let a page's records be labelled with the ``.pyxl`` that
    emitted them instead of the module key it is compiled under.
    """
    from pyxle.cli.logger import Verbosity  # noqa: PLC0415

    from .log_forwarding import BrowserConsoleLogHandler  # noqa: PLC0415

    verbose = getattr(logger, "verbosity", None) == Verbosity.VERBOSE
    handler = BrowserConsoleLogHandler(
        overlay,
        loop,
        verbose=verbose,
        project_root=project_root,
        build_root=build_root,
    )
    handler.attach()
    return handler


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
    # When true, periodically print a live observability panel (request/SSR
    # metrics) to the terminal. Dev-only convenience; off by default.
    dashboard: bool = False
    dashboard_interval: float = 5.0
    # When set, open the system browser at this server path once the dev
    # server is ready (used by ``pyxle studio`` to land on the dashboard).
    open_browser_path: Optional[str] = None
    # (host, port) of the in-process debugpy debug server when ``pyxle dev
    # --inspect`` hosted one; recorded in the discovery file so editors can
    # attach without configuration.
    inspect_endpoint: Optional[tuple] = None
    # ``--inspect-wait``: block until a debugger attaches. Applied AFTER the
    # discovery file is written so editors can find the endpoint to attach to.
    inspect_wait: bool = False

    async def start(self) -> None:
        """Run the development server until the underlying uvicorn server exits."""

        logger = self.logger
        start_time = time.perf_counter()
        settings = self._ensure_vite_port_available(self.settings)
        self.settings = settings

        logger.debug("Preparing Pyxle development server")

        await self._ensure_node_modules(settings)

        # Claim the project's dev-server record before anything generates a
        # client config from it. The record says which addresses this project's
        # ``vite.config.js`` describes (see ``dev_origins.active_dev_session``),
        # and a stale one — left by a crashed run, its pid since recycled by an
        # unrelated process — would otherwise be treated as a live server whose
        # settings this build must preserve.
        self._write_discovery_file(settings)

        summary = self._run_initial_build(settings)
        self._log_initial_build(summary)

        write_client_bootstrap_files(settings)

        # Re-assert the discovery file (the VS Code extension reads it to attach
        # the debugger and open Studio): the first build pass may rmtree a stale
        # build root, taking the claim above with it. The watcher never observes
        # .pyxle-build, so the file is stable from here.
        self._write_discovery_file(settings)

        # Everything from here until the serve try/finally can still fail — most
        # notably a Ctrl+C during ``--inspect-wait`` below, or app construction —
        # and that path is *before* the finally that removes the discovery file.
        # Guard it so an aborted startup never leaves a stale dev-server.json
        # advertising a server that no longer exists.
        try:
            # ``--inspect-wait``: block for a debugger now that the discovery file
            # exists, so an editor can read the endpoint and attach. Runs before
            # the event loop starts serving, so boot-time breakpoints still bind.
            if self.inspect_wait and self.inspect_endpoint is not None:
                import debugpy  # noqa: PLC0415 - only reached when --inspect set up debugpy

                logger.info("Waiting for a debugger to attach (--inspect-wait)…")
                debugpy.wait_for_client()

            registry = build_metadata_registry(settings)
            route_table = build_route_table(registry)
            logger.debug(
                f"Discovered {len(route_table.pages)} page route(s) and "
                f"{len(route_table.apis)} API route(s)"
            )

            _pool = None
            if settings.ssr_workers > 0:
                from pyxle.ssr.worker_pool import SsrWorkerPool  # noqa: PLC0415

                from pyxle.ssr.template import vite_owns_stylesheets  # noqa: PLC0415

                _pool = SsrWorkerPool(
                    size=settings.ssr_workers,
                    project_root=settings.project_root,
                    client_root=settings.client_build_dir,
                    pages_root=settings.pages_dir,
                    vite_owns_css=vite_owns_stylesheets(settings),
                )

            app = create_starlette_app(settings, route_table, logger=logger, pool=_pool)
            # Seed the gate from the startup build, so a page that was already
            # broken before the server started is refused from the first
            # request rather than only after the next save.
            _record_build_failures(app, summary)
            overlay = _resolve_overlay(app)
            loop = asyncio.get_running_loop()
        except BaseException:
            self._remove_discovery_file(settings)
            raise

        def _handle_rebuild(stats: WatcherStatistics) -> None:
            # A build pass can rmtree a corrupt build root (schema mismatch),
            # taking the discovery file with it — re-assert it after every pass.
            self._write_discovery_file(settings)
            # Studio hears about every finished rebuild — success or failure —
            # so its activity feed always matches what the terminal reported.
            _notify_studio_rebuild(
                getattr(app.state, "pyxle_studio", None), loop, stats
            )
            # Which sources are broken decides what the server may serve, so it
            # is recorded before anything else acts on this pass.
            _record_build_failures(app, stats.summary)
            # A pass that failed on one file still built every other file, so
            # the reload goes out either way — an edit to a working page must
            # land while an unrelated page is unparseable. The error is
            # broadcast after it, so a client that acts on both ends up showing
            # the failure rather than a stale all-clear.
            _maybe_schedule_reload(overlay, loop, stats)
            _notify_rebuild_error(overlay, loop, stats)
            if not _pass_changed_running_code(stats):
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
                        "Route table not refreshed — the previous one is still "
                        f"serving, so this change has not taken effect: {exc}"
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
        dashboard_task: asyncio.Task | None = None
        announce_task: asyncio.Task | None = None
        log_forwarder: "BrowserConsoleLogHandler | None" = None

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

            # Let the watcher refresh the static-file index when a public/ file
            # is added or removed, so it becomes discoverable without a restart.
            static_index = getattr(app.state, "pyxle_static_index", None)
            public_index_refresh = (
                static_index.resync if static_index is not None else None
            )
            watcher = ProjectWatcher(
                settings,
                logger=logger,
                on_rebuild=_handle_rebuild,
                public_index_refresh=public_index_refresh,
            )
            self._watcher = watcher

            logger.debug(
                "Starting Starlette on http://"
                f"{settings.starlette_host}:{settings.starlette_port} "
                f"(Vite proxy at http://{settings.vite_host}:{settings.vite_port})"
            )

            watcher.start()
            _set_app_ready_flag(app, True)

            # Dev-only: forward server-side ``logging`` records to the browser
            # devtools console over the overlay WebSocket. Attached here (after
            # the app is ready) and detached in the ``finally`` below so nothing
            # leaks past shutdown. Never runs under ``pyxle serve``.
            if settings.debug and overlay is not None:
                log_forwarder = _attach_log_forwarding(
                    overlay, loop, logger, settings.project_root, settings.build_root
                )

            # The readiness banner is announced from a side task rather than
            # here, because everything that can still fail — the ASGI lifespan
            # (plugin startup, database connections, config validation) and the
            # socket bind — happens inside ``server.serve()`` below. Announcing
            # first turns a failed boot into a green "ready" line followed by a
            # silent exit.
            announce_task = loop.create_task(
                self._announce_when_serving(
                    server, settings, route_table, start_time, vite_process
                )
            )
            dashboard_task = self._start_dashboard(app, loop)
            try:
                await server.serve()
            except asyncio.CancelledError:
                logger.warning("Dev server cancellation requested; shutting down")
                server.should_exit = True
                raise
        finally:
            _set_app_ready_flag(app, False)
            if log_forwarder is not None:
                log_forwarder.detach()
            if announce_task is not None:
                announce_task.cancel()
            if dashboard_task is not None:
                dashboard_task.cancel()
            if watcher is not None:
                watcher.close()
                self._watcher = None
            # Remove the discovery file only AFTER the watcher is closed: a
            # last-moment rebuild event (an editor flushing a save as the user
            # Ctrl+C's) would otherwise re-create it right after removal.
            self._remove_discovery_file(settings)
            if tailwind_process is not None:
                await tailwind_process.stop()
            if vite_process is not None:
                await vite_process.stop()
            logger.debug("Dev server stopped")

    def _write_discovery_file(self, settings: DevServerSettings) -> None:
        """Persist this server's coordinates for editor tooling.

        ``<build_root>/dev-server.json`` describes the running dev server —
        HTTP/Vite ports, the Studio URL, and the debugpy endpoint when
        ``--inspect`` hosted one. The VS Code extension reads it to power
        one-click attach and "Open Studio". Written atomically; never raises
        (a failed write must not take down the dev server).
        """
        import pyxle  # noqa: PLC0415 - version only; avoids import cycles at module load

        from .studio import STUDIO_PATH  # noqa: PLC0415
        from .studio import is_enabled as _studio_is_enabled  # noqa: PLC0415

        browser_host = settings.starlette_host
        if browser_host in ("0.0.0.0", "::", ""):
            browser_host = "127.0.0.1"
        studio_url: Optional[str] = None
        if settings.debug and _studio_is_enabled(getattr(settings, "studio", None)):
            studio_url = (
                f"http://{browser_host}:{settings.starlette_port}{STUDIO_PATH}"
            )
        payload = {
            "pid": os.getpid(),
            "version": pyxle.__version__,
            "startedAt": time.time(),
            "projectRoot": str(settings.project_root),
            "server": {"host": settings.starlette_host, "port": settings.starlette_port},
            "vite": {"host": settings.vite_host, "port": settings.vite_port},
            "url": f"http://{browser_host}:{settings.starlette_port}",
            "studio": studio_url,
            "debugpy": (
                {"host": self.inspect_endpoint[0], "port": self.inspect_endpoint[1]}
                if self.inspect_endpoint is not None
                else None
            ),
        }
        target = settings.build_root / "dev-server.json"
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
            # os.replace can raise PermissionError on Windows when the target is
            # momentarily open by a reader (the editor polling it); retry briefly
            # before giving up (it is re-written on every rebuild regardless).
            for attempt in range(5):
                try:
                    tmp.replace(target)
                    break
                except PermissionError:  # pragma: no cover - Windows-only timing
                    if attempt == 4:
                        tmp.unlink(missing_ok=True)
                        raise
                    time.sleep(0.05 * (attempt + 1))
        except OSError as exc:
            self.logger.debug(f"Could not write dev-server.json: {exc}")

    def _remove_discovery_file(self, settings: DevServerSettings) -> None:
        path = settings.build_root / "dev-server.json"
        try:
            # Only remove the file if it still describes THIS process. A second
            # `pyxle dev` on the same project may have overwritten it with its own
            # coordinates; deleting that would strand the still-running instance
            # (no attach / Open Studio). A missing or unreadable file falls
            # through to unlink (nothing to strand).
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(data, dict) and data.get("pid") not in (
                    None,
                    os.getpid(),
                ):
                    return
            except (OSError, ValueError):
                # Unreadable or not valid JSON — there is no pid to compare, so
                # nothing can be stranded by removing it. Fall through to unlink.
                pass
            path.unlink(missing_ok=True)
        except OSError:  # pragma: no cover - best-effort cleanup
            pass

    def _maybe_open_browser(self, settings: DevServerSettings) -> None:
        """Open the system browser at ``open_browser_path``, if set.

        Runs on a daemon thread — ``webbrowser.open`` can block for seconds on
        some platforms and must never stall server startup. A bind-all host is
        rewritten to a loopback address the browser can actually reach.
        """
        path = self.open_browser_path
        if not path:
            return
        host = settings.starlette_host
        if host in ("0.0.0.0", "::", ""):
            host = "127.0.0.1"
        url = f"http://{host}:{settings.starlette_port}{path}"
        self.logger.info(f"Opening {url}")

        def _open() -> None:
            import webbrowser  # noqa: PLC0415

            try:
                webbrowser.open(url)
            except Exception:  # noqa: BLE001 — a headless box is not an error
                pass

        import threading  # noqa: PLC0415

        threading.Thread(target=_open, name="pyxle-open-browser", daemon=True).start()

    def _start_dashboard(self, app, loop) -> "Optional[asyncio.Task]":
        """Start the terminal observability dashboard task, if enabled."""
        if not self.dashboard:
            return None
        registry = getattr(app.state, "pyxle_metrics", None)
        started_at = getattr(app.state, "pyxle_started_at", None)
        if registry is None or not isinstance(started_at, (int, float)):
            return None

        from pyxle.observability.dashboard import run_dashboard  # noqa: PLC0415

        return loop.create_task(
            run_dashboard(
                get_snapshot=registry.snapshot,
                emit=self.logger.info,
                uptime=lambda: max(0.0, time.time() - float(started_at)),
                interval_s=self.dashboard_interval,
            )
        )

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
        """Build the project once before serving.

        A file that does not compile is reported and the server starts anyway:
        the routes that *do* build stay usable, and the broken one answers with
        the compile error instead of taking the whole app down. Any other
        failure (an unreadable build directory, a scanner refusing the project)
        is fatal, because it says nothing can be served at all.
        """
        try:
            summary = build_once(settings, force_rebuild=True)
        except BuildFailed as exc:
            for failure in exc.failures:
                self.logger.error(f"Build failed: {failure.describe()}")
            return exc.summary
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
            # Detailed build breakdown is verbose-only noise; the curated ready
            # summary reports the route count. `--verbose` restores this.
            self.logger.debug(f"Initial build completed — {message}")
        else:
            self.logger.debug("Initial build completed with no changes detected")

    async def _announce_when_serving(
        self,
        server: uvicorn.Server,
        settings: DevServerSettings,
        route_table: RouteTable,
        start_time: float,
        vite_process: ViteProcess | None,
    ) -> None:
        """Announce readiness once uvicorn is genuinely accepting connections.

        ``uvicorn.Server.started`` flips at the very end of the server's startup
        sequence — after the ASGI lifespan completed (where plugins start,
        databases connect and settings are validated) *and* after the listening
        socket is bound. Waiting for it means a boot that dies in either place
        prints no success banner at all, instead of the green "ready in 646 ms"
        that used to precede a silent exit.

        The caller cancels this task when the server stops, so the wait needs no
        timeout of its own.
        """
        while not server.started:
            if server.should_exit:
                return
            await asyncio.sleep(READY_POLL_INTERVAL_S)

        # Vite can pass its readiness probe and then crash-loop on an
        # unsupported toolchain. A success banner beside a red Vite fatal is the
        # worst possible signal, so surface the failure instead.
        if vite_process is not None and not vite_process.running:
            self.logger.error(
                "Vite exited immediately after starting; the dev server is "
                "not ready. Check the Vite output above (an unsupported "
                "Node.js version is the most common cause)."
            )
            return

        self._log_ready_summary(self.logger, settings, route_table, start_time)
        self._maybe_open_browser(settings)

    def _log_ready_summary(
        self,
        logger: "ConsoleLogger",
        settings: DevServerSettings,
        route_table: RouteTable,
        start_time: float,
    ) -> None:
        """Emit the curated, always-visible dev-server startup summary.

        Shows the local URL, the Vite URL, the route count, and the total
        startup time — the signal a developer actually wants — while the raw
        Vite firehose and internal build chatter stay behind ``--verbose``.
        """
        elapsed_ms = (time.perf_counter() - start_time) * 1000
        logger.success(f"Pyxle dev server ready in {elapsed_ms:.0f} ms")
        # Print addresses a browser can actually open. ``0.0.0.0`` is a bind
        # address, not a destination — printing it verbatim gives the developer
        # a URL that cannot be clicked.
        local_host = (
            "localhost"
            if is_wildcard_host(settings.starlette_host)
            else settings.starlette_host
        )
        logger.info(f"  Local:   http://{local_host}:{settings.starlette_port}")
        if is_wildcard_host(settings.starlette_host):
            for address in local_ipv4_addresses():
                logger.info(
                    f"  Network: http://{address}:{settings.starlette_port}"
                )
        vite_host = (
            "localhost" if is_wildcard_host(settings.vite_host) else settings.vite_host
        )
        logger.info(f"  Vite:    http://{vite_host}:{settings.vite_port}")
        logger.info(
            f"  Routes:  {len(route_table.pages)} page(s), "
            f"{len(route_table.apis)} API route(s)"
        )
        # A page whose scripts are served from a host the visitor cannot reach
        # renders completely and never hydrates, and says nothing about it in
        # the browser. Say it here instead.
        unreachable = vite_reachability_warning(
            starlette_host=settings.starlette_host,
            starlette_port=settings.starlette_port,
            vite_host=settings.vite_host,
            vite_port=settings.vite_port,
        )
        if unreachable is not None:
            logger.warning(unreachable)
        # Surface Studio so the dashboard is discoverable — otherwise a flagship
        # dev feature is invisible unless you already know its URL.
        from .studio import STUDIO_PATH  # noqa: PLC0415
        from .studio import is_enabled as _studio_is_enabled  # noqa: PLC0415

        if settings.debug and _studio_is_enabled(getattr(settings, "studio", None)):
            logger.info(
                f"  Studio:  http://{local_host}:{settings.starlette_port}{STUDIO_PATH}"
            )

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
        action_route_hooks=app.state.pyxle_route_hooks[2]
        if len(app.state.pyxle_route_hooks) > 2
        else (),
        stream_render=getattr(app.state, "pyxle_stream_render", None),
        studio=getattr(app.state, "pyxle_studio", None),
    )


def _apply_refreshed_routes(app, new_routes, error_boundaries) -> None:
    """Swap a freshly built route list into the live app and drop the SSR
    render cache. Must run on the event-loop thread — the list swap is a single
    atomic assignment relative to (synchronous) request route-matching.
    """
    app.router.routes[:] = new_routes
    app.state.error_boundaries = error_boundaries
    # Component paths are canonicalised through a per-process memo, so a
    # rebuild that moves or re-links build output must drop it alongside the
    # bundle cache or a stale canonical path would be handed to the worker.
    clear_resolved_paths()
    renderer = getattr(app.state, "ssr_renderer", None)
    if renderer is not None:
        renderer.clear()


def _pass_changed_running_code(stats: WatcherStatistics) -> bool:
    """Whether this pass changed anything the server actually runs.

    Two different things count, and using only the first is a bug that has hit
    Pyxle before: a build *artifact* changed (a page recompiled, an API module
    copied), **or** a Python module was dropped from ``sys.modules``. A helper
    beside an endpoint — ``pages/api/_shared.py``, anything under ``dev.watch``
    — is never a build artifact, so editing one produces a summary with no
    changes whatsoever. Judging by the summary alone, the listener concluded
    nothing had happened and skipped the route-table refresh, which is what
    re-imports endpoint modules: the endpoint went on serving the helper's old
    values with no rebuild line, no error, and nothing to restart. This is the
    same predicate the watcher already uses to decide whether to advance the
    module-reload generation; the two must not disagree.

    A pass with no summary at all died before it could describe anything, so
    there is nothing safe to act on.
    """
    if stats.summary is None:
        return False
    return bool(stats.summary.any_changes() or stats.purged_modules)


def _record_build_failures(app, summary: BuildSummary | None) -> None:
    """Publish which sources the last pass could not compile.

    The registry is what stops a page whose build failed being served as a
    healthy ``200`` from the previous pass' artifacts. It is replaced wholesale
    every pass — including with an empty set after a clean one — so it always
    describes the build on disk right now and never accumulates stale entries.

    A ``None`` summary means the pass died before it could say anything about
    individual files; the previous set is left in place rather than being
    replaced with a claim that nothing is broken.
    """
    registry = getattr(getattr(app, "state", None), "pyxle_build_failures", None)
    if registry is None or summary is None:
        return
    registry.replace(summary.failures)


def _notify_rebuild_cleared(overlay, loop) -> None:
    """Retract the sticky rebuild error after a pass that compiled everything.

    Pairs with :func:`_notify_rebuild_error`: without it the overlay would keep
    replaying a build failure to every newly connected client long after the
    file was fixed.
    """
    if overlay is None:
        return
    coroutine = overlay.notify_clear(route_path=_REBUILD_ROUTE_LABEL)
    try:
        asyncio.run_coroutine_threadsafe(coroutine, loop)
    except RuntimeError:  # loop shutting down — nothing to notify
        coroutine.close()


#: Route label the rebuild reports errors under. Not a real route, so it can
#: never collide with a page's own error state in the overlay's error table.
_REBUILD_ROUTE_LABEL = "(rebuild)"


def _notify_rebuild_error(overlay, loop, stats: WatcherStatistics) -> bool:
    """Broadcast a failed rebuild to the browser overlay.

    The architecture docs promise that a build failure (e.g. a parser error
    saved mid-edit) reaches the WebSocket overlay so the browser shows it
    inline — the watcher thread marshals the notification onto the event
    loop here. The overlay keeps the error until it is retracted, so a client
    that connects afterwards (a reload, a second tab) is told about it too.

    Returns ``True`` when the stats describe a failure, whether or not an
    overlay is connected.
    """
    if stats.error is None:
        _notify_rebuild_cleared(overlay, loop)
        return False
    if overlay is not None:
        if isinstance(stats.error, BuildFailed):
            # A compile failure already names every file it is about. Listing
            # the paths that *triggered* the pass beside it points at the file
            # the developer just saved, which is usually not the broken one.
            detail = str(stats.error)
        else:
            changed = ", ".join(
                path.as_posix() if isinstance(path, Path) else str(path)
                for path in stats.changed_paths
            )
            detail = f"{stats.error} (changed: {changed or 'unknown'})"
        breadcrumbs = [
            {
                "label": "Rebuild",
                "status": "failed",
                "detail": detail,
            }
        ]
        try:
            asyncio.run_coroutine_threadsafe(
                overlay.notify_error(
                    route_path=_REBUILD_ROUTE_LABEL,
                    error=stats.error,
                    breadcrumbs=breadcrumbs,
                ),
                loop,
            )
        except RuntimeError:  # loop shutting down — nothing to notify
            pass
    return True


def _notify_studio_rebuild(studio, loop, stats: WatcherStatistics) -> None:
    """Broadcast a finished rebuild to Pyxle Studio's event stream.

    Runs on the watcher thread; the coroutine is marshaled onto the event
    loop. A shutting-down loop is tolerated (nothing left to notify).
    """
    if studio is None:
        return
    coroutine = studio.notify_rebuild(stats)
    try:
        asyncio.run_coroutine_threadsafe(coroutine, loop)
    except RuntimeError:  # loop shutting down — nothing to notify
        coroutine.close()


def _maybe_schedule_reload(overlay, loop, stats: WatcherStatistics) -> bool:
    """Reload connected clients for whatever this pass actually rebuilt.

    A :class:`~pyxle.devserver.builder.BuildFailed` pass still carries a real
    summary — it compiled every file except the broken ones — so its successful
    half is reloaded too. Any other exception means the pass stopped somewhere
    unknown, and its summary cannot be trusted to describe the build on disk.
    """
    if overlay is None:
        return False
    if stats.summary is None:
        return False
    if stats.error is not None and not isinstance(stats.error, BuildFailed):
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
