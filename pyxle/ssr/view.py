"""Utilities for building SSR responses from compiled page routes."""

from __future__ import annotations

import importlib
import importlib.util
import inspect
import logging
import re
import secrets
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, List, Optional, Tuple

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response, StreamingResponse

from pyxle.devserver.error_pages import ErrorBoundaryRegistry
from pyxle.devserver.overlay import OverlayManager
from pyxle.devserver.routes import PageRoute
from pyxle.devserver.settings import DevServerSettings

from .renderer import (
    BrowserGlobalRenderError,
    CjsDependencyRenderError,
    ComponentRenderer,
    ComponentRenderError,
    InlineStyleFragment,
    detect_browser_only_global,
    detect_dynamic_require,
)
from .template import (
    _AUTH_SEED_ABSENT,
    ManifestLookupError,
    build_document_shell,
    render_document,
    render_error_document,
    render_head_markup,
)


class LoaderExecutionError(RuntimeError):
    """Raised when a page loader returns an unexpected value."""


#: The exact ``AttributeError`` message Starlette's ``State`` raises when
#: server code reads an attribute nothing populated (``request.state.db``
#: without the pyxle-db plugin, ``request.state.user`` without an auth
#: middleware, ...). Anchored so only genuine ``State`` misses match.
_STATE_ATTRIBUTE_MESSAGE_RE = re.compile(
    r"^'State' object has no attribute '(?P<name>[^']+)'$"
)


def missing_state_attribute(error: BaseException) -> str | None:
    """Return the missing ``request.state`` attribute name for ``error``.

    Matches exactly the ``AttributeError`` Starlette's ``State`` raises when a
    loader or action reads a ``request.state`` attribute that no plugin or
    middleware provided. Any other exception — including other
    ``AttributeError``\\ s — returns ``None`` so it flows through the normal
    error path untouched.
    """
    if not isinstance(error, AttributeError):
        return None
    match = _STATE_ATTRIBUTE_MESSAGE_RE.match(str(error))
    return match.group("name") if match else None


def _missing_state_message(attribute: str) -> str:
    if attribute == "db":
        return (
            "request.state.db is not set — it is provided by the pyxle-db "
            'plugin. Add it to pyxle.config.json ("plugins": ["pyxle-db"]) '
            "and restart the dev server."
        )
    return (
        f"request.state.{attribute} is not set — state attributes are "
        "provided by plugins or middleware (for example, request.state.db "
        'requires the pyxle-db plugin: "plugins": ["pyxle-db"] in '
        "pyxle.config.json). Check your plugin/middleware configuration."
    )


class MissingRequestStateError(LoaderExecutionError):
    """Raised when server code reads an unset ``request.state`` attribute.

    Wraps the bare ``AttributeError: 'State' object has no attribute '<name>'``
    Starlette raises in that case with actionable guidance (state attributes
    are provided by plugins or middleware; ``request.state.db`` needs the
    pyxle-db plugin). Subclasses :class:`LoaderExecutionError` so every loader
    pipeline (buffered, streaming, navigation) reports it as a loader-stage
    failure: guidance in the dev overlay and server log, the generic sanitized
    response in production. The original ``AttributeError`` stays reachable
    through ``__cause__``. Also used by the ``@action`` dispatcher for the
    same diagnosis on action requests.
    """

    def __init__(self, attribute: str) -> None:
        super().__init__(_missing_state_message(attribute))
        self.attribute = attribute


class HeadEvaluationError(RuntimeError):
    """Raised when HEAD cannot be resolved at runtime."""


#: Response header carrying a render's cache lifetime (seconds) from the page
#: handler's perspective. Set by :func:`build_page_response` when a loader
#: declared a ``{data, revalidate}`` envelope; read and stripped by the page
#: handler when it decides what TTL to store the rendered HTML under.
REVALIDATE_HEADER = "x-pyxle-revalidate"


@dataclass(slots=True)
class PageArtifacts:
    component_props: dict[str, Any]
    body_html: str
    head_elements: tuple[str, ...]
    head_markup: str
    inline_styles: tuple[InlineStyleFragment, ...]
    status_code: int
    revalidate: float | None = None


@dataclass(slots=True)
class _StreamingPrelude:
    """Everything a render needs that is known *before* the component renders.

    Produced by running the loader and resolving the (static) HEAD, so the
    streaming path can flush the document head before the React shell renders.
    The buffered path reuses it and then renders + merges runtime ``<Head>``.
    """

    component_props: dict[str, Any]
    head_elements: tuple[str, ...]
    layout_head_jsx_blocks: tuple[str, ...]
    status_code: int
    revalidate: float | None
    csrf_token: str | None


_logger = logging.getLogger(__name__)


def _log_render_failure(
    page: PageRoute, *, stage: str, error: BaseException, status_code: int
) -> None:
    """Record a server-side log line for a failed page render.

    Production error responses are deliberately sanitized -- they expose no
    exception detail to the client (CLAUDE.md rule 18) -- so this log is the
    only record of what actually failed. Only genuine server faults
    (``status_code >= 500``) are logged at error level; an intentional sub-500
    signal (e.g. a loader raising a 404) is not a server error and stays quiet.
    """
    if status_code < 500:
        return
    _logger.error(
        "SSR %s error while rendering route %s: %s",
        stage,
        page.path,
        error,
        exc_info=error,
    )


def _enrich_render_error(exc: ComponentRenderError, page: PageRoute) -> ComponentRenderError:
    """Upgrade a browser-global ``ReferenceError`` into an actionable error.

    A component that evaluates a browser global (``window``, ``document``, …)
    at render scope fails SSR with a bare ``"window is not defined"`` whose
    traceback is entirely framework frames — it names no user file and hints
    at no fix. When the render error matches that shape, return a
    :class:`~pyxle.ssr.renderer.BrowserGlobalRenderError` that explains the
    server/browser split, names the page's ``.pyxl`` source file, and points
    at the remedy. Any other render error is returned unchanged so unrelated
    failures flow through exactly as before.

    The enriched message is shown in development (error overlay, dev error
    page) and written to the server log; production responses are already
    sanitized to a generic document, so no file path or internals reach the
    HTTP body (CLAUDE.md rule 18).
    """
    if isinstance(exc, (BrowserGlobalRenderError, CjsDependencyRenderError)):
        return exc
    source_file = f"pages/{page.source_relative_path.as_posix()}"
    global_name = detect_browser_only_global(str(exc))
    if global_name is not None:
        enriched: ComponentRenderError = BrowserGlobalRenderError(
            global_name=global_name,
            source_file=source_file,
            original_message=str(exc),
        )
        enriched.__cause__ = exc
        return enriched
    module_name = detect_dynamic_require(str(exc))
    if module_name is not None:
        enriched = CjsDependencyRenderError(
            module_name=module_name,
            source_file=source_file,
            original_message=str(exc),
        )
        # Preserve the original exception as the cause so log tracebacks keep the
        # raw Node runtime failure alongside the enriched explanation.
        enriched.__cause__ = exc
        return enriched
    return exc


def _error_response(
    *,
    settings: DevServerSettings,
    page: PageRoute,
    stage: str,
    error: BaseException,
    status_code: int,
) -> HTMLResponse:
    """Log the failure server-side, then return the sanitized HTML error page.

    Centralizes the "log, then render the fallback document" pair so every
    error branch in :func:`build_page_response` behaves consistently and the
    production response never leaks internals.
    """
    _log_render_failure(page, stage=stage, error=error, status_code=status_code)
    fallback = render_error_document(settings=settings, page=page, error=error)
    return HTMLResponse(fallback, status_code=status_code)


def _resolve_nav_cache_ttl(
    settings: DevServerSettings, path: str, page: PageRoute | None = None
) -> int | None:
    """Return the client navigation-cache lifetime (seconds) for ``path``.

    The client navigation cache reuses this per-page value so a page's
    prefetch/seed freshness matches the server's actual cacheability. Resolved in
    priority order:

    1. An explicit edge ``cache`` config entry for the path → its max-age (the
       page is shared-cacheable for that long, so the client may reuse it too).
    2. A page ``CACHE`` directive (``cache_revalidate``) → that lifetime.
    3. A page with a ``@server`` loader but **no** declared cache lifetime is
       dynamic — it renders ``private, no-cache`` and its data can change between
       requests (live updates, per-user content). Return ``0`` ("never cache") so
       a mutation is visible immediately on back/forward navigation instead of
       being hidden behind the default window until it expires.
    4. Otherwise (a static, loader-less page whose markup never varies) → ``None``
       → the client's default navigation-cache lifetime.

    Pages that want client navigation caching can opt in explicitly with a
    ``cache`` config entry; the safe default is to stay fresh.
    """
    cache = getattr(settings, "cache", None)
    config_ttl = cache.max_age_for(path) if cache is not None else None
    if config_ttl is not None:
        return config_ttl
    if page is not None:
        if page.cache_revalidate is not None:
            return page.cache_revalidate
        if page.has_loader:
            return 0
    return None


def _attach_revalidate(response: Response, revalidate: float | None) -> None:
    """Stamp a render's cache lifetime onto the response for the page handler.

    The page handler reads :data:`REVALIDATE_HEADER` to decide the TTL under
    which it stores the rendered HTML, then strips it before the response
    reaches the client.
    """
    if revalidate is not None:
        response.headers[REVALIDATE_HEADER] = f"{revalidate:g}"


async def build_page_response(
    *,
    request: Request,
    settings: DevServerSettings,
    page: PageRoute,
    renderer: ComponentRenderer,
    overlay: OverlayManager | None = None,
    error_boundaries: ErrorBoundaryRegistry | None = None,
    suppress_per_user: bool = False,
) -> Response:
    if settings.debug:
        _purge_page_modules(settings.pages_dir)
    loader_breadcrumb = _initial_loader_breadcrumb(page)

    try:
        artifacts = await _create_page_artifacts(
            request=request,
            settings=settings,
            page=page,
            renderer=renderer,
            loader_breadcrumb=loader_breadcrumb,
            suppress_per_user=suppress_per_user,
        )
        script_nonce = secrets.token_urlsafe(24)
        nav_cache_ttl = _resolve_nav_cache_ttl(settings, request.url.path, page=page)
        try:
            shell = build_document_shell(
                settings=settings,
                page=page,
                props=artifacts.component_props,
                script_nonce=script_nonce,
                head_elements=artifacts.head_elements,
                inline_styles=artifacts.inline_styles,
                nav_cache_ttl=nav_cache_ttl,
                auth_seed=_auth_seed_for_request(request),
            )
        except ManifestLookupError:
            document = render_document(
                settings=settings,
                page=page,
                body_html=artifacts.body_html,
                props=artifacts.component_props,
                script_nonce=script_nonce,
                head_elements=artifacts.head_elements,
                inline_styles=artifacts.inline_styles,
                nav_cache_ttl=nav_cache_ttl,
                auth_seed=_auth_seed_for_request(request),
            )
            if overlay is not None:
                await overlay.notify_clear(route_path=page.path)
            fallback_response: Response = HTMLResponse(
                document, status_code=artifacts.status_code
            )
            _attach_revalidate(fallback_response, artifacts.revalidate)
            return fallback_response

        async def _document_stream():
            yield shell.prefix.encode("utf-8")
            yield artifacts.body_html.encode("utf-8")
            yield shell.suffix.encode("utf-8")

        if overlay is not None:
            await overlay.notify_clear(route_path=page.path)
        streamed_response: Response = StreamingResponse(
            _document_stream(),
            status_code=artifacts.status_code,
            media_type="text/html",
        )
        _attach_revalidate(streamed_response, artifacts.revalidate)
        return streamed_response
    except Exception as exc:
        return await _handle_render_exception(
            exc,
            request=request,
            settings=settings,
            page=page,
            renderer=renderer,
            error_boundaries=error_boundaries,
            overlay=overlay,
            loader_breadcrumb=loader_breadcrumb,
        )


async def render_page_body_html(
    *,
    request: Request,
    settings: DevServerSettings,
    page: PageRoute,
    renderer: ComponentRenderer,
    suppress_per_user: bool = False,
) -> tuple[str, int]:
    """Render a page and return ``(body_html, status_code)``.

    Runs the same loader + SSR path as :func:`build_page_response` but returns
    the rendered component HTML *without* the surrounding document shell, so
    callers (such as the ``.md`` markdown resolver) can post-process the
    content. Propagates the same render-stage exceptions as the page pipeline.
    """
    breadcrumb = _initial_loader_breadcrumb(page)
    artifacts = await _create_page_artifacts(
        request=request,
        settings=settings,
        page=page,
        renderer=renderer,
        loader_breadcrumb=breadcrumb,
        suppress_per_user=suppress_per_user,
    )
    return artifacts.body_html, artifacts.status_code


async def run_page_loader(
    *,
    request: Request,
    settings: DevServerSettings,
    page: PageRoute,
) -> Any:
    """Run a page's ``@server`` loader and return its data — no SSR render.

    A lighter-weight counterpart to :func:`render_page_body_html` for callers
    (such as the ``.md`` markdown resolver) that only need the loader's return
    value. Returns the loader's data dict; a page with no loader returns ``{}``.
    Propagates loader exceptions.
    """
    if settings.debug:
        _purge_page_modules(settings.pages_dir)
    payload, _status, _revalidate, _module = await _execute_loader(
        page, request, module=None, debug=settings.debug
    )
    return payload


async def _handle_render_exception(
    exc: BaseException,
    *,
    request: Request,
    settings: DevServerSettings,
    page: PageRoute,
    renderer: ComponentRenderer,
    error_boundaries: ErrorBoundaryRegistry | None,
    overlay: OverlayManager | None,
    loader_breadcrumb: dict[str, str],
) -> Response:
    """Map a render-pipeline exception to an error-boundary or sanitized page.

    Shared by the buffered and streaming page builders so both honour the same
    error-boundary contract. Known render-stage exceptions try the nearest
    ``error.pyxl`` first; any unexpected fault returns the sanitized fallback
    without exposing internals.
    """
    from pyxle.runtime import LoaderError

    if isinstance(exc, LoaderError):
        stage, status_code = "loader", exc.status_code
        loader_breadcrumb = _make_loader_breadcrumb(page, status="failed", detail=str(exc))
    elif isinstance(exc, LoaderExecutionError):
        stage, status_code = "loader", 500
        loader_breadcrumb = _make_loader_breadcrumb(page, status="failed", detail=str(exc))
    elif isinstance(exc, HeadEvaluationError):
        stage, status_code = "server", 500
    elif isinstance(exc, ComponentRenderError):
        exc = _enrich_render_error(exc, page)
        stage, status_code = "renderer", 500
    else:  # pragma: no cover - defensive guardrail for unexpected faults
        if overlay is not None:
            await overlay.notify_error(
                route_path=page.path,
                error=exc,
                breadcrumbs=_compose_breadcrumbs(loader_breadcrumb, stage="server", message=str(exc)),
            )
        return _error_response(
            settings=settings, page=page, stage="server", error=exc, status_code=500,
        )

    if overlay is not None:
        await overlay.notify_error(
            route_path=page.path,
            error=exc,
            breadcrumbs=_compose_breadcrumbs(loader_breadcrumb, stage=stage, message=str(exc)),
        )
    boundary_response = await _try_error_boundary(
        request=request,
        settings=settings,
        renderer=renderer,
        error_boundaries=error_boundaries,
        route_path=page.path,
        error=exc,
        status_code=status_code,
    )
    if boundary_response is not None:
        return boundary_response
    return _error_response(
        settings=settings, page=page, stage=stage, error=exc, status_code=status_code,
    )


async def _first_stream_frame(frames) -> dict[str, Any]:
    """Pull the first frame from a render stream.

    An empty stream is treated as a terminal ``end`` so the caller emits an
    empty (but valid) document rather than hanging.
    """
    try:
        return await frames.__anext__()
    except StopAsyncIteration:  # pragma: no cover - a live worker always emits
        return {"type": "end"}


async def build_streaming_page_response(
    *,
    request: Request,
    settings: DevServerSettings,
    page: PageRoute,
    renderer: ComponentRenderer,
    stream_render: Callable[..., Any],
    overlay: OverlayManager | None = None,
    error_boundaries: ErrorBoundaryRegistry | None = None,
    suppress_per_user: bool = False,
) -> Response:
    """Render *page* as a streamed HTML response via ``renderToPipeableStream``.

    Used for pages that opt into streaming (a ``<Suspense>`` boundary or a
    ``loading.pyxl``). The document head is flushed from the static HEAD before
    the React shell renders; the shell and any Suspense boundaries stream in;
    the hydration scripts come last. A shell-level failure (an error before the
    first byte) falls back to the error boundary exactly like the buffered path
    — no partial document is ever emitted in that case.

    ``stream_render`` is the worker pool's ``render_stream`` async generator
    callable. ``renderer`` is still used for the error-boundary fallback render.
    """
    if settings.debug:
        _purge_page_modules(settings.pages_dir)
    loader_breadcrumb = _initial_loader_breadcrumb(page)

    try:
        from pyxle.ssr.head_merger import merge_head_elements

        prelude = await _create_streaming_prelude(
            request=request,
            settings=settings,
            page=page,
            loader_breadcrumb=loader_breadcrumb,
            suppress_per_user=suppress_per_user,
        )
        # Streaming flushes the head before the component renders, so only the
        # static HEAD (the HEAD variable + JSX/layout <Head> blocks) can appear.
        # Runtime <Head> registered during render arrives too late and is
        # intentionally omitted — a documented streaming limitation.
        static_head = merge_head_elements(
            head_variable=prelude.head_elements,
            head_jsx_blocks=page.head_jsx_blocks,
            layout_head_jsx_blocks=prelude.layout_head_jsx_blocks,
            runtime_head_blocks=(),
        )
        script_nonce = secrets.token_urlsafe(24)
        nav_cache_ttl = _resolve_nav_cache_ttl(settings, request.url.path, page=page)
        try:
            shell = build_document_shell(
                settings=settings,
                page=page,
                props=prelude.component_props,
                script_nonce=script_nonce,
                head_elements=static_head,
                inline_styles=(),
                nav_cache_ttl=nav_cache_ttl,
                auth_seed=_auth_seed_for_request(request),
            )
        except ManifestLookupError:
            # No client manifest to link the hydration bundle — fall back to the
            # buffered path, which has its own dev-mode document assembly.
            return await build_page_response(
                request=request,
                settings=settings,
                page=page,
                renderer=renderer,
                overlay=overlay,
                error_boundaries=error_boundaries,
                suppress_per_user=suppress_per_user,
            )

        # Await the first frame *before* sending any bytes so a shell error maps
        # to the error boundary instead of a half-written document.
        frames = stream_render(
            page.client_module_path,
            prelude.component_props,
            request_pathname=request.url.path,
            csrf_token=prelude.csrf_token,
            fallback_path=(
                page.loading_boundary.client_module_path
                if page.loading_boundary is not None
                else None
            ),
        )
        first_frame = await _first_stream_frame(frames)
        if first_frame.get("type") == "error":
            await frames.aclose()
            raise ComponentRenderError(
                first_frame.get("error")
                or "Streaming render failed before the first byte"
            )

        if overlay is not None:
            await overlay.notify_clear(route_path=page.path)

        async def _document_stream():
            yield shell.prefix.encode("utf-8")
            if first_frame.get("type") == "chunk":
                yield first_frame["html"].encode("utf-8")
            async for frame in frames:
                if frame.get("type") == "chunk":
                    yield frame["html"].encode("utf-8")
                else:
                    # Terminal end/error frame: the body is complete (React has
                    # already streamed any Suspense fallbacks on a mid-stream
                    # error). Stop reading the body.
                    break
            yield shell.suffix.encode("utf-8")

        response: Response = StreamingResponse(
            _document_stream(),
            status_code=prelude.status_code,
            media_type="text/html",
        )
        _attach_revalidate(response, prelude.revalidate)
        return response
    except Exception as exc:
        return await _handle_render_exception(
            exc,
            request=request,
            settings=settings,
            page=page,
            renderer=renderer,
            error_boundaries=error_boundaries,
            overlay=overlay,
            loader_breadcrumb=loader_breadcrumb,
        )


async def build_page_navigation_response(
    *,
    request: Request,
    settings: DevServerSettings,
    page: PageRoute,
    renderer: ComponentRenderer,
    overlay: OverlayManager | None = None,
    error_boundaries: ErrorBoundaryRegistry | None = None,
) -> JSONResponse:
    from pyxle.runtime import LoaderError

    if settings.debug:
        _purge_page_modules(settings.pages_dir)
    loader_breadcrumb = _initial_loader_breadcrumb(page)

    try:
        if page.uses_suspense or page.loading_boundary is not None:
            # A streaming-eligible page can't be rendered buffered to extract its
            # head — renderToString throws on a suspending component — and its
            # runtime <Head> wouldn't reach the flushed head anyway. Build the nav
            # payload from the loader + static head only, mirroring the initial
            # streamed load. The client renders the body itself (wrapping in the
            # loading boundary via loadingAssetPath), so no server body render is
            # needed here.
            from pyxle.ssr.head_merger import merge_head_elements

            prelude = await _create_streaming_prelude(
                request=request,
                settings=settings,
                page=page,
                loader_breadcrumb=loader_breadcrumb,
                suppress_per_user=False,
            )
            static_head = merge_head_elements(
                head_variable=prelude.head_elements,
                head_jsx_blocks=page.head_jsx_blocks,
                layout_head_jsx_blocks=prelude.layout_head_jsx_blocks,
                runtime_head_blocks=(),
            )
            nav_status_code = prelude.status_code
            nav_component_props = prelude.component_props
            nav_head_markup = render_head_markup(static_head)
        else:
            artifacts = await _create_page_artifacts(
                request=request,
                settings=settings,
                page=page,
                renderer=renderer,
                loader_breadcrumb=loader_breadcrumb,
            )
            nav_status_code = artifacts.status_code
            nav_component_props = artifacts.component_props
            nav_head_markup = artifacts.head_markup

        if overlay is not None:
            await overlay.notify_clear(route_path=page.path)
        payload = {
            "ok": True,
            "routePath": page.path,
            "requestedPath": request.url.path,
            "statusCode": nav_status_code,
            "page": {
                "clientAssetPath": page.client_asset_path,
                "moduleKey": page.module_key,
                # The nearest loading.pyxl's client asset (or None). Carried
                # per-route so a client-side navigation wraps the target page in
                # the same loading boundary the server would — no stale global.
                "loadingAssetPath": (
                    page.loading_boundary.client_asset_path
                    if page.loading_boundary is not None
                    else None
                ),
                # The nearest error.pyxl's client asset (or None). Carried
                # per-route so a client-side render fault on the navigated-to
                # page renders the same error.pyxl the server would — the
                # client error boundary's fallback, kept in lockstep per route.
                "errorAssetPath": (
                    page.error_boundary.client_asset_path
                    if page.error_boundary is not None
                    else None
                ),
            },
            "props": nav_component_props,
            "headMarkup": nav_head_markup,
            # Per-page client navigation-cache lifetime (seconds). Mirrors the
            # page's edge-cache TTL so prefetched data stays fresh exactly as
            # long as the CDN would serve it; ``None`` → client default.
            "navCacheTtlSeconds": _resolve_nav_cache_ttl(settings, request.url.path, page=page),
        }
        return JSONResponse(payload, status_code=nav_status_code)
    except LoaderError as exc:
        loader_breadcrumb = _make_loader_breadcrumb(page, status="failed", detail=str(exc))
        return await _navigation_error_response(
            request=request,
            settings=settings,
            page=page,
            overlay=overlay,
            loader_breadcrumb=loader_breadcrumb,
            stage="loader",
            error=exc,
            status_code=exc.status_code,
        )
    except LoaderExecutionError as exc:
        loader_breadcrumb = _make_loader_breadcrumb(page, status="failed", detail=str(exc))
        return await _navigation_error_response(
            request=request,
            settings=settings,
            page=page,
            overlay=overlay,
            loader_breadcrumb=loader_breadcrumb,
            stage="loader",
            error=exc,
        )
    except HeadEvaluationError as exc:
        return await _navigation_error_response(
            request=request,
            settings=settings,
            page=page,
            overlay=overlay,
            loader_breadcrumb=loader_breadcrumb,
            stage="server",
            error=exc,
        )
    except ComponentRenderError as exc:
        return await _navigation_error_response(
            request=request,
            settings=settings,
            page=page,
            overlay=overlay,
            loader_breadcrumb=loader_breadcrumb,
            stage="renderer",
            error=_enrich_render_error(exc, page),
        )
    except Exception as exc:  # pragma: no cover - defensive guardrail
        return await _navigation_error_response(
            request=request,
            settings=settings,
            page=page,
            overlay=overlay,
            loader_breadcrumb=loader_breadcrumb,
            stage="server",
            error=exc,
        )


async def build_not_found_response(
    *,
    request: Request,
    settings: DevServerSettings,
    renderer: ComponentRenderer,
    error_boundaries: ErrorBoundaryRegistry | None = None,
    overlay: OverlayManager | None = None,
) -> Optional[Response]:
    """Render the nearest ``not-found.pyxl`` for the requested path.

    Returns ``None`` if no not-found boundary exists (caller should fall back
    to the default 404 response).
    """
    if error_boundaries is None:
        return None

    route_path = request.url.path
    boundary_page = error_boundaries.find_not_found_boundary(route_path)
    if boundary_page is None:
        return None

    if settings.debug:
        _purge_page_modules(settings.pages_dir)

    try:
        artifacts = await _create_page_artifacts(
            request=request,
            settings=settings,
            page=boundary_page,
            renderer=renderer,
            loader_breadcrumb=_initial_loader_breadcrumb(boundary_page),
        )
        script_nonce = secrets.token_urlsafe(24)
        document = render_document(
            settings=settings,
            page=boundary_page,
            body_html=artifacts.body_html,
            props=artifacts.component_props,
            script_nonce=script_nonce,
            head_elements=artifacts.head_elements,
            inline_styles=artifacts.inline_styles,
            auth_seed=_auth_seed_for_request(request),
        )
        return HTMLResponse(document, status_code=404)
    except Exception:
        # If the not-found boundary itself fails, give up and let the caller
        # use the default 404 response.
        return None


def _record_render_metric(request: Request, kind: str, duration_ms: float) -> None:
    """Record a loader or SSR-render duration into the metrics registry, if any.

    ``kind`` is ``"loader"`` or ``"render"``. A no-op when no registry is bound
    to the request's app (e.g. in unit tests that build a bare Starlette app).
    """
    from pyxle.observability.metrics import get_metrics  # noqa: PLC0415

    registry = get_metrics(request)
    if registry is None:
        return
    if kind == "loader":
        registry.observe_loader(duration_ms)
    else:
        registry.observe_render(duration_ms)


async def _execute_loader(
    page: PageRoute,
    request: Request,
    *,
    module: Any | None,
    debug: bool = False,
) -> Tuple[dict[str, Any], int, float | None, Any | None]:
    if not page.has_loader:
        return {}, 200, None, module

    if module is None:
        module = _import_server_module(page.module_key, page.server_module_path, debug=debug)
    loader = getattr(module, page.loader_name or "", None)
    if loader is None:
        raise LoaderExecutionError(
            f"Loader '{page.loader_name}' not found in module {page.module_key}"
        )

    from pyxle.observability.otel import span  # noqa: PLC0415

    _loader_start = time.perf_counter()
    with span("loader"):
        result = await _invoke_loader_callable(loader, request)
    _record_render_metric(request, "loader", (time.perf_counter() - _loader_start) * 1000.0)

    payload, status_code, revalidate = _normalize_loader_result(result, page)
    return payload, status_code, revalidate, module


async def _invoke_loader_callable(loader: Callable[..., Any], request: Request) -> Any:
    """Call a ``@server`` loader (sync or async) and return its result.

    A read of an unset ``request.state`` attribute is translated into
    :class:`MissingRequestStateError` (guidance instead of a bare
    ``AttributeError``), chaining the original exception; every other
    exception propagates untouched.
    """
    try:
        result = loader(request)
        if hasattr(result, "__await__"):
            result = await result
    except AttributeError as exc:
        attribute = missing_state_attribute(exc)
        if attribute is None:
            raise
        raise MissingRequestStateError(attribute) from exc
    return result


async def _execute_layout_loaders(
    *,
    settings: DevServerSettings,
    page: PageRoute,
    request: Request,
) -> dict[str, Any] | None:
    """Execute ``@server`` loaders declared in ancestor layout/template files.

    Returns a dict of loader results (one entry per layout that has a loader),
    or ``None`` if no layout declares a loader.
    """
    from pyxle.devserver.registry import find_layout_loaders

    layout_loader_infos = find_layout_loaders(settings, page.source_relative_path)
    if not layout_loader_infos:
        return None

    layout_data: dict[str, Any] = {}
    for info in layout_loader_infos:
        module = _import_server_module(info.module_key, info.server_module_path, debug=settings.debug)
        loader_fn = getattr(module, info.loader_name, None)
        if loader_fn is None:
            continue

        result = await _invoke_loader_callable(loader_fn, request)

        # Layout loaders return a plain dict (no status code).
        if isinstance(result, tuple) and result:
            result = result[0]
        if isinstance(result, Mapping):
            layout_data.update(result)

    return layout_data or None


def _resolve_head_elements(
    page: PageRoute,
    module,
    loader_payload: Mapping[str, Any],
    *,
    debug: bool = False,
) -> tuple[str, ...]:
    if not page.head_is_dynamic:
        return page.head_elements

    if module is None:
        module = _import_server_module(page.module_key, page.server_module_path, debug=debug)

    head_value = getattr(module, "HEAD", None)
    if head_value is None:
        return tuple()

    if callable(head_value):
        head_value = _evaluate_head_callable(page, head_value, loader_payload)

    return _normalize_head_entries(page, head_value)


def _coerce_revalidate(value: Any, page: PageRoute) -> float | None:
    """Validate a loader's ``revalidate`` hint.

    ``None`` means "cache until explicitly invalidated"; a non-negative number
    is the freshness window in seconds. Anything else (a bool, a negative, a
    string) is a programming error surfaced as a structured loader failure
    rather than silently caching forever or not at all.
    """
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise LoaderExecutionError(
            f"Loader for {page.path} returned revalidate={value!r}; "
            "expected a non-negative number of seconds or None"
        )
    if value < 0:
        raise LoaderExecutionError(
            f"Loader for {page.path} returned a negative revalidate ({value})"
        )
    return float(value)


def _normalize_loader_result(
    result: Any, page: PageRoute
) -> Tuple[dict[str, Any], int, float | None]:
    status_code = 200
    revalidate: float | None = None
    payload = result

    if isinstance(result, tuple) and result:
        payload = result[0]
        if len(result) > 1:
            status_code = int(result[1])

    # ``{data, revalidate}`` envelope: a loader may declare its own cache
    # lifetime (ROADMAP 2.1). Recognised only in its exact two-key shape so an
    # ordinary loader returning "data"/"revalidate" as page props is never
    # mistaken for a cache directive.
    if (
        isinstance(payload, Mapping)
        and set(payload) == {"data", "revalidate"}
        and isinstance(payload["data"], Mapping)
    ):
        revalidate = _coerce_revalidate(payload["revalidate"], page)
        payload = payload["data"]

    if not isinstance(payload, Mapping):
        raise LoaderExecutionError(
            f"Loader for {page.path} must return a mapping or (mapping, status_code) tuple"
        )

    return dict(payload), status_code, revalidate


def _compose_component_props(
    loader_payload: dict[str, Any],
    layout_data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    props: dict[str, Any] = {"data": loader_payload}
    if layout_data:
        props["layoutData"] = layout_data
    return props


def _auth_seed_for_request(request: Request) -> Any:
    """Pluck the auth seed an auth provider stashed on the request scope.

    The pyxle-auth session middleware publishes ``scope["pyxle.auth"]`` as
    ``{"user": ..., "endpoints": {...}}``. SSR forwards it to
    ``window.__PYXLE_AUTH__`` so the client ``useAuth`` hook shows the
    signed-in user on the first frame and finds the (possibly relocated) auth
    endpoints.

    Returns the ABSENT sentinel when no provider populated the scope (or set
    it to a non-object value), so the document emits no seed script and
    ``useAuth`` resolves over the network.
    """
    seed = request.scope.get("pyxle.auth")
    if isinstance(seed, dict):
        return seed
    return _AUTH_SEED_ABSENT


def _csrf_token_for_request(request: Request) -> str | None:
    """Pluck the CSRF token the CSRF middleware stashed on the request scope.

    The middleware computes the active token before invoking the inner
    app and stores it under ``scope["pyxle.csrf_token"]``. SSR forwards
    it to ``globalThis.__PYXLE_CSRF_TOKEN__`` so ``<Form>`` can embed it
    in the rendered HTML as ``<input type="hidden" ...>`` — that's what
    makes a no-JS submission satisfy the CSRF check.

    Returns ``None`` when CSRF is disabled or the middleware isn't in
    the stack; ``<Form>`` will then skip the hidden field and fall back
    to the cookie / header path that JavaScript handles.
    """
    token = request.scope.get("pyxle.csrf_token")
    if isinstance(token, str) and token:
        return token
    return None


async def _create_streaming_prelude(
    *,
    request: Request,
    settings: DevServerSettings,
    page: PageRoute,
    loader_breadcrumb: dict[str, str],
    suppress_per_user: bool,
) -> _StreamingPrelude:
    """Run the loader and resolve everything known before the component renders.

    Shared by the buffered and streaming paths: the loader, the (static) HEAD,
    layout data, composed props, and the per-request CSRF token. The streaming
    path uses this to flush the document head before the React shell renders;
    the buffered path follows it with a render and a runtime-``<Head>`` merge.
    """
    module = None
    if page.head_is_dynamic:
        module = _import_server_module(
            page.module_key, page.server_module_path, debug=settings.debug,
        )

    loader_props, status_code, revalidate, module = await _execute_loader(
        page,
        request,
        module=module,
        debug=settings.debug,
    )

    if page.has_loader:
        loader_breadcrumb["status"] = "passed"
        loader_breadcrumb["detail"] = f"Returned {len(loader_props)} key(s) with status {status_code}"

    head_elements = _resolve_head_elements(page, module, loader_props, debug=settings.debug)

    from pyxle.devserver.registry import find_layout_head_jsx_blocks

    layout_head_jsx_blocks = find_layout_head_jsx_blocks(settings, page.source_relative_path)

    # Execute layout loaders (if any layout has a @server decorator)
    layout_data = await _execute_layout_loaders(
        settings=settings,
        page=page,
        request=request,
    )

    component_props = _compose_component_props(loader_props, layout_data)
    # On a publicly-cacheable render, suppress the per-user CSRF token so the
    # shared cached body never carries one user's token (<Form> falls back to
    # the cookie/header JS path).
    csrf_token = None if suppress_per_user else _csrf_token_for_request(request)

    return _StreamingPrelude(
        component_props=component_props,
        head_elements=head_elements,
        layout_head_jsx_blocks=layout_head_jsx_blocks,
        status_code=status_code,
        revalidate=revalidate,
        csrf_token=csrf_token,
    )


async def _create_page_artifacts(
    *,
    request: Request,
    settings: DevServerSettings,
    page: PageRoute,
    renderer: ComponentRenderer,
    loader_breadcrumb: dict[str, str],
    suppress_per_user: bool = False,
) -> PageArtifacts:
    from pyxle.ssr.head_merger import merge_head_elements

    prelude = await _create_streaming_prelude(
        request=request,
        settings=settings,
        page=page,
        loader_breadcrumb=loader_breadcrumb,
        suppress_per_user=suppress_per_user,
    )

    from pyxle.observability.otel import span  # noqa: PLC0415

    _render_start = time.perf_counter()
    with span("ssr.render"):
        render_result = await renderer.render(
            page.client_module_path,
            prelude.component_props,
            request_pathname=request.url.path,
            csrf_token=prelude.csrf_token,
        )
    _record_render_metric(request, "render", (time.perf_counter() - _render_start) * 1000.0)
    body_html = render_result.html
    inline_styles = render_result.inline_styles

    # Convert runtime-extracted head elements (from <Head> components) to blocks
    runtime_head_blocks = list(render_result.head_elements)

    merged_head_elements = merge_head_elements(
        head_variable=prelude.head_elements,
        head_jsx_blocks=page.head_jsx_blocks,
        layout_head_jsx_blocks=prelude.layout_head_jsx_blocks,
        runtime_head_blocks=tuple(runtime_head_blocks),
    )

    head_markup = render_head_markup(merged_head_elements)

    return PageArtifacts(
        component_props=prelude.component_props,
        body_html=body_html,
        head_elements=merged_head_elements,
        head_markup=head_markup,
        inline_styles=inline_styles,
        status_code=prelude.status_code,
        revalidate=prelude.revalidate,
    )


def _initial_loader_breadcrumb(page: PageRoute) -> dict[str, str]:
    if page.has_loader:
        return _make_loader_breadcrumb(
            page,
            status="pending",
            detail="Awaiting loader execution",
        )
    return _make_loader_breadcrumb(
        page,
        status="skipped",
        detail="No loader defined",
    )


async def _try_error_boundary(
    *,
    request: Request,
    settings: DevServerSettings,
    renderer: ComponentRenderer,
    error_boundaries: ErrorBoundaryRegistry | None,
    route_path: str,
    error: BaseException,
    status_code: int,
) -> Optional[Response]:
    """Attempt to render the nearest ``error.pyxl`` for *route_path*.

    Returns an :class:`HTMLResponse` if an error boundary was found and
    rendered successfully, or ``None`` if no boundary exists or the boundary
    itself fails.
    """
    if error_boundaries is None:
        return None

    boundary_page = error_boundaries.find_error_boundary(route_path)
    if boundary_page is None:
        return None

    # Build error context that the error page component receives as props.
    error_context = _build_error_context(error, status_code, debug=settings.debug)

    try:
        _boundary_render_start = time.perf_counter()
        render_result = await renderer.render(
            boundary_page.client_module_path,
            {"error": error_context},
            request_pathname=request.url.path,
            csrf_token=_csrf_token_for_request(request),
        )
        _record_render_metric(
            request, "render", (time.perf_counter() - _boundary_render_start) * 1000.0
        )
        script_nonce = secrets.token_urlsafe(24)
        head_elements = boundary_page.head_elements
        document = render_document(
            settings=settings,
            page=boundary_page,
            body_html=render_result.html,
            props={"error": error_context},
            script_nonce=script_nonce,
            head_elements=head_elements,
            inline_styles=render_result.inline_styles,
            auth_seed=_auth_seed_for_request(request),
        )
        return HTMLResponse(document, status_code=status_code)
    except Exception:
        # If the error boundary itself fails, let the caller fall back to the
        # default error document — we must not enter an infinite error loop.
        return None


def _build_error_context(
    error: BaseException, status_code: int, *, debug: bool
) -> dict[str, Any]:
    """Build the error context dict passed as component props to ``error.pyxl``.

    Author-raised :class:`~pyxle.runtime.LoaderError` /
    :class:`~pyxle.runtime.ActionError` messages are intentional, user-facing
    copy, so they pass through verbatim in every environment (along with any
    ``data`` payload). For any *other* exception the message originates inside
    the framework or the Node SSR runtime and may carry a stack trace, file
    path, row ID, or secret. In production (``debug=False``) such messages are
    replaced with a generic string so an ``error.pyxl`` boundary never leaks
    internals (CLAUDE.md rule 18); in development they are surfaced (redacted
    for obvious secrets, mirroring the dev error overlay and
    :func:`_navigation_error_response`).
    """
    from pyxle.runtime import ActionError, LoaderError

    context: dict[str, Any] = {
        "message": str(error),
        "statusCode": status_code,
        "type": error.__class__.__name__,
    }

    if isinstance(error, (LoaderError, ActionError)):
        context["message"] = error.message
        if error.data:
            context["data"] = error.data
        return context

    if debug:
        from pyxle.devserver._security import redact_sensitive_patterns  # noqa: PLC0415

        context["message"] = redact_sensitive_patterns(
            str(error) or error.__class__.__name__
        )
    else:
        context["message"] = "An unexpected error occurred."
        # Hide the exception class name too: a third-party class
        # (e.g. ``InsufficientPrivilege``) can itself disclose which internal
        # subsystem failed. Mirror the JSON nav-error path, which uses
        # "ServerError" in production for non-author exceptions.
        context["type"] = "ServerError"

    return context


async def _navigation_error_response(
    *,
    request: Request,
    settings: DevServerSettings,
    page: PageRoute,
    overlay: OverlayManager | None,
    loader_breadcrumb: dict[str, str],
    stage: str,
    error: BaseException,
    status_code: int = 500,
) -> JSONResponse:
    breadcrumbs = _compose_breadcrumbs(loader_breadcrumb, stage=stage, message=str(error))
    if overlay is not None:
        await overlay.notify_error(route_path=page.path, error=error, breadcrumbs=breadcrumbs)

    _log_render_failure(page, stage=stage, error=error, status_code=status_code)

    # Navigation errors return JSON, so they need the same production
    # sanitization the HTML error document applies (CLAUDE.md rule 18): an
    # exception message may carry file paths, row IDs, or secrets, and a
    # client must never receive them. In dev we surface the detail (redacted
    # for obvious secrets, mirroring the dev error overlay) to aid debugging.
    if settings.debug:
        from pyxle.devserver._security import redact_sensitive_patterns  # noqa: PLC0415

        error_message = redact_sensitive_patterns(str(error) or error.__class__.__name__)
        error_type = error.__class__.__name__
    else:
        error_message = "The server encountered an error while processing this request."
        error_type = "ServerError"

    payload = {
        "ok": False,
        "routePath": page.path,
        "requestedPath": request.url.path,
        "stage": stage,
        "error": error_message,
        "errorType": error_type,
    }
    return JSONResponse(payload, status_code=status_code)


def _ensure_app_root_importable(module_path: Path) -> None:
    """Add the project root to ``sys.path`` if not already present.

    Compiled server modules live under
    ``<project_root>/<build_dir>/server/pages/...``.  Walking up to the
    build-directory ancestor and taking its parent gives the project root,
    regardless of page nesting depth.
    """
    resolved = module_path.resolve()
    for parent in resolved.parents:
        if parent.name.startswith(".pyxle"):
            project_root = str(parent.parent)
            if project_root not in sys.path:
                sys.path.insert(0, project_root)
            return


def _import_server_module(
    module_key: str, module_path: Path, *, debug: bool = False,
):
    if module_key in sys.modules:
        if not debug:
            return sys.modules[module_key]
        del sys.modules[module_key]

    _ensure_app_root_importable(module_path)

    spec = importlib.util.spec_from_file_location(module_key, module_path)
    if spec is None or spec.loader is None:
        raise LoaderExecutionError(f"Unable to load page module at {module_path!s}")

    module = importlib.util.module_from_spec(spec)
    sys.modules[module_key] = module
    spec.loader.exec_module(module)
    return module


def _purge_page_modules(pages_dir: Path) -> None:
    try:
        root = pages_dir.resolve()
    except FileNotFoundError:
        return
    removed: list[str] = []
    for name, module in list(sys.modules.items()):
        module_file = getattr(module, "__file__", None)
        if not module_file:
            continue
        try:
            module_path = Path(module_file).resolve()
        except (OSError, ValueError):
            continue
        try:
            module_path.relative_to(root)
        except ValueError:
            continue
        removed.append(name)
    if not removed:
        return
    importlib.invalidate_caches()
    for name in removed:
        sys.modules.pop(name, None)


def _evaluate_head_callable(
    page: PageRoute,
    head_callable: Callable[[Mapping[str, Any]], object],
    loader_payload: Mapping[str, Any],
) -> Any:
    try:
        value = head_callable(loader_payload)
    except TypeError as exc:
        raise HeadEvaluationError(
            f"Callable HEAD for {page.path} must accept exactly one argument (loader data)",
        ) from exc

    if inspect.isawaitable(value):
        # Close the coroutine to prevent "was never awaited" warnings.
        if hasattr(value, "close"):
            value.close()
        raise HeadEvaluationError(
            f"Callable HEAD for {page.path} must return synchronously",
        )

    return value


def _normalize_head_entries(page: PageRoute, value: Any) -> tuple[str, ...]:
    if value is None:
        return tuple()

    if isinstance(value, str):
        return (value,)

    if isinstance(value, (list, tuple)):
        normalized: list[str] = []
        for item in value:
            if not isinstance(item, str):
                raise HeadEvaluationError(
                    f"HEAD entries for {page.path} must be strings; got {type(item).__name__}",
                )
            normalized.append(item)
        return tuple(normalized)

    raise HeadEvaluationError(
        f"HEAD for {page.path} must be a string, list of strings, or callable; got {type(value).__name__}",
    )


__all__ = [
    "LoaderExecutionError",
    "MissingRequestStateError",
    "missing_state_attribute",
    "build_page_response",
    "build_page_navigation_response",
    "build_not_found_response",
]


def _make_loader_breadcrumb(page: PageRoute, *, status: str, detail: str) -> dict[str, str]:
    label = "Loader" if not page.loader_name else f"Loader ({page.loader_name})"
    return {"label": label, "status": status, "detail": detail}


def _compose_breadcrumbs(
    loader_breadcrumb: dict[str, str],
    *,
    stage: str,
    message: str,
) -> List[dict[str, str]]:
    if stage == "loader":
        renderer_status = "blocked"
        renderer_detail = "Renderer skipped because the loader failed."
    elif stage == "renderer":
        renderer_status = "failed"
        renderer_detail = message
    else:
        renderer_status = "unknown"
        renderer_detail = "Renderer outcome unknown due to server error."

    hydration_detail = (
        "Hydration never executed because SSR failed."
        if stage in {"loader", "renderer"}
        else "Hydration blocked by unexpected server error."
    )

    return [
        loader_breadcrumb,
        {"label": "Renderer", "status": renderer_status, "detail": renderer_detail},
        {"label": "Hydration", "status": "blocked", "detail": hydration_detail},
    ]
