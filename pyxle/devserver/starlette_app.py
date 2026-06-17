"""Helpers assembling the Starlette application for `pyxle dev`."""

from __future__ import annotations

import importlib.util
import inspect
import math
import mimetypes
import sys
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from email.utils import formatdate, parsedate
from hashlib import md5
from pathlib import Path
from types import ModuleType
from typing import Any, Callable, Iterable, Sequence

from starlette.applications import Starlette
from starlette.datastructures import Headers
from starlette.endpoints import HTTPEndpoint
from starlette.exceptions import HTTPException
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Mount, Route, Router, WebSocketRoute
from starlette.staticfiles import NotModifiedResponse, StaticFiles
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from pyxle.cache import (
    PageCache,
    build_page_cache,
    set_active_cache,
    warm_page_cache,
)
from pyxle.cli.logger import ConsoleLogger
from pyxle.ssr import (
    ComponentRenderer,
    build_page_navigation_response,
    build_page_response,
)
from pyxle.ssr.renderer import pool_render_factory
from pyxle.ssr.view import (
    REVALIDATE_HEADER,
    build_not_found_response,
    build_streaming_page_response,
)

from .error_pages import ErrorBoundaryRegistry, build_error_boundary_registry
from .middleware import MiddlewareHookError, load_custom_middlewares
from .overlay import OverlayManager
from .proxy import ViteProxy
from .route_hooks import (
    DEFAULT_API_POLICIES,
    DEFAULT_PAGE_POLICIES,
    RouteContext,
    RouteHookCallable,
    RouteHookError,
    load_route_hooks,
    wrap_with_route_hooks,
)
from .routes import ActionRoute, ApiRoute, PageRoute, RouteTable, select_static_pages
from .settings import DevServerSettings

_API_HTTP_METHODS: Sequence[str] = ("GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS")
_NAVIGATION_HEADER = "x-pyxle-navigation"


def _ensure_project_root_on_sys_path(project_root: Path) -> None:
    """Guarantee the project root is importable for custom middleware hooks."""

    root = str(project_root)
    if root not in sys.path:
        sys.path.insert(0, root)


def _import_middleware_class(import_string: str) -> type:
    """Resolve ``package.module:Attribute`` into a class.

    Used by the plugin-contributed middleware path. Kept separate from
    :mod:`pyxle.devserver.middleware` because plugins hand us
    ``(class_import_string, options_dict)`` rather than a Middleware
    instance, and the existing loader normalises to a Middleware
    instance — different contract.
    """
    if ":" in import_string:
        module_name, _, attribute = import_string.partition(":")
    elif "." in import_string:
        module_name, _, attribute = import_string.rpartition(".")
    else:
        raise ValueError(
            f"Middleware spec {import_string!r} must be 'package.module:Class'"
            " or 'package.module.Class'"
        )
    import importlib as _importlib  # noqa: PLC0415
    module = _importlib.import_module(module_name)
    try:
        cls = getattr(module, attribute)
    except AttributeError as exc:
        raise AttributeError(
            f"Module '{module_name}' has no attribute '{attribute}'"
        ) from exc
    if not isinstance(cls, type):
        raise TypeError(
            f"Middleware spec {import_string!r} resolved to non-class "
            f"{type(cls).__name__}"
        )
    return cls


class ApiRouteError(RuntimeError):
    """Raised when an API module cannot be resolved to a valid handler."""


class PageRouteError(RuntimeError):
    """Raised when a page's ``websocket`` handler cannot be resolved."""


class HttpOnlyStaticFiles(StaticFiles):
    """Static files app that gracefully rejects non-HTTP scopes."""

    def __init__(self, *args, close_code: int = 4404, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._close_code = close_code

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        scope_type = scope.get("type")
        if scope_type != "http":
            if scope_type == "websocket":
                await send({"type": "websocket.close", "code": self._close_code})
                return
            return
        await super().__call__(scope, receive, send)


_SECURITY_HEADERS: tuple[tuple[bytes, bytes], ...] = (
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"SAMEORIGIN"),
    (b"referrer-policy", b"strict-origin-when-cross-origin"),
)


class _SecurityHeadersMiddleware:
    """Inject standard security response headers (production only)."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_security_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                headers.extend(_SECURITY_HEADERS)
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_with_security_headers)


def _index_static_files(directory: Path | None, *, prefix: str = "") -> frozenset[str]:
    """Return the set of URL paths served from ``directory`` (e.g. ``/favicon.ico``).

    Built once when the static middleware is constructed so request handling can
    decide in O(1) whether a path is a static asset. This avoids a filesystem
    ``stat`` plus a raised-and-caught 404 on every *dynamic* request (API routes,
    SSR pages) — the common case — since those paths simply aren't in the set and
    fall straight through to the app. The middleware only runs when serving a
    production build, whose output is immutable, so this startup snapshot stays
    correct for the life of the process.
    """
    if directory is None:
        return frozenset()
    base = Path(directory)
    if not base.is_dir():
        return frozenset()
    paths: set[str] = set()
    for entry in base.rglob("*"):
        if entry.is_file():
            paths.add(f"{prefix}/{entry.relative_to(base).as_posix()}")
    return frozenset(paths)


# Per-file and per-process budgets for the in-memory static cache. Both are
# enforced once at startup (the production build is immutable, so the cache
# never grows afterwards — bounded by construction, no runtime eviction).
_STATIC_CACHE_MAX_FILE_BYTES = 1024 * 1024
_STATIC_CACHE_MAX_TOTAL_BYTES = 32 * 1024 * 1024


def _static_cache_control(path: str, *, is_client: bool) -> bytes:
    """Cache-Control value for a static asset URL path.

    Vite content-hashed bundles (``/client/.../dist/assets/...``) are
    immutable and cacheable forever; everything else revalidates hourly.
    """

    if is_client and "/dist/assets/" in path:
        return b"public, max-age=31536000, immutable"
    return b"public, max-age=3600"


@dataclass(frozen=True, slots=True)
class _CachedAsset:
    """A small static file fully loaded into memory at startup."""

    body: bytes
    raw_headers: tuple[tuple[bytes, bytes], ...]
    headers: Headers


def _is_not_modified(response_headers: Headers, request_headers: Headers) -> bool:
    """Return ``True`` when a 304 can be served for a cached asset.

    Mirrors ``StaticFiles.is_not_modified`` from the pinned Starlette
    release (``If-None-Match`` etag list first, ``If-Modified-Since``
    fallback) so memory-cached and disk-served responses negotiate
    conditionals identically.
    """

    try:
        if_none_match = request_headers["if-none-match"]
        etag = response_headers["etag"]
        if etag in [tag.strip(" W/") for tag in if_none_match.split(",")]:
            return True
    except KeyError:
        pass

    try:
        if_modified_since = parsedate(request_headers["if-modified-since"])
        last_modified = parsedate(response_headers["last-modified"])
        if (
            if_modified_since is not None
            and last_modified is not None
            and if_modified_since >= last_modified
        ):
            return True
    except KeyError:
        pass

    return False


def _load_static_memory_cache(
    directory: Path | None,
    *,
    prefix: str = "",
    max_file_bytes: int,
    budget: int,
) -> tuple[dict[str, _CachedAsset], int]:
    """Load files from ``directory`` into memory, returning the remaining budget.

    Files larger than ``max_file_bytes`` and files that would exceed the
    remaining ``budget`` are skipped — they keep streaming from disk via
    ``StaticFiles``. Walk order is sorted for determinism. Headers mirror
    ``FileResponse`` (content-length, last-modified, mtime/size etag,
    guessed content-type) plus the Cache-Control value the disk path adds.
    """

    cache: dict[str, _CachedAsset] = {}
    if directory is None:
        return cache, budget
    base = Path(directory)
    if not base.is_dir():
        return cache, budget

    for entry in sorted(base.rglob("*")):
        if not entry.is_file():
            continue
        stat_result = entry.stat()
        size = stat_result.st_size
        if size > max_file_bytes or size > budget:
            continue
        url_path = f"{prefix}/{entry.relative_to(base).as_posix()}"
        body = entry.read_bytes()
        budget -= size

        media_type = mimetypes.guess_type(entry.name)[0] or "text/plain"
        if media_type.startswith("text/"):
            media_type += "; charset=utf-8"
        etag_base = f"{stat_result.st_mtime}-{size}"
        etag = f'"{md5(etag_base.encode(), usedforsecurity=False).hexdigest()}"'
        raw_headers = (
            (b"content-length", str(size).encode("latin-1")),
            (b"content-type", media_type.encode("latin-1")),
            (b"last-modified", formatdate(stat_result.st_mtime, usegmt=True).encode("latin-1")),
            (b"etag", etag.encode("latin-1")),
            (b"cache-control", _static_cache_control(url_path, is_client=bool(prefix))),
        )
        cache[url_path] = _CachedAsset(
            body=body,
            raw_headers=raw_headers,
            headers=Headers(raw=list(raw_headers)),
        )
    return cache, budget


class StaticAssetsMiddleware:
    """Serve client + public assets ahead of dynamic catch-all routes.

    When ``cache_in_memory`` is enabled (production serve — the build output
    is immutable), small files are fully loaded into memory at startup and
    served without touching the filesystem or hopping to a worker thread.
    The cache is bounded by construction: per-file and total-byte budgets
    are enforced during the startup walk and the cache never grows after.
    Oversized files keep streaming from disk through ``StaticFiles``.
    """

    def __init__(
        self,
        app,
        *,
        public_directory: Path | None = None,
        client_directory: Path | None = None,
        cache_in_memory: bool = False,
        cache_max_file_bytes: int = _STATIC_CACHE_MAX_FILE_BYTES,
        cache_max_total_bytes: int = _STATIC_CACHE_MAX_TOTAL_BYTES,
    ) -> None:
        self.app = app
        self._public_static = (
            HttpOnlyStaticFiles(directory=public_directory, check_dir=False)
            if public_directory is not None
            else None
        )
        self._client_static = (
            HttpOnlyStaticFiles(directory=client_directory, check_dir=False)
            if client_directory is not None
            else None
        )
        # Snapshot the served files so a dynamic request skips the stat + 404
        # that StaticFiles would otherwise incur on a miss (see
        # _index_static_files). Production build output is immutable.
        self._public_paths = _index_static_files(public_directory)
        self._client_paths = _index_static_files(client_directory, prefix="/client")

        self._memory_cache: dict[str, _CachedAsset] = {}
        if cache_in_memory:
            budget = cache_max_total_bytes
            self._memory_cache, budget = _load_static_memory_cache(
                public_directory,
                max_file_bytes=cache_max_file_bytes,
                budget=budget,
            )
            client_cache, budget = _load_static_memory_cache(
                client_directory,
                prefix="/client",
                max_file_bytes=cache_max_file_bytes,
                budget=budget,
            )
            self._memory_cache.update(client_cache)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        method = scope.get("method", "GET").upper()
        if method not in ("GET", "HEAD"):
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")

        cached = self._memory_cache.get(path)
        if cached is not None:
            await self._send_cached(cached, scope, receive, send, method=method)
            return

        if self._client_static is not None and path.startswith("/client"):
            # O(1) membership check first: only touch the filesystem for a path
            # that is actually a known static asset, so dynamic requests that
            # merely share the path space don't pay a stat + caught 404.
            if path in self._client_paths and await self._try_static(
                self._client_static,
                scope,
                receive,
                send,
                prefix="/client",
            ):
                return

        if self._public_static is not None and not path.startswith("/client"):
            if path in self._public_paths and await self._try_static(
                self._public_static, scope, receive, send
            ):
                return

        await self.app(scope, receive, send)

    @staticmethod
    async def _send_cached(
        asset: _CachedAsset,
        scope: Scope,
        receive: Receive,
        send: Send,
        *,
        method: str,
    ) -> None:
        """Send a memory-cached asset, honouring conditional request headers."""

        request_headers = Headers(scope=scope)
        if _is_not_modified(asset.headers, request_headers):
            response = NotModifiedResponse(asset.headers)
            await response(scope, receive, send)
            return

        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": list(asset.raw_headers),
            }
        )
        # Mirror FileResponse: HEAD responses carry the full content-length
        # but an empty body.
        body = b"" if method == "HEAD" else asset.body
        await send({"type": "http.response.body", "body": body, "more_body": False})

    @staticmethod
    async def _try_static(
        static_app: HttpOnlyStaticFiles,
        scope: Scope,
        receive: Receive,
        send: Send,
        *,
        prefix: str = "",
    ) -> bool:
        selected_scope = scope
        original_path = scope.get("path", "")
        if prefix:
            if not original_path.startswith(prefix):
                return False
            stripped = original_path[len(prefix) :] or "/"
            candidate = dict(scope)
            candidate["path"] = stripped
            raw_path = scope.get("raw_path")
            if isinstance(raw_path, (bytes, bytearray)):
                candidate["raw_path"] = stripped.encode("utf-8")
            selected_scope = candidate

        # Vite hashed assets (e.g. /client/dist/assets/index-a1b2c3d4.js)
        # are immutable and can be cached forever; see _static_cache_control.
        cache_control = _static_cache_control(
            original_path, is_client=prefix == "/client"
        )

        async def _send_with_cache_headers(message):
            if message.get("type") == "http.response.start":
                headers = list(message.get("headers", []))
                headers.append((b"cache-control", cache_control))
                message = {**message, "headers": headers}
            await send(message)

        try:
            await static_app(selected_scope, receive, _send_with_cache_headers)
            return True
        except HTTPException as exc:
            if exc.status_code == 404:
                return False
            raise


def build_api_router(
    routes: Iterable[ApiRoute],
    *,
    route_hooks: Sequence[RouteHookCallable] | None = None,
) -> Router:
    """Create a Starlette ``Router`` populated from compiled API artifacts."""

    router = Router()
    hooks = list(route_hooks or [])

    for route in routes:
        module = _import_module(route.module_key, route.server_module_path, debug=True)
        http_handler, ws_handler = _resolve_api_handlers(module)
        context = RouteContext(
            target="api",
            path=route.path,
            source_relative_path=route.source_relative_path,
            source_absolute_path=route.source_absolute_path,
            module_key=route.module_key,
            content_hash=route.content_hash,
            allowed_methods=tuple(_API_HTTP_METHODS),
        )

        if http_handler is not None:
            if inspect.isclass(http_handler) and issubclass(http_handler, HTTPEndpoint):
                # HTTPEndpoint classes are ASGI applications — Starlette
                # dispatches them natively (including threadpool dispatch
                # for sync methods and built-in 405 handling). Route hooks
                # wrap request→response callables and don't match that
                # shape, so class endpoints bypass them — same rationale
                # as WebSocket routes below.
                router.add_route(route.path, http_handler)  # type: ignore[arg-type]
            else:
                # Function endpoints (async or sync — sync ones are
                # threadpooled by ensure_async_handler inside the wrapper)
                # run through the route hook chain.
                wrapped = wrap_with_route_hooks(http_handler, hooks=hooks, context=context)
                router.add_route(route.path, wrapped, methods=list(_API_HTTP_METHODS))  # type: ignore[arg-type]

        if ws_handler is not None:
            # WebSocket routes aren't run through the HTTP route hooks —
            # hooks wrap a request→response callable and the WS lifecycle
            # (accept/send/recv/close) doesn't match that shape. Route
            # hooks that need to run for WS upgrades should do so in the
            # WS handler body.
            router.routes.append(WebSocketRoute(route.path, ws_handler))

    return router


def _import_module(
    module_key: str, module_path: Path, *, debug: bool = False,
) -> ModuleType:
    """Import a compiled module located at ``module_path`` under ``module_key``.

    Ensures the project root is on ``sys.path`` so that user-level imports
    (e.g. ``from db import ...``) resolve without manual ``sys.path`` hacks.

    When *debug* is ``False`` (production), a previously imported module is
    returned from ``sys.modules`` without re-execution.  When *debug* is
    ``True``, the module is always re-imported from disk so that code changes
    take effect immediately.
    """

    if module_key in sys.modules:
        if not debug:
            return sys.modules[module_key]
        del sys.modules[module_key]
        importlib.invalidate_caches()

    # Compiled modules live under <project_root>/<build_dir>/server/...
    # Walk up to the build-directory ancestor to find the project root.
    resolved = module_path.resolve()
    for parent in resolved.parents:
        if parent.name.startswith(".pyxle"):
            _root = str(parent.parent)
            if _root not in sys.path:
                sys.path.insert(0, _root)
            break

    spec = importlib.util.spec_from_file_location(module_key, module_path)
    if spec is None or spec.loader is None:
        raise ApiRouteError(f"Unable to load API module at {module_path!s}")

    module = importlib.util.module_from_spec(spec)
    sys.modules[module_key] = module

    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        # Don't leave a half-initialised module behind: a later debug=False
        # import of the same key would return the broken partial silently
        # instead of re-raising (and re-imports during debug expect a clean
        # slate). Match importlib's own failure semantics.
        sys.modules.pop(module_key, None)
        raise ApiRouteError(f"Failed to import API module {module_key}: {exc}") from exc

    return module


def _resolve_api_handlers(module: ModuleType) -> "tuple[Any, Any]":
    """Return ``(http_handler, websocket_handler)`` for an API module.

    An API module may export any combination of:

    * ``endpoint`` — async or sync callable, or :class:`HTTPEndpoint`
      subclass that handles HTTP requests. Sync callables are dispatched
      through Starlette's threadpool so blocking bodies don't stall the
      event loop.
    * ``websocket`` — async callable ``(ws)`` or
      :class:`WebSocketEndpoint` subclass that handles the WS protocol.
    * An :class:`HTTPEndpoint` subclass somewhere in the module (picked
      up automatically for backward compatibility with older files
      that predate the explicit ``endpoint`` attribute).

    At least one of the two must be present; otherwise the module is
    rejected with :class:`ApiRouteError` so the developer gets a clear
    message instead of a silent 404.
    """
    http_handler: Any = None
    ws_handler: Any = None

    if hasattr(module, "endpoint"):
        candidate = getattr(module, "endpoint")
        if not callable(candidate):
            raise ApiRouteError(
                f"API module {module.__name__} exposes 'endpoint' but it is not callable"
            )
        http_handler = candidate

    if hasattr(module, "websocket"):
        candidate = getattr(module, "websocket")
        if not callable(candidate):
            raise ApiRouteError(
                f"API module {module.__name__} exposes 'websocket' but it is not callable"
            )
        ws_handler = candidate

    if http_handler is None and ws_handler is None:
        # Fall back to an ``HTTPEndpoint`` subclass defined in the
        # module. Kept for compatibility with Pyxle apps that predate
        # the explicit ``endpoint`` attribute convention.
        for attribute in module.__dict__.values():
            if (
                inspect.isclass(attribute)
                and issubclass(attribute, HTTPEndpoint)
                and attribute is not HTTPEndpoint
            ):
                http_handler = attribute
                break

    if http_handler is None and ws_handler is None:
        raise ApiRouteError(
            f"API module {module.__name__} must define an 'endpoint' callable, "
            "a 'websocket' callable, or an HTTPEndpoint subclass"
        )

    return http_handler, ws_handler


def _resolve_api_handler(module: ModuleType):
    """Compatibility shim for callers that only care about the HTTP half.

    Older code (and the error-overlay dispatcher) hasn't been updated to
    the split pair, but both the internal callers in this file use the
    newer :func:`_resolve_api_handlers`. Keep this around so third-party
    devserver plugins that reach in don't silently break.
    """
    http_handler, _ = _resolve_api_handlers(module)
    if http_handler is None:
        raise ApiRouteError(
            f"API module {module.__name__} has only a WebSocket endpoint — "
            "use _resolve_api_handlers() instead."
        )
    return http_handler


def build_page_router(
    routes: Iterable[PageRoute],
    *,
    settings: DevServerSettings,
    renderer: ComponentRenderer,
    overlay: OverlayManager | None = None,
    route_hooks: Sequence[RouteHookCallable] | None = None,
    error_boundaries: ErrorBoundaryRegistry | None = None,
    page_cache: PageCache | None = None,
    stream_render: Callable[..., Any] | None = None,
) -> Router:
    """Create a router serving compiled pages via server-side rendering."""

    router = Router()
    hooks = list(route_hooks or [])

    for route in routes:
        handler = _make_page_handler(
            route,
            settings=settings,
            renderer=renderer,
            overlay=overlay,
            error_boundaries=error_boundaries,
            page_cache=page_cache,
            stream_render=stream_render,
        )
        context = RouteContext(
            target="page",
            path=route.path,
            source_relative_path=route.source_relative_path,
            source_absolute_path=route.source_absolute_path,
            module_key=route.module_key,
            content_hash=route.content_hash,
            has_loader=route.has_loader,
            head_elements=route.head_elements,
            allowed_methods=("GET",),
        )
        handler = wrap_with_route_hooks(handler, hooks=hooks, context=context)
        router.add_route(route.path, handler, methods=["GET"])

        if route.has_websocket:
            # A page that declares `async def websocket(ws)` also serves a
            # WebSocket route at the SAME path. Starlette dispatches the HTTP
            # Route for an http-scope request and this WebSocketRoute for a
            # websocket-scope upgrade, so both coexist (path params resolve
            # into ws.scope["path_params"]). Like the API WS path (see
            # build_api_router), WS routes bypass the HTTP route-hook chain —
            # hooks wrap a request→response callable, which the WS lifecycle
            # (accept/send/recv/close) doesn't match. Any per-request work a WS
            # upgrade needs (auth, origin checks) belongs in the handler body.
            ws_handler = _resolve_page_websocket(route, settings=settings)
            router.routes.append(WebSocketRoute(route.path, ws_handler))

    return router


def _resolve_page_websocket(route: PageRoute, *, settings: DevServerSettings) -> Any:
    """Import a page's server module and return its ``websocket`` handler.

    Raises :class:`PageRouteError` when the metadata names a handler the module
    doesn't actually expose (a stale build), so the developer gets a clear
    message instead of a route that 500s on connect.
    """
    module = _import_module(
        route.module_key, route.server_module_path, debug=settings.debug
    )
    handler = getattr(module, route.websocket_name, None)
    if not callable(handler):
        raise PageRouteError(
            f"Page {route.path!r} declares a websocket handler "
            f"{route.websocket_name!r}, but its server module exposes no such "
            "callable. Re-run the build."
        )
    return handler


# Page-cache status header set on responses the server-side cache touched:
# HIT (served fresh from cache), STALE (served stale while revalidating), or
# MISS (rendered now and stored).
_CACHE_STATUS_HEADER = "x-pyxle-cache"

# Fallback s-maxage for a cached entry with no explicit revalidate window
# (cache-until-invalidated). Only reachable via the compile-time cache
# directive; loader-envelope and edge-config entries always carry a number.
_DEFAULT_CACHE_SECONDS = 3600


def _public_cache_control(seconds: int) -> str:
    """The shared-cache directive for a publicly cacheable page response."""

    return f"public, s-maxage={seconds}, stale-while-revalidate={seconds * 5}"


def _read_revalidate_header(response: Response) -> float | None:
    """Pop the framework's internal revalidate header (set by a loader envelope).

    Returns the declared cache lifetime in seconds, or ``None`` when the render
    declared none. The header is stripped so it never reaches the client.
    """

    raw = response.headers.get(REVALIDATE_HEADER)
    if raw is None:
        return None
    del response.headers[REVALIDATE_HEADER]
    try:
        return float(raw)
    except ValueError:  # pragma: no cover - the header is framework-produced
        return None


def _effective_cache_ttl(
    response: Response,
    request: Request,
    cache_config: object | None,
    *,
    directive_ttl: float | None = None,
) -> float | None:
    """Resolve a render's cache TTL.

    Precedence: a loader ``{data, revalidate}`` envelope wins over a page's
    compile-time ``CACHE = {"revalidate": N}`` directive, which wins over the
    project's edge ``cache`` config. ``None`` means the route is not
    server-cacheable for this request.
    """

    loader_ttl = _read_revalidate_header(response)
    if loader_ttl is not None:
        return loader_ttl
    if directive_ttl is not None:
        return directive_ttl
    if cache_config is not None:
        edge = cache_config.max_age_for(request.url.path)
        if edge is not None:
            return float(edge)
    return None


async def _read_response_body(response: Response) -> bytes:
    """Materialise a page response (possibly a stream) into bytes for caching."""

    body = getattr(response, "body", None)
    if body is not None:
        return bytes(body)
    chunks: list[bytes] = []
    async for chunk in response.body_iterator:
        chunks.append(
            chunk if isinstance(chunk, (bytes, bytearray)) else str(chunk).encode("utf-8")
        )
    return b"".join(chunks)


def _if_none_match_matches(header: str | None, etag: str) -> bool:
    """RFC 7232 §3.2 If-None-Match test for a cached page.

    Handles a comma-separated list of validators, weak (``W/``) comparison
    (the stored ETag is always strong, so this reduces to comparing the
    opaque tag), and the ``*`` wildcard. A raw string-equality check would
    wrongly re-send the full body for any of those forms.
    """

    if header is None:
        return False
    tokens = [token.strip() for token in header.split(",")]
    if "*" in tokens:
        return True

    def _strong(tag: str) -> str:
        return tag[2:] if tag.startswith("W/") else tag

    target = _strong(etag)
    return any(_strong(token) == target for token in tokens)


def _serve_cache_entry(entry, *, request: Request, status_label: str) -> Response:
    """Build a response from a stored render, answering If-None-Match with 304."""

    if _if_none_match_matches(request.headers.get("if-none-match"), entry.etag):
        response: Response = Response(status_code=304)
    else:
        response = Response(
            content=entry.body, status_code=entry.status_code, media_type="text/html"
        )
    response.headers["ETag"] = entry.etag
    response.headers["Vary"] = _NAVIGATION_HEADER
    response.headers[_CACHE_STATUS_HEADER] = status_label
    # ceil so a sub-second window never collapses to s-maxage=0.
    seconds = math.ceil(entry.revalidate) if entry.revalidate is not None else _DEFAULT_CACHE_SECONDS
    response.headers["Cache-Control"] = _public_cache_control(seconds)
    return response


def _synthetic_get_request(request: Request) -> Request:
    """A standalone GET request cloned from ``request`` for background re-render.

    ISR revalidation runs after the original response is sent, so it must not
    reuse the live request's receive channel. The clone is also deliberately
    stripped of per-user / per-request inputs — query string, ``Cookie``,
    ``Authorization`` — so a background re-render produces the same shared bytes
    no matter which user happened to observe staleness; it must never bake one
    user's request into the entry every other user then receives.
    """

    scope = dict(request.scope)
    scope["method"] = "GET"
    scope["query_string"] = b""
    scope["headers"] = [
        (name, value)
        for (name, value) in scope.get("headers", [])
        if name.lower() not in (b"cookie", b"authorization")
    ]
    # Drop the per-user CSRF token so a background re-render never bakes the
    # triggering user's token into the shared entry.
    scope.pop("pyxle.csrf_token", None)

    async def _receive() -> Message:
        return {"type": "http.request", "body": b"", "more_body": False}

    return Request(scope, _receive)


def _make_page_revalidator(
    *,
    request: Request,
    route: PageRoute,
    settings: DevServerSettings,
    renderer: ComponentRenderer,
    error_boundaries: ErrorBoundaryRegistry | None,
    page_cache: PageCache,
    cache_key: str,
):
    """Build the coroutine that re-renders a stale page and refreshes the cache."""

    cache_config = getattr(settings, "cache", None)

    async def _revalidate() -> None:
        fresh = await build_page_response(
            request=_synthetic_get_request(request),
            settings=settings,
            page=route,
            renderer=renderer,
            overlay=None,
            error_boundaries=error_boundaries,
        )
        if fresh.status_code != 200:
            return
        ttl = _effective_cache_ttl(
            fresh, request, cache_config, directive_ttl=route.cache_revalidate
        )
        if ttl is None:
            return
        body = await _read_response_body(fresh)
        await page_cache.store(cache_key, body, status_code=200, revalidate=ttl)

    return _revalidate


async def _build_cached_page_response(
    *,
    request: Request,
    route: PageRoute,
    settings: DevServerSettings,
    renderer: ComponentRenderer,
    overlay: OverlayManager | None,
    error_boundaries: ErrorBoundaryRegistry | None,
    page_cache: PageCache | None,
    stream_render: Callable[..., Any] | None = None,
) -> Response:
    """Render a page, or serve it from the server-side page cache.

    Caching applies only to GET requests on routes that declared themselves
    publicly cacheable -- a loader ``{data, revalidate}`` envelope or an edge
    ``cache`` config entry -- the same "renders no per-user data" contract the
    edge cache uses. A fresh hit skips both the loader and the Node SSR render;
    a stale hit serves the stale bytes and refreshes in the background (ISR); a
    miss renders now and stores the result.
    """

    cache_config = getattr(settings, "cache", None)
    # Only GET requests with an empty query string are cacheable: the cache key
    # is the route path, so a query-varying render (?q=, ?page=) must never share
    # an entry with a different query. Requests carrying a query fall through to
    # a live render.
    cacheable_request = request.method == "GET" and not request.url.query
    cache_key = PageCache.make_key(request.url.path)

    if page_cache is not None and cacheable_request:
        lookup = await page_cache.get(cache_key)
        if lookup is not None:
            if lookup.is_stale:
                page_cache.schedule_revalidation(
                    cache_key,
                    _make_page_revalidator(
                        request=request,
                        route=route,
                        settings=settings,
                        renderer=renderer,
                        error_boundaries=error_boundaries,
                        page_cache=page_cache,
                        cache_key=cache_key,
                    ),
                )
            return _serve_cache_entry(
                lookup.entry,
                request=request,
                status_label="STALE" if lookup.is_stale else "HIT",
            )

    # A route declared cacheable at compile time (a CACHE directive) or via the
    # edge `cache` config renders no per-user data, so its per-user CSRF token is
    # suppressed from the rendered HTML — a shared cached body must not carry one
    # user's token. (A loader-envelope-only route, whose cacheability isn't known
    # until after the render, is caught by the store-time guard below instead.)
    statically_cacheable = route.cache_revalidate is not None or (
        cache_config is not None
        and cache_config.max_age_for(request.url.path) is not None
    )

    # Streaming SSR (opt-in): a page that uses <Suspense> — or one wrapped in a
    # route-level loading.pyxl boundary — streams its shell before its async
    # boundaries resolve, for a faster TTFB. It only applies to routes that are
    # NOT publicly cacheable — a cacheable route must materialise its body to
    # store + ETag it, so streaming would buy nothing and can't be cached. The
    # buffered path stays the default for everything else.
    if (
        stream_render is not None
        and (route.uses_suspense or route.loading_boundary is not None)
        and not statically_cacheable
    ):
        streamed = await build_streaming_page_response(
            request=request,
            settings=settings,
            page=route,
            renderer=renderer,
            stream_render=stream_render,
            overlay=overlay,
            error_boundaries=error_boundaries,
        )
        streamed.headers["Vary"] = _NAVIGATION_HEADER
        streamed.headers["Cache-Control"] = "private, no-cache"
        return streamed

    response = await build_page_response(
        request=request,
        settings=settings,
        page=route,
        renderer=renderer,
        overlay=overlay,
        error_boundaries=error_boundaries,
        suppress_per_user=statically_cacheable and cacheable_request,
    )
    # HTML page responses carry Vary so a browser that cached both the HTML and
    # a nav-JSON payload for the same URL knows they are distinct entries.
    response.headers["Vary"] = _NAVIGATION_HEADER

    ttl = _effective_cache_ttl(
        response, request, cache_config, directive_ttl=route.cache_revalidate
    )
    if ttl is not None:
        # A route declared cacheable is served `public, s-maxage=N` so a
        # CDN/proxy can absorb the load — the CSRF middleware drops its per-user
        # cookie from such responses. Every other page stays `private,
        # no-cache`, never shared between users.
        response.headers["Cache-Control"] = _public_cache_control(math.ceil(ttl))
    else:
        response.headers["Cache-Control"] = "private, no-cache"

    if (
        page_cache is not None
        and ttl is not None
        and cacheable_request
        and response.status_code == 200
    ):
        body = await _read_response_body(response)
        # Safety net for a loader-envelope route that also renders a <Form>:
        # never store a body that still carries the requester's CSRF token — it
        # is per-user data and must not be shared. Such a page renders live each
        # request instead of being cached.
        token = request.scope.get("pyxle.csrf_token")
        if isinstance(token, str) and token and token.encode("utf-8") in body:
            return response
        await page_cache.store(
            cache_key, body, status_code=response.status_code, revalidate=ttl
        )
        served = Response(
            content=body, status_code=response.status_code, media_type="text/html"
        )
        for key, value in response.headers.items():
            if key.lower() != "content-length":
                served.headers[key] = value
        served.headers["ETag"] = PageCache.make_etag(body)
        served.headers[_CACHE_STATUS_HEADER] = "MISS"
        # Conditional GET: if the client already holds this exact render, 304.
        if _if_none_match_matches(request.headers.get("if-none-match"), served.headers["ETag"]):
            not_modified = Response(status_code=304)
            for key, value in served.headers.items():
                if key.lower() not in ("content-length", "content-type"):
                    not_modified.headers[key] = value
            return not_modified
        return served

    return response


def _make_page_handler(
    route: PageRoute,
    *,
    settings: DevServerSettings,
    renderer: ComponentRenderer,
    overlay: OverlayManager | None,
    error_boundaries: ErrorBoundaryRegistry | None = None,
    page_cache: PageCache | None = None,
    stream_render: Callable[..., Any] | None = None,
):
    async def handler(request: Request):  # pragma: no cover - thin wrapper
        wants_navigation_payload = request.headers.get(_NAVIGATION_HEADER) == "1"
        if wants_navigation_payload:
            response = await build_page_navigation_response(
                request=request,
                settings=settings,
                page=route,
                renderer=renderer,
                overlay=overlay,
                error_boundaries=error_boundaries,
            )
            # Navigation JSON MUST be cached separately from the HTML
            # response for the same URL. Without Vary, the browser's
            # HTTP cache can serve stale JSON when the user returns to
            # a backgrounded tab (the reload request omits the nav
            # header, but the cache matches on URL alone). `no-store`
            # prevents caching entirely — the Pyxle client already has
            # its own in-memory navigationCache for dedup.
            response.headers["Vary"] = _NAVIGATION_HEADER
            response.headers["Cache-Control"] = "no-store"
            return response

        return await _build_cached_page_response(
            request=request,
            route=route,
            settings=settings,
            renderer=renderer,
            overlay=overlay,
            error_boundaries=error_boundaries,
            page_cache=page_cache,
            stream_render=stream_render,
        )

    handler.__name__ = f"page_{route.module_key.replace('.', '_')}"
    return handler


def build_action_router(
    routes: Iterable[ActionRoute], *, debug: bool = False,
) -> Router:
    """Create a Starlette ``Router`` for auto-generated ``@action`` endpoints.

    Each action is registered as ``POST /api/__actions/<page_path>/<action_name>``.
    The handler imports the page server module, locates the action function by name,
    validates the ``__pyxle_action__`` tag, and dispatches the request to it.

    For pages with catch-all or dynamic route parameters, a single catch-all
    action route (``is_catchall=True``) is also registered.  It captures the
    trailing path segments and extracts the action name from the last one,
    allowing the client to resolve actions regardless of the active sub-path.

    When *debug* is ``True``, action modules are re-imported from disk on every
    request so code changes take effect immediately.  When ``False`` (production),
    modules are cached after first import.
    """

    router = Router()

    for route in routes:
        if route.is_catchall:
            handler = _make_catchall_action_handler(route, debug=debug)
        else:
            handler = _make_action_handler(route, debug=debug)
        router.add_route(route.path, handler, methods=["POST"])

    return router


_MAX_ACTION_BODY_BYTES = 10 * 1024 * 1024  # 10 MB


def _maybe_install_form_body_shim(request: Request) -> None:
    """Make ``await request.json()`` work for form-encoded action bodies.

    When the action endpoint is hit by a no-JS ``<Form>`` POST, the
    body arrives as ``application/x-www-form-urlencoded`` (or
    ``multipart/form-data``) — Starlette's ``request.json()`` would then
    raise ``JSONDecodeError`` on the very first byte. To keep user
    actions ergonomic (the documented and demo-shown pattern is
    ``body = await request.json()``), this shim:

    1. Detects the form content type and replaces ``request.json`` with
       a coroutine that returns the parsed form fields as a dict.
    2. Strips the synthetic ``_csrf_token`` field so it doesn't leak
       into action ``body`` payloads — the CSRF middleware has already
       validated and consumed it.

    JSON requests are untouched.
    """
    content_type = (request.headers.get("content-type") or "").lower()
    if not (
        "application/x-www-form-urlencoded" in content_type
        or "multipart/form-data" in content_type
    ):
        return

    async def _form_as_json() -> dict[str, object]:
        form = await request.form()
        result: dict[str, object] = {}
        for key in form:
            if key == "_csrf_token":
                continue
            values = form.getlist(key)
            result[key] = values[0] if len(values) == 1 else list(values)
        return result

    # Bypass Starlette's cached ``_json``: monkey-patch the bound method
    # for this request only. We deliberately don't subclass ``Request``
    # since user middleware may compare types or rely on the original.
    request.json = _form_as_json  # type: ignore[method-assign]


async def _dispatch_action(
    request: Request,
    module_key: str,
    server_module_path: Path,
    action_name: str,
    *,
    debug: bool = False,
) -> JSONResponse:
    """Shared dispatch logic for both specific and catch-all action handlers."""
    from pyxle.devserver._security import SAFE_IDENTIFIER_RE
    from pyxle.devserver.validation import (
        PydanticNotInstalledError,
        get_cached_body_model,
        validate_body,
    )
    from pyxle.runtime import ActionError, ValidationActionError

    # L-9: reject obviously invalid action names early.
    if not SAFE_IDENTIFIER_RE.match(action_name):
        return JSONResponse(
            {"ok": False, "error": "Invalid action name"},
            status_code=400,
        )

    # L-10: reject oversized request bodies before doing any work.
    content_length = request.headers.get("content-length")
    if content_length and content_length.isdigit() and int(content_length) > _MAX_ACTION_BODY_BYTES:
        return JSONResponse(
            {"ok": False, "error": "Request body too large"},
            status_code=413,
        )

    try:
        module = _import_module(module_key, server_module_path, debug=debug)
    except ApiRouteError as exc:
        error_msg = str(exc) if debug else "Internal server error"
        return JSONResponse({"ok": False, "error": error_msg}, status_code=500)

    # M-5: collapse existence + decorator check to prevent enumeration.
    action_fn = getattr(module, action_name, None)
    if action_fn is None or not getattr(action_fn, "__pyxle_action__", False):
        return JSONResponse(
            {"ok": False, "error": f"Action '{action_name}' not found"},
            status_code=404,
        )

    # I-5: warn when a synchronous function is decorated as @action.
    if not inspect.iscoroutinefunction(action_fn):
        import logging as _logging  # noqa: PLC0415

        _logging.getLogger(__name__).warning(
            "Action '%s' in module '%s' is synchronous. "
            "Actions should be async functions.",
            action_name,
            module_key,
        )

    # Progressive-enhancement support for ``<Form>``: when the request
    # body is form-encoded (no-JS form POST) we transparently expose it
    # as JSON to the action via a thin shim on ``request.json``. User
    # code can keep doing ``await request.json()`` regardless of how the
    # body arrived. The shim is keyed off content-type so JSON requests
    # take the original Starlette path unchanged.
    _maybe_install_form_body_shim(request)

    # When the action type-hints a Pydantic model as its body parameter, parse
    # and validate the request body into the model and inject it; otherwise the
    # action is called with just ``request`` (unchanged). Introspection is
    # cached per function object.
    try:
        resolved = get_cached_body_model(action_fn)
    except PydanticNotInstalledError as exc:
        error_msg = str(exc) if debug else "Internal server error"
        return JSONResponse({"ok": False, "error": error_msg}, status_code=500)

    try:
        if resolved is None:
            result = await action_fn(request)
        else:
            try:
                body_payload = await request.json()
            except Exception:
                raise ValidationActionError(
                    fields={"__root__": ["Request body must be valid JSON."]}
                ) from None
            body = validate_body(resolved.model, body_payload)
            result = await action_fn(request, **{resolved.param_name: body})
    except ActionError as exc:
        payload: dict[str, object] = {"ok": False, "error": exc.message}
        if exc.data:
            payload["data"] = exc.data
        if exc.fields:
            payload["fields"] = exc.fields
        return JSONResponse(payload, status_code=exc.status_code)
    except Exception as exc:
        error_msg = str(exc) if debug else "Internal server error"
        return JSONResponse({"ok": False, "error": error_msg}, status_code=500)

    if not isinstance(result, dict):
        return JSONResponse(
            {"ok": False, "error": "Action must return a JSON-serializable dict"},
            status_code=500,
        )

    # If the action used ``invalidate_routes(...)`` on a plain dict, the
    # invalidation targets were stashed under ``__pyxle_invalidate__``.
    # Lift them into an HTTP response header and strip the sentinel from
    # the body before serialising.
    invalidate_hints = result.pop("__pyxle_invalidate__", None)
    response = JSONResponse({"ok": True, **result})
    if invalidate_hints:
        if isinstance(invalidate_hints, str):
            invalidate_hints = [invalidate_hints]
        joined = ", ".join(u for u in invalidate_hints if u)
        if joined:
            response.headers["x-pyxle-invalidate"] = joined
    return response


def _make_action_handler(route: ActionRoute, *, debug: bool = False):
    async def handler(request: Request):
        return await _dispatch_action(
            request, route.module_key, route.server_module_path, route.action_name,
            debug=debug,
        )

    handler.__name__ = f"action_{route.module_key.replace('.', '_')}_{route.action_name}"
    return handler


def _make_catchall_action_handler(route: ActionRoute, *, debug: bool = False):
    """Create a handler that extracts the action name from a catch-all path.

    The client constructs action URLs using ``window.location.pathname``.
    For catch-all pages (e.g. ``/docs/{slug:path}``), the browser path
    includes dynamic segments (e.g. ``/docs/getting-started/installation``),
    producing an action URL like
    ``/api/__actions/docs/getting-started/installation/search_docs``.

    This handler captures the trailing path via ``{_pyxle_action_path:path}``
    and treats the last segment as the action name.
    """

    async def handler(request: Request):
        action_path = request.path_params.get("_pyxle_action_path", "")
        action_name = action_path.rsplit("/", 1)[-1] if action_path else ""

        if not action_name:
            return JSONResponse(
                {"ok": False, "error": "Action name missing from request path"},
                status_code=400,
            )

        return await _dispatch_action(
            request, route.module_key, route.server_module_path, action_name,
            debug=debug,
        )

    handler.__name__ = f"action_{route.module_key.replace('.', '_')}_catchall"
    return handler


def build_static_files_mount(
    settings: DevServerSettings,
    *,
    directory: Path | None = None,
    mount_path: str = "/",
) -> Mount:
    """Return a Starlette ``Mount`` serving static assets."""

    target = directory or settings.public_dir
    static_app = HttpOnlyStaticFiles(directory=target, check_dir=False)
    return Mount(mount_path, app=static_app, name="pyxle-public")


def build_client_assets_mount(directory: Path, *, mount_path: str = "/client") -> Mount:
    """Serve built client bundles (e.g., ``dist/client``) under ``/client``."""

    static_app = HttpOnlyStaticFiles(directory=directory, check_dir=False)
    return Mount(mount_path, app=static_app, name="pyxle-client-assets")



def _build_app_routes(
    *,
    settings: DevServerSettings,
    routes: RouteTable,
    renderer: ComponentRenderer,
    overlay: OverlayManager | None,
    api_route_hooks: Sequence[RouteHookCallable],
    page_route_hooks: Sequence[RouteHookCallable],
    page_cache: PageCache | None = None,
    stream_render: Callable[..., Any] | None = None,
) -> tuple[list[Any], ErrorBoundaryRegistry]:
    """Build the ordered Starlette route list for a route table.

    Shared by initial app construction and the dev-server hot route-table
    refresh so both produce an identical route set (API, action, page, the
    overlay WebSocket, health probes, and the not-found catch-all, in that
    order). Returns the route list plus the freshly built error-boundary
    registry.
    """
    error_boundaries = build_error_boundary_registry(list(routes.error_boundary_pages))
    api_router = build_api_router(
        routes.apis,
        route_hooks=[*DEFAULT_API_POLICIES, *api_route_hooks],
    )
    page_router = build_page_router(
        routes.pages,
        settings=settings,
        renderer=renderer,
        overlay=overlay,
        route_hooks=[*DEFAULT_PAGE_POLICIES, *page_route_hooks],
        error_boundaries=error_boundaries,
        page_cache=page_cache,
        stream_render=stream_render,
    )
    action_router = build_action_router(routes.actions, debug=settings.debug)

    built: list[Any] = []
    built.extend(api_router.routes)
    built.extend(action_router.routes)
    built.extend(page_router.routes)
    if overlay is not None:
        built.append(WebSocketRoute("/__pyxle__/overlay", overlay.websocket_endpoint))
    built.append(Route("/healthz", _healthz_endpoint, methods=["GET"]))
    built.append(Route("/readyz", _readyz_endpoint, methods=["GET"]))
    # Catch-all 404 handler from not-found.pyxl boundaries — registered last so
    # it only matches when no concrete route does.
    if error_boundaries.has_not_found_pages:
        not_found_handler = _make_not_found_handler(
            settings=settings,
            renderer=renderer,
            overlay=overlay,
            error_boundaries=error_boundaries,
        )
        built.append(Route("/{path:path}", not_found_handler, methods=["GET"]))
    return built, error_boundaries


def create_starlette_app(
    settings: DevServerSettings,
    routes: RouteTable,
    *,
    logger: ConsoleLogger | None = None,
    public_static_dir: Path | None = None,
    client_static_dir: Path | None = None,
    serve_static: bool = True,
    pool: object | None = None,
    prerender_dir: Path | None = None,
) -> Starlette:
    """Assemble a Starlette application exposing API/page routes and optional static mounts.

    If ``pool`` is an :class:`~pyxle.ssr.worker_pool.SsrWorkerPool`, renders are
    dispatched to the pool instead of spawning a new Node.js process per request.
    The pool is started in the Starlette lifespan and stopped on shutdown.
    """

    console_logger = logger or ConsoleLogger()

    settings = _maybe_attach_manifest(settings, console_logger)

    _ensure_project_root_on_sys_path(settings.project_root)

    if pool is not None:
        renderer = ComponentRenderer(factory=pool_render_factory(pool))
    else:
        renderer = ComponentRenderer()
    # Streaming SSR needs the worker pool's multi-frame render_stream. Without a
    # pool (single-process fallback) streaming is unavailable and every page
    # renders buffered.
    stream_render: Callable[..., Any] | None = getattr(pool, "render_stream", None)
    # Server-side page cache: enabled for production serves, off in debug so a
    # cached render never masks an edit during development. Only routes that
    # declared themselves cacheable (a loader `revalidate` or an edge `cache`
    # entry) are ever stored, so leaving it on in production is zero-config and
    # safe. The backend (in-memory default, file, or Redis) is chosen by
    # PYXLE_PAGE_CACHE_BACKEND; see pyxle.cache.build_page_cache.
    page_cache: PageCache | None = build_page_cache(debug=settings.debug)
    overlay: OverlayManager | None = None
    vite_proxy: ViteProxy | None = None
    proxy_middleware: Middleware | None = None
    static_middleware: Middleware | None = None

    if settings.debug:
        vite_proxy = ViteProxy(settings, logger=console_logger)
        overlay_origins: set[str] = {
            f"http://localhost:{settings.starlette_port}",
            f"http://127.0.0.1:{settings.starlette_port}",
            f"http://localhost:{settings.vite_port}",
            f"http://127.0.0.1:{settings.vite_port}",
        }
        overlay = OverlayManager(
            logger=console_logger, allowed_origins=overlay_origins
        )

        class _ViteProxyMiddleware(BaseHTTPMiddleware):
            def __init__(self, app):
                super().__init__(app)
                self._proxy = vite_proxy

            async def dispatch(self, request: Request, call_next):  # pragma: no cover - middleware wrapper
                if self._proxy.should_proxy(request):
                    return await self._proxy.handle(request)
                return await call_next(request)

        proxy_middleware = Middleware(_ViteProxyMiddleware)

    try:
        user_middleware = load_custom_middlewares(settings.custom_middlewares)
    except MiddlewareHookError as exc:
        console_logger.error(str(exc))
        raise

    # --- CORS middleware ---
    cors_middleware: Middleware | None = None

    _ALL_INTERFACES = ("0.0.0.0", "::", "")
    _LOOPBACK_HOSTS = (*_ALL_INTERFACES, "127.0.0.1", "localhost")

    def _vite_dev_cors_kwargs(host: str, port: int) -> dict:
        """Return ``CORSMiddleware`` origin kwargs for the Vite dev server.

        Browsers treat ``localhost`` and ``127.0.0.1`` as distinct origins,
        so both are listed when the server is on a loopback address.  When
        bound to all interfaces (``0.0.0.0`` / ``::``), LAN access is
        allowed via a regex restricted to ``localhost``, loopback, and
        RFC 1918 private IP ranges — **not** arbitrary hostnames.
        """
        import re  # noqa: PLC0415

        if host in _ALL_INTERFACES:
            console_logger.warning(
                "Dev server bound to all interfaces (0.0.0.0). "
                "CORS allows localhost and private-network origins only."
            )
            # Match localhost, 127.0.0.1, and RFC 1918 private ranges only.
            private_re = re.compile(
                rf"^https?://(?:localhost|127\.0\.0\.1"
                rf"|10\.\d{{1,3}}\.\d{{1,3}}\.\d{{1,3}}"
                rf"|172\.(?:1[6-9]|2\d|3[01])\.\d{{1,3}}\.\d{{1,3}}"
                rf"|192\.168\.\d{{1,3}}\.\d{{1,3}}):{port}$"
            )
            return {
                "allow_origins": [f"http://localhost:{port}", f"http://127.0.0.1:{port}"],
                "allow_origin_regex": private_re.pattern,
            }
        if host in _LOOPBACK_HOSTS:
            return {"allow_origins": [f"http://localhost:{port}", f"http://127.0.0.1:{port}"]}
        return {"allow_origins": [f"http://{host}:{port}"]}

    if settings.cors is not None and getattr(settings.cors, "enabled", False):
        from starlette.middleware.cors import CORSMiddleware

        origins = list(settings.cors.origins)
        cors_extra: dict = {}
        # In debug mode, ensure the Vite dev server origin is always allowed
        # so that HMR and asset requests from the Vite port succeed.
        if settings.debug:
            vite_kwargs = _vite_dev_cors_kwargs(settings.vite_host, settings.vite_port)
            for vite_origin in vite_kwargs.get("allow_origins", []):
                if vite_origin not in origins:
                    origins.append(vite_origin)
            if "allow_origin_regex" in vite_kwargs:
                cors_extra["allow_origin_regex"] = vite_kwargs["allow_origin_regex"]

        cors_middleware = Middleware(
            CORSMiddleware,
            allow_origins=origins,
            allow_methods=list(settings.cors.methods),
            allow_headers=list(settings.cors.headers),
            allow_credentials=settings.cors.credentials,
            max_age=settings.cors.max_age,
            **cors_extra,
        )
    elif settings.debug:
        # No user-configured CORS, but in dev mode we still need to allow
        # cross-origin requests from the Vite dev server (different port).
        from starlette.middleware.cors import CORSMiddleware

        vite_cors = _vite_dev_cors_kwargs(settings.vite_host, settings.vite_port)
        # Do not combine allow_credentials=True with a wildcard-style
        # origin regex — only set credentials for explicit origins.
        uses_regex = "allow_origin_regex" in vite_cors
        cors_middleware = Middleware(
            CORSMiddleware,
            **vite_cors,
            allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
            allow_headers=["*"],
            allow_credentials=not uses_regex,
            max_age=600,
        )

    # --- CSRF middleware ---
    csrf_middleware: Middleware | None = None
    if settings.csrf is not None and getattr(settings.csrf, "enabled", False):
        import os

        from .csrf import CsrfMiddleware

        # In production, default cookie_secure to True unless explicitly
        # overridden by the user configuration.
        cookie_secure = settings.csrf.cookie_secure
        if not settings.debug and not cookie_secure:
            cookie_secure = True

        csrf_middleware = Middleware(
            CsrfMiddleware,
            secret=os.environ.get("PYXLE_SECRET_KEY", ""),
            cookie_name=settings.csrf.cookie_name,
            header_name=settings.csrf.header_name,
            cookie_secure=cookie_secure,
            cookie_samesite=settings.csrf.cookie_samesite,
            exempt_paths=settings.csrf.exempt_paths,
        )

    try:
        page_route_hooks = load_route_hooks(settings.page_route_hooks)
        api_route_hooks = load_route_hooks(settings.api_route_hooks)
    except RouteHookError as exc:
        console_logger.error(str(exc))
        raise

    # Resolve plugin specs up-front so config errors surface at boot
    # (before the first request) instead of on-demand. Plugins are
    # instantiated here; ``on_startup`` runs inside the lifespan so
    # async work (DB connections, HTTP clients) happens at the right
    # moment.
    from pyxle.plugins import (  # noqa: PLC0415
        PluginContext,
        PluginSpec,
        load_plugins,
        run_shutdown,
        run_startup,
        set_active_context,
    )

    _plugin_specs = tuple(
        PluginSpec.from_config_entry(entry, source=str(settings.project_root))
        for entry in settings.plugins
    )
    _plugins = load_plugins(_plugin_specs)
    _plugin_ctx = PluginContext(settings=settings)

    @asynccontextmanager
    async def lifespan(app: Starlette):  # pragma: no cover - lifecycle orchestration
        if pool is not None:
            await pool.start()
        # Startup plugins AFTER the SSR pool so plugins that need
        # access to the pool (e.g. something that preloads pages)
        # can see a running pool. Failures here propagate and abort
        # startup — the right posture for "pyxle-db can't reach the
        # database".
        await run_startup(_plugins, _plugin_ctx)
        # Two access paths for the resulting services:
        #   1. ``request.app.state.pyxle_plugins.require("name")`` —
        #      the explicit form; works in handlers that already have
        #      the request and want to stay context-pure.
        #   2. ``from pyxle.plugins import plugin; plugin("name")`` —
        #      the Django-style shortcut backed by the module-level
        #      active context we set here. Short and import-based;
        #      preferred for most app code.
        app.state.pyxle_plugins = _plugin_ctx
        set_active_context(_plugin_ctx)
        # Register the page cache so `pyxle.cache.invalidate(path)` can reach it
        # from actions without threading a handle through the request.
        if page_cache is not None:
            app.state.pyxle_page_cache = page_cache
            set_active_cache(page_cache)
            # Warm the cache from any build-time pre-rendered pages
            # (`pyxle build --static`) so their first request is a hit.
            if prerender_dir is not None and prerender_dir.exists():
                static_paths = [route.path for route in select_static_pages(routes.pages)]
                warmed = await warm_page_cache(page_cache, static_paths, prerender_dir)
                if warmed:
                    console_logger.info(
                        f"Warmed {warmed} pre-rendered page(s) from {prerender_dir}"
                    )
        try:
            yield
        finally:
            # Shutdown in reverse order — best-effort; individual
            # failures are logged, not re-raised.
            if page_cache is not None:
                set_active_cache(None)
                await page_cache.aclose()
            set_active_context(None)
            await run_shutdown(_plugins, _plugin_ctx)
            if pool is not None:
                await pool.stop()
            if vite_proxy is not None:
                await vite_proxy.close()

    if serve_static:
        public_directory = public_static_dir or settings.public_dir
        static_middleware = Middleware(
            StaticAssetsMiddleware,
            public_directory=public_directory if public_directory.exists() else None,
            client_directory=client_static_dir if client_static_dir and client_static_dir.exists() else None,
            # Memory-cache small assets only when serving an immutable
            # production build; dev keeps reading from disk so edits to
            # public/ files show up without a restart.
            cache_in_memory=not settings.debug,
        )

    middleware_stack: list[Middleware] = []

    # GZip compression in production mode (reduces bandwidth ~60-70%).
    if not settings.debug:
        from starlette.middleware.gzip import GZipMiddleware  # noqa: PLC0415

        middleware_stack.append(Middleware(GZipMiddleware, minimum_size=500))

    # Security response headers in production mode.
    if not settings.debug:
        middleware_stack.append(Middleware(_SecurityHeadersMiddleware))

    if cors_middleware is not None:
        middleware_stack.append(cors_middleware)
    if csrf_middleware is not None:
        middleware_stack.append(csrf_middleware)
    if static_middleware is not None:
        middleware_stack.append(static_middleware)
    middleware_stack.extend(user_middleware)
    # Plugin-contributed middleware slots in between the host app's
    # user middleware and the Vite proxy. Each plugin can return any
    # number of ``(import_string, options)`` pairs from ``middleware()``.
    for _plugin in _plugins:
        for entry in _plugin.middleware() or ():
            try:
                import_string, options = entry
            except (TypeError, ValueError):
                console_logger.warning(
                    "Plugin '%s' returned a middleware entry that isn't "
                    "(import_string, options); skipping: %r",
                    _plugin.name,
                    entry,
                )
                continue
            try:
                middleware_cls = _import_middleware_class(import_string)
            except Exception as exc:
                console_logger.error(
                    "Plugin '%s' middleware '%s' could not be loaded: %s",
                    _plugin.name,
                    import_string,
                    exc,
                )
                raise
            middleware_stack.append(Middleware(middleware_cls, **dict(options or {})))
    if proxy_middleware is not None:
        middleware_stack.append(proxy_middleware)

    app = Starlette(
        debug=settings.debug,
        middleware=middleware_stack,
        lifespan=lifespan,
    )

    app.state.pyxle_started_at = time.time()
    app.state.pyxle_ready = False

    app_routes, error_boundaries = _build_app_routes(
        settings=settings,
        routes=routes,
        renderer=renderer,
        overlay=overlay,
        api_route_hooks=api_route_hooks,
        page_route_hooks=page_route_hooks,
        page_cache=page_cache,
        stream_render=stream_render,
    )
    app.router.routes.extend(app_routes)

    app.state.vite_proxy = vite_proxy
    app.state.ssr_renderer = renderer
    app.state.overlay = overlay
    app.state.error_boundaries = error_boundaries
    # The dev-server hot route-table refresh reuses these to rebuild routes live
    # on a source change (see ``DevServer._handle_rebuild``). Config-derived
    # hooks are stable across rebuilds — config changes still need a restart.
    app.state.pyxle_route_hooks = (api_route_hooks, page_route_hooks)
    # Streaming SSR's render_stream is bound to the worker pool, which outlives
    # route-table refreshes — stash it so a hot rebuild keeps streaming wired.
    app.state.pyxle_stream_render = stream_render
    # One in-process pub/sub broker per app process, shared by every WebSocket
    # connection (pyxle.realtime.channel reads it off app.state). In-process
    # only — see the multi-worker caveat documented in pyxle.realtime.channels
    # and logged at serve time when --workers > 1.
    from pyxle.realtime import InProcessBroker  # noqa: PLC0415 - lazy, optional path

    app.state.pyxle_broker = InProcessBroker()

    return app


def _make_not_found_handler(
    *,
    settings: DevServerSettings,
    renderer: ComponentRenderer,
    overlay: OverlayManager | None,
    error_boundaries: ErrorBoundaryRegistry,
):
    """Create a catch-all handler that renders the nearest ``not-found.pyxl``."""

    from starlette.responses import HTMLResponse as _HTMLResponse

    async def handler(request: Request):  # pragma: no cover - thin wrapper
        response = await build_not_found_response(
            request=request,
            settings=settings,
            renderer=renderer,
            error_boundaries=error_boundaries,
            overlay=overlay,
        )
        if response is not None:
            return response
        # No not-found boundary rendered — return a plain 404.
        return _HTMLResponse("Not Found", status_code=404)

    handler.__name__ = "pyxle_not_found"
    return handler


def _maybe_attach_manifest(settings: DevServerSettings, logger: ConsoleLogger) -> DevServerSettings:
    if settings.debug or settings.page_manifest is not None:
        return settings

    manifest_path = settings.project_root / "dist" / "page-manifest.json"
    if not manifest_path.exists():
        logger.warning(
            f"Production mode enabled but page-manifest.json not found at {manifest_path}"
        )
        return settings

    from pyxle.build.manifest import load_manifest

    try:
        manifest_data = load_manifest(manifest_path)
    except Exception as exc:  # pragma: no cover - defensive logging
        logger.error(f"Failed to load page-manifest.json: {exc}")
        return settings

    return replace(settings, page_manifest=manifest_data)


def _health_payload(app: Starlette) -> dict[str, object]:
    started_at = getattr(app.state, "pyxle_started_at", None)
    ready = bool(getattr(app.state, "pyxle_ready", False))
    uptime = 0.0
    if isinstance(started_at, (int, float)):
        uptime = max(0.0, time.time() - float(started_at))

    return {
        "status": "ok",
        "ready": ready,
        "uptime": uptime,
    }


async def _healthz_endpoint(request: Request) -> JSONResponse:
    payload = _health_payload(request.app)
    return JSONResponse(payload)


async def _readyz_endpoint(request: Request) -> JSONResponse:
    payload = _health_payload(request.app)
    status_code = 200 if payload["ready"] else 503
    return JSONResponse(payload, status_code=status_code)


__all__ = [
    "build_action_router",
    "build_api_router",
    "build_page_router",
    "build_static_files_mount",
    "build_client_assets_mount",
    "create_starlette_app",
    "ApiRouteError",
]
