"""Pyxle command-line interface package."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Optional

import typer
import uvicorn

from pyxle import __version__
from pyxle.compiler import CompilationResult, compile_file
from pyxle.compiler.exceptions import CompilationError
from pyxle.config import ConfigError, PyxleConfig, apply_env_overrides, load_config
from pyxle.devserver.routes import build_route_table
from pyxle.devserver.scripts import GlobalScriptConfigError
from pyxle.devserver.styles import GlobalStyleConfigError
from pyxle.env import EnvFileError, load_env_files

from .init import log_next_steps as _log_next_steps
from .init import run_init as _run_init
from .logger import ConsoleLogger, LogFormat, Verbosity

# Lazily loaded devserver components to avoid import cycles during test collection.
DevServer = None  # type: ignore[assignment]
DevServerSettings = None  # type: ignore[assignment]
create_starlette_app = None  # type: ignore[assignment]

app = typer.Typer(
    name="pyxle",
    add_completion=True,
    no_args_is_help=True,
    help="Pyxle CLI to scaffold and manage full-stack Python web projects.",
)

_logger = ConsoleLogger()
# Placeholder for tests to monkeypatch; resolved lazily at runtime.
run_build = None  # type: ignore[assignment]


def version_callback(value: bool) -> None:
    """Print the package version when ``--version`` is requested."""

    if value:
        typer.echo(__version__)
        raise typer.Exit()


@app.callback()
def main(  # pragma: no cover - Typer handles option parsing.
    ctx: typer.Context,
    version: Optional[bool] = typer.Option(  # noqa: FBT002 - CLI option signature.
        None,
        "--version",
        help="Show Pyxle version and exit.",
        callback=version_callback,
        is_eager=True,
        is_flag=True,
    ),
    log_format: LogFormat = typer.Option(
        LogFormat.CONSOLE,
        "--log-format",
        help="Formatter for CLI logs (console or json).",
        show_default=True,
    ),
    verbose: bool = typer.Option(  # noqa: FBT002 - CLI option signature.
        False,
        "--verbose",
        "-v",
        help="Show debug-level output.",
        is_flag=True,
    ),
    quiet: bool = typer.Option(  # noqa: FBT002 - CLI option signature.
        False,
        "--quiet",
        "-q",
        help="Suppress informational output; only show warnings and errors.",
        is_flag=True,
    ),
) -> None:
    """Root callback primarily used to expose global options."""

    # The body intentionally does nothing; Typer invokes command functions.
    ctx.ensure_object(dict)  # pragma: no cover - exercised implicitly by Typer
    _logger.set_formatter(log_format)
    if quiet:
        _logger.set_verbosity(Verbosity.QUIET)
    elif verbose:
        _logger.set_verbosity(Verbosity.VERBOSE)
    else:
        _logger.set_verbosity(Verbosity.NORMAL)


def get_logger() -> ConsoleLogger:
    """Return the default console logger used across CLI commands."""

    return _logger


def _resolve_run_build():
    if callable(run_build):  # type: ignore[call-arg]
        return run_build  # type: ignore[return-value]

    from pyxle.build import run_build as _run_build  # noqa: PLC0415

    return _run_build


@app.command(help="Create a new Pyxle project scaffold.")
def init(
    name: str = typer.Argument(..., help="Name of the project directory to create."),
    force: bool = typer.Option(
        False,
        "--force",
        "-f",
        help="Overwrite the target directory if it already exists.",
    ),
    template: str = typer.Option(
        "default",
        "--template",
        "-t",
        help="Specify the project template to use (placeholder).",
    ),
    install_deps: bool = typer.Option(
        False,
        "--install/--no-install",
        help="Install Python and Node dependencies after scaffolding completes.",
        show_default=True,
    ),
) -> None:
    """Entry-point for the ``pyxle init`` command."""

    logger = get_logger()
    try:
        project_path = _run_init(
            name,
            force,
            template,
            logger,
            log_steps=not install_deps,
        )
    except typer.Exit:
        raise
    except Exception as exc:  # pragma: no cover - unexpected runtime errors
        logger.error(f"Unexpected error: {exc}")
        raise typer.Exit(code=1) from exc

    if install_deps:
        _install_dependencies(project_path, logger=logger)
        _log_next_steps(logger, project_path, include_install_hint=False)


def _ensure_directory(directory: Path, logger: ConsoleLogger) -> Path:
    resolved = directory.expanduser().resolve()
    if not resolved.exists():
        logger.error(f"Directory '{resolved}' does not exist.")
        raise typer.Exit(code=1)
    if not resolved.is_dir():
        logger.error(f"Path '{resolved}' is not a directory.")
        raise typer.Exit(code=1)
    return resolved


def _in_virtualenv() -> bool:
    """Return ``True`` when running inside a virtual environment.

    Covers ``venv``/``virtualenv`` (``sys.prefix`` diverges from the base
    interpreter prefix) and Conda environments.
    """
    if os.environ.get("VIRTUAL_ENV") or os.environ.get("CONDA_PREFIX"):
        return True
    return sys.prefix != getattr(sys, "base_prefix", sys.prefix)


def _run_subprocess(command: list[str], *, cwd: Path, label: str, logger: ConsoleLogger) -> None:
    logger.step(label, " ".join(command))
    try:
        subprocess.run(command, cwd=cwd, check=True)
    except FileNotFoundError as exc:
        logger.error(f"{label} failed — command '{command[0]}' was not found.")
        raise typer.Exit(code=1) from exc
    except subprocess.CalledProcessError as exc:
        logger.error(f"{label} failed with exit code {exc.returncode}.")
        raise typer.Exit(code=1) from exc


def _install_dependencies(
    project_root: Path,
    *,
    logger: ConsoleLogger,
    install_python: bool = True,
    install_node: bool = True,
) -> None:
    if not install_python and not install_node:
        logger.warning("Skipping dependency installation (both installers disabled).")
        return

    if install_python:
        if not _in_virtualenv():
            logger.warning(
                "No virtual environment detected. Installing into the system "
                "Python can fail on modern Linux (PEP 668 'externally-managed-"
                "environment'). Recommended: create one first — "
                "`python -m venv .venv && source .venv/bin/activate` "
                "(Windows: `.venv\\Scripts\\activate`) — then run `pyxle install`."
            )
        python_cmd = [sys.executable, "-m", "pip", "install", "-r", "requirements.txt"]
        _run_subprocess(
            python_cmd,
            cwd=project_root,
            label="Python dependencies",
            logger=logger,
        )

    if install_node:
        node_cmd = ["npm", "install"]
        _run_subprocess(
            node_cmd,
            cwd=project_root,
            label="Node dependencies",
            logger=logger,
        )

    logger.success("Dependencies installed.")


@app.command(help="Install Python and Node dependencies for a Pyxle project.")
def install(
    directory: Path = typer.Argument(
        Path("."),
        help="Project directory containing requirements.txt and package.json.",
        show_default=True,
    ),
    python_deps: bool = typer.Option(
        True,
        "--python/--no-python",
        help="Install Python dependencies via pip.",
        show_default=True,
    ),
    node_deps: bool = typer.Option(
        True,
        "--node/--no-node",
        help="Install Node dependencies via npm.",
        show_default=True,
    ),
) -> None:
    """Install project dependencies inside the specified directory."""

    logger = get_logger()
    resolved = _ensure_directory(directory, logger)
    _install_dependencies(
        resolved,
        logger=logger,
        install_python=python_deps,
        install_node=node_deps,
    )


@app.command(help="Run the Pyxle development server with hot reload.")
def dev(
    directory: Path = typer.Argument(
        Path("."),
        help="Project root containing pages/ and public/ directories.",
        show_default=True,
    ),
    host: Optional[str] = typer.Option(
        None,
        "--host",
        help="Hostname for the Starlette server (defaults to config or 127.0.0.1).",
        show_default=False,
    ),
    port: Optional[int] = typer.Option(
        None,
        "--port",
        help="Port for the Starlette server (defaults to config or 8000).",
        show_default=False,
    ),
    vite_host: Optional[str] = typer.Option(
        None,
        "--vite-host",
        help="Hostname for the Vite dev server (defaults to config or 127.0.0.1).",
        show_default=False,
    ),
    vite_port: Optional[int] = typer.Option(
        None,
        "--vite-port",
        help="Port for the Vite dev server (defaults to config or 5173).",
        show_default=False,
    ),
    debug: Optional[bool] = typer.Option(
        None,
        "--debug/--no-debug",
        help="Enable debug behaviour for the development server (defaults to config or True).",
        show_default=False,
    ),
    ssr_workers: Optional[int] = typer.Option(
        None,
        "--ssr-workers",
        help="Number of persistent Node.js SSR worker processes (0 = per-request subprocess mode, default: 1).",
        show_default=False,
        min=0,
    ),
    config_file: Optional[Path] = typer.Option(
        None,
        "--config",
        help="Path to a pyxle.config.json file (defaults to <project>/pyxle.config.json).",
    ),
    print_config: bool = typer.Option(
        False,
        "--print-config/--no-print-config",
        help="Print the merged configuration before starting the dev server.",
        show_default=True,
    ),
    tailwind: bool = typer.Option(
        True,
        "--tailwind/--no-tailwind",
        help="Automatically run the Tailwind CSS watcher if tailwind.config.* is present.",
        show_default=True,
    ),
    dashboard: bool = typer.Option(
        False,
        "--dashboard/--no-dashboard",
        help="Periodically print a live observability panel (request/SSR metrics) to the terminal.",
        show_default=True,
    ),
) -> None:
    """Entry-point for the ``pyxle dev`` command."""

    logger = get_logger()
    project_root = directory.expanduser().resolve()

    global DevServer, DevServerSettings
    if DevServer is None or DevServerSettings is None:  # noqa: PLC0206 - module-level caching
        from pyxle.devserver import DevServer as _DevServer
        from pyxle.devserver import DevServerSettings as _DevServerSettings

        DevServer = _DevServer
        DevServerSettings = _DevServerSettings

    global create_starlette_app
    if create_starlette_app is None:  # noqa: PLC0206 - module-level caching
        from pyxle.devserver.starlette_app import (
            create_starlette_app as _create_starlette_app,
        )

        create_starlette_app = _create_starlette_app

    if not project_root.exists():
        logger.error(f"Project directory '{project_root}' does not exist.")
        raise typer.Exit(code=1)
    if not project_root.is_dir():
        logger.error(f"Path '{project_root}' is not a directory.")
        raise typer.Exit(code=1)

    try:
        load_env_files(project_root, mode="development")
    except EnvFileError as exc:
        logger.error(f"Failed to load .env files: {exc}")
        raise typer.Exit(code=1) from exc

    try:
        file_config: PyxleConfig = load_config(project_root, config_path=config_file)
        file_config = apply_env_overrides(file_config)
    except ConfigError as exc:
        logger.error(str(exc))
        raise typer.Exit(code=1) from exc

    overrides = _collect_cli_overrides(
        host=host,
        port=port,
        vite_host=vite_host,
        vite_port=vite_port,
        debug=debug,
    )

    effective_config = file_config.apply_overrides(**overrides)

    if print_config:
        pretty = json.dumps(effective_config.to_dict(), indent=2)
        logger.info(f"Effective configuration:\n{pretty}")

    resolved_styles = _resolve_global_style_entries(project_root, effective_config)
    resolved_scripts = _resolve_global_script_entries(project_root, effective_config)

    extra_settings: dict[str, object] = {}
    if ssr_workers is not None:
        extra_settings["ssr_workers"] = ssr_workers

    try:
        settings = DevServerSettings.from_project_root(  # type: ignore[union-attr]
            project_root,
            **effective_config.to_devserver_kwargs(),
            global_stylesheets=resolved_styles,
            global_scripts=resolved_scripts,
            **extra_settings,
        )
    except (GlobalStyleConfigError, GlobalScriptConfigError) as exc:
        logger.error(str(exc))
        raise typer.Exit(code=1) from exc

    logger.info(
        "Starting Pyxle dev server on http://"
        f"{settings.starlette_host}:{settings.starlette_port}"
        f" with Vite proxy at http://{settings.vite_host}:{settings.vite_port}"
    )

    server = DevServer(settings, logger=logger, tailwind=tailwind, dashboard=dashboard)  # type: ignore[call-arg]

    try:
        asyncio.run(server.start())
    except KeyboardInterrupt:  # pragma: no cover - handled manually during runtime
        logger.warning("Keyboard interrupt received; stopping dev server")
    except Exception as exc:  # pragma: no cover - unexpected runtime errors
        logger.error(f"Dev server encountered an error: {exc}")
        raise typer.Exit(code=1) from exc


@app.command(help="Build production-ready assets for deployment.")
def build(
    directory: Path = typer.Argument(
        Path("."),
        help="Project root containing pages/ and public/ directories.",
        show_default=True,
    ),
    config_file: Optional[Path] = typer.Option(
        None,
        "--config",
        help="Path to a pyxle.config.json file (defaults to <project>/pyxle.config.json).",
    ),
    out_dir: Optional[Path] = typer.Option(
        None,
        "--out-dir",
        help="Directory where build artifacts should be written (defaults to <project>/dist).",
    ),
    incremental: bool = typer.Option(
        False,
        "--incremental/--no-incremental",
        help="Reuse cached artifacts to rebuild only changed files.",
        show_default=True,
    ),
    static: bool = typer.Option(
        False,
        "--static/--no-static",
        help="Pre-render pages with no per-request data to HTML at build time (SSG).",
        show_default=True,
    ),
) -> None:
    """Entry-point for the ``pyxle build`` command."""

    logger = get_logger()
    project_root = directory.expanduser().resolve()

    global DevServerSettings
    if DevServerSettings is None:  # noqa: PLC0206 - module-level caching
        from pyxle.devserver import DevServerSettings as _DevServerSettings

        DevServerSettings = _DevServerSettings

    if not project_root.exists():
        logger.error(f"Project directory '{project_root}' does not exist.")
        raise typer.Exit(code=1)
    if not project_root.is_dir():
        logger.error(f"Path '{project_root}' is not a directory.")
        raise typer.Exit(code=1)

    try:
        load_env_files(project_root, mode="production")
    except EnvFileError as exc:
        logger.error(f"Failed to load .env files: {exc}")
        raise typer.Exit(code=1) from exc

    try:
        file_config: PyxleConfig = load_config(project_root, config_path=config_file)
        file_config = apply_env_overrides(file_config)
    except ConfigError as exc:
        logger.error(str(exc))
        raise typer.Exit(code=1) from exc

    production_config = file_config.apply_overrides(debug=False)
    resolved_styles = _resolve_global_style_entries(project_root, production_config)
    resolved_scripts = _resolve_global_script_entries(project_root, production_config)

    try:
        settings = DevServerSettings.from_project_root(  # type: ignore[union-attr]
            project_root,
            **production_config.to_devserver_kwargs(),
            global_stylesheets=resolved_styles,
            global_scripts=resolved_scripts,
        )
    except (GlobalStyleConfigError, GlobalScriptConfigError) as exc:
        logger.error(str(exc))
        raise typer.Exit(code=1) from exc

    resolved_out_dir: Path | None
    if out_dir is None:
        resolved_out_dir = None
    else:
        candidate = out_dir.expanduser()
        if candidate.is_absolute():
            resolved_out_dir = candidate.resolve()
        else:
            resolved_out_dir = (project_root / candidate).resolve()

    logger.info("Building Pyxle project for production")
    if incremental:
        logger.info("Incremental mode enabled — unchanged sources will be skipped")

    runner = _resolve_run_build()

    try:
        result = runner(
            settings,
            logger=logger,
            dist_dir=resolved_out_dir,
            force_rebuild=not incremental,
        )
    except Exception as exc:  # pragma: no cover - unexpected runtime errors
        logger.error(f"Build failed: {exc}")
        raise typer.Exit(code=1) from exc

    summary = result.summary
    logger.success(
        "Build completed — "
        f"{len(summary.compiled_pages)} page(s) compiled, "
        f"{len(summary.copied_api_modules)} API module(s) copied, "
        f"{len(summary.copied_client_assets)} client asset(s) copied"
    )
    if result.client_manifest_path is not None:
        logger.step("Client manifest", detail=str(result.client_manifest_path))
    if result.page_manifest_path is not None:
        logger.step("Page manifest", detail=str(result.page_manifest_path))
    logger.step("Server modules", detail=str(result.server_dir))
    logger.step("Metadata", detail=str(result.metadata_dir))
    public_detail = str(result.public_dir)
    if not result.public_dir.exists():
        public_detail += " (not generated)"
    logger.step("Public assets", detail=public_detail)
    logger.step("Artifacts", detail=str(result.dist_dir))

    if static:
        from pyxle.build.static_gen import generate_static_site  # noqa: PLC0415

        try:
            rendered = generate_static_site(settings, result.dist_dir, logger=logger)
        except Exception as exc:  # pragma: no cover - prerender boots the Node SSR pool
            logger.error(f"Static prerender failed: {exc}")
            raise typer.Exit(code=1) from exc
        logger.step("Static pages", detail=f"{len(rendered)} pre-rendered")


@app.command(help="Serve a production build without starting Vite.")
def serve(
    directory: Path = typer.Argument(
        Path("."),
        help="Project root containing the build artifacts.",
        show_default=True,
    ),
    host: Optional[str] = typer.Option(
        None,
        "--host",
        help="Hostname for the Starlette server (defaults to config or 127.0.0.1).",
        show_default=False,
    ),
    port: Optional[int] = typer.Option(
        None,
        "--port",
        help="Port for the Starlette server (defaults to config or 8000).",
        show_default=False,
    ),
    dist_dir: Optional[Path] = typer.Option(
        None,
        "--dist-dir",
        help="Directory containing production artifacts (defaults to <project>/dist).",
    ),
    skip_build: bool = typer.Option(
        False,
        "--skip-build/--no-skip-build",
        help="Skip running `pyxle build` before serving.",
        show_default=True,
    ),
    config_file: Optional[Path] = typer.Option(
        None,
        "--config",
        help="Path to a pyxle.config.json file (defaults to <project>/pyxle.config.json).",
    ),
    serve_static: bool = typer.Option(
        True,
        "--serve-static/--no-serve-static",
        help="Serve dist/public and /client assets directly from pyxle serve (disable when offloading to a CDN).",
        show_default=True,
    ),
    ssr_workers: Optional[int] = typer.Option(
        None,
        "--ssr-workers",
        help="Number of persistent Node.js SSR worker processes per server worker "
        "(0 = auto-size to CPU cores, capped at 4; default: 1).",
        show_default=False,
        min=0,
    ),
    workers: int = typer.Option(
        1,
        "--workers",
        "-w",
        help="Number of server worker processes, one per CPU core. >1 serves the "
        "build across that many uvicorn worker processes (multi-core); --ssr-workers "
        "then applies per worker. Default 1 = single process.",
        show_default=True,
        min=1,
    ),
) -> None:
    """Entry-point for the ``pyxle serve`` command."""

    logger = get_logger()
    project_root = _ensure_directory(directory, logger)

    global DevServerSettings
    if DevServerSettings is None:  # noqa: PLC0206 - module-level caching
        from pyxle.devserver import DevServerSettings as _DevServerSettings

        DevServerSettings = _DevServerSettings

    try:
        load_env_files(project_root, mode="production")
    except EnvFileError as exc:
        logger.error(f"Failed to load .env files: {exc}")
        raise typer.Exit(code=1) from exc

    try:
        file_config: PyxleConfig = load_config(project_root, config_path=config_file)
        file_config = apply_env_overrides(file_config)
    except ConfigError as exc:
        logger.error(str(exc))
        raise typer.Exit(code=1) from exc

    production_config = file_config.apply_overrides(
        debug=False,
        starlette_host=host,
        starlette_port=port,
    )
    resolved_styles = _resolve_global_style_entries(project_root, production_config)
    resolved_scripts = _resolve_global_script_entries(project_root, production_config)

    serve_extra: dict[str, object] = {}
    if ssr_workers is not None:
        serve_extra["ssr_workers"] = ssr_workers

    try:
        settings = DevServerSettings.from_project_root(  # type: ignore[union-attr]
            project_root,
            **production_config.to_devserver_kwargs(),
            global_stylesheets=resolved_styles,
            global_scripts=resolved_scripts,
            **serve_extra,
        )
    except (GlobalStyleConfigError, GlobalScriptConfigError) as exc:
        logger.error(str(exc))
        raise typer.Exit(code=1) from exc

    resolved_dist = _resolve_dist_directory(project_root, dist_dir)

    if not skip_build:
        logger.info("Building project before serving")
        runner = _resolve_run_build()
        try:
            runner(settings, logger=logger, dist_dir=resolved_dist, force_rebuild=True)
        except Exception as exc:  # pragma: no cover - unexpected runtime errors
            logger.error(f"Build failed: {exc}")
            raise typer.Exit(code=1) from exc
    else:
        logger.warning("Skipping production build; using existing dist artifacts.")

    # Multi-process serving: hand uvicorn an importable app factory and let it
    # supervise ``workers`` worker subprocesses. Each rebuilds its own app (and
    # SSR pool) from environment variables — see pyxle.build.production.
    if workers > 1:
        _serve_multiworker(
            project_root,
            config_path=config_file,
            dist_dir=dist_dir,
            resolved_dist=resolved_dist,
            host=settings.starlette_host,
            port=settings.starlette_port,
            ssr_workers=ssr_workers,
            serve_static=serve_static,
            workers=workers,
            logger=logger,
        )
        return

    from pyxle.build.production import (  # noqa: PLC0415 - heavy import deferred to runtime
        ProductionServeError,
        build_production_app,
    )

    try:
        app, pool_size = build_production_app(
            settings, resolved_dist, serve_static=serve_static, logger=logger
        )
    except ProductionServeError as exc:
        logger.error(str(exc))
        raise typer.Exit(code=1) from exc

    logger.info(
        f"Serving Pyxle build on http://{settings.starlette_host}:{settings.starlette_port} "
        f"(dist: {resolved_dist}, ssr_workers: {pool_size})"
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

    try:
        asyncio.run(server.serve())
    except KeyboardInterrupt:  # pragma: no cover - handled manually during runtime
        logger.warning("Keyboard interrupt received; stopping production server")
    except Exception as exc:  # pragma: no cover - unexpected runtime errors
        logger.error(f"Production server encountered an error: {exc}")
        raise typer.Exit(code=1) from exc


def _dist_has_websocket_pages(resolved_dist: Path) -> bool:
    """Whether any compiled page in ``resolved_dist`` declares a WS handler.

    Reads the build's ``page-manifest.json`` (a ``{route: entry}`` map) and
    checks for a ``websocket`` entry. Used to warn, at multi-worker serve time,
    that the in-process broker doesn't span workers.
    """
    manifest_path = resolved_dist / "page-manifest.json"
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(data, dict):
        return False
    return any(
        isinstance(entry, dict) and "websocket" in entry
        for entry in data.values()
    )


def _serve_multiworker(
    project_root: Path,
    *,
    config_path: Optional[Path],
    dist_dir: Optional[Path],
    resolved_dist: Path,
    host: str,
    port: int,
    ssr_workers: Optional[int],
    serve_static: bool,
    workers: int,
    logger: ConsoleLogger,
) -> None:
    """Serve a production build across ``workers`` uvicorn worker processes.

    uvicorn's multi-worker mode spawns subprocesses that re-import the app via an
    import string, so the build must already exist (the caller built it once).
    Each worker re-imports :func:`pyxle.build.production.create_app`, which
    rebuilds the app — and its own SSR worker pool — from the ``PYXLE_SERVE_*``
    environment variables set here.
    """
    from pyxle.build.production import (  # noqa: PLC0415 - heavy import deferred to runtime
        FACTORY_IMPORT_STRING,
        serve_worker_env,
    )

    os.environ.update(
        serve_worker_env(
            project_root,
            config_path=config_path,
            dist_dir=dist_dir,
            host=host,
            port=port,
            ssr_workers=ssr_workers,
            serve_static=serve_static,
        )
    )
    logger.info(
        f"Serving Pyxle build on http://{host}:{port} across {workers} worker "
        f"process(es) (dist: {resolved_dist})"
    )
    if _dist_has_websocket_pages(resolved_dist):
        logger.warning(
            "This build has WebSocket page(s) and is running with "
            f"{workers} workers. The default in-process realtime broker does "
            "NOT span worker processes — a client on one worker won't receive "
            "messages published on another. Use a shared broker (e.g. Redis) or "
            "sticky-session load balancing for cross-worker realtime."
        )
    try:
        # loop is left at uvicorn's default ("auto" → uvloop). Forcing the pure
        # asyncio loop here adds a ~40-50ms per-request stall on Linux when
        # multiple worker processes share the listening socket (epoll wakeup
        # behaviour; reproduced on uvicorn 0.42 and 0.49, absent on macOS and
        # absent with workers=1). uvloop does not exhibit it.
        uvicorn.run(
            FACTORY_IMPORT_STRING,
            factory=True,
            host=host,
            port=port,
            workers=workers,
            lifespan="auto",
            log_config=None,
        )
    except KeyboardInterrupt:  # pragma: no cover - handled manually during runtime
        logger.warning("Keyboard interrupt received; stopping production server")


@app.command(help="Validate .pyxl files, configuration, and project dependencies.")
def check(
    directory: Path = typer.Argument(
        Path("."),
        help="Project root to validate.",
        show_default=True,
    ),
    config_file: Optional[Path] = typer.Option(
        None,
        "--config",
        help="Path to a pyxle.config.json file.",
    ),
) -> None:
    """Run validation checks on a Pyxle project without starting the server."""

    logger = get_logger()
    project_root = directory.expanduser().resolve()
    errors: list[str] = []
    warnings: list[str] = []

    if not project_root.exists() or not project_root.is_dir():
        logger.error(f"Project directory '{project_root}' does not exist.")
        raise typer.Exit(code=1)

    pages_dir = project_root / "pages"
    if not pages_dir.is_dir():
        errors.append("Missing pages/ directory")

    # --- Config ---
    try:
        load_config(project_root, config_path=config_file)
    except ConfigError as exc:
        errors.append(f"Config error: {exc}")

    # --- Dependencies ---
    node_modules = project_root / "node_modules"
    if not node_modules.is_dir():
        warnings.append("node_modules/ not found — run 'pyxle install' or 'npm install'")

    package_json = project_root / "package.json"
    if not package_json.is_file():
        warnings.append("No package.json found")

    # --- Compile all .pyxl files ---
    #
    # Tolerant mode collects every diagnostic per file instead of stopping
    # at the first error, so a single ``pyxle check`` run reports ALL
    # syntax/structural problems across the project in one pass.
    # ``validate_jsx=True`` adds Babel-backed JSX syntax validation so
    # broken JSX (``<Tag>...<`` unclosed, mismatched braces, etc.) is
    # surfaced here instead of waiting for the build step.
    pyxl_count = 0
    diagnostics: list[tuple[str, str]] = []  # (file, message) pairs
    if pages_dir.is_dir():
        from pyxle.compiler.parser import PyxParser

        parser = PyxParser()
        for pyxl_file in sorted(pages_dir.rglob("*.pyxl")):
            pyxl_count += 1
            rel_path = str(pyxl_file.relative_to(project_root))
            # Tolerant-mode parse should never raise, but we defensively
            # wrap each call so any unexpected parser bug on a single
            # file doesn't abort the entire check and lose the
            # diagnostics for every file that follows.
            try:
                result = parser.parse(
                    pyxl_file, tolerant=True, validate_jsx=True
                )
            except Exception as exc:  # noqa: BLE001 — defensive CLI boundary
                diagnostics.append(
                    (
                        rel_path,
                        f"[python] parser crashed: {type(exc).__name__}: "
                        f"{exc}",
                    )
                )
                continue
            for diag in result.diagnostics:
                if diag.line is not None:
                    message = f"[{diag.section}] line {diag.line}: {diag.message}"
                else:
                    message = f"[{diag.section}] {diag.message}"
                diagnostics.append((rel_path, message))

    # --- Report ---
    logger.info(f"Checked {pyxl_count} .pyxl file(s) in {project_root.name}/")

    for warning in warnings:
        logger.warning(warning)

    if diagnostics:
        for diag_file, diag_msg in diagnostics:
            logger.diagnostic(diag_msg, file=diag_file, severity="error")

    if errors or diagnostics:
        for error in errors:
            logger.error(error)
        total_errors = len(errors) + len(diagnostics)
        logger.error(f"Check failed with {total_errors} error(s)")
        raise typer.Exit(code=1)

    logger.success("All checks passed")


@app.command(help="Run TypeScript checking on compiled JSX output.")
def typecheck(
    directory: Path = typer.Argument(
        Path("."),
        help="Project root containing pages/ directory.",
        show_default=True,
    ),
    config_file: Optional[Path] = typer.Option(
        None,
        "--config",
        help="Path to a pyxle.config.json file.",
    ),
) -> None:
    """Type-check the generated client code using TypeScript.

    Compiles all .pyxl files, then runs ``tsc --noEmit`` against the
    generated JSX in ``.pyxle-build/client/``.
    """

    logger = get_logger()
    project_root = directory.expanduser().resolve()

    if not project_root.exists() or not project_root.is_dir():
        logger.error(f"Project directory '{project_root}' does not exist.")
        raise typer.Exit(code=1)

    global DevServerSettings
    if DevServerSettings is None:  # noqa: PLC0206 - module-level caching
        from pyxle.devserver import DevServerSettings as _DevServerSettings

        DevServerSettings = _DevServerSettings

    try:
        file_config: PyxleConfig = load_config(project_root, config_path=config_file)
    except ConfigError as exc:
        logger.error(str(exc))
        raise typer.Exit(code=1) from exc

    try:
        settings = DevServerSettings.from_project_root(  # type: ignore[union-attr]
            project_root,
            **file_config.to_devserver_kwargs(),
        )
    except Exception as exc:
        logger.error(f"Failed to create settings: {exc}")
        raise typer.Exit(code=1) from exc

    from pyxle.devserver.builder import build_once  # noqa: PLC0415

    logger.info("Building project before type-checking")
    try:
        build_once(settings)
    except Exception as exc:
        logger.error(f"Build failed: {exc}")
        raise typer.Exit(code=1) from exc

    client_dir = settings.client_build_dir
    tsconfig_path = client_dir / "tsconfig.json"
    if not tsconfig_path.is_file():
        logger.error(f"tsconfig.json not found at '{tsconfig_path}'")
        raise typer.Exit(code=1)

    tsc_command = _find_tsc(project_root)
    if tsc_command is None:
        logger.error(
            "TypeScript compiler (tsc) not found. "
            "Install it with 'npm install --save-dev typescript' or 'npm install -g typescript'."
        )
        raise typer.Exit(code=1)

    full_command = [*tsc_command, "--noEmit", "--project", str(tsconfig_path)]
    logger.step("TypeScript", " ".join(full_command))

    try:
        result = subprocess.run(
            full_command,
            cwd=str(project_root),
            capture_output=True,
            text=True,
            timeout=120,
        )
    except FileNotFoundError:
        logger.error("Failed to execute TypeScript compiler.")
        raise typer.Exit(code=1)
    except subprocess.TimeoutExpired:
        logger.error("TypeScript compilation timed out after 120 seconds.")
        raise typer.Exit(code=1)

    if result.stdout:
        for line in result.stdout.strip().splitlines():
            _emit_tsc_diagnostic(logger, line)

    if result.stderr:
        for line in result.stderr.strip().splitlines():
            logger.warning(line)

    if result.returncode == 0:
        logger.success("Type check passed — no errors found")
    else:
        error_count = sum(1 for line in (result.stdout or "").splitlines() if ": error TS" in line)
        logger.error(f"Type check failed with {error_count} error(s)")
        raise typer.Exit(code=1)


def _emit_tsc_diagnostic(logger: ConsoleLogger, line: str) -> None:
    """Parse a TypeScript error line and emit it as a structured diagnostic."""

    import re  # noqa: PLC0415

    match = re.match(r"^(.+?)\((\d+),(\d+)\):\s*error\s+(TS\d+):\s*(.+)$", line)
    if match:
        file_path, line_no, col_no, code, message = match.groups()
        logger.diagnostic(
            f"{code}: {message}",
            file=file_path,
            line=int(line_no),
            column=int(col_no),
        )
    else:
        logger.error(line)


def _find_tsc(project_root: Path) -> list[str] | None:
    """Locate the TypeScript compiler binary."""

    import shutil  # noqa: PLC0415

    # Local node_modules/.bin/tsc
    local_tsc = project_root / "node_modules" / ".bin" / "tsc"
    if local_tsc.exists():
        return [str(local_tsc)]

    local_tsc_cmd = project_root / "node_modules" / ".bin" / "tsc.cmd"
    if local_tsc_cmd.exists():
        return [str(local_tsc_cmd)]

    # Global tsc
    global_tsc = shutil.which("tsc")
    if global_tsc is not None:
        return [global_tsc]

    # npx fallback
    npx = shutil.which("npx")
    if npx is not None:
        return [npx, "--yes", "tsc"]

    return None


@app.command(help="Display the project's file-based route table.")
def routes(
    directory: Path = typer.Argument(
        Path("."),
        help="Project root containing pages/ directory.",
        show_default=True,
    ),
    config_file: Optional[Path] = typer.Option(
        None,
        "--config",
        help="Path to a pyxle.config.json file.",
    ),
    output_json: bool = typer.Option(
        False,
        "--json",
        help="Output the route table as JSON.",
    ),
) -> None:
    """Show which routes exist and what features they use."""

    logger = get_logger()
    project_root = directory.expanduser().resolve()

    global DevServerSettings
    if DevServerSettings is None:  # noqa: PLC0206 - module-level caching
        from pyxle.devserver import DevServerSettings as _DevServerSettings

        DevServerSettings = _DevServerSettings

    if not project_root.exists() or not project_root.is_dir():
        logger.error(f"Project directory '{project_root}' does not exist.")
        raise typer.Exit(code=1)

    try:
        file_config: PyxleConfig = load_config(project_root, config_path=config_file)
    except ConfigError as exc:
        logger.error(str(exc))
        raise typer.Exit(code=1) from exc

    try:
        settings = DevServerSettings.from_project_root(  # type: ignore[union-attr]
            project_root,
            **file_config.to_devserver_kwargs(),
        )
    except Exception as exc:
        logger.error(f"Failed to create settings: {exc}")
        raise typer.Exit(code=1) from exc

    from pyxle.devserver.builder import build_once

    try:
        build_once(settings)
    except Exception as exc:
        logger.error(f"Build failed: {exc}")
        raise typer.Exit(code=1) from exc

    from pyxle.devserver.registry import load_metadata_registry

    registry = load_metadata_registry(settings)
    table = build_route_table(registry)

    if output_json:
        rows = []
        for page in table.pages:
            rows.append({
                "path": page.path,
                "source": page.source_relative_path.as_posix(),
                "hasLoader": page.has_loader,
                "loaderName": page.loader_name,
                "actions": [a.get("name", "") for a in page.actions],
                "headDynamic": page.head_is_dynamic,
            })
        for api in table.apis:
            rows.append({
                "path": api.path,
                "source": api.source_relative_path.as_posix(),
                "type": "api",
            })
        typer.echo(json.dumps(rows, indent=2))
        return

    # Text table output
    logger.info(f"Routes for {project_root.name}/")
    logger.info("")

    if table.pages:
        logger.info("  Pages:")
        for page in table.pages:
            features: list[str] = []
            if page.has_loader:
                features.append(f"loader={page.loader_name}")
            if page.actions:
                features.append(f"actions={len(page.actions)}")
            if page.head_is_dynamic:
                features.append("dynamic-head")
            feature_str = f"  [{', '.join(features)}]" if features else ""
            source = page.source_relative_path.as_posix()
            logger.step(page.path, detail=f"{source}{feature_str}")

    if table.apis:
        logger.info("")
        logger.info("  API Routes:")
        for api in table.apis:
            source = api.source_relative_path.as_posix()
            logger.step(api.path, detail=source)

    if table.error_boundary_pages:
        logger.info("")
        logger.info("  Error Boundaries:")
        for boundary in table.error_boundary_pages:
            source = boundary.source_relative_path.as_posix()
            logger.step(boundary.path, detail=source)

    total = len(table.pages) + len(table.apis)
    logger.info("")
    logger.success(f"{total} route(s) found")


@app.command(help="Export an OpenAPI 3.1 schema generated from @action request models.")
def openapi(
    directory: Path = typer.Argument(
        Path("."),
        help="Project root containing pages/ directory.",
        show_default=True,
    ),
    config_file: Optional[Path] = typer.Option(
        None, "--config", help="Path to a pyxle.config.json file."
    ),
    out: Optional[Path] = typer.Option(
        None, "--out", "-o", help="Write the schema to this file (default: stdout)."
    ),
    title: str = typer.Option("Pyxle API", "--title", help="OpenAPI info.title."),
    api_version: str = typer.Option(
        "0.1.0", "--api-version", help="OpenAPI info.version."
    ),
) -> None:
    """Generate an OpenAPI document from every ``@action``'s Pydantic body model."""
    logger = get_logger()
    project_root = directory.expanduser().resolve()

    global DevServerSettings
    if DevServerSettings is None:  # noqa: PLC0206 - module-level caching
        from pyxle.devserver import DevServerSettings as _DevServerSettings

        DevServerSettings = _DevServerSettings

    if not project_root.exists() or not project_root.is_dir():
        logger.error(f"Project directory '{project_root}' does not exist.")
        raise typer.Exit(code=1)

    try:
        file_config: PyxleConfig = load_config(project_root, config_path=config_file)
    except ConfigError as exc:
        logger.error(str(exc))
        raise typer.Exit(code=1) from exc

    try:
        settings = DevServerSettings.from_project_root(  # type: ignore[union-attr]
            project_root, **file_config.to_devserver_kwargs()
        )
    except Exception as exc:
        logger.error(f"Failed to create settings: {exc}")
        raise typer.Exit(code=1) from exc

    from pyxle.devserver.builder import build_once

    try:
        build_once(settings)
    except Exception as exc:
        logger.error(f"Build failed: {exc}")
        raise typer.Exit(code=1) from exc

    from pyxle.devserver.openapi import build_openapi_document
    from pyxle.devserver.validation import PydanticNotInstalledError

    try:
        result = build_openapi_document(
            settings, title=title, version=api_version
        )
    except PydanticNotInstalledError as exc:
        logger.error(str(exc))
        raise typer.Exit(code=1) from exc

    if result.import_errors:
        for problem in result.import_errors:
            logger.error(f"Could not import page module: {problem}")
        raise typer.Exit(code=1)

    payload = json.dumps(result.document, indent=2)
    if out is not None:
        out_path = out.expanduser()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(payload + "\n", encoding="utf-8")
        logger.success(f"Wrote OpenAPI schema to {out_path}")
    else:
        typer.echo(payload)


@app.command(name="compile", help="Compile a single .pyxl file (developer utility).", hidden=True)
def compile_command(
    source: Path = typer.Argument(..., help="Path to the .pyxl file to compile."),
    build_root: Path = typer.Option(
        Path(".pyxle-build"),
        "--build-root",
        "-b",
        help="Directory where compiled artifacts should be written.",
        show_default=True,
    ),
) -> None:
    """Invoke the Pyxle compiler for manual verification workflows."""

    logger = get_logger()
    resolved_source = source.expanduser().resolve()
    resolved_build = build_root.expanduser().resolve()

    if not resolved_source.exists():
        logger.error(f"Source file '{resolved_source}' was not found.")
        raise typer.Exit(code=1)

    try:
        result: CompilationResult = compile_file(
            resolved_source,
            build_root=resolved_build,
        )
    except CompilationError as exc:
        logger.error(f"Compilation failed: {exc}")
        raise typer.Exit(code=1) from exc

    logger.step("Client artifact", result.client_output.as_posix())
    logger.step("Server artifact", result.server_output.as_posix())
    logger.step("Metadata", result.metadata_output.as_posix())
    logger.success(result.summary())


def _collect_cli_overrides(
    *,
    host: Optional[str],
    port: Optional[int],
    vite_host: Optional[str],
    vite_port: Optional[int],
    debug: Optional[bool],
) -> dict[str, object]:
    """Return CLI override values for non-null parameters."""

    overrides: dict[str, object] = {}

    if host is not None:
        overrides["starlette_host"] = host

    if port is not None:
        overrides["starlette_port"] = port

    if vite_host is not None:
        overrides["vite_host"] = vite_host

    if vite_port is not None:
        overrides["vite_port"] = vite_port

    if debug is not None:
        overrides["debug"] = debug

    return overrides


def _resolve_global_style_entries(project_root: Path, config: PyxleConfig) -> tuple[str, ...]:
    """Return the configured global styles, auto-detecting defaults when available."""

    entries = list(config.global_styles)
    default_candidate = Path("styles") / "global.css"
    if not entries:
        default_path = project_root / default_candidate
        if default_path.is_file():
            entries.append(default_candidate.as_posix())

    seen: set[str] = set()
    normalized: list[str] = []
    for entry in entries:
        candidate = (Path(entry.strip()).as_posix() if entry else "").strip()
        if not candidate:
            continue
        if candidate in seen:
            continue
        seen.add(candidate)
        normalized.append(candidate)

    return tuple(normalized)


def _resolve_global_script_entries(project_root: Path, config: PyxleConfig) -> tuple[str, ...]:
    """Return configured global scripts with duplicates removed."""

    del project_root  # scripts currently require explicit configuration
    entries = list(config.global_scripts)
    seen: set[str] = set()
    normalized: list[str] = []
    for entry in entries:
        candidate = (Path(entry.strip()).as_posix() if entry else "").strip()
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        normalized.append(candidate)
    return tuple(normalized)


def _resolve_dist_directory(project_root: Path, dist_dir: Optional[Path]) -> Path:
    """Return the resolved distribution directory for production assets."""

    if dist_dir is None:
        return (project_root / "dist").resolve()

    candidate = dist_dir.expanduser()
    if candidate.is_absolute():
        return candidate.resolve()
    return (project_root / candidate).resolve()
