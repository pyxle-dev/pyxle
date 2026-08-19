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


def test_pydantic_absent_raises_naming_the_action_and_file(
    tmp_path: Path, monkeypatch
) -> None:
    """Pydantic is genuinely required here — the page's action declares a model
    body — so the command may fail, but the message has to say which action and
    which file, not just "this action"."""
    from pyxle.devserver import validation

    settings = _project(tmp_path)
    _write(settings.pages_dir / "account" / "signup.pyxl", _SIGNUP_PAGE)
    build_once(settings)

    monkeypatch.setattr(validation, "_try_import_pydantic", lambda: None)
    with pytest.raises(PydanticNotInstalledError) as excinfo:
        build_openapi_document(settings)

    message = str(excinfo.value)
    assert "Action 'register' in pages/account/signup.pyxl" in message
    assert "pip install 'pyxle-framework[pydantic]'" in message
    assert excinfo.value.action == "register"
    assert excinfo.value.source == "pages/account/signup.pyxl"


def test_project_with_no_actions_needs_no_pydantic(
    tmp_path: Path, monkeypatch
) -> None:
    """The pristine-scaffold case: a project with no ``@action`` anywhere has
    nothing to validate, so the document generates without Pydantic and
    ``paths`` is legitimately empty."""
    from pyxle.devserver import validation

    settings = _project(tmp_path)
    # Unique page name: compiled modules are cached in ``sys.modules`` under a
    # key derived from the page path alone, so a name shared with another test
    # would hand this one that test's already-imported module.
    _write(
        settings.pages_dir / "actionless_home.pyxl",
        """import React from 'react';
export default function Home() { return <div/>; }
""",
    )
    build_once(settings)

    monkeypatch.setattr(validation, "_try_import_pydantic", lambda: None)
    result = build_openapi_document(settings)

    assert result.import_errors == []
    assert result.document["paths"] == {}
    assert result.document["openapi"] == "3.1.0"


def test_action_without_a_model_needs_no_pydantic(
    tmp_path: Path, monkeypatch
) -> None:
    """An action that takes no body needs nothing from Pydantic, so its
    operation is emitted with the permissive body even when it's absent."""
    from pyxle.devserver import validation

    settings = _project(tmp_path)
    _write(
        settings.pages_dir / "bodyless_action.pyxl",
        """from pyxle.runtime import action

@action
async def ping(request):
    return {"pong": True}

import React from 'react';
export default function Plain() { return <div/>; }
""",
    )
    build_once(settings)

    monkeypatch.setattr(validation, "_try_import_pydantic", lambda: None)
    result = build_openapi_document(settings)

    post = result.document["paths"]["/api/__actions/bodyless_action/ping"]["post"]
    assert post["requestBody"]["content"]["application/json"]["schema"] == {
        "type": "object"
    }


def test_source_label_falls_back_for_a_route_without_a_source(
    tmp_path: Path,
) -> None:
    """Directly-constructed routes carry the ``Path(".")`` default; the error
    omits the location rather than printing the placeholder. A page outside the
    project root keeps its absolute path."""
    from pyxle.devserver.openapi import _source_label
    from pyxle.devserver.routes import ActionRoute

    settings = _project(tmp_path)
    bare = ActionRoute(
        path="/api/__actions/x/go",
        page_path="/x",
        action_name="go",
        server_module_path=Path("x.py"),
        module_key="x",
    )
    assert _source_label(settings, bare) is None

    outside = tmp_path / "elsewhere" / "page.pyxl"
    external = ActionRoute(
        path="/api/__actions/x/go",
        page_path="/x",
        action_name="go",
        server_module_path=Path("x.py"),
        module_key="x",
        source_absolute_path=outside,
    )
    assert _source_label(settings, external) == outside.as_posix()


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
