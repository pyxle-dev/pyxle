"""HTTP surface of Pyxle Studio: UI shell, static assets, JSON API, SSE.

Every route built here is registered by ``_build_app_routes`` **only** when a
:class:`~pyxle.devserver.studio.StudioManager` exists — i.e. in debug mode
with the ``studio`` config block enabled. Production assembly never constructs
the manager, so none of these paths exist under ``pyxle serve``.

Security posture (a local tool, still hardened):

* Every endpoint is guarded twice: a request from a non-loopback client peer is
  refused unless its ``Host`` was explicitly opted in via
  ``studio.allowedHosts`` (so binding to ``0.0.0.0`` never silently exposes
  Studio, and a spoofed ``Host: localhost`` from the LAN is rejected), and a
  loopback peer's ``Host`` must be a known local hostname (the DNS-rebinding
  defence for the developer's own browser).
* Mutating endpoints are ``POST`` and require ``Content-Type:
  application/json``, so a cross-site form or no-CORS fetch can never invoke
  them; with CSRF enabled (the default) they additionally ride the app's
  double-submit protection.
* Responses carry ``Cache-Control: no-store``; the config view redacts both
  secret-shaped keys and secret-shaped values (DSNs, bearer tokens).
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import re
import time
from importlib import resources
from typing import TYPE_CHECKING, Any, AsyncIterator, Callable, Dict, List, Optional
from urllib.parse import urlencode

from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from . import STUDIO_PATH, StudioManager

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..routes import PageRoute, RouteTable
    from ..settings import DevServerSettings

_NO_STORE = "no-store"

#: Hostnames always accepted by the Host-header guard (loopback forms).
_LOOPBACK_HOSTNAMES = frozenset({"localhost", "127.0.0.1", "::1"})

#: Client peer addresses treated as loopback (fully trusted). ``localhost``
#: never appears as a peer address (it is resolved to an IP) but is included
#: defensively.
_LOOPBACK_PEERS = frozenset({"127.0.0.1", "::1", "localhost"})

#: Hosts that mean "all interfaces" when used as a bind address — never
#: meaningful as an *incoming* Host header, so excluded from the allowlist.
_BIND_ALL_HOSTS = frozenset({"0.0.0.0", "::", ""})

#: Static assets Studio may serve, with their content types. An allowlist
#: (not a directory walk) so the asset handler can never traverse paths.
_ASSET_CONTENT_TYPES = {
    "studio.css": "text/css; charset=utf-8",
    "studio.js": "text/javascript; charset=utf-8",
}

_SAFE_ASSET_NAME = re.compile(r"\A[A-Za-z0-9_.-]+\Z")

#: Config keys whose values are masked in the config view.
_SECRET_KEY_PATTERN = re.compile(
    r"secret|token|password|passwd|credential|api_?key|private|dsn",
    re.IGNORECASE,
)
_REDACTED = "••••••"

#: Upper bound for a single loader run from the tester, seconds. A wedged
#: loader should surface as a clear timeout, not a hung dashboard request.
_LOADER_TIMEOUT_S = 30.0

#: SSE keep-alive interval, seconds. Comments (`: ping`) keep intermediaries
#: from timing the stream out and let dead connections fail fast on write.
_SSE_PING_INTERVAL_S = 15.0


def _host_without_port(raw: str) -> str:
    """Extract the lowercase hostname from a ``Host`` header value."""
    value = raw.strip().lower()
    if value.startswith("["):  # bracketed IPv6, e.g. ``[::1]:8000``
        closing = value.find("]")
        if closing != -1:
            return value[1:closing]
        return value.lstrip("[")
    if ":" in value:
        return value.rsplit(":", 1)[0]
    return value


def _explicit_hostnames(config: Any) -> frozenset[str]:
    """Hostnames the operator explicitly opted in via ``studio.allowedHosts``."""
    return frozenset(
        _host_without_port(str(entry))
        for entry in getattr(config, "allowed_hosts", ()) or ()
    )


def _allowed_hostnames(settings: "DevServerSettings", config: Any) -> frozenset[str]:
    allowed = set(_LOOPBACK_HOSTNAMES)
    server_host = (settings.starlette_host or "").strip().lower()
    if server_host and server_host not in _BIND_ALL_HOSTS:
        allowed.add(server_host)
    return frozenset(allowed) | _explicit_hostnames(config)


def _peer_is_loopback(request: Request) -> bool:
    client = request.client
    if client is None:  # non-network transport (test ASGI, unix socket)
        return True
    return (client.host or "").strip().lower() in _LOOPBACK_PEERS


def _host_allowed(
    request: Request, allowed: frozenset[str], explicit: frozenset[str]
) -> bool:
    """Access guard for Studio endpoints — two independent defences.

    1. **Client-peer trust.** A request from a loopback peer is the normal case
       (the developer's own browser or editor). A request from any other peer
       address is only served when its ``Host`` was *explicitly* opted in via
       ``studio.allowedHosts`` — so binding the dev server to ``0.0.0.0`` never
       silently exposes Studio to the LAN, and a spoofed ``Host: localhost``
       from a remote socket is rejected because its peer isn't loopback.
    2. **DNS-rebinding guard.** For loopback peers, the ``Host`` must still be a
       known local hostname: a rebinding attack points an attacker name at
       127.0.0.1, so the browser arrives with a foreign ``Host``. An absent
       ``Host`` (impossible from a browser) is allowed for loopback peers only.
    """
    raw = request.headers.get("host")
    if _peer_is_loopback(request):
        if raw is None:
            return True
        return _host_without_port(raw) in allowed
    # Remote peer: require an explicitly-allowlisted Host, never the implicit
    # loopback names or the bind host.
    if raw is None:
        return False
    return _host_without_port(raw) in explicit


def _forbidden_host_response(request: Request) -> JSONResponse:
    return _json(
        {
            "ok": False,
            "error": (
                f"Host {request.headers.get('host', '')!r} is not allowed to access "
                "Pyxle Studio from this client. Add the hostname to "
                "\"studio\": {\"allowedHosts\": [...]} in pyxle.config.json to reach "
                "it from another device."
            ),
        },
        status_code=403,
    )


def _json(payload: Any, *, status_code: int = 200) -> JSONResponse:
    response = JSONResponse(payload, status_code=status_code)
    response.headers["Cache-Control"] = _NO_STORE
    return response


def _guarded(
    handler: Callable[[Request], Any],
    allowed: frozenset[str],
    explicit: frozenset[str],
) -> Callable[[Request], Any]:
    """Wrap *handler* with the Studio access guard (peer + Host)."""

    async def wrapper(request: Request) -> Response:
        if not _host_allowed(request, allowed, explicit):
            return _forbidden_host_response(request)
        return await handler(request)

    return wrapper


def _static_text(filename: str) -> str:
    return (
        resources.files("pyxle.devserver.studio")
        .joinpath("static")
        .joinpath(filename)
        .read_text(encoding="utf-8")
    )


# --------------------------------------------------------------------- routes


def build_studio_routes(
    *,
    settings: "DevServerSettings",
    routes: "RouteTable",
    manager: StudioManager,
) -> List[Route]:
    """Build every Studio route for the current route table.

    Called from ``_build_app_routes`` on initial assembly **and** on each hot
    route-table refresh, so the closures below always see the fresh
    :class:`RouteTable`.
    """
    allowed = _allowed_hostnames(settings, manager.config)
    explicit = _explicit_hostnames(manager.config)

    async def index(request: Request) -> Response:
        html = _static_text("index.html")
        response = Response(html, media_type="text/html; charset=utf-8")
        response.headers["Cache-Control"] = _NO_STORE
        # Defence in depth for a page that displays app internals.
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    async def asset(request: Request) -> Response:
        filename = request.path_params.get("filename", "")
        content_type = _ASSET_CONTENT_TYPES.get(filename)
        if content_type is None or not _SAFE_ASSET_NAME.match(filename):
            return _json({"ok": False, "error": "Unknown asset."}, status_code=404)
        body = _static_text(filename)
        response = Response(body, media_type=content_type)
        response.headers["Cache-Control"] = _NO_STORE
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    async def bootstrap(request: Request) -> Response:
        return _json(_bootstrap_payload(settings))

    async def routes_endpoint(request: Request) -> Response:
        payload = await asyncio.to_thread(_routes_payload, settings, routes)
        return _json(payload)

    async def action_schema(request: Request) -> Response:
        page_path = request.query_params.get("path", "")
        action_name = request.query_params.get("name", "")
        payload = _action_schema_payload(settings, routes, page_path, action_name)
        return _json(payload, status_code=200 if payload["ok"] else 404)

    async def run_loader(request: Request) -> Response:
        return await _run_loader_endpoint(request, settings=settings, routes=routes)

    async def metrics(request: Request) -> Response:
        return _json(_metrics_payload(request))

    async def config_endpoint(request: Request) -> Response:
        return _json(_config_payload(settings))

    async def check(request: Request) -> Response:
        payload = await asyncio.to_thread(_check_payload, settings)
        return _json(payload)

    async def recent_requests(request: Request) -> Response:
        return _json({"ok": True, "requests": manager.recent_requests()})

    async def events(request: Request) -> Response:
        return _sse_response(request, manager)

    api = f"{STUDIO_PATH}/api"
    return [
        Route(STUDIO_PATH, _guarded(index, allowed, explicit), methods=["GET"]),
        Route(f"{STUDIO_PATH}/", _guarded(index, allowed, explicit), methods=["GET"]),
        Route(
            f"{STUDIO_PATH}/assets/{{filename}}", _guarded(asset, allowed, explicit), methods=["GET"]
        ),
        Route(f"{api}/bootstrap", _guarded(bootstrap, allowed, explicit), methods=["GET"]),
        Route(f"{api}/routes", _guarded(routes_endpoint, allowed, explicit), methods=["GET"]),
        Route(f"{api}/action-schema", _guarded(action_schema, allowed, explicit), methods=["GET"]),
        Route(f"{api}/run-loader", _guarded(run_loader, allowed, explicit), methods=["POST"]),
        Route(f"{api}/metrics", _guarded(metrics, allowed, explicit), methods=["GET"]),
        Route(f"{api}/config", _guarded(config_endpoint, allowed, explicit), methods=["GET"]),
        Route(f"{api}/check", _guarded(check, allowed, explicit), methods=["POST"]),
        Route(f"{api}/requests", _guarded(recent_requests, allowed, explicit), methods=["GET"]),
        Route(f"{STUDIO_PATH}/events", _guarded(events, allowed, explicit), methods=["GET"]),
    ]


# ------------------------------------------------------------------ bootstrap


def _bootstrap_payload(settings: "DevServerSettings") -> Dict[str, Any]:
    import pyxle  # noqa: PLC0415 - avoid import cycles at module load

    from pyxle.config import default_csrf_cookie_name  # noqa: PLC0415

    csrf = getattr(settings, "csrf", None)
    csrf_enabled = csrf is not None and bool(getattr(csrf, "enabled", False))
    cookie_name = None
    header_name = None
    if csrf_enabled:
        cookie_name = getattr(csrf, "cookie_name", None) or default_csrf_cookie_name(
            settings.starlette_port
        )
        header_name = getattr(csrf, "header_name", None) or "x-csrf-token"
    return {
        "ok": True,
        "version": pyxle.__version__,
        "studioPath": STUDIO_PATH,
        "project": settings.project_root.name,
        "host": settings.starlette_host,
        "port": settings.starlette_port,
        "vitePort": settings.vite_port,
        "csrf": {
            "enabled": csrf_enabled,
            "cookieName": cookie_name,
            "headerName": header_name,
        },
    }


# --------------------------------------------------------------------- routes


def _edge_max_age(settings: "DevServerSettings", path: str) -> Optional[float]:
    cache_config = getattr(settings, "cache", None)
    if cache_config is None or not getattr(cache_config, "enabled", False):
        return None
    max_age_for = getattr(cache_config, "max_age_for", None)
    if max_age_for is None:
        return None
    return max_age_for(path)


def _routes_payload(settings: "DevServerSettings", routes: "RouteTable") -> Dict[str, Any]:
    """Serialise the route table for the Routes panel.

    Runs in a worker thread (``asyncio.to_thread``): layout resolution reads
    build metadata from disk per page.
    """
    from ..registry import find_layout_loaders  # noqa: PLC0415

    # Key action URLs by (module_key, action_name), not (page_path, …): a
    # catch-all page like ``[[...slug]]`` surfaces as several rows (``/docs``
    # AND ``/docs/{slug:path}``) that share one module, while the concrete
    # action route is registered under the primary path only. Keying by module
    # gives every alternate row the same runnable URL.
    action_urls: Dict[tuple[str, str], str] = {}
    for action in routes.actions:
        if action.is_catchall:
            continue
        action_urls[(action.module_key, action.action_name)] = action.path

    pages: List[Dict[str, Any]] = []
    for page in routes.pages:
        try:
            layouts = [
                {
                    "source": info.relative_path.as_posix(),
                    "loaderName": info.loader_name,
                }
                for info in find_layout_loaders(settings, page.source_relative_path)
            ]
        except Exception:  # noqa: BLE001 — a broken layout never hides the route
            layouts = []
        pages.append(
            {
                "path": page.path,
                "source": page.source_relative_path.as_posix(),
                "sourceAbsolute": str(page.source_absolute_path),
                "loader": (
                    {"name": page.loader_name, "line": page.loader_line}
                    if page.has_loader
                    else None
                ),
                "actions": [
                    {
                        "name": action.get("name"),
                        "line": action.get("line"),
                        "url": action_urls.get((page.module_key, action.get("name"))),
                    }
                    for action in page.actions
                ],
                "websocket": (
                    {"name": page.websocket_name, "line": page.websocket_line}
                    if page.has_websocket
                    else None
                ),
                "cache": {
                    "revalidate": page.cache_revalidate,
                    "edgeMaxAge": _edge_max_age(settings, page.path),
                },
                "usesSuspense": page.uses_suspense,
                "headDynamic": page.head_is_dynamic,
                "layouts": layouts,
                "boundaries": {
                    "loading": (
                        page.loading_boundary.source_relative_path.as_posix()
                        if page.loading_boundary is not None
                        else None
                    ),
                    "error": (
                        page.error_boundary.source_relative_path.as_posix()
                        if page.error_boundary is not None
                        else None
                    ),
                },
            }
        )

    apis = [
        {
            "path": api.path,
            "source": api.source_relative_path.as_posix(),
            "sourceAbsolute": str(api.source_absolute_path),
        }
        for api in routes.apis
    ]
    return {"ok": True, "pages": pages, "apis": apis}


# -------------------------------------------------------------- action schema


def _action_schema_payload(
    settings: "DevServerSettings",
    routes: "RouteTable",
    page_path: str,
    action_name: str,
) -> Dict[str, Any]:
    """Resolve one action's Pydantic body schema for the tester's form.

    Imports the page's compiled server module with ``debug=settings.debug`` so
    a hot rebuild is always respected (unlike the OpenAPI builder, which is a
    CLI-time tool and pins ``debug=False``).
    """
    # Resolve the page first, then match the action by module — the requested
    # path may be an alternate (e.g. ``/docs/{slug:path}``) of the page whose
    # concrete action route is registered under the primary path (``/docs``).
    page = _find_page(routes, page_path)
    target = None
    if page is not None:
        for action in routes.actions:
            if action.is_catchall:
                continue
            if (
                action.module_key == page.module_key
                and action.action_name == action_name
            ):
                target = action
                break
    if target is None:
        return {"ok": False, "error": f"No action {action_name!r} on page {page_path!r}."}

    from ..starlette_app import _import_module  # noqa: PLC0415 - lazy, avoids cycle
    from ..validation import PydanticNotInstalledError, resolve_body_model  # noqa: PLC0415

    try:
        module = _import_module(
            target.module_key, target.server_module_path, debug=settings.debug
        )
        action_fn = getattr(module, target.action_name, None)
        if action_fn is None or not getattr(action_fn, "__pyxle_action__", False):
            return {
                "ok": False,
                "error": f"Action {action_name!r} is not exported by its module.",
            }
        resolved = resolve_body_model(action_fn)
    except PydanticNotInstalledError as exc:
        return {"ok": True, "url": target.path, "schema": None, "note": str(exc)}
    except Exception as exc:  # noqa: BLE001 — importing user code can fail arbitrarily
        return {"ok": False, "error": _redact_error(f"{type(exc).__name__}: {exc}")}

    schema = None
    if resolved is not None:
        schema = resolved.model.model_json_schema()
    return {"ok": True, "url": target.path, "schema": schema, "note": None}


# ----------------------------------------------------------------- run loader


def _find_page(routes: "RouteTable", path: str) -> "Optional[PageRoute]":
    for page in routes.pages:
        if page.path == path:
            return page
    return None


def _substitute_path_params(pattern: str, params: Dict[str, Any]) -> str:
    """Fill ``{param}`` / ``{param:path}`` placeholders with tester values."""

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        return str(params.get(name, match.group(0)))

    return re.sub(r"\{([A-Za-z_][A-Za-z0-9_]*)(?::[a-z]+)?\}", replace, pattern)


def _synthesize_loader_request(
    request: Request,
    *,
    settings: "DevServerSettings",
    path: str,
    params: Dict[str, Any],
    query: Dict[str, Any],
) -> Request:
    """A standalone GET request for an in-process loader run.

    Deliberately minimal and non-derived: no cookies, no authorization, no
    caller headers — the tester exercises the loader, not the session. The
    live ``app`` is threaded through the scope so loader metrics record.
    """
    query_string = urlencode({str(k): str(v) for k, v in query.items()})
    scope: Dict[str, Any] = {
        "type": "http",
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("utf-8"),
        "root_path": "",
        "query_string": query_string.encode("utf-8"),
        "headers": [
            (b"host", f"{settings.starlette_host}:{settings.starlette_port}".encode()),
            (b"accept", b"application/json"),
        ],
        "server": (settings.starlette_host, settings.starlette_port),
        "client": ("127.0.0.1", 0),
        "path_params": dict(params),
        "state": {},
        "app": request.app,
        "pyxle": {"studio": True},
    }

    async def _receive() -> Dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    return Request(scope, _receive)


async def _run_loader_endpoint(
    request: Request,
    *,
    settings: "DevServerSettings",
    routes: "RouteTable",
) -> Response:
    content_type = request.headers.get("content-type", "")
    if not content_type.lower().startswith("application/json"):
        return _json(
            {"ok": False, "error": "Content-Type must be application/json."},
            status_code=415,
        )
    try:
        body = json.loads(await request.body() or b"{}")
    except json.JSONDecodeError:
        return _json({"ok": False, "error": "Request body is not valid JSON."}, status_code=400)
    if not isinstance(body, dict):
        return _json({"ok": False, "error": "Request body must be a JSON object."}, status_code=400)

    path = body.get("path")
    params = body.get("params") or {}
    query = body.get("query") or {}
    if not isinstance(path, str) or not path.startswith("/"):
        return _json({"ok": False, "error": "'path' must be a route path string."}, status_code=400)
    if not isinstance(params, dict) or not isinstance(query, dict):
        return _json(
            {"ok": False, "error": "'params' and 'query' must be JSON objects."},
            status_code=400,
        )

    page = _find_page(routes, path)
    if page is None:
        return _json({"ok": False, "error": f"No page route at {path!r}."}, status_code=404)
    if not page.has_loader:
        return _json(
            {"ok": True, "data": None, "note": "This page has no @server loader."},
        )

    from pyxle.runtime import LoaderError  # noqa: PLC0415 - zero-dep module
    from pyxle.ssr.view import (  # noqa: PLC0415 - lazy, avoids cycle
        LoaderCrashError,
        run_page_loader,
    )

    concrete_path = _substitute_path_params(path, params)
    synthetic = _synthesize_loader_request(
        request, settings=settings, path=concrete_path, params=params, query=query
    )
    started = time.perf_counter()
    try:
        data = await asyncio.wait_for(
            run_page_loader(request=synthetic, settings=settings, page=page),
            timeout=_LOADER_TIMEOUT_S,
        )
    except asyncio.TimeoutError:
        return _json(
            {
                "ok": False,
                "kind": "timeout",
                "error": f"Loader did not finish within {_LOADER_TIMEOUT_S:.0f}s.",
            },
            status_code=200,
        )
    except LoaderError as exc:
        return _json(
            {
                "ok": False,
                "kind": "loader_error",
                "status": exc.status_code,
                "error": _redact_error(str(exc)),
                "durationMs": round((time.perf_counter() - started) * 1000.0, 2),
            },
            status_code=200,
        )
    except Exception as exc:  # noqa: BLE001 — user loader code can raise anything
        # The page pipeline classifies an exception escaping a loader body as
        # LoaderCrashError so it can route to error.pyxl; the tester wants the
        # developer's own exception, so report the cause it wrapped.
        reported = exc.__cause__ if isinstance(exc, LoaderCrashError) else exc
        return _json(
            {
                "ok": False,
                "kind": "exception",
                "error": _redact_error(f"{type(reported).__name__}: {reported}"),
                "durationMs": round((time.perf_counter() - started) * 1000.0, 2),
            },
            status_code=200,
        )

    duration_ms = round((time.perf_counter() - started) * 1000.0, 2)
    try:
        # allow_nan=False so non-finite floats (inf/nan) trip the graceful
        # error path here, matching Starlette's strict JSONResponse encoder —
        # otherwise they would slip through and 500 at response render.
        json.dumps(data, allow_nan=False)
    except (TypeError, ValueError):
        return _json(
            {
                "ok": False,
                "kind": "exception",
                "error": "Loader returned data that is not JSON-serialisable.",
                "durationMs": duration_ms,
            },
            status_code=200,
        )
    return _json({"ok": True, "data": data, "durationMs": duration_ms})


# -------------------------------------------------------------------- metrics


def _metrics_payload(request: Request) -> Dict[str, Any]:
    registry = getattr(request.app.state, "pyxle_metrics", None)
    started_at = getattr(request.app.state, "pyxle_started_at", None)
    if registry is None:
        return {"ok": False, "error": "Metrics registry unavailable."}
    payload: Dict[str, Any] = {"ok": True, "snapshot": registry.snapshot()}
    if isinstance(started_at, (int, float)):
        payload["uptimeSeconds"] = max(0.0, time.time() - float(started_at))
    payload["buckets"] = {
        # The final Prometheus-style bucket bound is +inf, which JSON cannot
        # carry — it becomes null and the UI renders it as "+∞".
        name: [
            [None if bound == float("inf") else bound, count]
            for bound, count in histogram.cumulative_buckets()
        ]
        for name, histogram in (
            ("request", registry.request_duration),
            ("render", registry.render_duration),
            ("loader", registry.loader_duration),
            ("action", registry.action_duration),
        )
    }
    return payload


# --------------------------------------------------------------------- config


def _redact_error(message: str) -> str:
    from .._security import redact_sensitive_patterns  # noqa: PLC0415

    return redact_sensitive_patterns(message)


def _redact_value(key: Optional[str], value: Any) -> Any:
    """Recursively mask secrets in a config payload — by key AND by value.

    A value is masked when its *key* looks secret (``api_key``, ``token``, …)
    or when the value itself looks like a credential (a ``postgres://…`` DSN, a
    bearer token) — the latter catches secrets stored under an innocent key such
    as a plugin's ``url``, which key-name matching alone would leak.
    """
    if isinstance(value, dict):
        return {str(k): _redact_value(str(k), v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact_value(key, item) for item in value]
    if (
        key is not None
        and value is not None
        and not isinstance(value, bool)
        and _SECRET_KEY_PATTERN.search(key)
    ):
        return _REDACTED
    if isinstance(value, str):
        redacted = _redact_error(value)
        if redacted != value:
            return redacted
    return value


def _block_dict(config: Any) -> Any:
    if config is None:
        return None
    if dataclasses.is_dataclass(config) and not isinstance(config, type):
        return dataclasses.asdict(config)
    return config


def _config_payload(settings: "DevServerSettings") -> Dict[str, Any]:
    blocks = {
        name: _block_dict(getattr(settings, name, None))
        for name in (
            "cors",
            "csrf",
            "cache",
            "navigation",
            "rate_limit",
            "observability",
            "llms",
            "studio",
        )
    }
    payload = {
        "ok": True,
        "settings": settings.to_dict(),
        "blocks": blocks,
        "plugins": list(getattr(settings, "plugins", ()) or ()),
    }
    return _redact_value(None, payload)


# ---------------------------------------------------------------------- check


def _check_payload(settings: "DevServerSettings") -> Dict[str, Any]:
    """``pyxle check``-parity diagnostics over every ``.pyxl`` page.

    Runs in a worker thread: parsing is CPU + subprocess work (JSX validation
    shells out to Node). When Node is unavailable, JSX validation is skipped
    and the payload says so rather than failing the whole check.
    """
    import shutil  # noqa: PLC0415

    from pyxle.compiler.parser import PyxParser  # noqa: PLC0415

    started = time.perf_counter()
    validate_jsx = shutil.which("node") is not None
    parser = PyxParser()
    diagnostics: List[Dict[str, Any]] = []
    files_checked = 0

    pages_dir = settings.pages_dir
    if pages_dir.is_dir():
        for pyxl_file in sorted(pages_dir.rglob("*.pyxl")):
            files_checked += 1
            rel_path = pyxl_file.relative_to(settings.project_root).as_posix()
            try:
                result = parser.parse(
                    pyxl_file,
                    tolerant=True,
                    validate_jsx=validate_jsx,
                    validate_semantics=True,
                )
            except Exception as exc:  # noqa: BLE001 — parser bugs must not abort the run
                diagnostics.append(
                    {
                        "file": rel_path,
                        "fileAbsolute": str(pyxl_file),
                        "section": "python",
                        "severity": "error",
                        "message": _redact_error(
                            f"parser crashed: {type(exc).__name__}: {exc}"
                        ),
                        "line": None,
                        "column": None,
                    }
                )
                continue
            for diag in result.diagnostics:
                diagnostics.append(
                    {
                        "file": rel_path,
                        "fileAbsolute": str(pyxl_file),
                        "section": diag.section,
                        "severity": diag.severity,
                        "message": diag.message,
                        "line": diag.line,
                        "column": diag.column,
                    }
                )

    return {
        "ok": True,
        "filesChecked": files_checked,
        "jsxValidated": validate_jsx,
        "diagnostics": diagnostics,
        "durationMs": round((time.perf_counter() - started) * 1000.0, 2),
    }


# ------------------------------------------------------------------------ SSE


def _sse_response(request: Request, manager: StudioManager) -> StreamingResponse:
    """The ``/__pyxle/studio/events`` Server-Sent-Events stream.

    Emits ``request`` and ``rebuild`` events as they happen, with periodic
    comment pings as keep-alives. ``EventSource`` reconnects automatically,
    so a dev-server restart self-heals in the UI.
    """
    queue = manager.subscribe()

    async def stream() -> AsyncIterator[str]:
        try:
            yield "retry: 3000\n\n"
            while True:
                try:
                    event = await asyncio.wait_for(
                        queue.get(), timeout=_SSE_PING_INTERVAL_S
                    )
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
                    continue
                yield f"data: {json.dumps(event)}\n\n"
        finally:
            manager.unsubscribe(queue)

    response = StreamingResponse(stream(), media_type="text/event-stream")
    response.headers["Cache-Control"] = _NO_STORE
    response.headers["X-Accel-Buffering"] = "no"
    return response


__all__ = ["build_studio_routes"]
