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


class ActionCookies:
    """Cookies an ``@action`` asked for, applied to the response it produced.

    An action returns a dict, not a response, so it has nothing to call
    ``set_cookie`` on — which used to mean anything that had to set one
    (a session, a preference, a consent record) had to be written as an
    API route instead, splitting a page's own mutations across two
    places. The dispatcher exposes this recorder on ``request.state`` and
    applies it to the response it builds::

        @action
        async def choose_theme(request, body: "ThemeBody"):
            request.state.cookies.set(
                "theme", body.theme, max_age=31536000, samesite="lax"
            )
            return {"theme": body.theme}

    The arguments are Starlette's ``Response.set_cookie`` /
    ``delete_cookie`` arguments, unchanged and unvalidated here — this
    records the call and replays it, so the framework never becomes a
    second, staler copy of that signature.

    Cookies are applied to a successful response and to an
    :class:`ActionError` (both are the action's own answer), and not to
    an unexpected 500, where the action's intent is unknown.
    """

    __slots__ = ("_calls",)

    def __init__(self) -> None:
        self._calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    def set(self, *args: Any, **kwargs: Any) -> None:
        """Record a ``Response.set_cookie(...)`` call."""
        self._calls.append(("set_cookie", args, kwargs))

    def delete(self, *args: Any, **kwargs: Any) -> None:
        """Record a ``Response.delete_cookie(...)`` call."""
        self._calls.append(("delete_cookie", args, kwargs))

    def __bool__(self) -> bool:
        return bool(self._calls)

    def apply(self, response: Any) -> Any:
        """Replay the recorded calls onto *response*."""
        for method, args, kwargs in self._calls:
            getattr(response, method)(*args, **kwargs)
        return response


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



# ---------------------------------------------------------------------------
# The ``@action`` body-parameter contract
#
# These live beside ``action`` itself rather than in the devserver because the
# rule they describe is a property of the decorator's signature, and two
# consumers now need it: the dispatcher, which resolves the model from a live
# function object, and ``pyxle check``, which reads the same shape off the AST
# without importing user code. One writer of the message, two readers.
# ---------------------------------------------------------------------------


class ActionBodyError(RuntimeError):
    """Pyxle cannot work out how to supply an action's body parameter.

    Two shapes, one base so a caller cannot catch one and miss the other: the
    parameter is annotated with a model but Pydantic is missing
    (:class:`PydanticNotInstalledError`), or it carries no annotation at all,
    so there is nothing to build a model from
    (:class:`UnannotatedActionBodyError`). Every caller only surfaces
    ``str(exc)``; the base exists to keep their ``except`` clauses honest, not
    to carry behaviour.
    """

    def __init__(
        self, message: str, *, action: str | None = None, source: str | None = None
    ) -> None:
        super().__init__(message)
        self.action = action
        self.source = source

    def with_identity(self, *, action: str, source: str | None) -> "ActionBodyError":
        """Return the same failure, naming the action and the file to edit.

        The dispatcher raises these bare — reached while handling a request for
        one specific action, "this action" is unambiguous. Schema generation
        walks every action in the project, so it re-raises through this to say
        which file.
        """
        raise NotImplementedError  # pragma: no cover - subclasses implement


class PydanticNotInstalledError(ActionBodyError):
    """An action needs Pydantic to validate its body, but it isn't installed.

    Raised only when the body parameter **is** annotated: an unannotated one
    does not need Pydantic to begin with, and saying otherwise sends the reader
    to install a dependency that will not fix their problem — see
    :class:`UnannotatedActionBodyError`.
    """

    def __init__(self, *, action: str | None = None, source: str | None = None) -> None:
        if action is None:
            subject = "This action validates"
        else:
            where = f" in {source}" if source else ""
            subject = f"Action '{action}'{where} validates"
        super().__init__(
            f"{subject} its request body with a Pydantic model, but "
            "Pydantic is not installed. Install it with: "
            "pip install 'pyxle-framework[pydantic]'.",
            action=action,
            source=source,
        )

    def with_identity(self, *, action: str, source: str | None) -> "PydanticNotInstalledError":
        return PydanticNotInstalledError(action=action, source=source)


class UnannotatedActionBodyError(ActionBodyError):
    """An action requires a parameter it never described, so nothing can fill it.

    ``async def act(request, payload)`` asks Pyxle to supply ``payload`` from
    the request body while saying nothing about its shape. Installing Pydantic
    does not help — with Pydantic present the same call fails with
    ``TypeError: act() missing 1 required positional argument`` — so the message
    names the two things that do.
    """

    def __init__(
        self, *, param: str, action: str | None = None, source: str | None = None
    ) -> None:
        if action is None:
            subject = "This action"
        else:
            where = f" in {source}" if source else ""
            subject = f"Action '{action}'{where}"
        super().__init__(
            f"{subject} requires a parameter '{param}' that Pyxle would have to "
            f"supply from the request body, but '{param}' has no type "
            "annotation, so there is nothing to build a request model from. "
            "Either annotate it with a Pydantic model (and install Pydantic "
            "with: pip install 'pyxle-framework[pydantic]'), or take only "
            "'request' and read the body yourself with: await request.json().",
            action=action,
            source=source,
        )
        self.param = param

    def with_identity(self, *, action: str, source: str | None) -> "UnannotatedActionBodyError":
        return UnannotatedActionBodyError(param=self.param, action=action, source=source)

__all__ = [
    "server",
    "action",
    "ActionCookies",
    "ActionError",
    "ValidationActionError",
    "LoaderError",
    "invalidate_routes",
    "ActionBodyError",
    "PydanticNotInstalledError",
    "UnannotatedActionBodyError",
]
