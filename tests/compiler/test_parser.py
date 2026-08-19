from __future__ import annotations

import ast
from pathlib import Path
from textwrap import dedent

import pytest

from pyxle.compiler.exceptions import CompilationError
from pyxle.compiler.parser import PyxParser


def write(tmp_path: Path, relative: str, content: str) -> Path:
    target = tmp_path / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return target


def test_parse_static_page(tmp_path: Path) -> None:
    content = dedent(
        """
        import React from 'react';

        export default function About() {
            return <div>About</div>;
        }
        """
    ).strip("\n")

    source = write(tmp_path, "pages/about.pyxl", content)

    result = PyxParser().parse(source)

    assert result.python_code == ""
    assert "About" in result.jsx_code
    assert result.loader is None
    assert result.python_line_numbers == ()
    assert result.head_elements == ()


def test_parse_text_round_trip(tmp_path: Path) -> None:
    source_text = dedent(
        """
        import React from 'react';

        export default function About() {
            return <div>About</div>;
        }
        """
    ).strip("\n")

    source_path = write(tmp_path, "pages/about.pyxl", source_text)
    parser = PyxParser()

    from_disk = parser.parse(source_path)
    in_memory = parser.parse_text(source_text)

    assert in_memory.python_code == from_disk.python_code
    assert in_memory.jsx_code == from_disk.jsx_code
    assert in_memory.loader == from_disk.loader


def test_parse_loader_detection(tmp_path: Path) -> None:
    content = "\n".join(
        [
            "import random",
            "",
            "@server",
            "async def get_lucky(request):",
            "    number = random.randint(1, 10)",
            "    return {\"number\": number}",
            "",
            "import React from 'react';",
            "",
            "export default function Page({ data }) {",
            "    return <span>{data.number}</span>;",
            "}",
            "",
        ]
    )

    source = write(tmp_path, "pages/index.pyxl", content)
    result = PyxParser().parse(source)

    assert result.loader is not None
    assert result.loader.name == "get_lucky"
    assert result.loader.line_number == 4
    assert result.python_code.startswith("import random")
    assert "return <span>" in result.jsx_code
    assert result.head_elements == ()


def test_parse_non_async_loader_raises(tmp_path: Path) -> None:
    content = dedent(
        """
        @server
        def bad_loader(request):
            return {}

        export default function Demo() {
            return <div />;
        }
        """
    )

    source = write(tmp_path, "pages/bad.pyxl", content)

    with pytest.raises(CompilationError) as excinfo:
        PyxParser().parse(source)

    assert "async" in str(excinfo.value)


def test_parse_multiple_loaders_raises(tmp_path: Path) -> None:
    content = dedent(
        """
        @server
        async def first(request):
            return {}

        @server
        async def second(request):
            return {}

        export default function Demo() {
            return <div />;
        }
        """
    )

    source = write(tmp_path, "pages/multi.pyxl", content)

    with pytest.raises(CompilationError) as excinfo:
        PyxParser().parse(source)

    assert "Multiple" in str(excinfo.value)


def test_parse_nested_loader_not_allowed(tmp_path: Path) -> None:
    content = dedent(
        """
        async def outer():
            @server
            async def inner(request):
                return {}

        export default function Demo() {
            return <div />;
        }
        """
    )

    source = write(tmp_path, "pages/nested.pyxl", content)

    with pytest.raises(CompilationError) as excinfo:
        PyxParser().parse(source)

    assert "module scope" in str(excinfo.value)


def test_parse_windows_newlines_normalized(tmp_path: Path) -> None:
    base = dedent(
        """
        @server
        async def loader(request):
            return {"hello": "world"}

        export default function Demo() {
            return <div />;
        }
        """
    )

    source = write(tmp_path, "pages/windows.pyxl", base)
    result = PyxParser().parse(source)

    assert "\r" not in result.python_code
    assert "\r" not in result.jsx_code


def test_parse_nested_blocks_and_decorators(tmp_path: Path) -> None:
    content = dedent(
        """
        
        import httpx

        def log(message):
            return message.upper()

        def decorator(fn):
            return fn

        @decorator
        @server
        async def fetch_post(request):
            post_id = request.params.get("id")
            async with httpx.AsyncClient() as client:
                response = await client.get(f"https://example.com/{post_id}")
                if response.status_code == 404:
                    return {"error": log("missing")}, 404
                return response.json()

        # --- JavaScript/PSX ---
        import React from 'react';

        export default function Post({ data }) {
            return <article>{data.title}</article>;
        }
        """
    )

    source = write(tmp_path, "pages/posts/[id].pyxl", content)
    result = PyxParser().parse(source)

    assert result.loader is not None
    assert result.loader.name == "fetch_post"
    assert any("async with httpx.AsyncClient" in line for line in result.python_code.splitlines())
    assert any("return {\"error\"" in line for line in result.python_code.splitlines())
    assert result.loader.line_number == 13
    assert result.head_elements == ()


def test_parse_tuple_return_loaders_supported(tmp_path: Path) -> None:
    content = dedent(
        """
        @server
        async def loader(request):
            data = {"status": "ok"}
            return data, 201

        export default function Demo({ data }) {
            return <div>{data.status}</div>;
        }
        """
    )

    source = write(tmp_path, "pages/tuple.pyxl", content)
    result = PyxParser().parse(source)

    assert result.loader is not None
    assert "return data, 201" in result.python_code
    assert result.head_elements == ()


def test_parse_allows_interleaved_python_and_js_sections(tmp_path: Path) -> None:
    content = dedent(
        """
        from __future__ import annotations

        import React, { useEffect, useState } from 'react';
        import { Link } from 'pyxle/client';

        HEAD = "<title>Mixed</title>"

        @server
        async def load_home(request):
            return {"message": "hello"}

        const THEME_KEY = 'pyxle-theme-preference';

        def helper():
            return "extra"

        export default function Page({ data }) {
            return (
                <div>
                    <Link href="/">Hello {data.message}</Link>
                </div>
            );
        }
        """
    ).strip("\n")

    source = write(tmp_path, "pages/mixed.pyxl", content)
    result = PyxParser().parse(source)

    assert result.loader is not None
    assert result.loader.name == "load_home"
    assert "const THEME_KEY" in result.jsx_code
    assert "import React" in result.jsx_code
    assert "def helper" in result.python_code
    assert "HEAD =" in result.python_code
    assert "export default function Page" in result.jsx_code
    assert result.head_elements == ("<title>Mixed</title>",)


def test_parse_python_multiline_string_with_js_content(tmp_path: Path) -> None:
    content = dedent(
        """
        test = (" \\
        import React, { useEffect, useState } from 'react'; \\
        const VALUE = 1; \\
        ")

        export default function Demo() {
            return <div />;
        }
        """
    ).lstrip("\n")

    source = write(tmp_path, "pages/stringy.pyxl", content)
    result = PyxParser().parse(source)

    assert "import React, { useEffect, useState } from 'react';" in result.python_code
    assert "const VALUE = 1;" in result.python_code
    assert "import React, { useEffect, useState } from 'react';" not in result.jsx_code
    assert "export default function Demo" in result.jsx_code


def test_parse_python_line_continuation_not_treated_as_js(tmp_path: Path) -> None:
    content = dedent(
        """
        value = 1 + \\
            2

        export default function Demo() {
            return <div />;
        }
        """
    ).lstrip("\n")

    source = write(tmp_path, "pages/continuation.pyxl", content)
    result = PyxParser().parse(source)

    assert "value = 1 +" in result.python_code
    assert "2" in result.python_code
    assert "export default function Demo" in result.jsx_code


def test_parse_inconsistent_indentation_raises(tmp_path: Path) -> None:
    content = dedent(
        """
        @server
        async def loader(request):
            value = 1
                return value

        export default function Demo() {
            return <div />;
        }
        """
    )

    source = write(tmp_path, "pages/bad_indent.pyxl", content)

    with pytest.raises(CompilationError) as excinfo:
        PyxParser().parse(source)

    assert "unexpected" in str(excinfo.value).lower()


def test_parse_inconsistent_dedent_raises(tmp_path: Path) -> None:
    content = "\n".join(
        [
            "@server",
            "async def loader(request):",
            "    if request:",
            "        value = 1",
            "   return {'value': value}",
            "",
            "export default function Demo() {",
            "    return <div />;",
            "}",
            "",
        ]
    )

    source = write(tmp_path, "pages/bad_dedent.pyxl", content)

    with pytest.raises(CompilationError) as excinfo:
        PyxParser().parse(source)

    # The AST-driven parser surfaces Python's own SyntaxError message,
    # which can be "unexpected indent" or "inconsistent" depending on
    # the specific cause. Both contain the substring "indent".
    msg = str(excinfo.value).lower()
    assert "indent" in msg


def test_parser_helper_methods_cover_branches() -> None:
    """Cover the surviving internal helpers exposed by the parser module.

    The original test exercised a constellation of line-based heuristics
    (``_is_probable_python``, ``_is_probable_js``, ``_looks_like_js_import``
    et al.) that no longer exist after the AST-driven rewrite. The
    behaviors those helpers verified are now covered by behavior tests
    further down (``TestMultiSectionAutoDetection``) that exercise the
    same input patterns through the public ``parse_text`` API.

    What remains here is direct coverage of the small set of pure
    helpers that DID survive the rewrite — line-number mapping,
    decorator detection, and newline normalization.
    """
    from pyxle.compiler.parser import (
        _has_action_decorator,
        _has_server_decorator,
        _map_lineno,
        _normalize_newlines,
    )

    # Newline normalization
    assert _normalize_newlines("line1\r\nline2\rline3") == [
        "line1",
        "line2",
        "line3",
    ]

    # BOM stripping
    assert _normalize_newlines("\ufefffrom os import path\n") == [
        "from os import path",
        "",
    ]

    # Line number mapping
    assert _map_lineno(2, (10, 20, 30)) == 20
    assert _map_lineno(5, (10, 20, 30)) == 30
    assert _map_lineno(3, ()) == 3
    assert _map_lineno(None, ()) is None

    # @server decorator detection (bare, attribute, call form)
    assert _has_server_decorator([ast.Name(id="server")]) is True
    assert (
        _has_server_decorator(
            [ast.Attribute(value=ast.Name(id="loader"), attr="server")]
        )
        is True
    )
    assert (
        _has_server_decorator(
            [ast.Call(func=ast.Name(id="server"), args=[], keywords=[])]
        )
        is True
    )
    assert _has_server_decorator([]) is False

    # @action decorator detection
    assert _has_action_decorator([ast.Name(id="action")]) is True
    assert (
        _has_action_decorator(
            [ast.Attribute(value=ast.Name(id="runtime"), attr="action")]
        )
        is True
    )
    assert _has_action_decorator([]) is False


def test_parse_server_decorator_on_class_raises(tmp_path: Path) -> None:
    content = dedent(
        """
        @server
        class Handler:
            pass

        export default function Demo() {
            return <div />;
        }
        """
    )

    source = write(tmp_path, "pages/class.pyxl", content)

    with pytest.raises(CompilationError) as excinfo:
        PyxParser().parse(source)

    assert "functions" in str(excinfo.value)


def test_parse_loader_requires_request_argument(tmp_path: Path) -> None:
    content = dedent(
        """
        @server
        async def loader():
            return {}

        export default function Demo() {
            return <div />;
        }
        """
    )

    source = write(tmp_path, "pages/no_request.pyxl", content)

    with pytest.raises(CompilationError) as excinfo:
        PyxParser().parse(source)

    assert "request" in str(excinfo.value)


def test_parse_loader_requires_request_name(tmp_path: Path) -> None:
    content = dedent(
        """
        @server
        async def loader(req):
            return {}

        export default function Demo() {
            return <div />;
        }
        """
    )

    source = write(tmp_path, "pages/bad_request_name.pyxl", content)

    with pytest.raises(CompilationError) as excinfo:
        PyxParser().parse(source)

    assert "First argument" in str(excinfo.value)


def test_parse_python_helpers_without_loader(tmp_path: Path) -> None:
    content = dedent(
        """
        def helper():
            return "ok"

        export default function Demo() {
            return <div />;
        }
        """
    )

    source = write(tmp_path, "pages/helper_only.pyxl", content)
    result = PyxParser().parse(source)

    assert result.loader is None
    assert "helper" in result.python_code
    assert result.head_elements == ()


def test_parse_head_elements_from_literal(tmp_path: Path) -> None:
    content = dedent(
        """
        
        HEAD = [
            "<title>Custom</title>",
            '<meta name="description" content="Demo" />',
        ]

        @server
        async def loader(request):
            return {}

        # --- JavaScript/PSX ---
        import React from 'react';

        export default function Demo({ data }) {
            return <div>{data.message}</div>;
        }
        """
    )

    source = write(tmp_path, "pages/meta.pyxl", content)
    result = PyxParser().parse(source)

    assert result.head_elements == (
        "<title>Custom</title>",
        '<meta name="description" content="Demo" />',
    )
    assert result.head_is_dynamic is False


def test_parse_preserves_multiline_triple_quoted_python(tmp_path: Path) -> None:
    content = dedent(
        '''
        HEAD = """
        <title>Example</title>
        """

        @server
        async def loader(request):
            message = """
            Hello from Pyxle
            """
            return {"message": message}

        # --- JavaScript/PSX ---
        export default function Demo({ data }) {
            return <div>{data.message}</div>;
        }
        '''
    )

    source = write(tmp_path, "pages/multiline.pyxl", content)
    result = PyxParser().parse(source)

    assert '<title>Example</title>' in result.python_code
    assert 'message = """' in result.python_code
    assert "Hello from Pyxle" in result.python_code
    assert "return <div>" in result.jsx_code
    assert result.loader is not None


def test_parse_head_none_returns_empty_literal(tmp_path: Path) -> None:
    content = dedent(
        """
        HEAD = None

        # --- JavaScript/PSX ---
        export default function Demo() {
            return <div />;
        }
        """
    )

    source = write(tmp_path, "pages/head_none.pyxl", content)
    result = PyxParser().parse(source)

    assert result.head_elements == ()
    assert result.head_is_dynamic is False


def test_parse_head_tuple_literal(tmp_path: Path) -> None:
    content = dedent(
        """
        HEAD = ("<title>Tuple</title>",)

        # --- JavaScript/PSX ---
        export default function Demo() {
            return <div />;
        }
        """
    )

    source = write(tmp_path, "pages/head_tuple.pyxl", content)
    result = PyxParser().parse(source)

    assert result.head_elements == ("<title>Tuple</title>",)
    assert result.head_is_dynamic is False


def test_parse_marks_head_dynamic_when_expression(tmp_path: Path) -> None:
    content = dedent(
        """
        from pages.components import build_head

        HEAD = build_head(title="Dynamic", description="Demo")

        # --- JavaScript/PSX ---
        export default function Demo() {
            return <div />;
        }
        """
    )

    source = write(tmp_path, "pages/dynamic_head.pyxl", content)
    result = PyxParser().parse(source)

    assert result.head_elements == ()
    assert result.head_is_dynamic is True


def test_parse_head_list_with_non_string_marks_dynamic(tmp_path: Path) -> None:
    content = dedent(
        """
        HEAD = [
            "<title>Demo</title>",
            123,
        ]

        # --- JavaScript/PSX ---
        export default function Demo() {
            return <div />;
        }
        """
    )

    source = write(tmp_path, "pages/head_mixed.pyxl", content)
    result = PyxParser().parse(source)

    assert result.head_elements == ()
    assert result.head_is_dynamic is True


def test_parse_head_function_marks_dynamic(tmp_path: Path) -> None:
    content = dedent(
        """
        def HEAD(data):
            return "<title>Callable</title>"

        # --- JavaScript/PSX ---
        export default function Demo() {
            return <div />;
        }
        """
    )

    source = write(tmp_path, "pages/head_function.pyxl", content)
    result = PyxParser().parse(source)

    assert result.head_elements == ()
    assert result.head_is_dynamic is True


def test_parse_head_skips_other_assignments(tmp_path: Path) -> None:
    content = dedent(
        """
        TITLE = "<title>Ignored</title>"
        HEAD = "<title>Chosen</title>"

        # --- JavaScript/PSX ---
        export default function Demo() {
            return <div />;
        }
        """
    )

    source = write(tmp_path, "pages/head_with_title.pyxl", content)
    result = PyxParser().parse(source)

    assert result.head_elements == ("<title>Chosen</title>",)
    assert result.head_is_dynamic is False


def test_parse_head_elements_invalid_type_raises(tmp_path: Path) -> None:
    content = dedent(
        """
        
        HEAD = 123

        # --- JavaScript/PSX ---
        export default function Demo() {
            return <div />;
        }
        """
    )

    source = write(tmp_path, "pages/invalid_head.pyxl", content)

    with pytest.raises(CompilationError) as excinfo:
        PyxParser().parse(source)

    assert "HEAD" in str(excinfo.value)


def test_parse_triple_quoted_string_with_indented_content(tmp_path: Path) -> None:
    """Test that indented content inside triple-quoted strings doesn't trigger indentation errors."""
    content = dedent(
        '''
        HEAD = [
            """
            <title>Test Page</title>
            """,
            """
            <meta name="description" content="Test" />
            """,
        ]

        export default function Page() {
            return <div>Test</div>;
        }
        '''
    ).lstrip("\n")

    source = write(tmp_path, "pages/triple_quoted.pyxl", content)
    result = PyxParser().parse(source)

    assert '"""' in result.python_code
    assert "<title>Test Page</title>" in result.python_code
    assert '<meta name="description" content="Test" />' in result.python_code
    # One element per entry -- two triple-quoted entries, each keeping the
    # indentation it was written with.
    assert len(result.head_elements) == 2
    assert "<title>Test Page</title>" in result.head_elements[0]
    assert '<meta name="description" content="Test" />' in result.head_elements[1]
    assert "export default function Page" in result.jsx_code


# ---------------------------------------------------------------------------
# AST-driven multi-section detection (the user-requested feature)
# ---------------------------------------------------------------------------


class TestMultiSectionAutoDetection:
    """The new parser supports arbitrary alternation of Python and JSX
    blocks within a single ``.pyxl`` file, without requiring fence markers.
    """

    def test_python_jsx_python_jsx_alternation(self):
        """A file with imports → JSX helper → Python action → JSX export
        is segmented correctly without any markers."""
        result = PyxParser().parse_text(
            "from pyxle.runtime import server, action\n"
            "import os\n"
            "\n"
            "def helper():\n"
            "    return 'hi'\n"
            "\n"
            "import React from 'react';\n"
            "\n"
            "function MyComp() {\n"
            "    return <div />;\n"
            "}\n"
            "\n"
            "@action\n"
            "async def save_data(request):\n"
            "    return {'ok': True}\n"
            "\n"
            "export default function Page() {\n"
            "    return <div>Hello</div>;\n"
            "}\n"
        )
        # Both Python sections show up in python_code.
        assert "from pyxle.runtime import server, action" in result.python_code
        assert "def helper" in result.python_code
        assert "@action" in result.python_code
        assert "async def save_data" in result.python_code
        # Both JSX sections show up in jsx_code.
        assert "import React from 'react';" in result.jsx_code
        assert "function MyComp" in result.jsx_code
        assert "export default function Page" in result.jsx_code
        # Action is detected.
        assert len(result.actions) == 1
        assert result.actions[0].name == "save_data"

    def test_jsx_first_then_python_then_jsx(self):
        """JSX-first files are supported."""
        result = PyxParser().parse_text(
            "import React from 'react';\n"
            "\n"
            "function Helper() {\n"
            "    return <span />;\n"
            "}\n"
            "\n"
            "@server\n"
            "async def loader(request):\n"
            "    return {'x': 1}\n"
            "\n"
            "export default function Page() {\n"
            "    return <Helper />;\n"
            "}\n"
        )
        assert "import React from 'react';" in result.jsx_code
        assert "function Helper" in result.jsx_code
        assert "@server" in result.python_code
        assert "async def loader" in result.python_code
        assert "export default function Page" in result.jsx_code
        assert result.loader is not None
        assert result.loader.name == "loader"


# ---------------------------------------------------------------------------
# Modern Python language features
# ---------------------------------------------------------------------------


class TestModernPythonFeatures:
    """Edge cases for newer Python syntax that the AST parser must handle."""

    def test_walrus_operator_in_loader(self):
        result = PyxParser().parse_text(
            "@server\n"
            "async def loader(request):\n"
            "    if (n := request.params.get('n')) is not None:\n"
            "        return {'n': n}\n"
            "    return {}\n"
            "\n"
            "export default function P() { return <div />; }\n"
        )
        assert result.loader is not None
        assert result.loader.name == "loader"

    def test_match_statement_in_loader(self):
        result = PyxParser().parse_text(
            "@server\n"
            "async def loader(request):\n"
            "    match request.method:\n"
            "        case 'GET':\n"
            "            return {'verb': 'get'}\n"
            "        case _:\n"
            "            return {'verb': 'other'}\n"
            "\n"
            "export default function P() { return <div />; }\n"
        )
        assert result.loader is not None
        assert "match request.method" in result.python_code

    def test_async_generator_in_loader(self):
        result = PyxParser().parse_text(
            "async def gen():\n"
            "    yield 1\n"
            "    yield 2\n"
            "\n"
            "@server\n"
            "async def loader(request):\n"
            "    items = [x async for x in gen()]\n"
            "    return {'items': items}\n"
            "\n"
            "export default function P() { return <div />; }\n"
        )
        assert result.loader is not None

    def test_loader_with_positional_only_param(self):
        result = PyxParser().parse_text(
            "@server\n"
            "async def loader(request, /):\n"
            "    return {}\n"
            "\n"
            "export default function P() { return <div />; }\n"
        )
        assert result.loader is not None
        assert result.loader.parameters == ("request",)


class TestCacheDirective:
    """The compile-time ``CACHE = {"revalidate": N}`` page-cache directive."""

    @staticmethod
    def _parse(python: str):
        return PyxParser().parse_text(
            python + "\n\nexport default function P() { return <div />; }\n"
        )

    def test_no_directive_yields_none(self):
        assert self._parse("x = 1").cache_revalidate is None

    def test_extracts_revalidate_seconds(self):
        assert self._parse('CACHE = {"revalidate": 60}').cache_revalidate == 60.0

    def test_zero_seconds_is_valid(self):
        assert self._parse('CACHE = {"revalidate": 0}').cache_revalidate == 0.0

    def test_float_seconds_is_valid(self):
        assert self._parse('CACHE = {"revalidate": 1.5}').cache_revalidate == 1.5

    def test_non_dict_raises(self):
        with pytest.raises(CompilationError, match="CACHE must be a dict"):
            self._parse("CACHE = 60")

    def test_missing_revalidate_key_raises(self):
        with pytest.raises(CompilationError, match="revalidate"):
            self._parse('CACHE = {"ttl": 60}')

    @pytest.mark.parametrize("literal", ['"60"', "-1", "True", "None"])
    def test_invalid_revalidate_value_raises(self, literal):
        with pytest.raises(CompilationError, match="revalidate"):
            self._parse('CACHE = {"revalidate": ' + literal + "}")


class TestSyntaxErrorPosition:
    """A syntax error carries where it is, not just what it is.

    The dev server prints ``pages/about.pyxl:7:9: unexpected indent`` — a
    location a terminal linkifies and an editor can jump to — which it can only
    do if the parser hands the column up with the line.
    """

    def test_compilation_error_carries_line_and_column(self) -> None:
        source = (
            "@server\n"
            "async def load(request):\n"
            "    return {}\n"
            "        oops = 1\n"
            "\n"
            "import React from 'react';\n"
            "export default function Page() { return <div />; }\n"
        )

        with pytest.raises(CompilationError) as excinfo:
            PyxParser().parse_text(source)

        error = excinfo.value
        assert error.line_number == 4
        assert error.column is not None and error.column > 0
        assert "indent" in error.message

    def test_column_defaults_to_none(self) -> None:
        assert CompilationError("boom").column is None

    def test_str_is_unchanged_by_the_column(self) -> None:
        """The message stays stable; the location is assembled by the caller."""
        assert str(CompilationError("boom", 4, 9)) == "Line 4: boom"
        assert str(CompilationError("boom")) == "boom"


BROKEN_JSX_PAGE = """import React from 'react';

export default function Broken() {
    return (
        <main>
            <p>unterminated</p
        </main>
    );
}
"""


class TestJsxSyntaxReporting:
    """A JSX half Babel cannot parse must not compile silently.

    The extractor pass that reads <Head>/<Script>/<Image>/<Suspense> runs on
    every compile and has to parse the section to do it. It used to discard a
    parse failure and return empty metadata, so the compile "succeeded" — the
    rebuild logged a green tick, the page's <Head> was silently dropped, and
    the error only appeared later from the bundler, against the *generated*
    .jsx rather than the .pyxl the developer edits.
    """

    def test_broken_jsx_is_a_located_compile_error(self) -> None:
        with pytest.raises(CompilationError) as excinfo:
            PyxParser().parse_text(BROKEN_JSX_PAGE, report_jsx_syntax=True)

        error = excinfo.value
        assert "JSX syntax error" in error.message
        assert error.line_number == 7

    def test_the_line_is_the_pyxl_line_not_the_jsx_section_line(self) -> None:
        """Every JSX-side tool numbers from the start of the section it was
        handed; reporting that unmapped points at the wrong line."""
        source = (
            "from __future__ import annotations\n"
            "\n"
            "\n"
            "@server\n"
            "async def load(request):\n"
            "    return {}\n"
            "\n"
            "\n" + BROKEN_JSX_PAGE
        )

        with pytest.raises(CompilationError) as excinfo:
            PyxParser().parse_text(source, report_jsx_syntax=True)

        # The failure is on the 7th line of the JSX section, which is line 15
        # of the file. An unmapped report would have said 7.
        assert excinfo.value.line_number == 15
        assert source.splitlines()[14].strip() == "</main>"

    def test_the_production_build_path_is_unchanged(self) -> None:
        """Off by default, so a Babel/esbuild disagreement cannot newly break
        a production build that works today."""
        result = PyxParser().parse_text(BROKEN_JSX_PAGE)

        assert result.head_jsx_blocks == ()
        assert result.script_declarations == ()

    def test_healthy_jsx_is_unaffected(self) -> None:
        source = (
            "import React from 'react';\n"
            "import { Head } from 'pyxle/client';\n"
            "\n"
            "export default function Page() {\n"
            "    return <main><Head><title>Hi</title></Head></main>;\n"
            "}\n"
        )

        result = PyxParser().parse_text(source, report_jsx_syntax=True)

        assert result.head_jsx_blocks and "Hi" in result.head_jsx_blocks[0]

    def test_tolerant_mode_collects_it_as_a_jsx_diagnostic(self) -> None:
        result = PyxParser().parse_text(
            BROKEN_JSX_PAGE, tolerant=True, report_jsx_syntax=True
        )

        jsx = [d for d in result.diagnostics if d.section == "jsx"]
        assert len(jsx) == 1
        assert jsx[0].line == 7

    def test_it_is_reported_exactly_once_alongside_validate_jsx(self) -> None:
        """``validate_jsx`` would otherwise spawn Babel again to rediscover the
        same failure and emit a duplicate diagnostic for one error."""
        result = PyxParser().parse_text(
            BROKEN_JSX_PAGE, tolerant=True, validate_jsx=True, report_jsx_syntax=True
        )

        jsx = [d for d in result.diagnostics if d.section == "jsx"]
        assert len(jsx) == 1

    def test_a_broken_python_half_suppresses_it(self) -> None:
        """Broken Python is absorbed into the JSX section by the walker, so the
        JSX complaint would be a symptom, not the cause."""
        source = (
            "@server\n"
            "async def load(request):\n"
            "    return {}\n"
            "        bad_indent = 1\n"
            "\n"
            "import React from 'react';\n"
            "export default function P() { return <div />; }\n"
        )

        result = PyxParser().parse_text(
            source, tolerant=True, report_jsx_syntax=True
        )

        assert [d.section for d in result.diagnostics] == ["python"]


class TestJsxReportingDegradesWithoutTheToolchain:
    """A missing checker must never be reported as broken user code.

    Without this the first machine without Node — or without the language
    toolkit installed — fails every single file in the project with a JSX
    syntax error it invented.
    """

    MESSAGE = "Node.js not found. Install Node.js >=20.19 to parse JSX components."

    @staticmethod
    def _stub_extractor(monkeypatch, *, toolchain_available: bool):
        from pyxle.compiler import jsx_parser

        def fake(*_args, **_kwargs):
            return jsx_parser.JSXParseResult(
                components=(),
                error=TestJsxReportingDegradesWithoutTheToolchain.MESSAGE,
                error_line=1,
                toolchain_available=toolchain_available,
            )

        monkeypatch.setattr(jsx_parser, "parse_jsx_components", fake)

    def test_an_unavailable_checker_never_fails_the_compile(self, monkeypatch) -> None:
        self._stub_extractor(monkeypatch, toolchain_available=False)

        result = PyxParser().parse_text(BROKEN_JSX_PAGE, report_jsx_syntax=True)

        assert result.diagnostics == ()
        assert result.head_jsx_blocks == ()

    def test_the_same_message_from_a_working_checker_does_fail(
        self, monkeypatch
    ) -> None:
        """Identical error text, opposite outcome — the flag is what decides,
        not the wording, so no string matching can drift."""
        self._stub_extractor(monkeypatch, toolchain_available=True)

        with pytest.raises(CompilationError) as excinfo:
            PyxParser().parse_text(BROKEN_JSX_PAGE, report_jsx_syntax=True)

        assert self.MESSAGE in excinfo.value.message
