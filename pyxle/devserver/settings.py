"""Configuration models for the Pyxle development server."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Sequence

from .dev_origins import resolve_vite_bind_host
from .scripts import GlobalScript, resolve_global_scripts
from .styles import GlobalStylesheet, resolve_global_stylesheets

#: Subdirectory of the client build tree that Vite writes its bundle into.
#:
#: The client tree holds two very different things. Its root is the build
#: *input* — generated page JSX, Pyxle's own client components, the generated
#: ``vite.config.js`` and ``tsconfig.json`` — none of which a browser ever
#: requests. Everything the browser does load is what Vite emitted into this
#: subdirectory. It is therefore the boundary between private and public: the
#: production asset mount, the ``--analyze`` walk, and the base URL handed to
#: Vite are all rooted here.
CLIENT_BUNDLE_DIR_NAME = "dist"


@dataclass(frozen=True, slots=True)
class DevServerSettings:
    """Resolved configuration for running the Pyxle development server.

    This container keeps paths and network coordinates that other components
    rely upon. Use :meth:`from_project_root` to construct settings from a
    project layout; the helper ensures every path is absolute and ready for
    downstream filesystem operations.
    """

    project_root: Path
    build_root: Path
    pages_dir: Path
    public_dir: Path
    client_build_dir: Path
    server_build_dir: Path
    metadata_build_dir: Path
    starlette_host: str
    starlette_port: int
    vite_host: str
    vite_port: int
    debug: bool
    custom_middlewares: tuple[str, ...] = ()
    page_route_hooks: tuple[str, ...] = ()
    api_route_hooks: tuple[str, ...] = ()
    action_route_hooks: tuple[str, ...] = ()
    # Number of persistent Node.js SSR worker processes. 0 means per-request
    # subprocess rendering in the dev server, but auto-sizes the pool in
    # production serve (pyxle.build.production._resolve_pool_size).
    ssr_workers: int = 1
    # Optional: loaded page manifest for production asset resolution
    page_manifest: dict[str, Any] | None = None
    global_stylesheets: tuple[GlobalStylesheet, ...] = ()
    global_scripts: tuple[GlobalScript, ...] = ()
    # Dev-only file-watcher extras (see ``pyxle.config.DevConfig``). Ignored by
    # ``pyxle serve`` — production runs no watcher. ``dev_watch_dirs`` are extra
    # absolute directories to watch for hot reload (in addition to ``pages/``);
    # ``dev_ignore_globs`` are extra glob patterns, additive to the built-in
    # generated-output ignores, matched against each changed file's
    # project-relative path.
    dev_watch_dirs: tuple[Path, ...] = ()
    dev_ignore_globs: tuple[str, ...] = ()
    # CORS / CSRF config objects (optional, default = disabled)
    cors: Any = None
    csrf: Any = None
    # Edge-cache policy (CacheConfig). None / empty = no shared caching.
    cache: Any = None
    # Client navigation/prefetch cache policy (NavigationConfig). None = default.
    navigation: Any = None
    # Token-bucket rate limit (RateLimitConfig). None / disabled = no limit.
    rate_limit: Any = None
    # Request observability (ObservabilityConfig). None = framework defaults
    # (request-id + timing on).
    observability: Any = None
    # AI accessibility (LlmsConfig): per-page ``.md`` + ``/llms.txt``. None /
    # disabled = feature off.
    llms: Any = None
    # Pyxle Studio (StudioConfig): the dev-only web dashboard at
    # ``/__pyxle/studio``. None = framework default (enabled in debug mode);
    # only ever served when ``debug`` is true.
    studio: Any = None
    # Plugin entries from pyxle.config.json::plugins — raw payload
    # (strings or dicts), resolved to PluginSpec/PyxlePlugin instances
    # by the starlette app at startup. Empty tuple = no plugins.
    plugins: tuple[Any, ...] = ()
    # The app's display name (``name`` in pyxle.config.json). Empty means
    # "unset" — ``document_title_default`` then falls back to the project
    # directory name. Read only when a page renders with no <title> from the
    # page or any layout.
    app_name: str = ""

    @property
    def document_title_default(self) -> str:
        """The ``<title>`` to use when nothing else supplies one.

        Resolution order: the configured ``name``, then the project directory
        name. Deliberately never the framework's own name — a page with no
        title should read as the developer's app in the browser tab, not as
        Pyxle.
        """

        return self.app_name.strip() or self.project_root.name

    @classmethod
    def from_project_root(
        cls,
        project_root: Path | str,
        *,
        pages_dir: str = "pages",
        public_dir: str = "public",
        build_dir: str = ".pyxle-build",
        starlette_host: str = "127.0.0.1",
        starlette_port: int = 8000,
        vite_host: str = "127.0.0.1",
        vite_port: int = 5173,
        debug: bool = True,
        custom_middlewares: tuple[str, ...] | list[str] | None = None,
        page_route_hooks: tuple[str, ...] | list[str] | None = None,
        api_route_hooks: tuple[str, ...] | list[str] | None = None,
        action_route_hooks: tuple[str, ...] | list[str] | None = None,
        page_manifest: dict[str, Any] | None = None,
        global_stylesheets: Sequence[str] | Sequence[GlobalStylesheet] | None = None,
        global_scripts: Sequence[str] | Sequence[GlobalScript] | None = None,
        dev_watch: Sequence[str] | None = None,
        dev_ignore: Sequence[str] | None = None,
        ssr_workers: int = 1,
        cors: Any = None,
        csrf: Any = None,
        cache: Any = None,
        navigation: Any = None,
        rate_limit: Any = None,
        observability: Any = None,
        llms: Any = None,
        studio: Any = None,
        plugins: Sequence[Any] | None = None,
        app_name: str = "",
    ) -> "DevServerSettings":
        """Create settings derived from a project root directory."""

        root = Path(project_root).expanduser().resolve()
        build_root_path = root / build_dir
        middleware_specs: tuple[str, ...]
        middleware_specs = tuple(custom_middlewares) if custom_middlewares else ()
        page_hook_specs = tuple(page_route_hooks) if page_route_hooks else ()
        api_hook_specs = tuple(api_route_hooks) if api_route_hooks else ()
        action_hook_specs = tuple(action_route_hooks) if action_route_hooks else ()
        style_specs: tuple[GlobalStylesheet, ...] = ()
        if global_stylesheets:
            iterator = iter(global_stylesheets)
            try:
                first = next(iterator)
            except StopIteration:
                style_specs = ()
            else:
                if isinstance(first, GlobalStylesheet):  # type: ignore[arg-type]
                    style_specs = (first, *iterator)  # type: ignore[arg-type]
                else:
                    style_specs = resolve_global_stylesheets(root, global_stylesheets)  # type: ignore[arg-type]
        script_specs: tuple[GlobalScript, ...] = ()
        if global_scripts:
            iterator = iter(global_scripts)
            try:
                first_script = next(iterator)
            except StopIteration:
                script_specs = ()
            else:
                if isinstance(first_script, GlobalScript):  # type: ignore[arg-type]
                    script_specs = (first_script, *iterator)  # type: ignore[arg-type]
                else:
                    script_specs = resolve_global_scripts(root, global_scripts)  # type: ignore[arg-type]
        # Resolve extra watch directories against the project root, dropping any
        # that escape it (defence in depth — config parsing already rejects
        # traversal, but this keeps a direct caller from smuggling one in).
        dev_watch_paths: list[Path] = []
        for entry in dev_watch or ():
            resolved_dir = (root / entry).resolve()
            try:
                resolved_dir.relative_to(root)
            except ValueError:
                continue
            if resolved_dir not in dev_watch_paths:
                dev_watch_paths.append(resolved_dir)
        # A dev server told to answer off-box hands out documents whose scripts
        # come from Vite; a Vite still on the default loopback address can serve
        # nobody but this machine, so those documents render and never hydrate.
        # Only the untouched default follows — a host the developer named is
        # left alone and warned about instead (see ``dev_origins``).
        vite_host = resolve_vite_bind_host(starlette_host, vite_host)
        return cls(
            project_root=root,
            build_root=build_root_path,
            pages_dir=(root / pages_dir).resolve(),
            public_dir=(root / public_dir).resolve(),
            client_build_dir=(build_root_path / "client").resolve(),
            server_build_dir=(build_root_path / "server").resolve(),
            metadata_build_dir=(build_root_path / "metadata").resolve(),
            starlette_host=starlette_host,
            starlette_port=starlette_port,
            vite_host=vite_host,
            vite_port=vite_port,
            debug=debug,
            custom_middlewares=middleware_specs,
            page_route_hooks=page_hook_specs,
            api_route_hooks=api_hook_specs,
            action_route_hooks=action_hook_specs,
            page_manifest=page_manifest,
            global_stylesheets=style_specs,
            global_scripts=script_specs,
            dev_watch_dirs=tuple(dev_watch_paths),
            dev_ignore_globs=tuple(dev_ignore) if dev_ignore else (),
            ssr_workers=max(0, ssr_workers),
            cors=cors,
            csrf=csrf,
            cache=cache,
            navigation=navigation,
            rate_limit=rate_limit,
            observability=observability,
            llms=llms,
            studio=studio,
            plugins=tuple(plugins) if plugins else (),
            app_name=app_name.strip(),
        )

    def to_dict(self) -> Dict[str, Any]:
        """Return a serialisable view of the settings for debugging/logging."""

        return {
            "project_root": str(self.project_root),
            "build_root": str(self.build_root),
            "pages_dir": str(self.pages_dir),
            "public_dir": str(self.public_dir),
            "client_build_dir": str(self.client_build_dir),
            "server_build_dir": str(self.server_build_dir),
            "metadata_build_dir": str(self.metadata_build_dir),
            "starlette_host": self.starlette_host,
            "starlette_port": self.starlette_port,
            "vite_host": self.vite_host,
            "vite_port": self.vite_port,
            "debug": self.debug,
            "custom_middlewares": list(self.custom_middlewares),
            "page_route_hooks": list(self.page_route_hooks),
            "api_route_hooks": list(self.api_route_hooks),
            "action_route_hooks": list(self.action_route_hooks),
            "page_manifest_loaded": self.page_manifest is not None,
            "global_stylesheets": [sheet.as_dict() for sheet in self.global_stylesheets],
            "global_scripts": [script.as_dict() for script in self.global_scripts],
            "ssr_workers": self.ssr_workers,
        }
