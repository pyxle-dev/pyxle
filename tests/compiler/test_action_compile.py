"""Integration tests for ArtifactWriter with @action pages."""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

from pyxle.compiler.parser import PyxParser, PyxParseResult
from pyxle.compiler.writers import ArtifactWriter


def _make_writer(tmp_path: Path) -> ArtifactWriter:
    build_root = tmp_path / ".pyxle-build"
    return ArtifactWriter(
        build_root=build_root,
        client_root=build_root / "client",
        server_root=build_root / "server",
        metadata_root=build_root / "metadata",
    )


def test_writer_action_only_page(tmp_path: Path) -> None:
    """Page with @action (no @server loader) gets ensure_action_import."""
    source = tmp_path / "pages" / "form.pyxl"
    source.parent.mkdir(parents=True)
    source.write_text(
        dedent(
            """
            from pyxle.runtime import action

            @action
            async def submit(request):
                return {"ok": True}

            import React from 'react';
            export default function Page() { return <form/>; }
            """
        ).strip() + "\n",
        encoding="utf-8",
    )

    writer = _make_writer(tmp_path)
    parse_result = PyxParser().parse(source)
    result = writer.write(
        source_path=source,
        page_relative_path=Path("form.pyxl"),
        route_path="/form",
        alternate_route_paths=None,
        parse_result=parse_result,
    )

    server_code = result.server_output.read_text()
    # action import should be present
    assert "from pyxle.runtime import action" in server_code
    # metadata should list the action
    assert len(result.metadata.actions) == 1
    assert result.metadata.actions[0].name == "submit"


def test_writer_loader_and_action_page(tmp_path: Path) -> None:
    """Page with both @server and @action gets the combined import."""
    source = tmp_path / "pages" / "settings.pyxl"
    source.parent.mkdir(parents=True)
    source.write_text(
        dedent(
            """
            @server
            async def load(request):
                return {}

            @action
            async def save(request):
                return {"saved": True}

            import React from 'react';
            export default function Page() { return <div/>; }
            """
        ).strip() + "\n",
        encoding="utf-8",
    )

    writer = _make_writer(tmp_path)
    parse_result = PyxParser().parse(source)
    result = writer.write(
        source_path=source,
        page_relative_path=Path("settings.pyxl"),
        route_path="/settings",
        alternate_route_paths=None,
        parse_result=parse_result,
    )

    server_code = result.server_output.read_text()
    # Both imports should be satisfied
    assert "server" in server_code
    assert "action" in server_code
    assert len(result.metadata.actions) == 1
    assert result.metadata.actions[0].name == "save"
    assert result.metadata.loader_name == "load"


def test_writer_to_bool_handles_string_attributes(tmp_path: Path) -> None:
    """``ArtifactWriter`` accepts script declarations whose boolean
    attributes arrive as strings (e.g. ``async="async"``) in addition
    to true booleans. Babel's JSX extractor returns boolean attrs as
    booleans most of the time, but when an explicit ``"true"``/``"false"``
    string is used, ``to_bool`` must accept it."""
    source = tmp_path / "pages" / "stringly.pyxl"
    source.parent.mkdir(parents=True)
    source.write_text(
        "import React from 'react';\n"
        "export default function P() { return <div />; }\n",
        encoding="utf-8",
    )

    # Construct a fake parse_result with script declarations using
    # STRING-typed boolean attribute values.
    parse_result = PyxParseResult(
        python_code="",
        jsx_code="export default function P() { return <div />; }\n",
        loader=None,
        python_line_numbers=(),
        jsx_line_numbers=(1,),
        head_elements=(),
        head_is_dynamic=False,
        script_declarations=(
            {
                "src": "https://example.com/sdk.js",
                "strategy": "afterInteractive",
                "async": "true",  # string-shaped boolean
                "defer": "true",
                "module": "false",
                "noModule": "false",
            },
        ),
        image_declarations=(
            {
                "src": "/hero.png",
                "width": "800",
                "height": "400",
                "alt": "Hero",
                "priority": "true",  # string-shaped boolean
                "lazy": "false",
            },
        ),
    )

    writer = _make_writer(tmp_path)
    result = writer.write(
        source_path=source,
        page_relative_path=Path("stringly.pyxl"),
        route_path="/stringly",
        alternate_route_paths=None,
        parse_result=parse_result,
    )

    assert len(result.metadata.scripts) == 1
    assert result.metadata.scripts[0].async_ is True
    assert result.metadata.scripts[0].defer is True
    # QUAL-TOBOOL-1: explicit falsy strings must coerce to False (the old
    # `in ("true", value.lower())` check returned True for every non-empty
    # string, so these silently flipped to True).
    assert result.metadata.scripts[0].module is False
    assert result.metadata.scripts[0].no_module is False
    assert len(result.metadata.images) == 1
    assert result.metadata.images[0].priority is True
    assert result.metadata.images[0].lazy is False


def test_writer_to_bool_falsy_string_forms(tmp_path: Path) -> None:
    """QUAL-TOBOOL-1: ``defer="false"``, ``async="no"``, ``noModule="off"``,
    ``priority="0"`` etc. must all coerce to False, while a bare-presence
    string keeps HTML truthy semantics."""
    source = tmp_path / "pages" / "falsy.pyxl"
    source.parent.mkdir(parents=True)
    source.write_text(
        "export default function P() { return <div />; }\n", encoding="utf-8"
    )

    parse_result = PyxParseResult(
        python_code="",
        jsx_code="export default function P() { return <div />; }\n",
        loader=None,
        python_line_numbers=(),
        jsx_line_numbers=(1,),
        head_elements=(),
        head_is_dynamic=False,
        script_declarations=(
            {
                "src": "https://example.com/a.js",
                "async": "no",
                "defer": "false",
                "module": "0",
                "noModule": "off",
            },
            {"src": "https://example.com/b.js", "defer": "defer"},  # bare presence
        ),
        image_declarations=(
            {"src": "/x.png", "priority": "0", "lazy": "false"},
        ),
    )

    writer = _make_writer(tmp_path)
    result = writer.write(
        source_path=source,
        page_relative_path=Path("falsy.pyxl"),
        route_path="/falsy",
        alternate_route_paths=None,
        parse_result=parse_result,
    )

    first = result.metadata.scripts[0]
    assert first.async_ is False
    assert first.defer is False
    assert first.module is False
    assert first.no_module is False
    # A non-falsy string (HTML attribute presence) is still True.
    assert result.metadata.scripts[1].defer is True
    assert result.metadata.images[0].priority is False
    assert result.metadata.images[0].lazy is False


def test_writer_multiple_actions_in_metadata_json(tmp_path: Path) -> None:
    source = tmp_path / "pages" / "multi.pyxl"
    source.parent.mkdir(parents=True)
    source.write_text(
        dedent(
            """
            from pyxle.runtime import action

            @action
            async def create(request):
                return {}

            @action
            async def remove(request):
                return {}

            import React from 'react';
            export default function Page() { return <div/>; }
            """
        ).strip() + "\n",
        encoding="utf-8",
    )

    import json

    writer = _make_writer(tmp_path)
    parse_result = PyxParser().parse(source)
    result = writer.write(
        source_path=source,
        page_relative_path=Path("multi.pyxl"),
        route_path="/multi",
        alternate_route_paths=None,
        parse_result=parse_result,
    )

    metadata_json = json.loads(result.metadata_output.read_text())
    assert len(metadata_json["actions"]) == 2
    names = [a["name"] for a in metadata_json["actions"]]
    assert "create" in names
    assert "remove" in names
