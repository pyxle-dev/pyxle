"""Production ASGI application assembly for ``pyxle serve``.

This module provides two ways to construct the production app:

* :func:`build_production_app` — assemble the app from an already-resolved
  :class:`~pyxle.devserver.settings.DevServerSettings` plus the built ``dist``
  directory. The single-process ``pyxle serve`` fast path calls this directly.
* :func:`create_app` — a zero-argument, importable ASGI *factory* that rebuilds
  everything from environment variables. ``pyxle serve --workers N`` hands the
  import string ``"pyxle.build.production:create_app"`` to uvicorn; each worker
  subprocess re-imports and calls it to get its own app instance (uvicorn's
  multi-worker mode requires an import string, not a constructed app object).

Why this lives in ``pyxle.build``: assembling the production app spans the build
layer (``load_manifest``) and the devserver layer (route table + Starlette app).
Per the project's module-boundary rules ``build`` may depend on ``devserver``
(it already does), but neither may import ``pyxle.cli`` — so the small asset
resolution helpers below are mirrored from their CLI counterparts rather than
imported. Both copies are unit-tested.

Multi-core note: each worker's app starts its own SSR worker pool from the
Starlette lifespan (see :func:`pyxle.devserver.starlette_app.create_starlette_app`),
so multi-process serving needs no shared state. ``--ssr-workers`` is therefore
*per worker* in multi-worker mode (total render workers = ``workers × ssr_workers``).
"""

from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Optional, Sequence

from pyxle.build.manifest import load_manifest
from pyxle.cli.logger import ConsoleLogger
from pyxle.config import PyxleConfig, apply_env_overrides, load_config
from pyxle.devserver.registry import build_metadata_registry
from pyxle.devserver.routes import build_route_table
from pyxle.devserver.settings import DevServerSettings
from pyxle.devserver.starlette_app import create_starlette_app
from pyxle.env import load_env_files

if TYPE_CHECKING:  # pragma: no cover - typing only
    from starlette.applications import Starlette


class ProductionServeError(RuntimeError):
    """A production app could not be assembled from the built artifacts.

    Carries a fully-formed, user-facing message (the CLI logs ``str(exc)`` and
    exits non-zero).
    """


# Environment variables the parent ``pyxle serve`` process sets before handing
# the factory import string to uvicorn, so each worker subprocess can rebuild an
# identical app. Namespaced to avoid clashing with user/public env vars.
_ENV_PREFIX = "PYXLE_SERVE_"
ENV_PROJECT_ROOT = _ENV_PREFIX + "PROJECT_ROOT"
ENV_CONFIG = _ENV_PREFIX + "CONFIG"
ENV_DIST = _ENV_PREFIX + "DIST"
ENV_HOST = _ENV_PREFIX + "HOST"
ENV_PORT = _ENV_PREFIX + "PORT"
ENV_SSR_WORKERS = _ENV_PREFIX + "SSR_WORKERS"
ENV_SERVE_STATIC = _ENV_PREFIX + "SERVE_STATIC"

# Importable target for ``uvicorn.run(..., factory=True)``.
FACTORY_IMPORT_STRING = "pyxle.build.production:create_app"


def _dedupe_entries(entries: Sequence[str]) -> tuple[str, ...]:
    """Normalise and de-duplicate configured asset paths, preserving order."""
    seen: set[str] = set()
    normalized: list[str] = []
    for entry in entries:
        candidate = (Path(entry.strip()).as_posix() if entry else "").strip()
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        normalized.append(candidate)
    return tuple(normalized)


def _resolve_global_style_entries(project_root: Path, config: PyxleConfig) -> tuple[str, ...]:
    """Configured global styles, auto-detecting ``styles/global.css`` when present.

    Mirrors ``pyxle.cli._resolve_global_style_entries`` (kept in sync; ``build``
    may not import ``cli``).
    """
    entries = list(config.global_styles)
    default_candidate = Path("styles") / "global.css"
    if not entries and (project_root / default_candidate).is_file():
        entries.append(default_candidate.as_posix())
    return _dedupe_entries(entries)


def _resolve_global_script_entries(project_root: Path, config: PyxleConfig) -> tuple[str, ...]:
    """Configured global scripts (explicit only), de-duplicated.

    Mirrors ``pyxle.cli._resolve_global_script_entries``.
    """
    del project_root  # scripts require explicit configuration
    return _dedupe_entries(config.global_scripts)


def _resolve_dist_directory(project_root: Path, dist_dir: Optional[Path]) -> Path:
    """Resolve the production ``dist`` directory.

    Mirrors ``pyxle.cli._resolve_dist_directory``.
    """
    if dist_dir is None:
        return (project_root / "dist").resolve()
    candidate = dist_dir.expanduser()
    if candidate.is_absolute():
        return candidate.resolve()
    return (project_root / candidate).resolve()


def build_settings(
    project_root: Path,
    *,
    config_path: Optional[Path] = None,
    host: Optional[str] = None,
    port: Optional[int] = None,
    ssr_workers: Optional[int] = None,
) -> DevServerSettings:
    """Load ``.env`` + config and build production :class:`DevServerSettings`.

    This performs the same resolution as ``pyxle serve`` does before building,
    so a worker subprocess (which only has environment variables) can reconstruct
    identical settings. The manifest is NOT attached here — that happens in
    :func:`build_production_app` once the built ``dist`` is known.

    Raises the underlying ``EnvFileError`` / ``ConfigError`` /
    ``GlobalStyleConfigError`` / ``GlobalScriptConfigError`` on bad input.
    """
    load_env_files(project_root, mode="production")
    file_config = apply_env_overrides(load_config(project_root, config_path=config_path))
    production_config = file_config.apply_overrides(
        debug=False,
        starlette_host=host,
        starlette_port=port,
    )
    extra: dict[str, object] = {}
    if ssr_workers is not None:
        extra["ssr_workers"] = ssr_workers
    return DevServerSettings.from_project_root(
        project_root,
        **production_config.to_devserver_kwargs(),
        global_stylesheets=_resolve_global_style_entries(project_root, production_config),
        global_scripts=_resolve_global_script_entries(project_root, production_config),
        **extra,
    )


def _resolve_pool_size(ssr_workers: int) -> int:
    """Effective SSR worker-pool size (auto-scales when ``0`` is requested)."""
    if ssr_workers == 0:
        return min(os.cpu_count() or 2, 4)
    return ssr_workers


def resolve_server_workers(requested: int) -> int:
    """Effective number of server worker processes.

    ``0`` auto-detects from CPU cores (one worker per core, at least one); any
    other value is used as-is (clamped to a minimum of one).
    """
    if requested == 0:
        return max(1, os.cpu_count() or 1)
    return max(1, requested)


def _resolve_static_dirs(
    settings: DevServerSettings,
    resolved_dist: Path,
    serve_static: bool,
    logger: ConsoleLogger,
) -> tuple[Optional[Path], Optional[Path]]:
    """Resolve the public/client static mount directories from the built ``dist``."""
    if not serve_static:
        logger.info(
            "Static asset serving disabled; ensure your CDN or reverse proxy hosts "
            "/ and /client assets."
        )
        return None, None

    public_dir = resolved_dist / "public"
    if public_dir.exists():
        public_static_dir: Optional[Path] = public_dir
    else:
        logger.warning(
            f"Public assets directory '{public_dir}' does not exist — did you run "
            f"'pyxle build' first? Falling back to source directory "
            f"'{settings.public_dir}'."
        )
        public_static_dir = settings.public_dir

    client_dir = resolved_dist / "client"
    if client_dir.exists():
        client_static_dir: Optional[Path] = client_dir
    else:
        logger.warning(
            f"Client asset directory '{client_dir}' does not exist; /client requests will 404."
        )
        client_static_dir = None

    return public_static_dir, client_static_dir


def build_production_app(
    settings: DevServerSettings,
    resolved_dist: Path,
    *,
    serve_static: bool = True,
    logger: ConsoleLogger | None = None,
) -> tuple["Starlette", int]:
    """Assemble the production Starlette app from settings + a built ``dist``.

    Loads the page manifest, builds the route table, resolves static mounts and
    the SSR worker pool, and returns ``(app, pool_size)`` where ``pool_size`` is
    the number of SSR worker processes this app's pool will run.

    Assumes ``pyxle build`` already produced ``dist`` (this does NOT build).
    Raises :class:`ProductionServeError` if the build artifacts are missing or
    invalid.
    """
    log = logger or ConsoleLogger()

    manifest_path = resolved_dist / "page-manifest.json"
    if not manifest_path.exists():
        raise ProductionServeError(
            f"page-manifest.json not found at '{manifest_path}'. "
            "Run `pyxle build` first or remove --skip-build."
        )
    try:
        manifest_data = load_manifest(manifest_path)
    except Exception as exc:  # pragma: no cover - defensive logging parity with CLI
        raise ProductionServeError(f"Failed to load page-manifest.json: {exc}") from exc

    settings = replace(settings, debug=False, page_manifest=manifest_data)

    try:
        route_table = build_route_table(build_metadata_registry(settings))
    except Exception as exc:  # pragma: no cover - unexpected runtime errors
        raise ProductionServeError(f"Failed to prepare routes: {exc}") from exc

    public_static_dir, client_static_dir = _resolve_static_dirs(
        settings, resolved_dist, serve_static, log
    )

    pool = None
    pool_size = _resolve_pool_size(settings.ssr_workers)
    if pool_size > 0:
        from pyxle.ssr.worker_pool import SsrWorkerPool  # noqa: PLC0415 - heavy, lazy

        pool = SsrWorkerPool(
            size=pool_size,
            project_root=settings.project_root,
            client_root=settings.client_build_dir,
        )

    # A `pyxle build --static` run leaves pre-rendered pages here; the app warms
    # its cache from them on startup. Absent for a normal build (None-safe).
    prerender_dir = resolved_dist / "prerendered"

    app = create_starlette_app(
        settings,
        route_table,
        logger=log,
        pool=pool,
        public_static_dir=public_static_dir,
        client_static_dir=client_static_dir,
        serve_static=serve_static,
        prerender_dir=prerender_dir if prerender_dir.exists() else None,
    )
    app.state.pyxle_ready = True
    return app, pool_size


def serve_worker_env(
    project_root: Path,
    *,
    config_path: Optional[Path],
    dist_dir: Optional[Path],
    host: Optional[str],
    port: Optional[int],
    ssr_workers: Optional[int],
    serve_static: bool,
) -> dict[str, str]:
    """Build the environment overrides that let worker subprocesses rebuild the app.

    Returned values are merged into the child environment by the caller before
    invoking ``uvicorn.run(factory_import_string, factory=True, workers=N)``.
    """
    env: dict[str, str] = {ENV_PROJECT_ROOT: str(project_root)}
    if config_path is not None:
        env[ENV_CONFIG] = str(config_path)
    if dist_dir is not None:
        env[ENV_DIST] = str(dist_dir)
    if host is not None:
        env[ENV_HOST] = host
    if port is not None:
        env[ENV_PORT] = str(port)
    if ssr_workers is not None:
        env[ENV_SSR_WORKERS] = str(ssr_workers)
    env[ENV_SERVE_STATIC] = "1" if serve_static else "0"
    return env


def create_app() -> "Starlette":
    """Importable ASGI factory for ``pyxle serve --workers N`` worker subprocesses.

    Rebuilds the production app from the ``PYXLE_SERVE_*`` environment variables
    set by the parent process. Returns the Starlette app; uvicorn serves it and
    its lifespan starts this worker's own SSR pool.
    """
    project_root = Path(os.environ[ENV_PROJECT_ROOT])

    config_raw = os.environ.get(ENV_CONFIG)
    config_path = Path(config_raw) if config_raw else None

    dist_raw = os.environ.get(ENV_DIST)
    dist_dir = Path(dist_raw) if dist_raw else None

    host = os.environ.get(ENV_HOST) or None

    port_raw = os.environ.get(ENV_PORT)
    port = int(port_raw) if port_raw else None

    ssr_raw = os.environ.get(ENV_SSR_WORKERS)
    ssr_workers = int(ssr_raw) if ssr_raw not in (None, "") else None

    serve_static = os.environ.get(ENV_SERVE_STATIC, "1") != "0"

    settings = build_settings(
        project_root,
        config_path=config_path,
        host=host,
        port=port,
        ssr_workers=ssr_workers,
    )
    resolved_dist = _resolve_dist_directory(project_root, dist_dir)
    app, _ = build_production_app(settings, resolved_dist, serve_static=serve_static)
    return app
