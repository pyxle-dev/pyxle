"""Generate an OpenAPI 3.1 document from ``@action`` request models.

For every action route in the project, this imports the compiled server module,
introspects the action's signature for a Pydantic-typed ``body`` parameter (the
same resolution the dispatcher uses), and emits an OpenAPI ``post`` operation —
with the model's JSON Schema as the request body and a structured ``422``
validation-error response. Actions without a body model get a permissive
object request body.

The authoritative source is runtime introspection (not compiler metadata), so
the schema always matches what the dispatcher actually validates. Pydantic is
the optional ``[pydantic]`` extra; this module imports it lazily and raises
:class:`PydanticNotInstalledError` if it's absent.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pyxle.devserver.settings import DevServerSettings
from pyxle.devserver.validation import (
    PydanticNotInstalledError,
    _try_import_pydantic,
    resolve_body_model,
)

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
    """Build the OpenAPI 3.1 document for every ``@action`` in the project."""
    if _try_import_pydantic() is None:
        raise PydanticNotInstalledError()

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

        operation = _build_operation(route, action_fn, schemas)
        paths.setdefault(route.path, {})["post"] = operation

    document = {
        "openapi": "3.1.0",
        "info": {"title": title, "version": version},
        "paths": dict(sorted(paths.items())),
        "components": {"schemas": dict(sorted(schemas.items()))},
    }
    return OpenApiResult(document=document, import_errors=import_errors)


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
