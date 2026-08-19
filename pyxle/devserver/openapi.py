"""Generate an OpenAPI 3.1 document from ``@action`` request models.

For every action route in the project, this imports the compiled server module,
introspects the action's signature for a Pydantic-typed ``body`` parameter (the
same resolution the dispatcher uses), and emits an OpenAPI ``post`` operation —
with the model's JSON Schema as the request body and a structured ``422``
validation-error response. Actions without a body model get a permissive
object request body.

The authoritative source is runtime introspection (not compiler metadata), so
the schema always matches what the dispatcher actually validates.

Pydantic is the optional ``[pydantic]`` extra, and it is only needed for the
actions that actually declare a model body: a project whose actions take no
body — or which has no actions at all — generates its document without it, and
an empty ``paths`` object is the correct answer for a project with no actions.
:class:`PydanticNotInstalledError` is raised only when a specific action needs
a model resolved and Pydantic is absent; it names that action and its file.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pyxle.devserver.settings import DevServerSettings
from pyxle.devserver.validation import (
    PydanticNotInstalledError,
    resolve_body_model,
)

if TYPE_CHECKING:  # imported lazily at runtime to avoid a devserver import cycle
    from pyxle.devserver.routes import ActionRoute

# A reusable schema for the structured validation-error body the dispatcher
# returns (see pyxle.devserver.validation). Referenced from every validated
# action's 422 response.
_VALIDATION_ERROR_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "ok": {"type": "boolean", "const": False},
        "error": {"type": "string"},
        "fields": {
            "type": "object",
            "description": "Field path -> list of validation messages.",
            "additionalProperties": {"type": "array", "items": {"type": "string"}},
        },
    },
    "required": ["ok", "error", "fields"],
}


@dataclass(frozen=True, slots=True)
class OpenApiResult:
    """The generated document plus any per-module import failures."""

    document: dict[str, Any]
    import_errors: list[str] = field(default_factory=list)


def build_openapi_document(
    settings: DevServerSettings,
    *,
    title: str = "Pyxle API",
    version: str = "0.1.0",
) -> OpenApiResult:
    """Build the OpenAPI 3.1 document for every ``@action`` in the project.

    Raises :class:`PydanticNotInstalledError` only if an action declares a
    model-typed body and Pydantic is missing — the document for a project that
    needs no models is generated either way.
    """
    # Imported here (not at module top) to avoid a devserver import cycle.
    from pyxle.devserver.registry import load_metadata_registry
    from pyxle.devserver.routes import build_route_table
    from pyxle.devserver.starlette_app import _import_module

    registry = load_metadata_registry(settings)
    table = build_route_table(registry)

    paths: dict[str, Any] = {}
    schemas: dict[str, Any] = {"PyxleValidationError": _VALIDATION_ERROR_SCHEMA}
    import_errors: list[str] = []

    for route in table.actions:
        if route.is_catchall:
            # The catch-all route duplicates every concrete action path.
            continue
        try:
            module = _import_module(
                route.module_key, route.server_module_path, debug=False
            )
        except Exception as exc:  # one broken page must not abort the whole doc
            import_errors.append(f"{route.page_path} ({route.action_name}): {exc}")
            continue

        action_fn = getattr(module, route.action_name, None)
        if action_fn is None or not getattr(action_fn, "__pyxle_action__", False):
            continue

        try:
            operation = _build_operation(route, action_fn, schemas)
        except PydanticNotInstalledError as exc:
            # Re-raised with this route's identity: the deeper raise knows only
            # that *an* action needs a model, and a project-wide walk has to say
            # which file to edit.
            raise PydanticNotInstalledError(
                action=route.action_name,
                source=_source_label(settings, route),
            ) from exc
        paths.setdefault(route.path, {})["post"] = operation

    document = {
        "openapi": "3.1.0",
        "info": {"title": title, "version": version},
        "paths": dict(sorted(paths.items())),
        "components": {"schemas": dict(sorted(schemas.items()))},
    }
    return OpenApiResult(document=document, import_errors=import_errors)


def _source_label(settings: DevServerSettings, route: ActionRoute) -> str | None:
    """The route's source file, written the way the user typed it.

    Returns a project-relative path (``pages/signup.pyxl``), the absolute path
    when the page lives outside the project root, or ``None`` for a route
    carrying no source — the error then omits the location rather than printing
    the ``Path(".")`` placeholder.
    """
    absolute = route.source_absolute_path
    if absolute == Path("."):
        return None
    try:
        return absolute.relative_to(settings.project_root).as_posix()
    except ValueError:
        return absolute.as_posix()


def _operation_id(path: str) -> str:
    return (
        path.replace("/api/__actions/", "")
        .replace("/", "_")
        .replace("{", "")
        .replace("}", "")
    )


def _build_operation(
    route: Any, action_fn: Any, schemas: dict[str, Any]
) -> dict[str, Any]:
    operation: dict[str, Any] = {
        "operationId": _operation_id(route.path),
        "summary": f"{route.action_name} ({route.page_path})",
        "tags": [route.page_path],
        "responses": {
            "200": {
                "description": "Action succeeded",
                "content": {"application/json": {"schema": {"type": "object"}}},
            }
        },
    }

    resolved = resolve_body_model(action_fn)
    if resolved is None:
        operation["requestBody"] = {
            "content": {"application/json": {"schema": {"type": "object"}}}
        }
        return operation

    model_name = resolved.model.__name__
    schema = resolved.model.model_json_schema(
        ref_template="#/components/schemas/{model}"
    )
    # Pydantic nests referenced models under ``$defs``; lift them into the
    # shared components so the $refs (which the ref_template points at
    # components/schemas) resolve.
    for def_name, def_schema in schema.pop("$defs", {}).items():
        schemas.setdefault(def_name, def_schema)
    schemas[model_name] = schema

    operation["requestBody"] = {
        "required": True,
        "content": {
            "application/json": {
                "schema": {"$ref": f"#/components/schemas/{model_name}"}
            }
        },
    }
    operation["responses"]["422"] = {
        "description": "Request body failed validation",
        "content": {
            "application/json": {
                "schema": {"$ref": "#/components/schemas/PyxleValidationError"}
            }
        },
    }
    return operation


__all__ = ["OpenApiResult", "build_openapi_document"]
