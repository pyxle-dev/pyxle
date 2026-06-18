"""Runtime helpers exposed to compiled Pyxle artifacts."""

from __future__ import annotations

from typing import Any, Callable, TypeVar

F = TypeVar("F", bound=Callable[..., Any])


def server(function: F) -> F:
    """Mark a function as a Pyxle loader and return it unchanged.

    The decorator intentionally performs no wrapping so the original coroutine
    signature and attributes remain available to the runtime. It simply tags the
    function for future inspection by attaching ``__pyxle_loader__ = True``.
    """

    setattr(function, "__pyxle_loader__", True)
    return function


def action(function: F) -> F:
    """Mark a function as a Pyxle server action and return it unchanged.

    Server actions are async functions callable from React components via the
    ``useAction`` hook. They receive the full Starlette ``Request`` object and
    must return a JSON-serializable dict. The decorator adds no wrapping — it
    only tags the function with ``__pyxle_action__ = True`` for compiler and
    runtime inspection.

    Raise ``ActionError`` from within an action to return a structured error
    response to the client with a specific HTTP status code.
    """

    setattr(function, "__pyxle_action__", True)
    return function


class ActionError(Exception):
    """Raise from within a ``@action`` function to return a structured error.

    The ``message`` is forwarded to the client. ``status_code`` controls the
    HTTP response status (default 400). ``data`` carries any additional
    JSON-serializable payload included in the error response. ``fields``
    carries per-field validation messages — a map of field path to a list of
    messages — surfaced to the client as ``error.fields`` (see
    :class:`ValidationActionError`).
    """

    def __init__(
        self,
        message: str,
        status_code: int = 400,
        data: dict[str, Any] | None = None,
        *,
        fields: dict[str, list[str]] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.data = data or {}
        self.fields = fields or {}


class ValidationActionError(ActionError):
    """An :class:`ActionError` for request-body validation failures.

    Defaults to HTTP 422 and always carries ``fields`` (a map of field path —
    dotted for nested models, indexed for list items — to a list of human
    messages). Pyxle raises this automatically when an ``@action`` declares a
    Pydantic-typed ``body`` parameter and the request body fails validation;
    you can also raise it yourself for hand-rolled field errors::

        raise ValidationActionError(fields={"email": ["already taken"]})
    """

    def __init__(
        self,
        message: str = "Validation failed",
        *,
        fields: dict[str, list[str]],
        status_code: int = 422,
        data: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message, status_code=status_code, data=data, fields=fields)


class LoaderError(Exception):
    """Raise from a ``@server`` loader to trigger the nearest error boundary.

    When raised, the framework renders the closest ``error.pyxl`` page up the
    directory tree from the current route, passing the error context as props.
    If no ``error.pyxl`` is found, the default error document is used.

    The ``message`` is visible in the rendered error page. ``status_code``
    controls the HTTP response status (default 500). ``data`` carries any
    additional JSON-serializable context passed to the error boundary.
    """

    def __init__(
        self,
        message: str,
        status_code: int = 500,
        data: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.data = data or {}


_INVALIDATE_HEADER = "x-pyxle-invalidate"


def invalidate_routes(response: Any, *urls: str) -> Any:
    """Tell the client router to evict cached nav payloads for ``urls``.

    Call from an ``@action`` handler or an API endpoint when a mutation
    affects a route other than the one the caller is about to navigate
    to. The response gains an ``x-pyxle-invalidate`` header with the
    URLs comma-joined; the client's ``useAction`` / ``<Form>`` + plain
    ``fetch`` callers can opt into reading it to drop the matching
    navigation-cache entries before their next ``navigate()``.

    Usage::

        @action
        async def delete_post(request):
            ...
            response = {"ok": True}
            # Next time the user navigates to /posts, refetch:
            return invalidate_routes(response, "/posts")

    Works on any object Pyxle will serialise — including plain dicts
    (wrapped as JSON, header set by the framework) and Starlette
    :class:`Response` objects (header set directly). Returning the
    response unchanged is fine when no invalidation is needed.
    """
    if not urls:
        return response
    joined = ", ".join(u for u in urls if u)
    if not joined:
        return response

    # Case 1: a Starlette ``Response`` (or anything with ``.headers``
    # that supports item assignment) — set the header directly.
    headers = getattr(response, "headers", None)
    if headers is not None:
        try:
            # ``MutableHeaders`` accepts ``__setitem__``; add to an
            # existing header to preserve earlier invalidations.
            existing = headers.get(_INVALIDATE_HEADER, "")
            headers[_INVALIDATE_HEADER] = (
                f"{existing}, {joined}" if existing else joined
            )
            return response
        except (TypeError, AttributeError):
            pass

    # Case 2: a plain dict — stash the hint on a sentinel key. The
    # framework's action dispatcher pulls this off before serialising
    # and sets the HTTP header on the response. This keeps the user
    # API the same regardless of whether they return a dict or a
    # full Response object.
    if isinstance(response, dict):
        hints = response.pop("__pyxle_invalidate__", [])
        if isinstance(hints, str):
            hints = [hints]
        hints = list(hints) + [u for u in urls if u]
        response["__pyxle_invalidate__"] = hints
        return response

    return response


__all__ = [
    "server",
    "action",
    "ActionError",
    "ValidationActionError",
    "LoaderError",
    "invalidate_routes",
]
