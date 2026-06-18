"""Utilities for loading user-provided middleware hooks."""

from __future__ import annotations

import importlib
import inspect
from typing import Iterable, List, Mapping

from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware


class MiddlewareHookError(RuntimeError):
    """Raised when a middleware specification cannot be resolved."""


def load_custom_middlewares(specs: Iterable[str]) -> List[Middleware]:
    """Resolve middleware specifications into Starlette ``Middleware`` objects."""

    loaded: List[Middleware] = []
    for spec in specs:
        loaded.append(_load_single_middleware(spec))
    return loaded


def find_base_http_middlewares(middlewares: Iterable[Middleware]) -> List[str]:
    """Return the names of configured ``BaseHTTPMiddleware`` subclasses.

    Starlette's :class:`~starlette.middleware.base.BaseHTTPMiddleware` buffers
    the full response before passing it downstream, so it is **incompatible with
    streaming SSR**: when a ``<Suspense>`` boundary defers, the streamed
    ``StreamingResponse`` never produces a buffered body and Starlette raises
    ``RuntimeError: No response returned.`` We surface the offending class names
    at startup (see :func:`create_starlette_app`) when the build also has
    streaming-eligible routes, pointing users at the pure-ASGI pattern.
    """
    names: List[str] = []
    for middleware in middlewares:
        cls = getattr(middleware, "cls", None)
        if inspect.isclass(cls) and issubclass(cls, BaseHTTPMiddleware):
            names.append(cls.__name__)
    return names


def _load_single_middleware(spec: str) -> Middleware:
    module_name, separator, attribute = spec.partition(":")
    if not module_name or separator == "" or not attribute:
        raise MiddlewareHookError(
            "Middleware specifications must be of the form 'module:attribute'."
        )

    from pyxle.devserver._security import validate_python_module_path

    if not validate_python_module_path(module_name):
        raise MiddlewareHookError(
            f"Invalid module path '{module_name}'. "
            "Must be a valid dotted Python import path."
        )

    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:  # pragma: no cover - surfaced via tests
        raise MiddlewareHookError(f"Unable to import middleware module '{module_name}'.") from exc

    if not hasattr(module, attribute):
        raise MiddlewareHookError(
            f"Module '{module_name}' does not define attribute '{attribute}' for middleware."
        )

    value = getattr(module, attribute)
    middleware = _coerce_to_middleware(value, spec)
    if middleware is None:
        raise MiddlewareHookError(
            f"Middleware spec '{spec}' did not resolve to a valid Middleware or BaseHTTPMiddleware."
        )
    return middleware


def _coerce_to_middleware(value: object, spec: str) -> Middleware | None:
    if isinstance(value, Middleware):
        return value

    if inspect.isclass(value):
        return Middleware(value)

    if callable(value):  # factory returning middleware or tuple
        produced = value()
        return _coerce_to_middleware(produced, spec)
    if isinstance(value, tuple) and len(value) == 2:
        candidate, options = value
        if (
            inspect.isclass(candidate)
            and issubclass(candidate, BaseHTTPMiddleware)
            and isinstance(options, Mapping)
        ):
            return Middleware(candidate, **options)

    return None


__all__ = [
    "MiddlewareHookError",
    "find_base_http_middlewares",
    "load_custom_middlewares",
]
