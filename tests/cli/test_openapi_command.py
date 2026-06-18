"""Tests for the ``pyxle openapi`` CLI command."""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from pyxle.cli import app

runner = CliRunner()


_VALIDATED_PAGE = """from pyxle.runtime import action
from pydantic import BaseModel

class Signup(BaseModel):
    email: str
    age: int

@action
async def register(request, body: Signup):
    return {"ok": True}

import React from 'react';
export default function Signup() { return <div/>; }
"""


def _scaffold(project: Path, page_source: str, *, name: str = "index.pyxl") -> None:
    (project / "pages").mkdir(parents=True)
    (project / "public").mkdir()
    (project / "pages" / name).write_text(page_source, encoding="utf-8")


def test_openapi_prints_document_to_stdout() -> None:
    with runner.isolated_filesystem():
        _scaffold(Path("demo"), _VALIDATED_PAGE)

        result = runner.invoke(app, ["openapi", "demo"], catch_exceptions=False)

        assert result.exit_code == 0
        document = json.loads(result.stdout)
        assert document["openapi"] == "3.1.0"
        post = document["paths"]["/api/__actions/index/register"]["post"]
        ref = post["requestBody"]["content"]["application/json"]["schema"]["$ref"]
        assert ref == "#/components/schemas/Signup"


def test_openapi_writes_to_out_file() -> None:
    with runner.isolated_filesystem():
        _scaffold(Path("demo"), _VALIDATED_PAGE)

        result = runner.invoke(
            app, ["openapi", "demo", "--out", "schema.json"], catch_exceptions=False
        )

        assert result.exit_code == 0
        assert "Wrote OpenAPI schema" in result.stdout
        document = json.loads(Path("schema.json").read_text(encoding="utf-8"))
        assert document["info"]["title"] == "Pyxle API"
        assert "/api/__actions/index/register" in document["paths"]


def test_openapi_out_creates_parent_directories() -> None:
    with runner.isolated_filesystem():
        _scaffold(Path("demo"), _VALIDATED_PAGE)

        result = runner.invoke(
            app,
            ["openapi", "demo", "-o", "nested/dir/schema.json"],
            catch_exceptions=False,
        )

        assert result.exit_code == 0
        assert Path("nested/dir/schema.json").is_file()


def test_openapi_respects_title_and_version_flags() -> None:
    with runner.isolated_filesystem():
        _scaffold(Path("demo"), _VALIDATED_PAGE)

        result = runner.invoke(
            app,
            ["openapi", "demo", "--title", "Acme", "--api-version", "2.5.0"],
            catch_exceptions=False,
        )

        assert result.exit_code == 0
        document = json.loads(result.stdout)
        assert document["info"] == {"title": "Acme", "version": "2.5.0"}


def test_openapi_fails_on_missing_project() -> None:
    result = runner.invoke(app, ["openapi", "nonexistent_dir_xyz"], catch_exceptions=False)
    assert result.exit_code == 1


def test_openapi_reports_config_error() -> None:
    with runner.isolated_filesystem():
        project = Path("demo")
        _scaffold(project, _VALIDATED_PAGE)
        (project / "pyxle.config.json").write_text('{"unknown": 1}', encoding="utf-8")

        result = runner.invoke(app, ["openapi", "demo"], catch_exceptions=False)
        assert result.exit_code == 1


def test_openapi_reports_import_errors() -> None:
    with runner.isolated_filesystem():
        # A unique page name gives this a distinct module key so it can't pick
        # up another test's cached (valid) ``pages.index`` module from sys.modules.
        _scaffold(
            Path("demo"),
            """import a_module_that_does_not_exist  # noqa
from pyxle.runtime import action

@action
async def go(request):
    return {}

import React from 'react';
export default function Broken() { return <div/>; }
""",
            name="broken_import_page.pyxl",
        )

        result = runner.invoke(app, ["openapi", "demo"], catch_exceptions=False)
        assert result.exit_code == 1
        assert "Could not import page module" in result.stdout


def test_openapi_reports_pydantic_not_installed(monkeypatch) -> None:
    from pyxle.devserver import validation

    monkeypatch.setattr(validation, "_try_import_pydantic", lambda: None)
    with runner.isolated_filesystem():
        _scaffold(Path("demo"), _VALIDATED_PAGE)

        result = runner.invoke(app, ["openapi", "demo"], catch_exceptions=False)
        assert result.exit_code == 1
        assert "pydantic" in result.stdout.lower()
