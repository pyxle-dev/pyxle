"""Configuration models for the Pyxle development server."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Sequence

from .scripts import GlobalScript, resolve_global_scripts
from .styles import GlobalStylesheet, resolve_global_stylesheets


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
    # Number of persistent Node.js SSR worker processes (0 = legacy subprocess mode)
    ssr_workers: int = 1
    # Optional: loaded page manifest for production asset resolution
    page_manifest: dict[str, Any] | None = None
    global_stylesheets: tuple[GlobalStylesheet, ...] = ()
    global_scripts: tuple[GlobalScript, ...] = ()
    # CORS / CSRF config objects (optional, default = disabled)
    cors: Any = None
    csrf: Any = None
    # Edge-cache policy (CacheConfig). None / empty = no shared caching.
    cache: Any = None
    # Plugin entries from pyxle.config.json::plugins — raw payload
    # (strings or dicts), resolved to PluginSpec/PyxlePlugin instances
    # by the starlette app at startup. Empty tuple = no plugins.
    plugins: tuple[Any, ...] = ()

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
        page_manifest: dict[str, Any] | None = None,
        global_stylesheets: Sequence[str] | Sequence[GlobalStylesheet] | None = None,
        global_scripts: Sequence[str] | Sequence[GlobalScript] | None = None,
        ssr_workers: int = 1,
        cors: Any = None,
        csrf: Any = None,
        cache: Any = None,
        plugins: Sequence[Any] | None = None,
    ) -> "DevServerSettings":
        """Create settings derived from a project root directory."""

        root = Path(project_root).expanduser().resolve()
        build_root_path = root / build_dir
        middleware_specs: tuple[str, ...]
        middleware_specs = tuple(custom_middlewares) if custom_middlewares else ()
        page_hook_specs = tuple(page_route_hooks) if page_route_hooks else ()
        api_hook_specs = tuple(api_route_hooks) if api_route_hooks else ()
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
            page_manifest=page_manifest,
            global_stylesheets=style_specs,
            global_scripts=script_specs,
            ssr_workers=max(0, ssr_workers),
            cors=cors,
            csrf=csrf,
            cache=cache,
            plugins=tuple(plugins) if plugins else (),
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
            "page_manifest_loaded": self.page_manifest is not None,
            "global_stylesheets": [sheet.as_dict() for sheet in self.global_stylesheets],
            "global_scripts": [script.as_dict() for script in self.global_scripts],
            "ssr_workers": self.ssr_workers,
        }
