"""Route-level middleware hooks and default policies for Pyxle."""

from __future__ import annotations

import functools
import importlib
import inspect
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable, Iterable, List, Literal, Sequence

from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

RouteHookCallable = Callable[
    ["RouteContext", Request, Callable[[Request], Awaitable[Response]]],
    Awaitable[Response],
]


class RouteHookError(RuntimeError):
    """Raised when a route middleware specification cannot be resolved."""


class RouteHook:
    """Base class that makes it convenient to express lifecycle hooks."""

    async def on_pre_call(self, request: Request, context: "RouteContext") -> None:
        return None

    async def on_post_call(
        self, request: Request, response: Response, context: "RouteContext"
    ) -> None:
        return None

    async def on_error(
        self, request: Request, context: "RouteContext", exc: Exception
    ) -> None:
        return None




@dataclass(frozen=True, slots=True)
class RouteContext:
    """Metadata describing the current route for policy enforcement."""

    target: Literal["page", "api", "action"]
    path: str
    source_relative_path: Path
    source_absolute_path: Path
    module_key: str
    content_hash: str
    has_loader: bool = False
    head_elements: tuple[str, ...] = ()
    allowed_methods: tuple[str, ...] = ("GET",)

    def as_dict(self) -> dict[str, object]:
        return {
            "target": self.target,
            "path": self.path,
            "source": self.source_relative_path.as_posix(),
            "module": self.module_key,
            "contentHash": self.content_hash,
            "hasLoader": self.has_loader,
            "head": list(self.head_elements),
            "allowedMethods": list(self.allowed_methods),
        }


def load_route_hooks(specs: Iterable[str]) -> List[RouteHookCallable]:
    """Resolve module specifications into async route hook callables."""

    loaded: List[RouteHook] = []
    for spec in specs:
        loaded.append(_load_single_hook(spec))
    return loaded


def _load_single_hook(spec: str) -> RouteHookCallable:
    module_name, separator, attribute = spec.partition(":")
    if not module_name or separator == "" or not attribute:
        raise RouteHookError(
            "Route middleware specifications must be of the form 'module:attribute'."
        )

    from pyxle.devserver._security import validate_python_module_path

    if not validate_python_module_path(module_name):
        raise RouteHookError(
            f"Invalid module path '{module_name}'. "
            "Must be a valid dotted Python import path."
        )

    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:  # pragma: no cover - surfaced via unit tests
        raise RouteHookError(f"Unable to import route middleware module '{module_name}'.") from exc

    if not hasattr(module, attribute):
        raise RouteHookError(
            f"Module '{module_name}' does not define attribute '{attribute}' for route middleware."
        )

    candidate = getattr(module, attribute)
    hook = _resolve_route_hook(candidate, spec)
    if hook is not None:
        return hook

    raise RouteHookError(
        f"Route middleware spec '{spec}' did not resolve to an async callable accepting (context, request, call_next)."
    )


def _resolve_route_hook(value: object, spec: str) -> RouteHookCallable | None:
    if inspect.isclass(value):
        try:
            instance = value()  # type: ignore[call-arg]
        except Exception as exc:  # pragma: no cover - defensive guard
            raise RouteHookError(f"Failed to construct route hook '{value.__name__}': {exc}") from exc
        return _resolve_route_hook(instance, spec)

    if inspect.iscoroutinefunction(value):
        return value  # type: ignore[return-value]

    lifecycle = _wrap_lifecycle_hook(value)
    if lifecycle is not None:
        return lifecycle

    call_method = _get_async_call_method(value)
    if call_method is not None:
        async def _wrapped(context, request, call_next):
            return await call_method(context, request, call_next)

        return _wrapped

    if callable(value):
        produced = value()  # type: ignore[call-arg]
        return _resolve_route_hook(produced, spec)

    return None


def _get_async_call_method(value: object) -> Callable[["RouteContext", Request, Callable[[Request], Awaitable[Response]]], Awaitable[Response]] | None:
    call_method = getattr(value, "__call__", None)
    if call_method is not None and inspect.iscoroutinefunction(call_method):
        return call_method  # type: ignore[return-value]
    return None


def _wrap_lifecycle_hook(value: object) -> RouteHookCallable | None:
    on_pre = getattr(value, "on_pre_call", None)
    on_post = getattr(value, "on_post_call", None)
    on_error = getattr(value, "on_error", None)

    if not any(callable(candidate) for candidate in (on_pre, on_post, on_error)):
        return None

    async def _lifecycle(context, request, call_next):
        if callable(on_pre):
            await on_pre(request, context)
        try:
            response = await call_next(request)
        except Exception as exc:
            if callable(on_error):
                await on_error(request, context, exc)
            raise
        if callable(on_post):
            await on_post(request, response, context)
        return response

    return _lifecycle


async def attach_route_metadata(context: RouteContext, request: Request, call_next):
    """Default policy wiring route metadata into the ASGI scope for introspection."""

    state = request.scope.setdefault("pyxle", {})  # type: ignore[assignment]
    state["route"] = context.as_dict()
    return await call_next(request)


async def enforce_allowed_methods(context: RouteContext, request: Request, call_next):
    """Default API policy returning 405 for disallowed HTTP verbs."""

    method = request.method.upper()
    allowed = context.allowed_methods or ("GET",)
    if context.target == "api" and method not in allowed:
        detail = {
            "error": "method_not_allowed",
            "allowed": list(allowed),
            "path": context.path,
        }
        return JSONResponse(detail, status_code=405)
    return await call_next(request)


DEFAULT_PAGE_POLICIES: Sequence[RouteHookCallable] = (attach_route_metadata,)
DEFAULT_API_POLICIES: Sequence[RouteHookCallable] = (attach_route_metadata, enforce_allowed_methods)
# Actions are registered POST-only, so they need no method enforcement — just
# the route-metadata hook. User auth/policy hooks run after this.
DEFAULT_ACTION_POLICIES: Sequence[RouteHookCallable] = (attach_route_metadata,)


def is_async_callable(obj: object) -> bool:
    """Return ``True`` when *obj* is awaited directly by the route chain.

    Mirrors Starlette's own detection: unwraps ``functools.partial`` and
    accepts both coroutine functions and instances with an async
    ``__call__``.
    """

    while isinstance(obj, functools.partial):
        obj = obj.func
    return inspect.iscoroutinefunction(obj) or (
        callable(obj) and inspect.iscoroutinefunction(getattr(obj, "__call__", None))
    )


def ensure_async_handler(handler):
    """Normalize *handler* into an awaitable request→response callable.

    Synchronous handlers are dispatched through Starlette's threadpool so a
    blocking body (a DB driver, a sync SDK call) occupies a worker thread
    instead of freezing the event loop — the same semantics Starlette
    applies to sync endpoints registered without route hooks.
    """

    if is_async_callable(handler):
        return handler

    async def threadpool_handler(request: Request) -> Response:
        return await run_in_threadpool(handler, request)

    threadpool_handler.__name__ = getattr(handler, "__name__", "endpoint")
    return threadpool_handler


def wrap_with_route_hooks(
    handler,
    *,
    hooks: Sequence[RouteHookCallable],
    context: RouteContext,
):
    """Wrap a Starlette handler with the provided route hook chain.

    Synchronous handlers are normalized once at wrap time (see
    :func:`ensure_async_handler`) so the per-request chain stays branch-free.
    """

    if not hooks:
        return handler

    handler = ensure_async_handler(handler)

    async def run_chain(request: Request):
        async def call_next(index: int, current_request: Request):
            if index >= len(hooks):
                return await handler(current_request)
            hook = hooks[index]
            return await hook(context, current_request, lambda req: call_next(index + 1, req))

        return await call_next(0, request)

    run_chain.__name__ = getattr(handler, "__name__", "endpoint")
    return run_chain


__all__ = [
    "DEFAULT_ACTION_POLICIES",
    "DEFAULT_API_POLICIES",
    "DEFAULT_PAGE_POLICIES",
    "RouteContext",
    "RouteHook",
    "RouteHookCallable",
    "RouteHookError",
    "ensure_async_handler",
    "is_async_callable",
    "load_route_hooks",
    "wrap_with_route_hooks",
]
