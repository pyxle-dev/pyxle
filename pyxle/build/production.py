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
import sys
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Optional, Sequence

from pyxle.build.manifest import load_manifest
from pyxle.cli.logger import ConsoleLogger
from pyxle.config import PyxleConfig, apply_env_overrides, load_config
from pyxle.devserver.build import (
    CACHE_METADATA_FILENAME,
    BuildMetadata,
    load_build_metadata,
)
from pyxle.devserver.registry import build_metadata_registry
from pyxle.devserver.routes import build_route_table
from pyxle.devserver.scripts import GlobalScript, resolve_global_scripts
from pyxle.devserver.settings import CLIENT_BUNDLE_DIR_NAME, DevServerSettings
from pyxle.devserver.starlette_app import create_starlette_app
from pyxle.devserver.styles import GlobalStylesheet, resolve_global_stylesheets
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

#: Sub-directory of ``dist`` holding the app's own **source** files, mirrored
#: there by :func:`pyxle.build.pipeline._prepare_dist`.
DIST_APP_DIRNAME = "app"


def app_source_mirror(resolved_dist: Path) -> Optional[Path]:
    """Return ``dist/app`` — the shipped copy of the app's source tree.

    ``dist`` holds compiled artifacts, but a *running* server still reads plain
    source files from the project:

    * the Python modules colocated under ``pages/`` that routes import —
      ``pages/api/_shared.py``, ``pages/api/__init__.py``,
      ``pages/api/_internal/…``, ``pages/s/[slug]/queries.py``. They are not
      routes, so nothing compiles them into ``dist/server``, yet the compiled
      route that says ``from pages.api._shared import …`` cannot import without
      them;
    * ``pages/**/llms.py`` handlers and colocated ``pages/**/*.md`` files, which
      the AI-accessibility layer reads through ``settings.pages_dir``;
    * the configured global stylesheets, whose contents are read at render time
      and inlined into the document head.

    A deployment that ships only ``dist`` (a Docker ``COPY --from=build``, a CI
    artifact, an rsync of the build output) has none of them — so the build
    mirrors them into ``dist/app`` and serving falls back to that copy.

    Returns ``None`` when the directory is absent (a ``dist`` built by a Pyxle
    older than this mirror), leaving the previous project-root-only behaviour.
    """
    candidate = resolved_dist / DIST_APP_DIRNAME
    return candidate if candidate.is_dir() else None


def _asset_source_root(
    project_root: Path, app_mirror: Optional[Path], relative_entry: str
) -> Path:
    """Root to read a configured global stylesheet/script from.

    The project's own file always wins when it is deployed: ``pyxle serve``
    without ``--skip-build`` runs a build first, and that build must read (and
    re-mirror) the file the developer edited. ``dist/app`` is the fallback that
    keeps a dist-only deployment working. When the entry is in neither place the
    project root is returned so the normal validation raises its usual error.
    """
    if app_mirror is None or (project_root / relative_entry).is_file():
        return project_root
    return app_mirror if (app_mirror / relative_entry).is_file() else project_root


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


def _resolve_global_style_entries(
    project_root: Path, config: PyxleConfig, *, app_mirror: Optional[Path] = None
) -> tuple[str, ...]:
    """Configured global styles, auto-detecting ``styles/global.css`` when present.

    Mirrors ``pyxle.cli._resolve_global_style_entries`` (kept in sync; ``build``
    may not import ``cli``). The auto-detection also considers *app_mirror*, so a
    dist-only deployment finds the stylesheet the build shipped for it.
    """
    entries = list(config.global_styles)
    default_candidate = Path("styles") / "global.css"
    if not entries:
        root = _asset_source_root(project_root, app_mirror, default_candidate.as_posix())
        if (root / default_candidate).is_file():
            entries.append(default_candidate.as_posix())
    return _dedupe_entries(entries)


def _resolve_global_script_entries(project_root: Path, config: PyxleConfig) -> tuple[str, ...]:
    """Configured global scripts (explicit only), de-duplicated.

    Mirrors ``pyxle.cli._resolve_global_script_entries``.
    """
    del project_root  # scripts require explicit configuration
    return _dedupe_entries(config.global_scripts)


def resolve_global_assets(
    project_root: Path, config: PyxleConfig, *, app_mirror: Optional[Path] = None
) -> tuple[tuple[GlobalStylesheet, ...], tuple[GlobalScript, ...]]:
    """Resolve the configured global stylesheets/scripts for a production serve.

    Each entry is resolved against the project root when the file is deployed
    and against the ``dist/app`` mirror otherwise (see
    :func:`_asset_source_root`), then handed to
    :class:`~pyxle.devserver.settings.DevServerSettings` already resolved —
    which is what lets a dist-only deployment start at all. Resolving against
    the project root alone raises
    :class:`~pyxle.devserver.styles.GlobalStyleConfigError` for a configured
    stylesheet whose source was never shipped, and ``pyxle serve`` exits.

    Entries are resolved one at a time so configured order is preserved (it is
    the cascade order of the inlined stylesheets).
    """
    styles: list[GlobalStylesheet] = []
    for entry in _resolve_global_style_entries(project_root, config, app_mirror=app_mirror):
        root = _asset_source_root(project_root, app_mirror, entry)
        styles.extend(resolve_global_stylesheets(root, (entry,)))

    scripts: list[GlobalScript] = []
    for entry in _resolve_global_script_entries(project_root, config):
        root = _asset_source_root(project_root, app_mirror, entry)
        scripts.extend(resolve_global_scripts(root, (entry,)))

    return tuple(styles), tuple(scripts)


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
    dist_dir: Optional[Path] = None,
) -> DevServerSettings:
    """Load ``.env`` + config and build production :class:`DevServerSettings`.

    This performs the same resolution as ``pyxle serve`` does before building,
    so a worker subprocess (which only has environment variables) can reconstruct
    identical settings. The manifest is NOT attached here — that happens in
    :func:`build_production_app` once the built ``dist`` is known.

    *dist_dir* is only used to locate the :func:`app_source_mirror`, so a
    dist-only deployment can resolve global stylesheets/scripts that were
    shipped inside ``dist`` rather than beside the project.

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
    app_mirror = app_source_mirror(_resolve_dist_directory(project_root, dist_dir))
    stylesheets, scripts = resolve_global_assets(
        project_root, production_config, app_mirror=app_mirror
    )
    return DevServerSettings.from_project_root(
        project_root,
        **production_config.to_devserver_kwargs(),
        global_stylesheets=stylesheets,
        global_scripts=scripts,
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

    # Vite's bundle output, not the tree above it. ``dist/client/`` is the build
    # *input* directory — every page's unbundled JSX, Pyxle's own client
    # components, ``vite.config.js``, ``tsconfig.json`` — and the browser only
    # ever loads what Vite emitted into ``dist/client/dist/``. Mounting the
    # parent published the whole input tree; see CLIENT_BUNDLE_DIR_NAME.
    client_dir = resolved_dist / "client" / CLIENT_BUNDLE_DIR_NAME
    if client_dir.exists():
        client_static_dir: Optional[Path] = client_dir
    else:
        logger.warning(
            f"Client bundle directory '{client_dir}' does not exist; "
            f"/client requests will 404."
        )
        client_static_dir = None

    return public_static_dir, client_static_dir


def rebase_settings_onto_dist(
    settings: DevServerSettings, resolved_dist: Path
) -> DevServerSettings:
    """Point *settings* at the compiled artifacts inside ``dist``.

    ``DevServerSettings.from_project_root`` resolves ``build_root`` and its
    ``client``/``server``/``metadata`` children to the **intermediate**
    ``.pyxle-build`` directory, which is correct for ``pyxle dev`` but wrong for
    ``pyxle serve``: the served artifacts live in ``dist``, and ``dist`` is
    exactly what a deployment ships. Serving one while reading routing metadata
    from the other has two failure modes, both silent:

    * ``.pyxle-build`` is **absent** (a deploy or CI job that ships only
      ``dist``) — the registry finds no sources and every page 404s.
    * ``.pyxle-build`` is **stale or rewritten** — anything that recompiles a
      page into it after the build (a project's own ``compile_file``-based test
      helper, an editor tool, an interrupted ``pyxle dev``) resets that page's
      ``client_path`` to the unwrapped ``/pages/…`` module and drops its
      ``wrappers``, because those two fields are written by the layout
      composition pass and not by the compiler. The page then renders in
      production **without its layout** — the layout's ``@server`` loader still
      runs (loader discovery reads different keys of the same metadata), so the
      hydration payload looks correct while the layout markup is simply gone.

    ``dist/server``, ``dist/metadata`` and ``dist/client`` are verbatim copies
    of their ``.pyxle-build`` counterparts (see
    :func:`pyxle.build.pipeline._prepare_dist`), so re-rooting is a pure
    redirection. A ``dist`` that predates the layout is left alone and the
    caller keeps the old behaviour.

    ``pages_dir`` is re-rooted too, but only when the project's own ``pages/``
    is **not** deployed: it points at source files (``llms.py`` handlers,
    colocated ``.md``), and the deployed source is the developer's own copy, so
    it stays authoritative whenever it exists. See :func:`app_source_mirror`.
    """
    metadata_dir = resolved_dist / "metadata"
    server_dir = resolved_dist / "server"
    client_dir = resolved_dist / "client"
    if not (metadata_dir.is_dir() and server_dir.is_dir() and client_dir.is_dir()):
        return settings

    extra: dict[str, object] = {}
    mirrored_pages = _mirrored_pages_dir(settings, resolved_dist)
    if mirrored_pages is not None:
        extra["pages_dir"] = mirrored_pages

    return replace(
        settings,
        build_root=resolved_dist,
        metadata_build_dir=metadata_dir,
        server_build_dir=server_dir,
        client_build_dir=client_dir,
        **extra,
    )


def _mirrored_pages_dir(settings: DevServerSettings, resolved_dist: Path) -> Optional[Path]:
    """The mirrored ``pages/`` inside ``dist/app``, when it has to stand in.

    ``None`` when the project's own pages directory is deployed (it wins), when
    the dist carries no mirror, or when ``pagesDir`` points outside the project
    root — in which case nothing could have mirrored it.
    """
    if settings.pages_dir.is_dir():
        return None
    mirror = app_source_mirror(resolved_dist)
    if mirror is None:
        return None
    try:
        relative = settings.pages_dir.relative_to(settings.project_root)
    except ValueError:
        return None
    candidate = mirror / relative
    return candidate if candidate.is_dir() else None


def _ensure_app_source_importable(resolved_dist: Path) -> None:
    """Put ``dist/app`` on ``sys.path`` so the app's own modules import.

    A compiled route that says ``from pages.api._shared import GREETING``
    resolves that name the same way in production as in development: off
    ``sys.path``. The project root is added by the Starlette app itself
    (:func:`pyxle.devserver.starlette_app._ensure_project_root_on_sys_path`);
    this **appends** the mirror behind it, so a deployment that still carries
    its source tree keeps importing from source and only a dist-only deployment
    falls through to the shipped copy.
    """
    mirror = app_source_mirror(resolved_dist)
    if mirror is None:
        return
    entry = str(mirror)
    if entry not in sys.path:
        sys.path.append(entry)


def _load_dist_build_cache(
    build_root: Path, resolved_dist: Path, log: ConsoleLogger
) -> BuildMetadata:
    """Read the build-cache index (``meta.json``) that lists the compiled sources.

    Prefers the copy inside ``dist``. A ``dist`` produced by a Pyxle older than
    the one that started copying it has none, so fall back to *build_root* (the
    project's intermediate build directory) and say so — that fallback is what
    the stale-metadata bug rides on, and a deployment that ships only ``dist``
    has nothing to fall back to at all.
    """
    if (resolved_dist / CACHE_METADATA_FILENAME).is_file():
        return load_build_metadata(resolved_dist)

    log.warning(
        f"'{resolved_dist / CACHE_METADATA_FILENAME}' is missing — this dist was produced "
        f"by an older Pyxle. Falling back to '{build_root}', which may be stale. "
        "Re-run `pyxle build` to make the dist self-contained."
    )
    return load_build_metadata(build_root)


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

    # The build cache index has to be read against the *original* build root,
    # because the rebase below moves it into ``dist``.
    build_cache = _load_dist_build_cache(settings.build_root, resolved_dist, log)
    settings = rebase_settings_onto_dist(settings, resolved_dist)
    settings = replace(settings, debug=False, page_manifest=manifest_data)

    # Routes are imported while the route table is built (API modules) and on
    # every render (page modules); their own imports of the app's colocated
    # helpers have to resolve first.
    _ensure_app_source_importable(resolved_dist)

    try:
        route_table = build_route_table(build_metadata_registry(settings, build_cache))
    except Exception as exc:  # pragma: no cover - unexpected runtime errors
        raise ProductionServeError(f"Failed to prepare routes: {exc}") from exc

    public_static_dir, client_static_dir = _resolve_static_dirs(
        settings, resolved_dist, serve_static, log
    )

    pool = None
    pool_size = _resolve_pool_size(settings.ssr_workers)
    if pool_size > 0:
        from pyxle.ssr.worker_pool import SsrWorkerPool  # noqa: PLC0415 - heavy, lazy

        from pyxle.ssr.template import vite_owns_stylesheets  # noqa: PLC0415

        pool = SsrWorkerPool(
            size=pool_size,
            project_root=settings.project_root,
            client_root=settings.client_build_dir,
            pages_root=settings.pages_dir,
            vite_owns_css=vite_owns_stylesheets(settings),
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
        dist_dir=dist_dir,
    )
    resolved_dist = _resolve_dist_directory(project_root, dist_dir)
    app, _ = build_production_app(settings, resolved_dist, serve_static=serve_static)
    return app
