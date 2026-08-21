"""Pydantic request-body validation for ``@action`` handlers.

When an action declares a second parameter type-hinted with a Pydantic model::

    @action
    async def update_name(request, body: UpdateNameRequest):
        ...  # body is a validated UpdateNameRequest instance

the dispatcher parses the JSON request body, validates it into the model, and
passes the instance to the handler. A validation failure becomes a structured
``422`` response (see :func:`translate_validation_error`).

All Pydantic coupling lives here so the dispatcher stays thin and
``pyxle.runtime`` stays zero-dependency. Pydantic is the optional ``[pydantic]``
extra: it is imported **lazily**, the first time a model needs resolving or
validating, so apps that never type-hint a body pay nothing. Actions without a
model-typed parameter are dispatched exactly as before.
"""

from __future__ import annotations

import inspect
import typing
from dataclasses import dataclass
from typing import Any, Callable
from weakref import WeakKeyDictionary

# Re-exported: these moved to ``pyxle.runtime`` so ``pyxle check`` can reach the
# same message without importing the devserver (and Starlette with it). Imported
# here by name so every existing ``from pyxle.devserver.validation import ...``
# keeps working and catches the identical class object.
from pyxle.runtime import (
    ActionBodyError,
    PydanticNotInstalledError,
    UnannotatedActionBodyError,
    ValidationActionError,
)

try:  # PEP 604 ``X | None`` unions (Python 3.10+)
    from types import UnionType as _UnionType
except ImportError:  # pragma: no cover - 3.9 and earlier
    _UnionType = None  # type: ignore[assignment]


@dataclass(frozen=True, slots=True)
class ResolvedBody:
    """The body parameter of an action: the argument name and its model class."""

    param_name: str
    model: type


def _try_import_pydantic() -> Any | None:
    try:
        import pydantic
    except ImportError:
        return None
    return pydantic


def _unwrap_annotation(annotation: Any) -> Any:
    """Strip ``Annotated[...]`` and ``Optional``/``X | None`` to the inner type."""
    # Annotated[X, ...] -> X
    if getattr(annotation, "__metadata__", None) is not None:
        annotation = typing.get_args(annotation)[0]
    origin = typing.get_origin(annotation)
    if origin is typing.Union or (_UnionType is not None and origin is _UnionType):
        non_none = [a for a in typing.get_args(annotation) if a is not type(None)]
        if len(non_none) == 1:
            annotation = non_none[0]
            if getattr(annotation, "__metadata__", None) is not None:
                annotation = typing.get_args(annotation)[0]
    return annotation


def resolve_body_model(action_fn: Callable[..., Any]) -> ResolvedBody | None:
    """Find an action's Pydantic body parameter, or ``None``.

    The body is the first parameter other than ``request`` (the ``@action``
    contract names the first argument ``request``). Returns a
    :class:`ResolvedBody` when that parameter is annotated with a
    ``pydantic.BaseModel`` subclass (unwrapping ``Annotated`` / ``Optional``),
    or ``None`` for a legacy ``async def act(request)`` action.

    Raises :class:`UnannotatedActionBodyError` when that parameter is required
    but carries no annotation, and :class:`PydanticNotInstalledError` when it
    *is* annotated but Pydantic is missing. The annotation is read first
    precisely so the two cannot be confused: an unannotated parameter does not
    need Pydantic, and blaming Pydantic for it sends the reader to install a
    dependency that leaves them with the same failure.
    """
    signature = inspect.signature(action_fn)
    body_params = [
        param
        for param in signature.parameters.values()
        if param.name not in ("request", "self", "cls")
        and param.kind
        in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
    ]
    if not body_params:
        return None
    body_param = body_params[0]

    # Read the annotation before asking whether Pydantic is installed. Whether
    # Pydantic is needed at all is a property of the annotation, so deciding in
    # the other order produces a confident, wrong diagnosis for the commonest
    # mistake — a second parameter nobody annotated.
    try:
        hints = typing.get_type_hints(action_fn, include_extras=True)
    except Exception:
        hints = getattr(action_fn, "__annotations__", {})
    annotation = hints.get(body_param.name, body_param.annotation)

    if annotation is inspect.Parameter.empty:
        # Nothing to build a model from. An optional parameter simply keeps its
        # default; a required one cannot be filled, and saying so beats letting
        # the call fail later with a bare TypeError.
        if body_param.default is inspect.Parameter.empty:
            raise UnannotatedActionBodyError(param=body_param.name)
        return None

    pydantic = _try_import_pydantic()
    if pydantic is None:
        # Annotated, so the author does mean to validate — the install hint is
        # now true. An optional parameter still degrades to its default.
        if body_param.default is inspect.Parameter.empty:
            raise PydanticNotInstalledError()
        return None

    model = _unwrap_annotation(annotation)
    if isinstance(model, type) and issubclass(model, pydantic.BaseModel):
        return ResolvedBody(param_name=body_param.name, model=model)
    return None


# Introspection is cached per function object. In debug mode each request
# re-imports the page module, producing a fresh function object — the
# WeakKeyDictionary auto-evicts the old entry when that object is collected, so
# debug never serves a stale model and production introspects once per process.
_RESOLVE_CACHE: WeakKeyDictionary = WeakKeyDictionary()
_UNSET: Any = object()


def get_cached_body_model(action_fn: Callable[..., Any]) -> ResolvedBody | None:
    """:func:`resolve_body_model`, memoised on the function object."""
    try:
        cached = _RESOLVE_CACHE.get(action_fn, _UNSET)
    except TypeError:
        # Not weakly referenceable (exotic callable) — resolve without caching.
        return resolve_body_model(action_fn)
    if cached is not _UNSET:
        return cached
    resolved = resolve_body_model(action_fn)
    _RESOLVE_CACHE[action_fn] = resolved
    return resolved


def translate_validation_error(exc: Any) -> dict[str, list[str]]:
    """Map a ``pydantic.ValidationError`` to ``{field path: [messages]}``.

    Nested model fields use dotted paths (``address.zip``); list items use
    index paths (``tags.0``). A whole-body failure uses the key ``__root__``.
    Only field locations and messages are surfaced — never the input values.
    """
    fields: dict[str, list[str]] = {}
    for error in exc.errors():
        loc = error.get("loc", ())
        key = ".".join(str(part) for part in loc) or "__root__"
        fields.setdefault(key, []).append(str(error.get("msg", "Invalid value")))
    return fields


def validate_body(model: type, payload: Any) -> Any:
    """Validate ``payload`` into ``model``, raising :class:`ValidationActionError`.

    Pydantic is guaranteed available here (a :class:`ResolvedBody` was produced
    with it). A validation failure becomes a ``422`` with field-level messages.
    """
    import pydantic

    try:
        return model.model_validate(payload)
    except pydantic.ValidationError as exc:
        raise ValidationActionError(fields=translate_validation_error(exc)) from exc


__all__ = [
    "ActionBodyError",
    "PydanticNotInstalledError",
    "UnannotatedActionBodyError",
    "ResolvedBody",
    "resolve_body_model",
    "get_cached_body_model",
    "translate_validation_error",
    "validate_body",
]
