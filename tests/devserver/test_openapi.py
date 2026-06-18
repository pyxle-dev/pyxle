"""Tests for OpenAPI document generation from @action request models."""

from __future__ import annotations

from pathlib import Path

import pytest

from pyxle.devserver.builder import build_once
from pyxle.devserver.openapi import build_openapi_document
from pyxle.devserver.settings import DevServerSettings
from pyxle.devserver.validation import PydanticNotInstalledError


def _project(tmp_path: Path) -> DevServerSettings:
    root = tmp_path / "project"
    (root / "pages").mkdir(parents=True)
    (root / "public").mkdir()
    return DevServerSettings.from_project_root(root)


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


_SIGNUP_PAGE = """from pyxle.runtime import action
from pydantic import BaseModel

class Address(BaseModel):
    zip: str

class Signup(BaseModel):
    email: str
    age: int
    address: Address

@action
async def register(request, body: Signup):
    return {"ok": True}

import React from 'react';
export default function Signup() { return <div/>; }
"""


def test_build_openapi_document_for_validated_action(tmp_path: Path) -> None:
    settings = _project(tmp_path)
    _write(settings.pages_dir / "signup.pyxl", _SIGNUP_PAGE)
    build_once(settings)

    result = build_openapi_document(settings, title="Test API", version="9.9")
    doc = result.document

    assert result.import_errors == []
    assert doc["openapi"] == "3.1.0"
    assert doc["info"] == {"title": "Test API", "version": "9.9"}

    post = doc["paths"]["/api/__actions/signup/register"]["post"]
    ref = post["requestBody"]["content"]["application/json"]["schema"]["$ref"]
    assert ref == "#/components/schemas/Signup"
    assert post["requestBody"]["required"] is True
    assert "422" in post["responses"]
    assert "200" in post["responses"]

    schemas = doc["components"]["schemas"]
    assert "email" in schemas["Signup"]["properties"]
    # The nested model's $defs were lifted into components.schemas.
    assert "Address" in schemas
    assert "zip" in schemas["Address"]["properties"]
    # The shared validation-error schema is always present.
    assert "PyxleValidationError" in schemas


def test_action_without_model_gets_permissive_body(tmp_path: Path) -> None:
    settings = _project(tmp_path)
    _write(
        settings.pages_dir / "plain.pyxl",
        """from pyxle.runtime import action

@action
async def ping(request):
    return {"pong": True}

import React from 'react';
export default function Plain() { return <div/>; }
""",
    )
    build_once(settings)

    post = build_openapi_document(settings).document["paths"][
        "/api/__actions/plain/ping"
    ]["post"]
    schema = post["requestBody"]["content"]["application/json"]["schema"]
    assert schema == {"type": "object"}
    assert "422" not in post["responses"]


def test_unimportable_module_is_reported_not_fatal(tmp_path: Path) -> None:
    settings = _project(tmp_path)
    _write(
        settings.pages_dir / "broken.pyxl",
        """import a_module_that_does_not_exist  # noqa
from pyxle.runtime import action

@action
async def go(request):
    return {}

import React from 'react';
export default function Broken() { return <div/>; }
""",
    )
    build_once(settings)

    result = build_openapi_document(settings)
    assert len(result.import_errors) == 1
    assert "broken" in result.import_errors[0]
    # The doc is still produced (just without the broken page's operation).
    assert "/api/__actions/broken/go" not in result.document["paths"]


def test_pydantic_absent_raises(tmp_path: Path, monkeypatch) -> None:
    from pyxle.devserver import openapi

    settings = _project(tmp_path)
    _write(settings.pages_dir / "signup.pyxl", _SIGNUP_PAGE)
    build_once(settings)

    # Patch the name bound in the openapi module so its own fail-fast guard
    # (not just validation's deeper check) is exercised.
    monkeypatch.setattr(openapi, "_try_import_pydantic", lambda: None)
    with pytest.raises(PydanticNotInstalledError):
        build_openapi_document(settings)


def test_catchall_action_route_is_skipped(tmp_path: Path) -> None:
    """A catch-all page registers an extra catch-all action route that
    duplicates the concrete path; the document must include the concrete
    operation and skip the ``{_pyxle_action_path}`` catch-all."""
    settings = _project(tmp_path)
    _write(
        settings.pages_dir / "docs" / "[[...slug]].pyxl",
        """from pyxle.runtime import action
from pydantic import BaseModel

class Comment(BaseModel):
    text: str

@action
async def comment(request, body: Comment):
    return {"ok": True}

import React from 'react';
export default function Doc() { return <div/>; }
""",
    )
    build_once(settings)

    result = build_openapi_document(settings)
    assert result.import_errors == []
    paths = result.document["paths"]
    # The concrete action operation is present once...
    assert any(p.endswith("/comment") for p in paths)
    # ...and no catch-all ``{_pyxle_action_path}`` operation leaked in.
    assert not any("_pyxle_action_path" in p for p in paths)


def test_module_without_the_named_action_is_skipped(
    tmp_path: Path, monkeypatch
) -> None:
    """If a compiled module imports but doesn't expose the registered action
    as a real ``@action`` (e.g. it was renamed in source), that route is
    skipped rather than producing a bogus operation."""
    import pyxle.devserver.starlette_app as app_module

    settings = _project(tmp_path)
    _write(settings.pages_dir / "signup.pyxl", _SIGNUP_PAGE)
    build_once(settings)

    # ``build_openapi_document`` imports ``_import_module`` lazily from
    # starlette_app on each call, so patching it there takes effect.
    original = app_module._import_module

    def _strip_marker(module_key, module_path, *, debug=False):
        module = original(module_key, module_path, debug=debug)
        # Replace the action with a plain function (no __pyxle_action__).
        module.register = lambda request: {"ok": True}
        return module

    monkeypatch.setattr(app_module, "_import_module", _strip_marker)

    result = build_openapi_document(settings)
    assert result.import_errors == []
    assert "/api/__actions/signup/register" not in result.document["paths"]
