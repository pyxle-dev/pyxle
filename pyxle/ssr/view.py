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
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Any, List, Optional, Tuple

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response, StreamingResponse

from pyxle.compiler.head_elements import find_discarded_head_content
from pyxle.devserver.dev_origins import unhydratable_origin_warning
from pyxle.devserver.error_pages import ErrorBoundaryRegistry
from pyxle.devserver.overlay import OverlayManager
from pyxle.devserver.routes import PageRoute
from pyxle.devserver.settings import DevServerSettings

from .module_cache import GENERATION_ATTRIBUTE, current_generation
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

if TYPE_CHECKING:  # pragma: no cover - typing only
    # Imported lazily at runtime (see the call sites): pyxle.devserver.registry
    # is a heavier module than the SSR request path should pull in eagerly.
    from pyxle.devserver.registry import LayoutHeadSource


class LoaderExecutionError(RuntimeError):
    """Raised when a page loader cannot be run or returns an unexpected value.

    The loader-stage failure family. Every page pipeline (buffered, streaming,
    navigation) recognises it and reports it as a loader failure: the nearest
    ``error.pyxl`` renders, the status is 500, and the dev overlay's breadcrumbs
    mark the renderer as blocked by the loader. Raised directly for framework
    level problems (a missing loader function, an unloadable module, a bad
    return value or ``revalidate`` hint); see :class:`LoaderCrashError` for a
    loader whose own body raised.
    """


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
            "and restart the server."
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


class LoaderCrashError(LoaderExecutionError):
    """Raised when a ``@server`` loader's own body raises.

    A ``KeyError`` on a missing dict key, a ``TypeError`` on ``None``, a
    database driver's exception — the most ordinary failures an application
    has. They are *expected* loader-stage faults, not unexpected framework
    ones, so they are classified here at the invocation site rather than
    reaching the render pipeline bare. That is what routes them to the
    application's ``error.pyxl`` (status 500) instead of Pyxle's fallback
    document.

    The original exception stays reachable through ``__cause__``, so the dev
    overlay and the server log still show the real traceback pointing at the
    line in the ``.pyxl`` file. The message names the loader, the route, and
    the original exception; it is shown in development and written to the log,
    while production responses stay sanitized (CLAUDE.md rule 18).

    An author-raised :class:`~pyxle.runtime.LoaderError` is *not* wrapped: it
    carries a deliberate status code and user-facing message and propagates
    untouched.
    """

    def __init__(self, origin: str, cause: BaseException) -> None:
        detail = str(cause)
        summary = f"{type(cause).__name__}: {detail}" if detail else type(cause).__name__
        super().__init__(f"{origin} raised {summary}")
        self.origin = origin


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
    layout_head_variable: tuple[str, ...]
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


def _document_host(request: Request) -> str | None:
    """The hostname the browser used to reach this document.

    Dev-only: a Vite bound to every interface answers under this name too, so
    the ``<script src>`` in the document points at the same host the visitor
    already typed rather than a ``localhost`` that means their own machine.
    """

    return request.url.hostname


def _document_origin(request: Request) -> str | None:
    """The origin this document is being served to, port always explicit.

    The browser sends exactly this string as ``Origin`` on every module request
    the document makes, so it is what the dev server's allow-list is matched
    against. ``None`` when the request carries no host to speak of.
    """

    url = request.url
    hostname = url.hostname
    if not hostname:
        return None
    # An IPv6 literal needs its brackets back; ``hostname`` strips them, and
    # without them the address runs into the port separator.
    if ":" in hostname and not hostname.startswith("["):
        hostname = f"[{hostname}]"
    port = url.port or (443 if url.scheme == "https" else 80)
    return f"{url.scheme}://{hostname}:{port}"


#: How many distinct "this page cannot hydrate" warnings a process keeps, so a
#: browser reloading a dead page does not reprint the same paragraph every time.
#: Each entry is one origin that reached this dev server.
_ORIGIN_WARNING_MEMORY = 32


@lru_cache(maxsize=_ORIGIN_WARNING_MEMORY)
def _warn_once(message: str) -> None:
    """Emit ``message`` the first time it is seen (the cache *is* the dedupe)."""

    _logger.warning("%s", message)


def _warn_if_document_cannot_hydrate(
    settings: DevServerSettings, request: Request
) -> None:
    """Report a page served to a browser Vite will not serve modules to.

    The failure this catches has no other symptom: the document arrives whole
    and correct, the module requests are refused by CORS, and React never
    mounts. Vite logs nothing (it answered ``200``), the browser logs nothing a
    page can see, and the developer is left with an interface that renders and
    does nothing. Pyxle is the only party holding both facts — the origin it
    just served, and the allow-list it generated — so it is the only one that
    can say so.
    """

    if not settings.debug:
        return
    origin = _document_origin(request)
    if origin is None:
        return
    message = unhydratable_origin_warning(
        document_origin=origin,
        starlette_host=settings.starlette_host,
        starlette_port=settings.starlette_port,
        vite_port=settings.vite_port,
    )
    if message is not None:
        _warn_once(message)


def _error_response(
    *,
    settings: DevServerSettings,
    page: PageRoute,
    stage: str,
    error: BaseException,
    status_code: int,
    already_logged: bool = False,
    request_host: str | None = None,
) -> HTMLResponse:
    """Log the failure server-side, then return the sanitized HTML error page.

    Centralizes the "log, then render the fallback document" pair so every
    error branch in :func:`build_page_response` behaves consistently and the
    production response never leaks internals. Pass ``already_logged=True``
    when the caller has logged the failure itself, so it is recorded once.
    """
    if not already_logged:
        _log_render_failure(page, stage=stage, error=error, status_code=status_code)
    fallback = render_error_document(
        settings=settings,
        page=page,
        error=error,
        status_code=status_code,
        request_host=request_host,
    )
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
    loader_breadcrumb = _initial_loader_breadcrumb(page)
    _warn_if_document_cannot_hydrate(settings, request)

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
                request_host=_document_host(request),
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
                request_host=_document_host(request),
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

    Propagates loader exceptions with the same classification the page pipeline
    uses: an author-raised :class:`~pyxle.runtime.LoaderError` untouched, and
    anything the loader body raised as a :class:`LoaderCrashError` whose
    ``__cause__`` is the original exception.
    """
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
    error-boundary contract. A failure in *application* code — a loader
    (including a loader whose own body raised: see :class:`LoaderCrashError`),
    a ``HEAD``, a component — tries the nearest ``error.pyxl`` first.

    Anything else reaching this function is a fault in the framework's own
    render pipeline, and the ``else`` branch deliberately returns the sanitized
    fallback **without** consulting the boundary: running more application code
    to handle a framework fault can compound the failure, and the boundary
    render depends on the very machinery that just broke. Application faults do
    not land there — they are classified where they happen (loader exceptions
    in :func:`_invoke_loader_callable`, render exceptions in the renderer), not
    inferred from what is left over here.
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
    else:
        if overlay is not None:
            await overlay.notify_error(
                route_path=page.path,
                error=exc,
                breadcrumbs=_compose_breadcrumbs(loader_breadcrumb, stage="server", message=str(exc)),
            )
        return _error_response(
            settings=settings,
            page=page,
            stage="server",
            error=exc,
            status_code=500,
            request_host=_document_host(request),
        )

    if overlay is not None:
        await overlay.notify_error(
            route_path=page.path,
            error=exc,
            breadcrumbs=_compose_breadcrumbs(loader_breadcrumb, stage=stage, message=str(exc)),
        )
    # Log *before* the boundary attempt. A production response is deliberately
    # sanitized, so this log is the only record of what actually failed — and a
    # successfully rendered error.pyxl must not make the failure disappear from
    # the server's logs. (Sub-500 statuses stay quiet; see _log_render_failure.)
    _log_render_failure(page, stage=stage, error=exc, status_code=status_code)
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
        settings=settings,
        page=page,
        stage=stage,
        error=exc,
        status_code=status_code,
        already_logged=True,
        request_host=_document_host(request),
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
            layout_head_variable=prelude.layout_head_variable,
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
                request_host=_document_host(request),
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
                layout_head_variable=prelude.layout_head_variable,
                runtime_head_blocks=(),
            )
            nav_status_code = prelude.status_code
            nav_component_props = prelude.component_props
            nav_head_markup = render_head_markup(
                static_head, settings.document_title_default
            )
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
    except Exception as exc:
        # The JSON counterpart of _handle_render_exception's else branch: a
        # fault in the framework's own pipeline, reported as a server-stage
        # error. Application faults are classified before they get here.
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
            request_host=_document_host(request),
        )
        return HTMLResponse(document, status_code=404)
    except Exception:
        # If the not-found boundary itself fails, give up and let the caller
        # use the default 404 response. Never silently — log the swap.
        _logger.warning(
            "not-found boundary %s failed to render; serving the default 404",
            boundary_page.source_relative_path.as_posix(),
            exc_info=True,
        )
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
        result = await _invoke_loader_callable(loader, request, origin=_loader_origin(page))
    _record_render_metric(request, "loader", (time.perf_counter() - _loader_start) * 1000.0)

    payload, status_code, revalidate = _normalize_loader_result(result, page)
    return payload, status_code, revalidate, module


def _loader_origin(page: PageRoute) -> str:
    """Name a page loader for a :class:`LoaderCrashError` message."""
    return f"Loader {page.loader_name!r} for {page.path}"


async def _invoke_loader_callable(
    loader: Callable[..., Any], request: Request, *, origin: str
) -> Any:
    """Call a ``@server`` loader (sync or async) and return its result.

    Everything a loader's own body raises is classified here, at the boundary
    between framework code and application code, so the render pipeline can
    treat it as what it is — a loader-stage failure — instead of an unexpected
    framework fault:

    * :class:`~pyxle.runtime.LoaderError` is the author's deliberate signal
      (status code, user-facing message, ``data``) and propagates untouched.
    * A read of an unset ``request.state`` attribute becomes
      :class:`MissingRequestStateError` — guidance instead of a bare
      ``AttributeError``.
    * Anything else becomes :class:`LoaderCrashError`, chaining the original
      through ``__cause__``.

    ``origin`` names the loader in the resulting message (``"Loader 'load_user'
    for /users/{id}"``). ``BaseException`` is deliberately not caught, so an
    ``asyncio.CancelledError`` from a client disconnect stays a cancellation
    rather than becoming an error page.
    """
    try:
        result = loader(request)
        if hasattr(result, "__await__"):
            result = await result
    except Exception as exc:
        # Imported on the failure path only: this is the SSR hot path, and a
        # successful loader must not pay for the framework's error taxonomy.
        from pyxle.runtime import LoaderError  # noqa: PLC0415 - zero-dep module

        if isinstance(exc, (LoaderError, LoaderExecutionError)):
            raise
        attribute = missing_state_attribute(exc)
        if attribute is not None:
            raise MissingRequestStateError(attribute) from exc
        raise LoaderCrashError(origin, exc) from exc
    return result


@dataclass(frozen=True, slots=True)
class _LayoutLoaderResults:
    """What the layout chain's ``@server`` loaders produced for one request.

    ``merged`` is the flattened view the layout components receive as props (a
    later layout's key wins, as before). ``per_layout`` keeps each layout's own
    result under its source path, because a layout's ``HEAD`` callable is
    handed *its own* data, not the chain's.
    """

    merged: dict[str, Any] | None = None
    per_layout: Mapping[Path, Mapping[str, Any]] = field(default_factory=dict)


async def _execute_layout_loaders(
    *,
    settings: DevServerSettings,
    page: PageRoute,
    request: Request,
) -> _LayoutLoaderResults:
    """Execute ``@server`` loaders declared in ancestor layout/template files.

    Returns the merged loader data (``None`` when no layout declares a loader)
    alongside each layout's own result, keyed by source path.
    """
    from pyxle.devserver.registry import find_layout_loaders

    layout_loader_infos = find_layout_loaders(settings, page.source_relative_path)
    if not layout_loader_infos:
        return _LayoutLoaderResults()

    layout_data: dict[str, Any] = {}
    per_layout: dict[Path, Mapping[str, Any]] = {}
    for info in layout_loader_infos:
        module = _import_server_module(info.module_key, info.server_module_path, debug=settings.debug)
        loader_fn = getattr(module, info.loader_name, None)
        if loader_fn is None:
            continue

        result = await _invoke_loader_callable(
            loader_fn,
            request,
            origin=f"Layout loader {info.loader_name!r} in {info.relative_path.as_posix()}",
        )

        # Layout loaders return a plain dict (no status code).
        if isinstance(result, tuple) and result:
            result = result[0]
        if isinstance(result, Mapping):
            layout_data.update(result)
            per_layout[info.relative_path] = result

    return _LayoutLoaderResults(merged=layout_data or None, per_layout=per_layout)


def _resolve_head_elements(
    page: PageRoute,
    module,
    loader_payload: Mapping[str, Any],
    *,
    debug: bool = False,
) -> tuple[str, ...]:
    """Resolve a page's ``HEAD`` — literals straight from the compiler, anything
    else by importing the page module and evaluating it against loader data."""
    if not page.head_is_dynamic:
        return page.head_elements

    if module is None:
        module = _import_server_module(page.module_key, page.server_module_path, debug=debug)

    return _evaluate_head_value(page.path, getattr(module, "HEAD", None), loader_payload)


def _resolve_layout_head_elements(
    sources: Sequence[LayoutHeadSource],
    layout_payloads: Mapping[Path, Mapping[str, Any]],
    *,
    debug: bool = False,
) -> tuple[str, ...]:
    """Resolve every layout's ``HEAD`` in the chain, in wrapping order.

    The layout half of :func:`_resolve_head_elements`, and deliberately its
    twin: a layout's ``HEAD`` supports exactly the forms a page's does. A
    literal is taken from the compiler's static extraction; anything the
    compiler could not read — an f-string, a concatenation, ``json.dumps(...)``,
    a ``def HEAD(data)`` callable — is evaluated by importing the layout module,
    which is the same module object its ``@server`` loader ran in.

    A callable receives **that layout's own loader data**, mirroring the page
    contract where a page's callable receives that page's loader data. A layout
    with no loader gets an empty mapping, so ``data.get(...)`` is the safe
    idiom there.
    """
    if not sources:
        return tuple()

    elements: list[str] = []
    for source in sources:
        if not source.is_dynamic:
            elements.extend(source.static_elements)
            continue

        module = _import_server_module(
            source.module_key, source.server_module_path, debug=debug
        )
        elements.extend(
            _evaluate_head_value(
                source.relative_path.as_posix(),
                getattr(module, "HEAD", None),
                layout_payloads.get(source.relative_path, {}),
            )
        )

    return tuple(elements)


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

    from pyxle.devserver.registry import find_layout_head_contributions

    layout_head = find_layout_head_contributions(settings, page.source_relative_path)

    # Execute layout loaders (if any layout has a @server decorator) BEFORE
    # resolving the layout chain's HEAD: a layout's callable HEAD is handed its
    # own loader's data, so the data has to exist first.
    layout_results = await _execute_layout_loaders(
        settings=settings,
        page=page,
        request=request,
    )

    layout_head_variable = _resolve_layout_head_elements(
        layout_head.head_sources,
        layout_results.per_layout,
        debug=settings.debug,
    )

    component_props = _compose_component_props(loader_props, layout_results.merged)
    # On a publicly-cacheable render, suppress the per-user CSRF token so the
    # shared cached body never carries one user's token (<Form> falls back to
    # the cookie/header JS path).
    csrf_token = None if suppress_per_user else _csrf_token_for_request(request)

    return _StreamingPrelude(
        component_props=component_props,
        head_elements=head_elements,
        layout_head_jsx_blocks=layout_head.jsx_blocks,
        layout_head_variable=layout_head_variable,
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
        layout_head_variable=prelude.layout_head_variable,
        runtime_head_blocks=tuple(runtime_head_blocks),
    )

    head_markup = render_head_markup(merged_head_elements, settings.document_title_default)

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

    # The compiled boundary is wrapped in its ancestor layout chain exactly
    # like a normal page, so a layout with a ``@server`` loader needs its
    # loader data here too — without it the layout component crashes on the
    # missing props and the boundary silently degrades to the default error
    # document. A loader failing *while rendering the boundary* must never
    # mask the boundary itself: fall back to error-only props and let the
    # render proceed with whatever the layout can do without data.
    try:
        layout_results = await _execute_layout_loaders(
            settings=settings, page=boundary_page, request=request
        )
    except Exception as layout_exc:
        _logger.warning(
            "Layout loader failed while rendering error boundary %s: %s",
            boundary_page.source_relative_path.as_posix(),
            layout_exc,
        )
        layout_results = _LayoutLoaderResults()

    boundary_props: dict[str, Any] = {"error": error_context}
    if layout_results.merged:
        boundary_props["layoutData"] = layout_results.merged

    try:
        _boundary_render_start = time.perf_counter()
        render_result = await renderer.render(
            boundary_page.client_module_path,
            boundary_props,
            request_pathname=request.url.path,
            csrf_token=_csrf_token_for_request(request),
        )
        _record_render_metric(
            request, "render", (time.perf_counter() - _boundary_render_start) * 1000.0
        )
        script_nonce = secrets.token_urlsafe(24)
        head_elements = _boundary_head_elements(
            settings=settings,
            boundary_page=boundary_page,
            boundary_props=boundary_props,
            runtime_head_blocks=tuple(render_result.head_elements),
            layout_payloads=layout_results.per_layout,
        )
        document = render_document(
            settings=settings,
            page=boundary_page,
            body_html=render_result.html,
            props=boundary_props,
            script_nonce=script_nonce,
            head_elements=head_elements,
            inline_styles=render_result.inline_styles,
            auth_seed=_auth_seed_for_request(request),
            request_host=_document_host(request),
        )
        return HTMLResponse(document, status_code=status_code)
    except Exception:
        # If the error boundary itself fails, let the caller fall back to the
        # default error document — we must not enter an infinite error loop.
        # Never silently: the fallback swap is invisible without this log.
        _logger.warning(
            "Error boundary %s failed to render; serving the default error document",
            boundary_page.source_relative_path.as_posix(),
            exc_info=True,
        )
        return None


def _boundary_head_elements(
    *,
    settings: DevServerSettings,
    boundary_page: PageRoute,
    boundary_props: Mapping[str, Any],
    runtime_head_blocks: tuple[str, ...],
    layout_payloads: Mapping[Path, Mapping[str, Any]],
) -> tuple[str, ...]:
    """Build the document head for a page rendered through an error boundary.

    The boundary is an ordinary page wrapped in its ancestor layout chain, so
    its head is merged from exactly the sources a normal render uses
    (:func:`_create_page_artifacts`): the layout chain's two channels, the
    boundary's own ``HEAD`` variable and ``<Head>`` blocks, and the ``<Head>``
    elements the render just produced. Reading the ``HEAD`` variable alone left
    the one page a confused visitor is most likely to see without the site's
    stylesheet, favicon or title — including the boundary's own ``<Head>``.

    An evaluated ``HEAD`` (a callable, or any non-literal) is the one part that
    can fail here: it receives the error context rather than loader data, a
    shape it has never run against. That must not cost the visitor the boundary
    as well, so a failure degrades to the statically extracted elements and is
    logged rather than raised. The layout chain's ``HEAD`` is resolved under the
    same rule, with whatever layout loader data survived the failure — a root
    layout's JSON-LD or critical CSS should still reach the error page, but not
    at the price of the error page itself.
    """
    from pyxle.devserver.registry import find_layout_head_contributions  # noqa: PLC0415
    from pyxle.ssr.head_merger import merge_head_elements  # noqa: PLC0415

    try:
        head_variable = _resolve_head_elements(
            boundary_page, None, boundary_props, debug=settings.debug
        )
    except Exception as exc:
        _logger.warning(
            "HEAD evaluation failed for error boundary %s: %s",
            boundary_page.source_relative_path.as_posix(),
            exc,
        )
        head_variable = boundary_page.head_elements

    layout_head = find_layout_head_contributions(
        settings, boundary_page.source_relative_path
    )
    try:
        layout_head_variable = _resolve_layout_head_elements(
            layout_head.head_sources, layout_payloads, debug=settings.debug
        )
    except Exception as exc:
        _logger.warning(
            "Layout HEAD evaluation failed while rendering error boundary %s: %s",
            boundary_page.source_relative_path.as_posix(),
            exc,
        )
        layout_head_variable = tuple(
            element
            for source in layout_head.head_sources
            for element in source.static_elements
        )

    return merge_head_elements(
        head_variable=head_variable,
        head_jsx_blocks=boundary_page.head_jsx_blocks,
        layout_head_jsx_blocks=layout_head.jsx_blocks,
        layout_head_variable=layout_head_variable,
        runtime_head_blocks=runtime_head_blocks,
    )


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
    cached = sys.modules.get(module_key)
    if cached is not None:
        if not debug:
            return cached
        # Dev: reuse the imported module across requests — so module-level
        # globals persist exactly like `pyxle serve` — until a rebuild advances
        # the reload generation, at which point re-import from disk so edits take
        # effect. See pyxle.ssr.module_cache.
        if getattr(cached, GENERATION_ATTRIBUTE, None) == current_generation():
            return cached
        del sys.modules[module_key]

    _ensure_app_root_importable(module_path)

    # Debug mode execs the generated module as its .pyxl source (remapped
    # co_filename + line numbers via the compiler's embedded debug footer) —
    # .pyxl tracebacks and native debugger breakpoints. See compiler.linemap.
    loader = None
    if debug and module_path.suffix == ".py":
        from pyxle.compiler.linemap import PyxlSourceFileLoader  # noqa: PLC0415

        loader = PyxlSourceFileLoader(module_key, str(module_path))
    spec = importlib.util.spec_from_file_location(module_key, module_path, loader=loader)
    if spec is None or spec.loader is None:
        raise LoaderExecutionError(f"Unable to load page module at {module_path!s}")

    module = importlib.util.module_from_spec(spec)
    sys.modules[module_key] = module
    spec.loader.exec_module(module)
    if debug:
        setattr(module, GENERATION_ATTRIBUTE, current_generation())
    return module


def _evaluate_head_callable(
    origin: str,
    head_callable: Callable[[Mapping[str, Any]], object],
    loader_payload: Mapping[str, Any],
) -> Any:
    """Call a ``HEAD`` callable with its loader data.

    *origin* names what failed in the error message — a page's route path, or a
    layout's project-relative source path.
    """
    try:
        value = head_callable(loader_payload)
    except TypeError as exc:
        raise HeadEvaluationError(
            f"Callable HEAD for {origin} must accept exactly one argument (loader data)",
        ) from exc

    if inspect.isawaitable(value):
        # Close the coroutine to prevent "was never awaited" warnings.
        if hasattr(value, "close"):
            value.close()
        raise HeadEvaluationError(
            f"Callable HEAD for {origin} must return synchronously",
        )

    return value


def _normalize_head_entries(origin: str, value: Any) -> tuple[str, ...]:
    """Coerce an evaluated ``HEAD`` into a tuple of element strings."""
    if value is None:
        return tuple()

    if isinstance(value, str):
        return (value,)

    if isinstance(value, (list, tuple)):
        normalized: list[str] = []
        for item in value:
            if not isinstance(item, str):
                raise HeadEvaluationError(
                    f"HEAD entries for {origin} must be strings; got {type(item).__name__}",
                )
            normalized.append(item)
        return tuple(normalized)

    raise HeadEvaluationError(
        f"HEAD for {origin} must be a string, list of strings, or callable; got {type(value).__name__}",
    )


#: Multi-element ``HEAD`` entries already reported, as ``(origin, dropped)``.
#: A computed ``HEAD`` is re-evaluated on every render, so warning on the spot
#: would put one line per request in the log — the noise that gets a warning
#: filtered out and the bug ignored. Keyed on the dropped content as well as the
#: file so a *different* mistake in the same file is still heard.
_reported_discarded_head: set[tuple[str, str]] = set()

#: Bound for the set above (rule 17: no unbounded caches). The key is normally
#: stable — the dropped element rarely carries request data — so this holds one
#: entry per genuine mistake. If an app manages to vary it per request, the set
#: is cleared on overflow rather than grown or frozen: a rare repeat line is
#: better than either a leak or going silent.
_DISCARDED_HEAD_REPORT_LIMIT = 256


def _warn_discarded_head_content(origin: str, entries: Sequence[str]) -> None:
    """Report ``HEAD`` entries whose tail the sanitiser will drop.

    This is the render-time half of the check the compiler makes on a literal
    ``HEAD``. It cannot be an error: the value is only known once the page is
    rendered, so a second ``<meta>`` appearing for one row of data would take
    the whole page down for exactly the visitors who reach that row, having
    passed every test and every other request. Losing a tag must not cost the
    page — but it must not be silent either.
    """
    for entry in entries:
        discarded = find_discarded_head_content(entry)
        if discarded is None:
            continue
        key = (origin, discarded)
        if key in _reported_discarded_head:
            continue
        if len(_reported_discarded_head) >= _DISCARDED_HEAD_REPORT_LIMIT:
            _reported_discarded_head.clear()
        _reported_discarded_head.add(key)
        _logger.warning(
            "HEAD entry for %s contains more than one element; only the first "
            "is kept. Split it into separate list entries. Dropped: %s",
            origin,
            discarded if len(discarded) <= 200 else discarded[:197] + "...",
        )


def _evaluate_head_value(
    origin: str,
    head_value: Any,
    loader_payload: Mapping[str, Any],
) -> tuple[str, ...]:
    """Resolve a module's ``HEAD`` attribute into finished element strings.

    The single definition of what a ``HEAD`` may be — a string, a list of
    strings, or a callable taking loader data. Pages and layouts share it so
    the two can never drift into supporting different forms.
    """
    if head_value is None:
        return tuple()

    if callable(head_value):
        head_value = _evaluate_head_callable(origin, head_value, loader_payload)

    entries = _normalize_head_entries(origin, head_value)
    _warn_discarded_head_content(origin, entries)
    return entries


__all__ = [
    "LoaderCrashError",
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
