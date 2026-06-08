"""Utilities for building SSR responses from compiled page routes."""

from __future__ import annotations

import importlib
import importlib.util
import inspect
import logging
import secrets
import sys
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

from .renderer import ComponentRenderer, ComponentRenderError, InlineStyleFragment
from .template import (
    ManifestLookupError,
    build_document_shell,
    render_document,
    render_error_document,
    render_head_markup,
)


class LoaderExecutionError(RuntimeError):
    """Raised when a page loader returns an unexpected value."""


class HeadEvaluationError(RuntimeError):
    """Raised when HEAD cannot be resolved at runtime."""


@dataclass(slots=True)
class PageArtifacts:
    component_props: dict[str, Any]
    body_html: str
    head_elements: tuple[str, ...]
    head_markup: str
    inline_styles: tuple[InlineStyleFragment, ...]
    status_code: int


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


def _resolve_nav_cache_ttl(settings: DevServerSettings, path: str) -> int | None:
    """Return the configured edge-cache TTL (seconds) for ``path``, if any.

    The client navigation cache reuses this per-page value so a page's
    prefetch/seed freshness matches its declared edge-cache lifetime. Pages
    with no ``cache`` entry return ``None`` and fall back to the client's
    default navigation-cache lifetime.
    """
    cache = getattr(settings, "cache", None)
    if cache is None:
        return None
    return cache.max_age_for(path)


async def build_page_response(
    *,
    request: Request,
    settings: DevServerSettings,
    page: PageRoute,
    renderer: ComponentRenderer,
    overlay: OverlayManager | None = None,
    error_boundaries: ErrorBoundaryRegistry | None = None,
) -> Response:
    from pyxle.runtime import LoaderError

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
        )
        script_nonce = secrets.token_urlsafe(24)
        nav_cache_ttl = _resolve_nav_cache_ttl(settings, request.url.path)
        try:
            shell = build_document_shell(
                settings=settings,
                page=page,
                props=artifacts.component_props,
                script_nonce=script_nonce,
                head_elements=artifacts.head_elements,
                inline_styles=artifacts.inline_styles,
                nav_cache_ttl=nav_cache_ttl,
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
            )
            if overlay is not None:
                await overlay.notify_clear(route_path=page.path)
            return HTMLResponse(document, status_code=artifacts.status_code)

        async def _document_stream():
            yield shell.prefix.encode("utf-8")
            yield artifacts.body_html.encode("utf-8")
            yield shell.suffix.encode("utf-8")

        if overlay is not None:
            await overlay.notify_clear(route_path=page.path)
        return StreamingResponse(
            _document_stream(),
            status_code=artifacts.status_code,
            media_type="text/html",
        )
    except LoaderError as exc:
        # Structured loader error — try the nearest error boundary.
        loader_breadcrumb = _make_loader_breadcrumb(page, status="failed", detail=str(exc))
        if overlay is not None:
            await overlay.notify_error(
                route_path=page.path,
                error=exc,
                breadcrumbs=_compose_breadcrumbs(loader_breadcrumb, stage="loader", message=str(exc)),
            )
        boundary_response = await _try_error_boundary(
            request=request,
            settings=settings,
            renderer=renderer,
            error_boundaries=error_boundaries,
            route_path=page.path,
            error=exc,
            status_code=exc.status_code,
        )
        if boundary_response is not None:
            return boundary_response
        return _error_response(
            settings=settings, page=page, stage="loader", error=exc,
            status_code=exc.status_code,
        )
    except LoaderExecutionError as exc:
        loader_breadcrumb = _make_loader_breadcrumb(page, status="failed", detail=str(exc))
        if overlay is not None:
            await overlay.notify_error(
                route_path=page.path,
                error=exc,
                breadcrumbs=_compose_breadcrumbs(loader_breadcrumb, stage="loader", message=str(exc)),
            )
        boundary_response = await _try_error_boundary(
            request=request,
            settings=settings,
            renderer=renderer,
            error_boundaries=error_boundaries,
            route_path=page.path,
            error=exc,
            status_code=500,
        )
        if boundary_response is not None:
            return boundary_response
        return _error_response(
            settings=settings, page=page, stage="loader", error=exc, status_code=500,
        )
    except HeadEvaluationError as exc:
        if overlay is not None:
            await overlay.notify_error(
                route_path=page.path,
                error=exc,
                breadcrumbs=_compose_breadcrumbs(loader_breadcrumb, stage="server", message=str(exc)),
            )
        boundary_response = await _try_error_boundary(
            request=request,
            settings=settings,
            renderer=renderer,
            error_boundaries=error_boundaries,
            route_path=page.path,
            error=exc,
            status_code=500,
        )
        if boundary_response is not None:
            return boundary_response
        return _error_response(
            settings=settings, page=page, stage="server", error=exc, status_code=500,
        )
    except ComponentRenderError as exc:
        if overlay is not None:
            await overlay.notify_error(
                route_path=page.path,
                error=exc,
                breadcrumbs=_compose_breadcrumbs(loader_breadcrumb, stage="renderer", message=str(exc)),
            )
        boundary_response = await _try_error_boundary(
            request=request,
            settings=settings,
            renderer=renderer,
            error_boundaries=error_boundaries,
            route_path=page.path,
            error=exc,
            status_code=500,
        )
        if boundary_response is not None:
            return boundary_response
        return _error_response(
            settings=settings, page=page, stage="renderer", error=exc, status_code=500,
        )
    except Exception as exc:  # pragma: no cover - defensive guardrail
        if overlay is not None:
            await overlay.notify_error(
                route_path=page.path,
                error=exc,
                breadcrumbs=_compose_breadcrumbs(loader_breadcrumb, stage="server", message=str(exc)),
            )
        return _error_response(
            settings=settings, page=page, stage="server", error=exc, status_code=500,
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
        artifacts = await _create_page_artifacts(
            request=request,
            settings=settings,
            page=page,
            renderer=renderer,
            loader_breadcrumb=loader_breadcrumb,
        )
        if overlay is not None:
            await overlay.notify_clear(route_path=page.path)
        payload = {
            "ok": True,
            "routePath": page.path,
            "requestedPath": request.url.path,
            "statusCode": artifacts.status_code,
            "page": {
                "clientAssetPath": page.client_asset_path,
                "moduleKey": page.module_key,
            },
            "props": artifacts.component_props,
            "headMarkup": artifacts.head_markup,
            # Per-page client navigation-cache lifetime (seconds). Mirrors the
            # page's edge-cache TTL so prefetched data stays fresh exactly as
            # long as the CDN would serve it; ``None`` → client default.
            "navCacheTtlSeconds": _resolve_nav_cache_ttl(settings, request.url.path),
        }
        return JSONResponse(payload, status_code=artifacts.status_code)
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
            error=exc,
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
        )
        return HTMLResponse(document, status_code=404)
    except Exception:
        # If the not-found boundary itself fails, give up and let the caller
        # use the default 404 response.
        return None


async def _execute_loader(
    page: PageRoute,
    request: Request,
    *,
    module: Any | None,
    debug: bool = False,
) -> Tuple[dict[str, Any], int, Any | None]:
    if not page.has_loader:
        return {}, 200, module

    if module is None:
        module = _import_server_module(page.module_key, page.server_module_path, debug=debug)
    loader = getattr(module, page.loader_name or "", None)
    if loader is None:
        raise LoaderExecutionError(
            f"Loader '{page.loader_name}' not found in module {page.module_key}"
        )

    result = loader(request)
    if hasattr(result, "__await__"):
        result = await result  # type: ignore[assignment]

    payload, status_code = _normalize_loader_result(result, page)
    return payload, status_code, module


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

        result = loader_fn(request)
        if hasattr(result, "__await__"):
            result = await result

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


def _normalize_loader_result(result: Any, page: PageRoute) -> Tuple[dict[str, Any], int]:
    status_code = 200
    payload = result

    if isinstance(result, tuple) and result:
        payload = result[0]
        if len(result) > 1:
            status_code = int(result[1])

    if not isinstance(payload, Mapping):
        raise LoaderExecutionError(
            f"Loader for {page.path} must return a mapping or (mapping, status_code) tuple"
        )

    return dict(payload), status_code


def _compose_component_props(
    loader_payload: dict[str, Any],
    layout_data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    props: dict[str, Any] = {"data": loader_payload}
    if layout_data:
        props["layoutData"] = layout_data
    return props


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


async def _create_page_artifacts(
    *,
    request: Request,
    settings: DevServerSettings,
    page: PageRoute,
    renderer: ComponentRenderer,
    loader_breadcrumb: dict[str, str],
) -> PageArtifacts:
    module = None
    if page.head_is_dynamic:
        module = _import_server_module(
            page.module_key, page.server_module_path, debug=settings.debug,
        )

    loader_props, status_code, module = await _execute_loader(
        page,
        request,
        module=module,
        debug=settings.debug,
    )

    if page.has_loader:
        loader_breadcrumb["status"] = "passed"
        loader_breadcrumb["detail"] = f"Returned {len(loader_props)} key(s) with status {status_code}"

    head_elements = _resolve_head_elements(page, module, loader_props, debug=settings.debug)

    # Merge HEAD variable with JSX Head blocks and layout head blocks
    from pyxle.devserver.registry import find_layout_head_jsx_blocks
    from pyxle.ssr.head_merger import merge_head_elements

    layout_head_jsx_blocks = find_layout_head_jsx_blocks(settings, page.source_relative_path)

    # Execute layout loaders (if any layout has a @server decorator)
    layout_data = await _execute_layout_loaders(
        settings=settings,
        page=page,
        request=request,
    )

    component_props = _compose_component_props(loader_props, layout_data)
    render_result = await renderer.render(
        page.client_module_path,
        component_props,
        request_pathname=request.url.path,
        csrf_token=_csrf_token_for_request(request),
    )
    body_html = render_result.html
    inline_styles = render_result.inline_styles
    
    # Convert runtime-extracted head elements (from <Head> components) to blocks
    runtime_head_blocks = list(render_result.head_elements)
    
    merged_head_elements = merge_head_elements(
        head_variable=head_elements,
        head_jsx_blocks=page.head_jsx_blocks,
        layout_head_jsx_blocks=layout_head_jsx_blocks,
        runtime_head_blocks=tuple(runtime_head_blocks),
    )
    
    head_markup = render_head_markup(merged_head_elements)

    return PageArtifacts(
        component_props=component_props,
        body_html=body_html,
        head_elements=merged_head_elements,
        head_markup=head_markup,
        inline_styles=inline_styles,
        status_code=status_code,
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
    error_context = _build_error_context(error, status_code)

    try:
        render_result = await renderer.render(
            boundary_page.client_module_path,
            {"error": error_context},
            request_pathname=request.url.path,
            csrf_token=_csrf_token_for_request(request),
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
        )
        return HTMLResponse(document, status_code=status_code)
    except Exception:
        # If the error boundary itself fails, let the caller fall back to the
        # default error document — we must not enter an infinite error loop.
        return None


def _build_error_context(error: BaseException, status_code: int) -> dict[str, Any]:
    """Build the error context dict passed as component props to error.pyxl."""
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
