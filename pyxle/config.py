"""Configuration loading utilities for Pyxle projects."""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

DEFAULT_CONFIG_FILENAME = "pyxle.config.json"


class ConfigError(Exception):
    """Raised when a configuration file cannot be parsed or validated."""


@dataclass(frozen=True, slots=True)
class CorsConfig:
    """CORS configuration for the Pyxle application."""

    origins: tuple[str, ...] = ()
    methods: tuple[str, ...] = ("GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS")
    headers: tuple[str, ...] = ()
    credentials: bool = False
    max_age: int = 600

    @property
    def enabled(self) -> bool:
        return bool(self.origins)


@dataclass(frozen=True, slots=True)
class CsrfConfig:
    """CSRF protection configuration."""

    enabled: bool = True
    cookie_name: str = "pyxle-csrf"
    header_name: str = "x-csrf-token"
    cookie_secure: bool = False
    cookie_samesite: str = "lax"
    exempt_paths: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class CacheConfig:
    """Edge-cache policy: which page routes may be served from a shared cache
    (a CDN or reverse proxy), and for how long.

    Each entry maps a path pattern to a max-age in seconds — either an exact
    path (``/about``) or a prefix wildcard (``/docs/*``, which matches
    ``/docs`` and anything beneath it). A matched page response is sent
    ``Cache-Control: public, s-maxage=<N>, stale-while-revalidate=<N>``
    instead of the default ``private, no-cache``, and its CSRF cookie is
    omitted — a per-user ``Set-Cookie`` must never ride on a shared-cached
    response. Only mark routes that render no per-user data.
    """

    routes: tuple[tuple[str, int], ...] = ()

    @property
    def enabled(self) -> bool:
        return bool(self.routes)

    def max_age_for(self, path: str) -> int | None:
        """Return the ``s-maxage`` (seconds) for ``path`` if a route matches.

        Exact matches win over wildcards; among wildcards the most specific
        (longest) prefix wins, so ``/docs/api`` can override ``/docs/*``.
        """
        best_age: int | None = None
        best_score = -1
        for pattern, max_age in self.routes:
            if pattern.endswith("/*"):
                prefix = pattern[:-2]
                if path == prefix or path.startswith(prefix + "/"):
                    score = len(prefix)
                    if score > best_score:
                        best_score, best_age = score, max_age
            elif path == pattern:
                # Exact match: highest possible precedence.
                return max_age
        return best_age


@dataclass(frozen=True, slots=True)
class PyxleConfig:
    """Resolved configuration values for a Pyxle project."""

    pages_dir: str = "pages"
    public_dir: str = "public"
    build_dir: str = ".pyxle-build"
    starlette_host: str = "127.0.0.1"
    starlette_port: int = 8000
    vite_host: str = "127.0.0.1"
    vite_port: int = 5173
    debug: bool = True
    middleware: tuple[str, ...] = ()
    page_route_middleware: tuple[str, ...] = ()
    api_route_middleware: tuple[str, ...] = ()
    global_styles: tuple[str, ...] = ()
    global_scripts: tuple[str, ...] = ()
    cors: CorsConfig = CorsConfig()
    csrf: CsrfConfig = CsrfConfig()
    cache: CacheConfig = CacheConfig()
    # Plugin entries as the raw payload from ``pyxle.config.json`` —
    # either a bare string (``"pyxle-auth"``) or an object
    # (``{"name": "pyxle-auth", "settings": {...}}``). Resolved into
    # :class:`pyxle.plugins.PluginSpec` objects at devserver startup.
    # Kept as loose primitives here so this module stays import-free
    # and the plugin loader can live in its own place.
    plugins: tuple[Any, ...] = ()

    def to_devserver_kwargs(self) -> Dict[str, Any]:
        """Return keyword arguments for :class:`pyxle.devserver.DevServerSettings`."""

        return {
            "pages_dir": self.pages_dir,
            "public_dir": self.public_dir,
            "build_dir": self.build_dir,
            "starlette_host": self.starlette_host,
            "starlette_port": self.starlette_port,
            "vite_host": self.vite_host,
            "vite_port": self.vite_port,
            "debug": self.debug,
            "custom_middlewares": self.middleware,
            "page_route_hooks": self.page_route_middleware,
            "api_route_hooks": self.api_route_middleware,
            "cors": self.cors,
            "csrf": self.csrf,
            "cache": self.cache,
            "plugins": self.plugins,
        }

    def to_dict(self) -> Dict[str, Any]:
        """Return a serialisable dictionary of the configuration."""

        return {
            "pagesDir": self.pages_dir,
            "publicDir": self.public_dir,
            "buildDir": self.build_dir,
            "starlette": {"host": self.starlette_host, "port": self.starlette_port},
            "vite": {"host": self.vite_host, "port": self.vite_port},
            "debug": self.debug,
            "middleware": list(self.middleware),
            "routeMiddleware": {
                "pages": list(self.page_route_middleware),
                "apis": list(self.api_route_middleware),
            },
            "styling": {
                "globalStyles": list(self.global_styles),
                "globalScripts": list(self.global_scripts),
            },
        }

    def apply_overrides(
        self,
        *,
        pages_dir: Optional[str] = None,
        public_dir: Optional[str] = None,
        build_dir: Optional[str] = None,
        starlette_host: Optional[str] = None,
        starlette_port: Optional[int] = None,
        vite_host: Optional[str] = None,
        vite_port: Optional[int] = None,
        debug: Optional[bool] = None,
    ) -> "PyxleConfig":
        """Return a new configuration with optional overrides applied."""

        updated = self
        if pages_dir is not None:
            updated = replace(updated, pages_dir=pages_dir)
        if public_dir is not None:
            updated = replace(updated, public_dir=public_dir)
        if build_dir is not None:
            updated = replace(updated, build_dir=build_dir)
        if starlette_host is not None:
            updated = replace(updated, starlette_host=starlette_host)
        if starlette_port is not None:
            _validate_port(starlette_port, "--port")
            updated = replace(updated, starlette_port=starlette_port)
        if vite_host is not None:
            updated = replace(updated, vite_host=vite_host)
        if vite_port is not None:
            _validate_port(vite_port, "--vite-port")
            updated = replace(updated, vite_port=vite_port)
        if debug is not None:
            updated = replace(updated, debug=debug)
        return updated


def load_config(
    project_root: Path,
    *,
    config_path: Optional[Path] = None,
) -> PyxleConfig:
    """Load ``pyxle.config.json`` from ``project_root`` if present."""

    root = project_root.expanduser().resolve()
    if config_path is not None:
        candidate = config_path.expanduser().resolve()
    else:
        candidate = root / DEFAULT_CONFIG_FILENAME

    if not candidate.exists():
        return PyxleConfig()

    if not candidate.is_file():
        raise ConfigError(f"Configuration path '{candidate}' is not a file.")

    try:
        payload = json.loads(candidate.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:  # pragma: no cover - exercised via unit tests
        raise ConfigError(f"Failed to parse configuration: {exc.msg} (line {exc.lineno}).") from exc

    if not isinstance(payload, Mapping):
        raise ConfigError("Configuration file must contain a JSON object at the top level.")

    return _parse_config_dict(dict(payload), source=candidate)


def _parse_config_dict(data: Dict[str, Any], *, source: Path) -> PyxleConfig:
    allowed_top_keys = {
        "pagesDir",
        "publicDir",
        "buildDir",
        "starlette",
        "vite",
        "debug",
        "middleware",
        "routeMiddleware",
        "styling",
        "cors",
        "csrf",
        "cache",
        "plugins",
    }
    unknown_keys = set(data) - allowed_top_keys
    if unknown_keys:
        formatted = ", ".join(sorted(unknown_keys))
        raise ConfigError(f"Unknown configuration keys in '{source}': {formatted}.")

    pages_dir = _validate_directory_value(data.get("pagesDir", "pages"), "pagesDir")
    public_dir = _validate_directory_value(data.get("publicDir", "public"), "publicDir")
    build_dir = _validate_directory_value(data.get("buildDir", ".pyxle-build"), "buildDir")

    starlette = data.get("starlette", {})
    starlette_host, starlette_port = _parse_network_block(starlette, "starlette", source)

    vite = data.get("vite", {})
    vite_host, vite_port = _parse_network_block(vite, "vite", source)

    debug_value = data.get("debug", True)
    if not isinstance(debug_value, bool):
        raise ConfigError(
            f"Invalid value for 'debug' in '{source}': expected boolean, got {type(debug_value).__name__}."
        )

    middleware_specs = _parse_middleware_list(data.get("middleware"), source=source)
    page_route_specs, api_route_specs = _parse_route_middleware_block(
        data.get("routeMiddleware"),
        source=source,
    )
    global_styles, global_scripts = _parse_styling_block(data.get("styling"), source=source)
    cors_config = _parse_cors_block(data.get("cors"), source=source)
    csrf_config = _parse_csrf_block(data.get("csrf"), source=source)
    cache_config = _parse_cache_block(data.get("cache"), source=source)
    plugins = _parse_plugins_block(data.get("plugins"), source=source)

    return PyxleConfig(
        pages_dir=pages_dir,
        public_dir=public_dir,
        build_dir=build_dir,
        starlette_host=starlette_host,
        starlette_port=starlette_port,
        vite_host=vite_host,
        vite_port=vite_port,
        debug=debug_value,
        middleware=middleware_specs,
        page_route_middleware=page_route_specs,
        api_route_middleware=api_route_specs,
        global_styles=global_styles,
        global_scripts=global_scripts,
        cors=cors_config,
        csrf=csrf_config,
        cache=cache_config,
        plugins=plugins,
    )


def _parse_plugins_block(value: Any, *, source: Path) -> tuple[Any, ...]:
    """Parse the ``plugins`` array.

    Each entry is either a bare string name or an object with at least
    a ``name`` key. Full validation (including import-time resolution)
    happens later in :func:`pyxle.plugins.PluginSpec.from_config_entry`
    — here we only enforce the shape so config errors surface early.
    """
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ConfigError(
            f"Invalid value for 'plugins' in '{source}': expected a list."
        )
    entries: list[Any] = []
    for index, entry in enumerate(value):
        if isinstance(entry, str):
            if not entry.strip():
                raise ConfigError(
                    f"Invalid 'plugins[{index}]' in '{source}': empty string."
                )
            entries.append(entry.strip())
            continue
        if isinstance(entry, Mapping):
            if "name" not in entry or not isinstance(entry["name"], str) or not entry["name"].strip():
                raise ConfigError(
                    f"Invalid 'plugins[{index}]' in '{source}': object must "
                    "include a non-empty 'name' string."
                )
            entries.append(dict(entry))
            continue
        raise ConfigError(
            f"Invalid 'plugins[{index}]' in '{source}': expected string or object, "
            f"got {type(entry).__name__}."
        )
    return tuple(entries)


def _parse_styling_block(value: Any, *, source: Path) -> tuple[tuple[str, ...], tuple[str, ...]]:
    if value is None:
        return ((), ())
    if not isinstance(value, Mapping):
        raise ConfigError(
            f"Invalid value for 'styling' in '{source}': expected object with 'globalStyles'/'globalScripts' lists."
        )

    styles = _parse_path_list(value.get("globalStyles"), source=source, field_name="styling.globalStyles")
    scripts = _parse_path_list(value.get("globalScripts"), source=source, field_name="styling.globalScripts")
    return (styles, scripts)


def _parse_path_list(value: Any, *, source: Path, field_name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ConfigError(
            f"Invalid value for '{field_name}' in '{source}': expected list of file paths."
        )

    normalized: list[str] = []
    for index, entry in enumerate(value):
        if not isinstance(entry, str) or not entry.strip():
            raise ConfigError(
                f"Invalid entry at index {index} in '{field_name}' within '{source}': expected non-empty string."
            )
        normalized.append(entry.strip())

    return tuple(normalized)


def _validate_directory_value(value: Any, key: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"Invalid value for '{key}': expected non-empty string.")
    return value


def _parse_network_block(value: Any, key: str, source: Path) -> tuple[str, int]:
    if value is None:
        return ("127.0.0.1", 8000) if key == "starlette" else ("127.0.0.1", 5173)
    if not isinstance(value, Mapping):
        raise ConfigError(
            f"Invalid value for '{key}' in '{source}': expected object with 'host' and 'port'."
        )

    host = value.get("host", "127.0.0.1")
    if not isinstance(host, str) or not host.strip():
        raise ConfigError(
            f"Invalid host in '{key}' block of '{source}': expected non-empty string."
        )

    port = value.get("port", 8000 if key == "starlette" else 5173)
    _validate_port(port, f"{key}.port")

    return host, port


def _validate_port(value: Any, key: str) -> int:
    if not isinstance(value, int):
        raise ConfigError(f"Invalid value for '{key}': expected integer port value.")
    if value <= 0 or value > 65535:
        raise ConfigError(f"Port for '{key}' must be between 1 and 65535 (got {value}).")
    return value


def _parse_route_middleware_block(value: Any, *, source: Path) -> tuple[tuple[str, ...], tuple[str, ...]]:
    if value is None:
        return ((), ())
    if not isinstance(value, Mapping):
        raise ConfigError(
            f"Invalid value for 'routeMiddleware' in '{source}': expected object with 'pages'/'apis' arrays."
        )

    pages = _parse_middleware_list(value.get("pages"), source=source, field_name="routeMiddleware.pages")
    apis = _parse_middleware_list(value.get("apis"), source=source, field_name="routeMiddleware.apis")
    return (pages, apis)


def _parse_middleware_list(value: Any, *, source: Path, field_name: str = "middleware") -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ConfigError(
            f"Invalid value for '{field_name}' in '{source}': expected a list of module paths."
        )

    specs: list[str] = []
    for index, entry in enumerate(value):
        if not isinstance(entry, str) or not entry.strip():
            raise ConfigError(
                f"Invalid middleware entry at index {index} in '{source}' for '{field_name}': expected non-empty string."
            )
        specs.append(entry.strip())

    return tuple(specs)


def _parse_cors_block(value: Any, *, source: Path) -> CorsConfig:
    if value is None:
        return CorsConfig()
    if not isinstance(value, Mapping):
        raise ConfigError(
            f"Invalid value for 'cors' in '{source}': expected object with 'origins', 'methods', 'headers', 'credentials'."
        )

    origins = _parse_string_list(value.get("origins"), source=source, field_name="cors.origins")
    methods = _parse_string_list(value.get("methods"), source=source, field_name="cors.methods")
    headers = _parse_string_list(value.get("headers"), source=source, field_name="cors.headers")

    credentials = value.get("credentials", False)
    if not isinstance(credentials, bool):
        raise ConfigError(
            f"Invalid value for 'cors.credentials' in '{source}': expected boolean."
        )

    max_age = value.get("maxAge", 600)
    if not isinstance(max_age, int) or max_age < 0:
        raise ConfigError(
            f"Invalid value for 'cors.maxAge' in '{source}': expected non-negative integer."
        )

    return CorsConfig(
        origins=origins or (),
        methods=methods or ("GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"),
        headers=headers or (),
        credentials=credentials,
        max_age=max_age,
    )


def _parse_cache_block(value: Any, *, source: Path) -> CacheConfig:
    """Parse the ``cache`` block — a map of route pattern → max-age seconds.

    Accepts shorthand integers (``{"/": 120, "/docs/*": 300}``) or objects
    (``{"/": {"sMaxage": 120}}``). A pattern is an exact path or a ``/x/*``
    prefix wildcard.
    """
    if value is None:
        return CacheConfig()
    if not isinstance(value, Mapping):
        raise ConfigError(
            f"Invalid value for 'cache' in '{source}': expected an object mapping "
            f"route patterns to a max-age in seconds."
        )
    routes: list[tuple[str, int]] = []
    for pattern, rule in value.items():
        if not isinstance(pattern, str) or not pattern.startswith("/"):
            raise ConfigError(
                f"Invalid cache route '{pattern}' in '{source}': patterns must be "
                f"absolute paths (e.g. '/' or '/docs/*')."
            )
        if isinstance(rule, bool):
            # bool is an int subclass — reject explicitly so 'true' isn't a max-age.
            raise ConfigError(
                f"Invalid value for cache route '{pattern}' in '{source}': expected "
                f"a max-age in seconds or an object, got a boolean."
            )
        if isinstance(rule, int):
            max_age = rule
        elif isinstance(rule, Mapping):
            max_age = rule.get("sMaxage", rule.get("maxAge"))
            if not isinstance(max_age, int) or isinstance(max_age, bool):
                raise ConfigError(
                    f"Invalid 'sMaxage' for cache route '{pattern}' in '{source}': "
                    f"expected an integer number of seconds."
                )
        else:
            raise ConfigError(
                f"Invalid value for cache route '{pattern}' in '{source}': expected "
                f"a max-age in seconds or an object."
            )
        if max_age < 0:
            raise ConfigError(
                f"Invalid max-age for cache route '{pattern}' in '{source}': must be >= 0."
            )
        routes.append((pattern, max_age))
    return CacheConfig(routes=tuple(routes))


def _parse_csrf_block(value: Any, *, source: Path) -> CsrfConfig:
    if value is None:
        return CsrfConfig()
    if isinstance(value, bool):
        return CsrfConfig(enabled=value)
    if not isinstance(value, Mapping):
        raise ConfigError(
            f"Invalid value for 'csrf' in '{source}': expected boolean or object."
        )

    enabled = value.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ConfigError(f"Invalid value for 'csrf.enabled' in '{source}': expected boolean.")

    cookie_name = value.get("cookieName", "pyxle-csrf")
    if not isinstance(cookie_name, str) or not cookie_name.strip():
        raise ConfigError(f"Invalid value for 'csrf.cookieName' in '{source}': expected non-empty string.")

    header_name = value.get("headerName", "x-csrf-token")
    if not isinstance(header_name, str) or not header_name.strip():
        raise ConfigError(f"Invalid value for 'csrf.headerName' in '{source}': expected non-empty string.")

    cookie_secure = value.get("cookieSecure", False)
    if not isinstance(cookie_secure, bool):
        raise ConfigError(f"Invalid value for 'csrf.cookieSecure' in '{source}': expected boolean.")

    cookie_samesite = value.get("cookieSameSite", "lax")
    if not isinstance(cookie_samesite, str) or cookie_samesite.lower() not in {"strict", "lax", "none"}:
        raise ConfigError(
            f"Invalid value for 'csrf.cookieSameSite' in '{source}': expected 'strict', 'lax', or 'none'."
        )

    exempt_paths = _parse_string_list(value.get("exemptPaths"), source=source, field_name="csrf.exemptPaths")

    return CsrfConfig(
        enabled=enabled,
        cookie_name=cookie_name,
        header_name=header_name,
        cookie_secure=cookie_secure,
        cookie_samesite=cookie_samesite.lower(),
        exempt_paths=exempt_paths or (),
    )


def _parse_string_list(value: Any, *, source: Path, field_name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ConfigError(
            f"Invalid value for '{field_name}' in '{source}': expected list of strings."
        )
    result: list[str] = []
    for index, entry in enumerate(value):
        if not isinstance(entry, str) or not entry.strip():
            raise ConfigError(
                f"Invalid entry at index {index} in '{field_name}' within '{source}': expected non-empty string."
            )
        result.append(entry.strip())
    return tuple(result)


def apply_env_overrides(config: PyxleConfig) -> PyxleConfig:
    """Apply ``PYXLE_`` prefixed environment variables as config overrides.

    Supported variables (all optional):
    * ``PYXLE_HOST`` -> ``starlette_host``
    * ``PYXLE_PORT`` -> ``starlette_port``
    * ``PYXLE_VITE_HOST`` -> ``vite_host``
    * ``PYXLE_VITE_PORT`` -> ``vite_port``
    * ``PYXLE_DEBUG`` -> ``debug`` (accepts ``"true"``/``"1"`` or ``"false"``/``"0"``)
    * ``PYXLE_PAGES_DIR`` -> ``pages_dir``
    * ``PYXLE_PUBLIC_DIR`` -> ``public_dir``
    * ``PYXLE_BUILD_DIR`` -> ``build_dir``
    """

    import os  # noqa: PLC0415

    overrides: dict[str, object] = {}

    host = os.environ.get("PYXLE_HOST")
    if host is not None:
        overrides["starlette_host"] = host

    port = os.environ.get("PYXLE_PORT")
    if port is not None:
        try:
            overrides["starlette_port"] = int(port)
        except ValueError as exc:
            raise ConfigError(f"PYXLE_PORT must be an integer (got '{port}')") from exc

    vite_host = os.environ.get("PYXLE_VITE_HOST")
    if vite_host is not None:
        overrides["vite_host"] = vite_host

    vite_port = os.environ.get("PYXLE_VITE_PORT")
    if vite_port is not None:
        try:
            overrides["vite_port"] = int(vite_port)
        except ValueError as exc:
            raise ConfigError(f"PYXLE_VITE_PORT must be an integer (got '{vite_port}')") from exc

    debug = os.environ.get("PYXLE_DEBUG")
    if debug is not None:
        if debug.lower() in ("true", "1", "yes"):
            overrides["debug"] = True
        elif debug.lower() in ("false", "0", "no"):
            overrides["debug"] = False
        else:
            raise ConfigError(f"PYXLE_DEBUG must be true/false (got '{debug}')")

    pages_dir = os.environ.get("PYXLE_PAGES_DIR")
    if pages_dir is not None:
        overrides["pages_dir"] = pages_dir

    public_dir = os.environ.get("PYXLE_PUBLIC_DIR")
    if public_dir is not None:
        overrides["public_dir"] = public_dir

    build_dir = os.environ.get("PYXLE_BUILD_DIR")
    if build_dir is not None:
        overrides["build_dir"] = build_dir

    if not overrides:
        return config

    return config.apply_overrides(**overrides)


__all__ = [
    "PyxleConfig",
    "ConfigError",
    "CorsConfig",
    "CsrfConfig",
    "load_config",
    "apply_env_overrides",
    "DEFAULT_CONFIG_FILENAME",
]
