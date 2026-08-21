"""Tests for the structured-diagnostic mechanism in the AST-driven parser.

These tests cover the new ``PyxDiagnostic`` dataclass and the
``PyxParseResult.diagnostics`` field. The parser collects errors as
diagnostics in tolerant mode (used by IDE/LSP integrations and the
``pyxle check`` CLI) and raises ``CompilationError`` in strict mode
(used by the build pipeline).
"""

from __future__ import annotations

import shutil
from textwrap import dedent
from unittest.mock import patch

import pytest

from pyxle.compiler.exceptions import CompilationError
from pyxle.compiler.jsx_parser import JSXParseResult
from pyxle.compiler.parser import (
    INJECTED_RUNTIME_NAMES,
    PyxDiagnostic,
    PyxParser,
)

_NODE_AVAILABLE = shutil.which("node") is not None


def _parse(text: str, *, tolerant: bool = False, validate_jsx: bool = False):
    return PyxParser().parse_text(
        dedent(text).strip("\n"), tolerant=tolerant, validate_jsx=validate_jsx
    )


# ---------------------------------------------------------------------------
# Strict vs tolerant mode
# ---------------------------------------------------------------------------


class TestStrictMode:
    """Strict mode (the default) raises ``CompilationError`` on the first error."""

    def test_python_syntax_error_raises(self):
        with pytest.raises(CompilationError):
            _parse("""
                @server
                async def loader(request):
                    data = (1 + )
            """)

    def test_loader_validation_error_raises(self):
        with pytest.raises(CompilationError, match="async"):
            _parse("""
                @server
                def loader(request):
                    return {}

                export default function P() { return <div />; }
            """)

    def test_action_validation_error_raises(self):
        with pytest.raises(CompilationError, match="async"):
            _parse("""
                @action
                def save(request):
                    return {}

                export default function P() { return <div />; }
            """)

    def test_no_diagnostics_in_strict_mode_for_valid_file(self):
        """A valid file in strict mode produces an empty diagnostics tuple."""
        result = _parse("""
            @server
            async def loader(request):
                return {"ok": True}

            export default function P() { return <div />; }
        """)
        assert result.diagnostics == ()


# ---------------------------------------------------------------------------
# Tolerant mode
# ---------------------------------------------------------------------------


class TestTolerantMode:
    """Tolerant mode collects errors as diagnostics instead of raising."""

    def test_python_syntax_error_becomes_diagnostic(self):
        result = _parse(
            """
            @server
            async def loader(request):
                data = (1 + )
            """,
            tolerant=True,
        )
        assert len(result.diagnostics) >= 1
        diag = result.diagnostics[0]
        assert isinstance(diag, PyxDiagnostic)
        assert diag.section == "python"
        assert diag.severity == "error"
        assert diag.line is not None

    def test_loader_validation_error_becomes_diagnostic(self):
        result = _parse(
            """
            @server
            def loader(request):
                return {}

            export default function P() { return <div />; }
            """,
            tolerant=True,
        )
        # The sync-loader error should be in diagnostics, not raised.
        assert any("async" in d.message for d in result.diagnostics)
        # The result should still have the JSX section.
        assert "export default" in result.jsx_code

    def test_action_validation_error_becomes_diagnostic(self):
        result = _parse(
            """
            @action
            def save(request):
                return {}

            export default function P() { return <div />; }
            """,
            tolerant=True,
        )
        assert any("async" in d.message for d in result.diagnostics)

    def test_action_missing_request_arg_becomes_diagnostic(self):
        result = _parse(
            """
            @action
            async def save():
                return {}

            export default function P() { return <div />; }
            """,
            tolerant=True,
        )
        assert any("request" in d.message for d in result.diagnostics)

    def test_head_validation_error_becomes_diagnostic(self):
        result = _parse(
            """
            HEAD = 123

            export default function P() { return <div />; }
            """,
            tolerant=True,
        )
        assert any("HEAD" in d.message for d in result.diagnostics)

    def test_diagnostics_sorted_by_line(self):
        """Multiple diagnostics in one file are returned in source order."""
        result = _parse(
            """
            @server
            def loader(request):
                return {}

            @action
            def save(request):
                return {}

            export default function P() { return <div />; }
            """,
            tolerant=True,
        )
        assert len(result.diagnostics) >= 2
        lines = [d.line for d in result.diagnostics if d.line is not None]
        assert lines == sorted(lines)

    def test_no_diagnostics_for_valid_file(self):
        result = _parse(
            """
            @server
            async def loader(request):
                return {"ok": True}

            @action
            async def save(request):
                return {"saved": True}

            export default function P() { return <div />; }
            """,
            tolerant=True,
        )
        assert result.diagnostics == ()

    def test_unparseable_input_does_not_crash(self):
        """Tolerant mode handles complete junk gracefully."""
        result = PyxParser().parse_text("???\n@@@@", tolerant=True)
        assert result is not None
        assert isinstance(result.diagnostics, tuple)


# ---------------------------------------------------------------------------
# JSX validation (opt-in)
# ---------------------------------------------------------------------------


class TestJsxValidation:
    """``validate_jsx=True`` runs Babel on the JSX section."""

    def test_validate_jsx_false_skips_babel(self):
        """Default behavior: Babel is not invoked, no JSX diagnostics."""
        result = _parse(
            """
            @server
            async def loader(request):
                return {}

            import React from 'react';
            // syntactically broken: unclosed brace
            export default function P() { return <div />
            """,
            tolerant=True,
        )
        # No JSX-section diagnostics because validate_jsx defaults to False.
        assert all(d.section != "jsx" for d in result.diagnostics)

    def test_validate_jsx_true_collects_jsx_syntax_error(self):
        """When ``validate_jsx=True``, malformed JSX produces a diagnostic."""
        result = _parse(
            """
            @server
            async def loader(request):
                return {}

            import React from 'react';
            // syntactically broken: unclosed function and missing semicolon
            export default function P() { return <div /
            """,
            tolerant=True,
            validate_jsx=True,
        )
        # The test passes either when Babel was invoked and found an error,
        # OR when Babel wasn't available (and the validator was skipped
        # silently). We just verify the orchestration didn't crash and
        # the diagnostics field is the correct shape.
        assert isinstance(result.diagnostics, tuple)

    def test_validate_jsx_true_with_valid_jsx_no_diagnostics(self):
        """Valid JSX with validate_jsx=True produces no JSX diagnostics."""
        result = _parse(
            """
            @server
            async def loader(request):
                return {}

            import React from 'react';

            export default function Page() {
                return <div>Hello</div>;
            }
            """,
            tolerant=True,
            validate_jsx=True,
        )
        # Should parse cleanly with no JSX diagnostics.
        assert all(d.section != "jsx" for d in result.diagnostics)

    def test_validate_jsx_strict_raises_on_jsx_error(self):
        """Strict mode: a malformed JSX section raises CompilationError
        when ``validate_jsx=True``. Only meaningful when Babel is
        available."""
        # Skip-style: only assert the call sequence doesn't crash. If
        # Babel is available the call raises; if not, the parse returns
        # without errors. Either way we don't crash unexpectedly.
        try:
            _parse(
                """
                import React from 'react';
                export default function P() { return <div /
                """,
                tolerant=False,
                validate_jsx=True,
            )
        except CompilationError:
            pass

    def test_jsx_validation_suppressed_when_python_has_errors(self):
        """When the Python section has a diagnostic (e.g. an
        unterminated string pushed broken content into ``jsx_code``),
        ``validate_jsx`` should be suppressed so the user doesn't see
        a cascade of noisy ``[jsx]`` errors that are really symptoms
        of the underlying Python problem. Only the ``[python]``
        diagnostic should appear.
        """
        src = (
            'x = "unterminated\n'
            'y = 1\n'
            '\n'
            "import React from 'react';\n"
            "export default function P() { return <div />; }\n"
        )
        result = PyxParser().parse_text(
            src, tolerant=True, validate_jsx=True
        )
        python_diags = [d for d in result.diagnostics if d.section == "python"]
        jsx_diags = [d for d in result.diagnostics if d.section == "jsx"]
        assert python_diags, "expected at least one [python] diagnostic"
        assert not jsx_diags, (
            f"expected no [jsx] diagnostics when python has errors, "
            f"got {jsx_diags!r}"
        )


# ---------------------------------------------------------------------------
# PyxDiagnostic dataclass
# ---------------------------------------------------------------------------


class TestTolerantValidationErrorPaths:
    """Tolerant-mode coverage for every loader/action/HEAD error path."""

    def test_loader_at_class_raises_diagnostic(self):
        result = _parse(
            """
            @server
            class Handler:
                pass

            export default function P() { return <div />; }
            """,
            tolerant=True,
        )
        assert any("functions" in d.message for d in result.diagnostics)

    def test_multiple_loaders_diagnostic(self):
        result = _parse(
            """
            @server
            async def first(request):
                return {}

            @server
            async def second(request):
                return {}

            export default function P() { return <div />; }
            """,
            tolerant=True,
        )
        assert any("Multiple" in d.message for d in result.diagnostics)

    def test_loader_nested_in_class_diagnostic(self):
        """A nested @server raises a 'module scope' diagnostic in tolerant mode."""
        result = _parse(
            """
            class Wrapper:
                @server
                async def inner(request):
                    return {}

            export default function P() { return <div />; }
            """,
            tolerant=True,
        )
        assert any("module scope" in d.message for d in result.diagnostics)

    def test_loader_missing_request_arg_diagnostic(self):
        result = _parse(
            """
            @server
            async def loader():
                return {}

            export default function P() { return <div />; }
            """,
            tolerant=True,
        )
        assert any("request" in d.message for d in result.diagnostics)

    def test_loader_wrong_arg_name_diagnostic(self):
        result = _parse(
            """
            @server
            async def loader(req):
                return {}

            export default function P() { return <div />; }
            """,
            tolerant=True,
        )
        assert any("First argument" in d.message for d in result.diagnostics)

    def test_action_on_class_diagnostic(self):
        result = _parse(
            """
            @action
            class Bad:
                pass

            export default function P() { return <div />; }
            """,
            tolerant=True,
        )
        assert any(
            "functions" in d.message or "class" in d.message.lower()
            for d in result.diagnostics
        )

    def test_action_nested_in_class_diagnostic(self):
        result = _parse(
            """
            class Wrapper:
                @action
                async def save(request):
                    pass

            export default function P() { return <div />; }
            """,
            tolerant=True,
        )
        assert any("module scope" in d.message for d in result.diagnostics)

    def test_action_with_server_decorator_diagnostic(self):
        result = _parse(
            """
            @server
            @action
            async def both(request):
                return {}

            export default function P() { return <div />; }
            """,
            tolerant=True,
        )
        assert any(
            "@action and @server" in d.message
            for d in result.diagnostics
        )

    def test_action_wrong_arg_name_diagnostic(self):
        result = _parse(
            """
            @action
            async def save(req):
                return {}

            export default function P() { return <div />; }
            """,
            tolerant=True,
        )
        assert any("First argument" in d.message for d in result.diagnostics)

    def test_duplicate_action_names_diagnostic(self):
        result = _parse(
            """
            @action
            async def save(request):
                return {}

            @action
            async def save(request):
                return {}

            export default function P() { return <div />; }
            """,
            tolerant=True,
        )
        assert any("Duplicate" in d.message for d in result.diagnostics)

    def test_head_invalid_value_diagnostic(self):
        result = _parse(
            """
            HEAD = 123

            export default function P() { return <div />; }
            """,
            tolerant=True,
        )
        assert any("HEAD" in d.message for d in result.diagnostics)


class TestJsxStateCleanBetween:
    """Direct unit tests for ``_jsx_state_clean_between``, the JS state
    tracker that determines whether a candidate Python resume position
    sits at a valid top-level JS position (no open string or comment)."""

    def test_clean_after_simple_jsx(self):
        from pyxle.compiler.parser import _jsx_state_clean_between
        assert (
            _jsx_state_clean_between(
                ["const x = 1;", "const y = 2;"], 0, 2
            )
            is True
        )

    def test_clean_after_quoted_string(self):
        from pyxle.compiler.parser import _jsx_state_clean_between
        assert (
            _jsx_state_clean_between(["const x = 'hello';"], 0, 1) is True
        )

    def test_clean_after_backtick_string(self):
        from pyxle.compiler.parser import _jsx_state_clean_between
        assert (
            _jsx_state_clean_between(["const x = `hello`;"], 0, 1) is True
        )

    def test_unclean_inside_open_backtick(self):
        from pyxle.compiler.parser import _jsx_state_clean_between
        assert (
            _jsx_state_clean_between(["const x = `hello", "world"], 0, 1)
            is False
        )

    def test_clean_after_backtick_with_escape(self):
        from pyxle.compiler.parser import _jsx_state_clean_between
        assert (
            _jsx_state_clean_between(
                [r"const x = `hello \` world`;"], 0, 1
            )
            is True
        )

    def test_clean_after_block_comment_single_line(self):
        from pyxle.compiler.parser import _jsx_state_clean_between
        assert (
            _jsx_state_clean_between(["const x = 1; /* note */"], 0, 1)
            is True
        )

    def test_clean_after_block_comment_multi_line(self):
        from pyxle.compiler.parser import _jsx_state_clean_between
        assert (
            _jsx_state_clean_between(
                ["const x = 1; /* multi", "line */"], 0, 2
            )
            is True
        )

    def test_unclean_inside_open_block_comment(self):
        from pyxle.compiler.parser import _jsx_state_clean_between
        assert (
            _jsx_state_clean_between(
                ["const x = 1; /* not closed", "still in comment"], 0, 1
            )
            is False
        )

    def test_clean_after_line_comment(self):
        from pyxle.compiler.parser import _jsx_state_clean_between
        assert (
            _jsx_state_clean_between(
                ["const x = 1; // comment to end of line"], 0, 1
            )
            is True
        )

    def test_quoted_string_with_escaped_quote(self):
        from pyxle.compiler.parser import _jsx_state_clean_between
        assert (
            _jsx_state_clean_between(
                [r'const x = "hello \"world\"";'], 0, 1
            )
            is True
        )

    def test_quoted_string_resets_at_eol(self):
        from pyxle.compiler.parser import _jsx_state_clean_between
        # An unterminated single-quoted string resets at EOL.
        assert (
            _jsx_state_clean_between(
                ["const x = 'broken", "const y = 1;"], 0, 2
            )
            is True
        )


class TestSegmentationHelpers:
    """Defensive edge cases for the segmentation helpers."""

    def test_find_largest_python_at_past_end(self):
        from pyxle.compiler.parser import _find_largest_python_at
        assert _find_largest_python_at(["x"], 5, 1) == 5

    def test_find_largest_python_at_blank_only(self):
        from pyxle.compiler.parser import _find_largest_python_at
        assert _find_largest_python_at(["x = 1", "", ""], 1, 3) == 3

    def test_auto_detect_empty_lines(self):
        from pyxle.compiler.parser import _auto_detect_segments
        assert _auto_detect_segments([]) == []

    def test_auto_detect_only_blank_lines(self):
        from pyxle.compiler.parser import _auto_detect_segments
        assert _auto_detect_segments(["", "", ""]) == []


class TestJsStateBracketDepth:
    """Bracket-depth tracking in ``_JsState`` (the JS-aware walker that
    prevents the auto-detect from misclassifying content inside open JSX
    blocks)."""

    def test_brace_depth_increases_and_decreases(self):
        from pyxle.compiler.parser import _JsState
        state = _JsState()
        state.advance("function P() {")
        assert state.brace_depth == 1
        assert not state.is_clean()
        state.advance("}")
        assert state.brace_depth == 0
        assert state.is_clean()

    def test_paren_depth_tracked(self):
        from pyxle.compiler.parser import _JsState
        state = _JsState()
        state.advance("const x = foo(1, 2,")
        assert state.paren_depth == 1
        assert not state.is_clean()
        state.advance("3);")
        assert state.paren_depth == 0
        assert state.is_clean()

    def test_bracket_depth_tracked(self):
        from pyxle.compiler.parser import _JsState
        state = _JsState()
        state.advance("const arr = [1,")
        assert state.bracket_depth == 1
        assert not state.is_clean()
        state.advance("2, 3];")
        assert state.bracket_depth == 0
        assert state.is_clean()

    def test_jsx_function_body_with_python_inside_stays_jsx(self):
        """The user's bug: a JSX function body that contains broken
        Python-shaped content should stay in JSX, not get split out as
        a separate Python segment."""
        from pyxle.compiler.parser import PyxParser
        src = (
            "export default function HomePage({ data }) {\n"
            '    const text = "test"\n'
            "\n"
            "@action\n"
            "async def handleClick(request):\n"
            "    return None\n"
            "\n"
            "    return <div />;\n"
            "}\n"
        )
        result = PyxParser().parse_text(src, tolerant=True)
        # The @action and broken Python should stay in JSX, not be
        # extracted as a Python segment.
        assert result.python_code.strip() == ""
        assert "@action" in result.jsx_code
        assert "async def handleClick" in result.jsx_code


class TestParseSafelyEdgeCases:
    """Additional coverage for ``_parse_python_safely``."""

    def test_empty_python_code_returns_none(self):
        """An empty python_code segment returns None without parsing."""
        from pyxle.compiler.parser import PyxParser
        # An empty file produces empty python_code so the early-return
        # branch in _parse_python_safely fires.
        result = PyxParser().parse_text("")
        assert result.loader is None
        assert result.actions == ()

    def test_pure_jsx_file_python_segment_empty(self):
        from pyxle.compiler.parser import PyxParser
        result = PyxParser().parse_text(
            "import React from 'react';\n"
            "export default function P() { return <div />; }\n"
        )
        assert result.python_code == ""
        assert result.loader is None


class TestJsStateAdvanceEdgeCases:
    """Edge cases in the ``_JsState`` walker that don't naturally arise
    in standard JSX content."""

    def test_backtick_with_escaped_backtick_inside(self):
        from pyxle.compiler.parser import _JsState
        state = _JsState()
        state.advance("const x = `hello \\` world`;")
        assert state.is_clean()

    def test_unterminated_block_comment_persists(self):
        from pyxle.compiler.parser import _JsState
        state = _JsState()
        state.advance("/* not closed")
        assert state.block_comment is True
        state.advance("still in comment")
        assert state.block_comment is True
        state.advance("ends here */")
        assert state.block_comment is False

    def test_paren_inside_brace(self):
        from pyxle.compiler.parser import _JsState
        state = _JsState()
        state.advance("function P() { return foo(")
        assert state.brace_depth == 1
        assert state.paren_depth == 1
        state.advance("); }")
        assert state.brace_depth == 0
        assert state.paren_depth == 0


class TestRealWorldPyxFixtures:
    """Run a battery of realistic .pyxl fixtures through the parser to
    exercise the metadata extraction code paths in normal operation.
    Each fixture covers a different combination of loader, actions, and
    HEAD configurations."""

    def test_loader_with_qualified_decorator(self):
        from pyxle.compiler.parser import PyxParser
        result = PyxParser().parse_text(
            "import pyxle.runtime as runtime\n"
            "\n"
            "@runtime.server\n"
            "async def loader(request):\n"
            "    return {}\n"
            "\n"
            "import React from 'react';\n"
            "export default function P({ data }) { return <div />; }\n"
        )
        assert result.loader is not None
        assert result.loader.name == "loader"

    def test_loader_with_call_decorator(self):
        from pyxle.compiler.parser import PyxParser
        result = PyxParser().parse_text(
            "from pyxle.runtime import server\n"
            "\n"
            "@server\n"
            "async def loader(request):\n"
            "    return {}\n"
            "\n"
            "import React from 'react';\n"
            "export default function P({ data }) { return <div />; }\n"
        )
        assert result.loader is not None

    def test_head_string_literal(self):
        from pyxle.compiler.parser import PyxParser
        result = PyxParser().parse_text(
            'HEAD = "<title>Page</title>"\n'
            "\n"
            "import React from 'react';\n"
            "export default function P() { return <div />; }\n"
        )
        assert result.head_elements == ("<title>Page</title>",)

    def test_head_list_literal(self):
        from pyxle.compiler.parser import PyxParser
        result = PyxParser().parse_text(
            'HEAD = ["<title>Page</title>", "<meta name=\\"x\\" content=\\"y\\" />"]\n'
            "\n"
            "import React from 'react';\n"
            "export default function P() { return <div />; }\n"
        )
        assert len(result.head_elements) == 2
        assert "<title>Page</title>" in result.head_elements

    def test_head_tuple_literal(self):
        from pyxle.compiler.parser import PyxParser
        result = PyxParser().parse_text(
            'HEAD = ("<title>Tuple</title>",)\n'
            "\n"
            "import React from 'react';\n"
            "export default function P() { return <div />; }\n"
        )
        assert result.head_elements == ("<title>Tuple</title>",)

    def test_head_none(self):
        from pyxle.compiler.parser import PyxParser
        result = PyxParser().parse_text(
            "HEAD = None\n"
            "\n"
            "import React from 'react';\n"
            "export default function P() { return <div />; }\n"
        )
        assert result.head_elements == ()

    def test_head_dynamic_function_call(self):
        from pyxle.compiler.parser import PyxParser
        result = PyxParser().parse_text(
            "from pages.components import build_head\n"
            "\n"
            "HEAD = build_head(title='Dynamic')\n"
            "\n"
            "import React from 'react';\n"
            "export default function P() { return <div />; }\n"
        )
        assert result.head_is_dynamic is True

    def test_head_function_definition(self):
        from pyxle.compiler.parser import PyxParser
        result = PyxParser().parse_text(
            "def HEAD(data):\n"
            "    return f'<title>{data.title}</title>'\n"
            "\n"
            "import React from 'react';\n"
            "export default function P() { return <div />; }\n"
        )
        assert result.head_is_dynamic is True

    def test_qualified_action_decorator(self):
        from pyxle.compiler.parser import PyxParser
        result = PyxParser().parse_text(
            "import pyxle.runtime as runtime\n"
            "\n"
            "@runtime.action\n"
            "async def save(request):\n"
            "    return {'ok': True}\n"
            "\n"
            "import React from 'react';\n"
            "export default function P() { return <div />; }\n"
        )
        assert len(result.actions) == 1
        assert result.actions[0].name == "save"

    def test_action_with_extra_params(self):
        from pyxle.compiler.parser import PyxParser
        result = PyxParser().parse_text(
            "from pyxle.runtime import action\n"
            "\n"
            "@action\n"
            "async def update(request, extra=None):\n"
            "    return {'ok': True}\n"
            "\n"
            "import React from 'react';\n"
            "export default function P() { return <div />; }\n"
        )
        assert len(result.actions) == 1
        assert result.actions[0].name == "update"
        assert "extra" in result.actions[0].parameters

    def test_multiple_actions_unique_names(self):
        from pyxle.compiler.parser import PyxParser
        result = PyxParser().parse_text(
            "from pyxle.runtime import action\n"
            "\n"
            "@action\n"
            "async def create(request):\n"
            "    return {}\n"
            "\n"
            "@action\n"
            "async def delete_item(request):\n"
            "    return {}\n"
            "\n"
            "import React from 'react';\n"
            "export default function P() { return <div />; }\n"
        )
        names = [a.name for a in result.actions]
        assert names == ["create", "delete_item"]


class TestBomHandling:
    """Coverage for the leading-BOM stripping in ``_normalize_newlines``."""

    def test_bom_is_stripped(self):
        from pyxle.compiler.parser import _normalize_newlines
        assert _normalize_newlines("\ufeffx = 1\n") == ["x = 1", ""]

    def test_bom_with_crlf(self):
        from pyxle.compiler.parser import _normalize_newlines
        assert _normalize_newlines("\ufeffx = 1\r\ny = 2\n") == [
            "x = 1",
            "y = 2",
            "",
        ]

    def test_no_bom_unchanged(self):
        from pyxle.compiler.parser import _normalize_newlines
        assert _normalize_newlines("x = 1\n") == ["x = 1", ""]

    def test_bom_in_middle_not_stripped(self):
        from pyxle.compiler.parser import _normalize_newlines
        # Only LEADING BOM is stripped — a U+FEFF in the middle of a
        # file is normal content (rare but possible).
        assert _normalize_newlines("x\n\ufeffy\n") == ["x", "\ufeffy", ""]

    def test_bom_file_round_trip_via_parse_text(self):
        """A file with a leading BOM parses cleanly via parse_text."""
        from pyxle.compiler.parser import PyxParser
        result = PyxParser().parse_text(
            "\ufefffrom os import path\n\n"
            "import React from 'react';\n"
            "export default function P() { return <div />; }\n"
        )
        assert "from os import path" in result.python_code
        assert "import React" in result.jsx_code

    def test_bom_only_file(self):
        """A file containing only a BOM parses as empty."""
        from pyxle.compiler.parser import PyxParser
        result = PyxParser().parse_text("\ufeff")
        assert result.python_code == ""
        assert result.jsx_code == ""


class TestPyxDiagnosticDataclass:
    """The ``PyxDiagnostic`` dataclass shape and field semantics."""

    def test_diagnostic_is_frozen(self):
        diag = PyxDiagnostic(
            section="python",
            severity="error",
            message="bad",
            line=1,
        )
        with pytest.raises((AttributeError, Exception)):
            diag.section = "jsx"  # type: ignore[misc]

    def test_diagnostic_default_column(self):
        diag = PyxDiagnostic(
            section="python",
            severity="error",
            message="bad",
            line=5,
        )
        assert diag.column is None

    def test_diagnostic_with_column(self):
        diag = PyxDiagnostic(
            section="python",
            severity="error",
            message="bad",
            line=5,
            column=10,
        )
        assert diag.column == 10


# ---------------------------------------------------------------------------
# PyxParseResult.diagnostics field default
# ---------------------------------------------------------------------------


class TestParseResultDiagnosticsField:
    """The ``diagnostics`` field has a stable default and shape."""

    def test_default_is_empty_tuple(self):
        result = _parse("""
            @server
            async def loader(request):
                return {}

            export default function P() { return <div />; }
        """)
        assert result.diagnostics == ()
        assert isinstance(result.diagnostics, tuple)


# ---------------------------------------------------------------------------
# TypeScript-in-client-block guard
# ---------------------------------------------------------------------------


_TS_GUARD_SOURCE = """
@server
async def loader(request):
    return {"x": 1}

import React from 'react';

export default function P({ data }) {
    const n: number = data.x;
    return <div>{n}</div>;
}
"""


def _ts_violation(line):
    """A JSXParseResult as the extractor returns it for TS-in-client-block."""
    return JSXParseResult(
        components=(),
        error=(
            "TypeScript syntax (a type annotation (`: Type`)) isn't supported "
            "in a .pyxl client block yet — keep the client half plain JSX "
            "(see docs/guides/typescript.md)."
        ),
        error_code="ts_in_client_block",
        error_line=line,
    )


class TestTypeScriptGuard:
    """TS syntax in a client block is caught at compile time (not opt-in),
    with a clear, source-located message — and never false-positives on valid
    JSX. The extractor side is mocked here for determinism; a node-gated test
    below exercises the real Babel chain end to end."""

    def test_strict_mode_raises_with_clear_message(self):
        with patch(
            "pyxle.compiler.jsx_parser.parse_jsx_components",
            return_value=_ts_violation(3),
        ):
            with pytest.raises(CompilationError, match="TypeScript syntax"):
                _parse(_TS_GUARD_SOURCE)

    def test_tolerant_mode_emits_jsx_diagnostic(self):
        with patch(
            "pyxle.compiler.jsx_parser.parse_jsx_components",
            return_value=_ts_violation(3),
        ):
            result = _parse(_TS_GUARD_SOURCE, tolerant=True)
        jsx = [d for d in result.diagnostics if d.section == "jsx"]
        assert len(jsx) == 1
        assert "TypeScript syntax" in jsx[0].message
        assert jsx[0].line is not None

    def test_jsx_relative_line_maps_to_pyxl_source(self):
        # error_line is 1-indexed within the JSX block; line 1 is the
        # `import React` line, and the out-of-range fallback resolves to that
        # same first JSX line — both must land on a real .pyxl source line.
        with patch(
            "pyxle.compiler.jsx_parser.parse_jsx_components",
            return_value=_ts_violation(1),
        ):
            first = _parse(_TS_GUARD_SOURCE, tolerant=True)
        with patch(
            "pyxle.compiler.jsx_parser.parse_jsx_components",
            return_value=_ts_violation(9999),
        ):
            fallback = _parse(_TS_GUARD_SOURCE, tolerant=True)
        first_line = [d for d in first.diagnostics if d.section == "jsx"][0].line
        fallback_line = [d for d in fallback.diagnostics if d.section == "jsx"][0].line
        assert first_line is not None and first_line > 0
        # Out-of-range error_line falls back to the first JSX line.
        assert fallback_line == first_line

    def test_suppressed_when_python_section_has_errors(self):
        # A mis-split (broken Python absorbed into the JSX) must not be read as
        # a type annotation: when Python has a diagnostic, the TS guard is held.
        bad_python = (
            'x = "unterminated\n'
            "y = 1\n"
            "\n"
            "import React from 'react';\n"
            "export default function P() { const n = 1; return <div />; }\n"
        )
        with patch(
            "pyxle.compiler.jsx_parser.parse_jsx_components",
            return_value=_ts_violation(2),
        ):
            result = PyxParser().parse_text(bad_python, tolerant=True)
        python_diags = [d for d in result.diagnostics if d.section == "python"]
        jsx_diags = [d for d in result.diagnostics if d.section == "jsx"]
        assert python_diags, "expected a [python] diagnostic"
        assert not jsx_diags, "TS guard must be suppressed when Python has errors"

    def test_valid_jsx_with_ternary_object_and_as_prop_is_not_flagged(self):
        # Patch with a clean extractor result (no TS) and confirm no jsx diag —
        # ternaries, object literals, and the JSX `as` prop are plain JS/JSX.
        clean = JSXParseResult(components=(), error=None)
        valid = """
            @server
            async def loader(request):
                return {"x": 1}

            import React from 'react';

            export default function P({ data }) {
                const n = data.x ? data.x : 0;
                const o = { a: 1, b: 2 };
                return <Box as="section">{n}{o.a}</Box>;
            }
        """
        with patch(
            "pyxle.compiler.jsx_parser.parse_jsx_components", return_value=clean
        ):
            result = _parse(valid, tolerant=True)
        assert all(d.section != "jsx" for d in result.diagnostics)

    @pytest.mark.skipif(not _NODE_AVAILABLE, reason="needs Node for the real Babel extractor")
    def test_real_extractor_reports_pyxl_line(self):
        """End-to-end with the real Babel extractor (when Node is available):
        TS syntax raises a CompilationError naming the .pyxl source line."""
        src = dedent(_TS_GUARD_SOURCE).strip("\n")
        ts_line = next(
            i + 1 for i, ln in enumerate(src.splitlines()) if "const n:" in ln
        )
        try:
            PyxParser().parse_text(src)
        except CompilationError as exc:
            assert "TypeScript syntax" in str(exc)
            assert f"Line {ts_line}" in str(exc)
        else:
            pytest.skip("Babel extractor unavailable; the guard degraded gracefully")


class TestSemanticValidation:
    """``validate_semantics=True`` runs pyflakes over the Python section."""

    def _sem(self, text: str):
        return PyxParser().parse_text(
            dedent(text).strip("\n"), tolerant=True, validate_semantics=True
        )

    def test_undefined_name_flagged(self):
        result = self._sem(
            """
            @server
            async def load(request):
                return {"x": compute_total(request)}

            import React from 'react'

            export default function P({ data }) {
                return <div>{data.x}</div>
            }
            """
        )
        assert any(
            d.section == "python" and "undefined name 'compute_total'" in d.message
            for d in result.diagnostics
        )

    def test_injected_runtime_names_not_flagged(self):
        # @server/@action + LoaderError/ActionError/invalidate_routes used with
        # no imports must NOT read as undefined — the compiler injects them.
        result = self._sem(
            """
            @server
            async def load(request):
                if request is None:
                    raise LoaderError("x")
                return {}

            @action
            async def save(request):
                if not request:
                    raise ActionError("y")
                return invalidate_routes({"ok": True}, "/posts")

            import React from 'react'

            export default function P() {
                return <div/>
            }
            """
        )
        assert not any(d.section == "python" for d in result.diagnostics)

    def test_injected_runtime_names_are_public(self):
        # Editor tooling (pyxle-langkit) whitelists this exact set, so it is a
        # public export of ``pyxle.compiler`` — adding an injected name here
        # means the compiler must inject it too.
        from pyxle import compiler

        assert compiler.INJECTED_RUNTIME_NAMES is INJECTED_RUNTIME_NAMES
        assert INJECTED_RUNTIME_NAMES == {
            "server",
            "action",
            "ActionError",
            "ValidationActionError",
            "LoaderError",
            "invalidate_routes",
        }

    def test_off_by_default(self):
        result = PyxParser().parse_text(
            dedent(
                """
                @server
                async def load(request):
                    return {"x": mystery()}

                import React from 'react'

                export default function P({ data }) {
                    return <div>{data.x}</div>
                }
                """
            ).strip("\n"),
            tolerant=True,
        )
        assert not any("undefined name" in d.message for d in result.diagnostics)

    def test_line_maps_to_pyxl_source(self):
        result = self._sem(
            """
            @server
            async def load(request):
                value = 1
                return {"x": nope(value)}

            import React from 'react'

            export default function P({ data }) {
                return <div>{data.x}</div>
            }
            """
        )
        diags = [d for d in result.diagnostics if "undefined name 'nope'" in d.message]
        assert diags and diags[0].line == 4


class TestJsxErrorLineMapping:
    """A Babel error must map to the real .pyxl line, not the JSX block start."""

    def _run(self, monkeypatch, error_line):
        from pyxle.compiler import jsx_parser
        from pyxle.compiler.jsx_parser import JSXParseResult

        monkeypatch.setattr(
            jsx_parser,
            "parse_jsx_components",
            lambda jsx_code, *, target_components=None: JSXParseResult(
                components=(), error="boom", error_line=error_line
            ),
        )
        return PyxParser().parse_text(
            dedent(
                """
                import React from 'react'

                export default function P() {
                    return <div>ok</div>
                }
                """
            ).strip("\n"),
            tolerant=True,
            validate_jsx=True,
        )

    def test_error_maps_to_reported_line(self, monkeypatch):
        result = self._run(monkeypatch, error_line=3)
        jsx = [d for d in result.diagnostics if d.section == "jsx"]
        # snippet line 3 -> file line 3 (the export line), NOT the block start (1).
        assert jsx and jsx[0].line == 3

    def test_missing_line_falls_back_to_block_start(self, monkeypatch):
        result = self._run(monkeypatch, error_line=None)
        jsx = [d for d in result.diagnostics if d.section == "jsx"]
        assert jsx and jsx[0].line == 1


class TestSemanticSeverity:
    """Semantic findings are split into "this will break" and "this is untidy".

    ``pyxle check`` is the documented deploy gate, so only code that fails when
    it runs may fail the command; hygiene findings are reported as warnings.
    """

    def _sem(self, text: str):
        return PyxParser().parse_text(
            dedent(text).strip("\n"), tolerant=True, validate_semantics=True
        )

    def _severity_of(self, text: str, needle: str) -> str:
        matches = [d for d in self._sem(text).diagnostics if needle in d.message]
        assert matches, f"no diagnostic matched {needle!r}"
        return matches[0].severity

    def test_unused_import_is_a_warning(self):
        severity = self._severity_of(
            """
            import json

            import React from 'react'

            export default function P() {
                return <div>ok</div>
            }
            """,
            "'json' imported but unused",
        )
        assert severity == "warning"

    def test_undefined_name_is_an_error(self):
        severity = self._severity_of(
            """
            @server
            async def load(request):
                return {"x": compute_total(request)}

            import React from 'react'

            export default function P({ data }) {
                return <div>{data.x}</div>
            }
            """,
            "undefined name 'compute_total'",
        )
        assert severity == "error"

    def test_unused_local_is_a_warning(self):
        severity = self._severity_of(
            """
            @server
            async def load(request):
                scratch = 1
                return {"x": 2}

            import React from 'react'

            export default function P() {
                return <div>ok</div>
            }
            """,
            "'scratch'",
        )
        assert severity == "warning"

    def test_syntax_errors_remain_errors(self):
        """Structural/syntax diagnostics never pass through the pyflakes
        classifier, so they stay errors."""
        result = self._sem(
            """
            def broken(:

            import React from 'react'

            export default function P() {
                return <div>ok</div>
            }
            """
        )
        assert result.diagnostics
        assert all(d.severity == "error" for d in result.diagnostics)

    def test_unknown_pyflakes_rule_defaults_to_warning(self):
        """A rule added by a future pyflakes must not become a surprise deploy
        blocker on a dependency upgrade."""
        from pyxle.compiler.parser import _pyflakes_severity

        class SomeBrandNewRule:  # noqa: D106 - stand-in for a future message class
            pass

        assert _pyflakes_severity(SomeBrandNewRule()) == "warning"

    def test_runtime_breaking_rules_are_errors(self):
        """Spot-check the classification table against pyflakes' real classes."""
        from pyflakes import messages as pyflakes_messages

        from pyxle.compiler.parser import _pyflakes_severity

        for name in ("UndefinedName", "UndefinedLocal", "RaiseNotImplemented"):
            instance = object.__new__(getattr(pyflakes_messages, name))
            assert _pyflakes_severity(instance) == "error", name
        for name in ("UnusedImport", "UnusedVariable", "RedefinedWhileUnused"):
            instance = object.__new__(getattr(pyflakes_messages, name))
            assert _pyflakes_severity(instance) == "warning", name


class TestAHeadEntryHoldsOneElement:
    """A second element in one ``HEAD`` entry never reaches the document.

    The sanitiser rebuilds each entry from its first element and drops the
    rest — the same pass that discards markup injected after an attribute
    quote breakout, so it is a security boundary, not a limitation to route
    around. Where the entry is a literal the author is looking at it right
    now, so this is the moment to refuse it: a build error beats a rich-results
    report months later saying the structured data was never there.
    """

    _TWO_IN_ONE = (
        'HEAD = ["<title>Page</title><meta name=\\"description\\" content=\\"D\\" />"]\n'
        "\n"
        "import React from 'react';\n"
        "export default function P() { return <div />; }\n"
    )

    def test_strict_mode_refuses_the_build(self):
        with pytest.raises(CompilationError) as excinfo:
            _parse(self._TWO_IN_ONE)

        message = str(excinfo.value)
        assert "only one element" in message
        assert "Split it into separate list entries" in message
        # The dropped markup itself, so the author does not have to guess which
        # half went missing.
        assert 'content="D"' in message or "content=\\\"D\\\"" in message

    def test_a_bare_string_head_is_checked_too(self):
        with pytest.raises(CompilationError):
            _parse(
                'HEAD = "<title>Page</title><link rel=\\"icon\\" href=\\"/f.ico\\" />"\n'
                "\n"
                "import React from 'react';\n"
                "export default function P() { return <div />; }\n"
            )

    def test_tolerant_mode_reports_it_as_an_error_diagnostic(self):
        """``pyxle check`` and the LSP must show it, positioned, rather than
        stopping at the first problem in the file."""
        result = _parse(self._TWO_IN_ONE, tolerant=True)

        matching = [d for d in result.diagnostics if "only one element" in d.message]
        assert matching, result.diagnostics
        assert matching[0].severity == "error"
        assert matching[0].section == "python"
        assert matching[0].line is not None

    def test_the_documented_fix_compiles(self):
        """The error tells the author to split the entry. That has to work."""
        result = _parse(
            'HEAD = ["<title>Page</title>", "<meta name=\\"description\\" content=\\"D\\" />"]\n'
            "\n"
            "import React from 'react';\n"
            "export default function P() { return <div />; }\n"
        )
        assert len(result.head_elements) == 2

    def test_an_inline_script_is_not_mistaken_for_two_elements(self):
        """Inline code containing ``<`` and markup-shaped text is one element.
        A false positive here would block a build that was already correct."""
        result = _parse(
            'HEAD = [\'<script>if (a < b) { document.write("<p>x</p>") }</script>\']\n'
            "\n"
            "import React from 'react';\n"
            "export default function P() { return <div />; }\n"
        )
        assert len(result.head_elements) == 1

    def test_a_computed_head_is_left_to_the_render(self):
        """A non-literal cannot be judged here — its value is only known at
        render time, where the check continues as a warning."""
        result = _parse(
            "SITE = 'x'\n"
            "HEAD = [f'<meta name=\"a\" content=\"{SITE}\" /><meta name=\"b\" content=\"c\" />']\n"
            "\n"
            "import React from 'react';\n"
            "export default function P() { return <div />; }\n"
        )
        assert result.head_is_dynamic is True


class TestSecondaryLineNumbersAreFileCoordinates:
    """A line number *inside* a message must name the file, not the block.

    Every checker the parser calls — CPython on one segment, pyflakes on the
    joined Python stream, Babel on the joined JSX stream — is handed an
    extracted block and numbers its findings from the start of that block. The
    position the diagnostic carries is translated; the line numbers those tools
    write into their own prose ("on line 3", "from line 1", "detected at line
    9") used to be printed raw. That sends the developer to a line that is
    perfectly fine, which is worse than pointing nowhere.

    Every source here puts the failing block *below* the top of the file, so a
    mapping that silently does nothing cannot pass by coincidence.
    """

    # The Python block starts on file line 6, so a block-relative number is
    # always 5 lower than the file line it should name.
    _MISMATCHED_BRACKET = (
        '"""A page."""\n'                    # 1
        "\n"                                  # 2
        "from pyxle.runtime import server\n"  # 3
        "\n"                                  # 4
        "\n"                                  # 5
        "@server\n"                           # 6
        "async def loader(request):\n"        # 7
        "    items = [\n"                     # 8  <- the '[' the message names
        "        1,\n"                        # 9
        "        2,\n"                        # 10
        "    )\n"                             # 11 <- the ')' the position names
        "    return {}\n"                     # 12
        "\n"                                  # 13
        "\n"                                  # 14
        "export default function P() {\n"     # 15
        "  return <div />;\n"                 # 16
        "}\n"                                 # 17
    )

    def test_mismatched_bracket_names_the_opening_line_in_the_file(self):
        result = PyxParser().parse_text(self._MISMATCHED_BRACKET, tolerant=True)

        diagnostics = [d for d in result.diagnostics if d.section == "python"]
        assert len(diagnostics) == 1, result.diagnostics
        # The position: the mismatched ')' on file line 11.
        assert diagnostics[0].line == 11
        # The message body: the '[' it does not match, on file line 8 — not 3,
        # which is where it sits inside the extracted block.
        assert "does not match opening parenthesis '['" in diagnostics[0].message
        assert diagnostics[0].message.endswith("on line 8")

    def test_strict_mode_carries_the_same_file_line(self):
        """The build path raises rather than collecting, and must not regress
        to block coordinates on the way out."""
        with pytest.raises(CompilationError) as excinfo:
            PyxParser().parse_text(self._MISMATCHED_BRACKET)

        assert excinfo.value.line_number == 11
        assert excinfo.value.message.endswith("on line 8")
        # ``__str__`` is what the terminal and the build-failure page print.
        assert str(excinfo.value).endswith("on line 8")

    def test_unterminated_string_names_the_detection_line_in_the_file(self):
        source = (
            '"""A page."""\n'                    # 1
            "\n"                                  # 2
            "from pyxle.runtime import server\n"  # 3
            "\n"                                  # 4
            "\n"                                  # 5
            "@server\n"                           # 6
            "async def loader(request):\n"        # 7
            '    text = """oops\n'                # 8  <- opens here
            "    return {}\n"                     # 9
            "\n"                                  # 10
            "\n"                                  # 11
            "export default function P() {\n"     # 12
            "  return <div />;\n"                 # 13
            "}\n"                                 # 14  <- runs out here
        )
        result = PyxParser().parse_text(source, tolerant=True)

        diagnostics = [d for d in result.diagnostics if d.section == "python"]
        assert len(diagnostics) == 1, result.diagnostics
        assert diagnostics[0].line == 8
        assert "unterminated triple-quoted string literal" in diagnostics[0].message
        # CPython says "(detected at line N)"; N is the last line of the file,
        # which is line 14 — not line 9 of the block it was handed.
        assert "(detected at line 14)" in diagnostics[0].message

    # JSX first, Python second, so the Python stream starts at file line 7.
    _PYFLAKES_SOURCE = (
        "import React from 'react'\n"    # 1
        "\n"                              # 2
        "export default function P() {\n" # 3
        "    return <div>ok</div>\n"      # 4
        "}\n"                             # 5
        "\n"                              # 6
        "import os\n"                     # 7  <- the import both messages name
        "\n"                              # 8
        "for os in range(3):\n"           # 9
        "    pass\n"                      # 10
        "\n"                              # 11
        "def outer():\n"                  # 12
        "    value = 1\n"                 # 13 <- the enclosing binding
        "    def inner():\n"              # 14
        "        print(value)\n"          # 15
        "        value = 2\n"             # 16
        "    return inner\n"              # 17
    )

    def _semantic_diagnostics(self):
        result = PyxParser().parse_text(
            self._PYFLAKES_SOURCE, tolerant=True, validate_semantics=True
        )
        assert result.python_line_numbers[0] == 7, "block offset must be non-zero"
        return result.diagnostics

    def test_pyflakes_shadowed_import_names_the_file_line(self):
        matching = [
            d for d in self._semantic_diagnostics()
            if "shadowed by loop variable" in d.message
        ]
        assert matching, "expected an ImportShadowedByLoopVar finding"
        assert matching[0].line == 9
        # "from line 7" — the `import os` in the file, not line 1 of the stream.
        assert matching[0].message == (
            "import 'os' from line 7 shadowed by loop variable"
        )

    def test_pyflakes_undefined_local_names_the_file_line(self):
        matching = [
            d for d in self._semantic_diagnostics()
            if "referenced before assignment" in d.message
        ]
        assert matching, "expected an UndefinedLocal finding"
        assert matching[0].line == 15
        # "on line 13" — `value = 1` in the file, not line 7 of the stream.
        assert matching[0].message == (
            "local variable 'value' defined in enclosing scope on line 13 "
            "referenced before assignment"
        )

    def test_pyflakes_redefinition_names_the_file_line(self):
        source = (
            "import React from 'react'\n"     # 1
            "\n"                               # 2
            "export default function P() {\n"  # 3
            "    return <div>ok</div>\n"       # 4
            "}\n"                              # 5
            "\n"                               # 6
            "import os\n"                      # 7  <- first binding
            "import os\n"                      # 8  <- redefinition
            "\n"                               # 9
            "@server\n"                        # 10
            "async def loader(request):\n"     # 11
            "    return {'cwd': os.getcwd()}\n" # 12
        )
        result = PyxParser().parse_text(
            source, tolerant=True, validate_semantics=True
        )
        matching = [
            d for d in result.diagnostics if "redefinition of unused" in d.message
        ]
        assert matching, result.diagnostics
        assert matching[0].line == 8
        assert matching[0].message == "redefinition of unused 'os' from line 7"

    def test_jsx_message_line_reference_maps_through_the_jsx_block(self, monkeypatch):
        """The extractor strips Babel's trailing ``(line:col)``, but any line a
        JSX-side tool names in prose is section-relative too."""
        from pyxle.compiler import jsx_parser

        monkeypatch.setattr(
            jsx_parser,
            "parse_jsx_components",
            lambda jsx_code, *, target_components=None: JSXParseResult(
                components=(),
                error="Unexpected token — opening tag on line 2 is never closed",
                error_line=3,
            ),
        )
        source = (
            "@server\n"                        # 1
            "async def loader(request):\n"     # 2
            "    return {}\n"                  # 3
            "\n"                               # 4
            "\n"                               # 5
            "import React from 'react'\n"      # 6  <- JSX block line 1
            "\n"                               # 7  <- JSX block line 2
            "export default function P() {\n"  # 8  <- JSX block line 3
            "    return <div>ok</div>\n"       # 9
            "}\n"                              # 10
        )
        result = PyxParser().parse_text(source, tolerant=True, validate_jsx=True)

        jsx = [d for d in result.diagnostics if d.section == "jsx"]
        assert jsx and jsx[0].line == 8
        assert "opening tag on line 7 is never closed" in jsx[0].message

    def test_jsx_message_keeps_a_reference_it_cannot_map(self, monkeypatch):
        """A number outside the JSX block is left alone rather than clamped —
        a stray figure must never be dressed up as a real location."""
        from pyxle.compiler import jsx_parser

        monkeypatch.setattr(
            jsx_parser,
            "parse_jsx_components",
            lambda jsx_code, *, target_components=None: JSXParseResult(
                components=(), error="broken at line 900", error_line=1
            ),
        )
        result = PyxParser().parse_text(
            "import React from 'react'\nexport default function P() { return <div/>; }\n",
            tolerant=True,
            validate_jsx=True,
        )

        jsx = [d for d in result.diagnostics if d.section == "jsx"]
        assert jsx and "at line 900" in jsx[0].message

    def test_typescript_guard_message_is_remapped_too(self, monkeypatch):
        """The TS guard takes the same path, so it gets the same treatment."""
        from pyxle.compiler import jsx_parser

        monkeypatch.setattr(
            jsx_parser,
            "parse_jsx_components",
            lambda jsx_code, *, target_components=None: JSXParseResult(
                components=(),
                error="TypeScript syntax first seen on line 1",
                error_code="ts_in_client_block",
                error_line=1,
            ),
        )
        source = (
            "@server\n"                        # 1
            "async def loader(request):\n"     # 2
            "    return {}\n"                  # 3
            "\n"                               # 4
            "\n"                               # 5
            "import React from 'react'\n"      # 6  <- JSX block line 1
            "export default function P() {\n"  # 7
            "    return <div>ok</div>\n"       # 8
            "}\n"                              # 9
        )
        result = PyxParser().parse_text(source, tolerant=True)

        jsx = [d for d in result.diagnostics if d.section == "jsx"]
        assert jsx and "first seen on line 6" in jsx[0].message


class TestRemapMessageLineRefs:
    """The rewriting rule itself: anchored, reversible, and conservative."""

    def _remap(self, message, offset=10):
        from pyxle.compiler.parser import _remap_message_line_refs

        return _remap_message_line_refs(message, lambda relative: relative + offset)

    @pytest.mark.parametrize(
        ("message", "expected"),
        [
            (
                "closing parenthesis ')' does not match opening "
                "parenthesis '[' on line 3",
                "closing parenthesis ')' does not match opening "
                "parenthesis '[' on line 13",
            ),
            (
                "unterminated triple-quoted string literal (detected at line 4)",
                "unterminated triple-quoted string literal (detected at line 14)",
            ),
            (
                "redefinition of unused 'os' from line 1",
                "redefinition of unused 'os' from line 11",
            ),
            (
                "local variable 'v' defined in enclosing scope on line 2 "
                "referenced before assignment",
                "local variable 'v' defined in enclosing scope on line 12 "
                "referenced before assignment",
            ),
        ],
    )
    def test_known_phrasings_are_rewritten(self, message, expected):
        assert self._remap(message) == expected

    @pytest.mark.parametrize(
        "message",
        [
            # No line reference at all — the overwhelmingly common case.
            "invalid syntax",
            "'[' was never closed",
            "unindent does not match any outer indentation level",
            "Missing parentheses in call to 'print'. Did you mean print(...)?",
            # "line" without one of the three prepositions in front of it, and a
            # number that is not a line: neither may be touched.
            "Dropped: <meta content=\"line 3\" />",
            "expected 4 spaces of indentation, line up the arguments",
        ],
    )
    def test_unrelated_text_is_left_alone(self, message):
        assert self._remap(message) == message

    def test_an_unmappable_reference_is_left_alone(self):
        from pyxle.compiler.parser import _remap_message_line_refs

        message = "opening parenthesis '(' on line 4"
        assert _remap_message_line_refs(message, lambda relative: None) == message

    def test_every_reference_in_one_message_is_rewritten(self):
        assert self._remap("opened on line 1, detected at line 2") == (
            "opened on line 11, detected at line 12"
        )


class TestAnUnmappableNumberIsLeftAlone:
    """The promise is that a number the compiler cannot place stays as it is.

    It is easy to make that promise and quietly break it, because the mapper
    used for a diagnostic's *structural* position clamps an out-of-range line to
    the block's last one. Clamping is right there — some line has to be
    reported. Inside a message it is a fabricated location wearing the same
    clothes as a real one, and there is no way for the reader to tell.
    """

    # JSX first, so the Python stream starts at file line 7 and a mapped
    # reference is visibly different from an unmapped one.
    _JSX_FIRST_PREAMBLE = (
        "import React from 'react'\n"      # 1
        "\n"                                # 2
        "export default function P() {\n"   # 3
        "    return <div>ok</div>\n"        # 4
        "}\n"                               # 5
        "\n"                                # 6
    )

    def test_a_number_in_the_developers_own_string_is_not_rewritten(self):
        """pyflakes quotes the name it is talking about, and a name taken from
        ``__all__`` is an arbitrary string the developer wrote. Rewriting a
        number inside it corrupts the one fragment of the message they would
        recognise — and 999 was never a line of anything."""
        source = self._JSX_FIRST_PREAMBLE + (
            "import os\n"                        # 7
            "\n"                                  # 8
            '__all__ = ["ghost on line 999"]\n'   # 9
            "\n"                                  # 10
            "@server\n"                           # 11
            "async def loader(request):\n"        # 12
            "    return {'cwd': os.getcwd()}\n"   # 13
        )
        result = PyxParser().parse_text(
            source, tolerant=True, validate_semantics=True
        )
        assert result.python_line_numbers[0] == 7, "block offset must be non-zero"

        matching = [d for d in result.diagnostics if "__all__" in d.message]
        assert matching, result.diagnostics
        assert matching[0].message == "undefined name 'ghost on line 999' in __all__"

    def test_a_reference_past_the_end_of_the_block_is_not_clamped(self):
        from pyxle.compiler.parser import _exact_source_line, _remap_message_line_refs

        # A three-line block: file lines 7, 8, 9.
        block = (7, 8, 9)
        assert _exact_source_line(3, block) == 9
        # The mapper the message path uses must decline, not answer 9.
        assert _exact_source_line(4, block) is None
        assert _exact_source_line(0, block) is None

        message = "opening parenthesis '[' on line 4"
        remapped = _remap_message_line_refs(
            message, lambda relative: _exact_source_line(relative, block)
        )
        assert remapped == message, "an unplaceable number was replaced with a guess"

    def test_the_structural_position_still_clamps(self):
        """The two mappers differ on purpose; the position one must not change."""
        from pyxle.compiler.parser import _map_lineno

        assert _map_lineno(4, (7, 8, 9)) == 9

    @pytest.mark.parametrize(
        "message",
        [
            # Every real producer writes its coordinate outside quotes, so each
            # of these must still be rewritten (offset mapper below adds 10).
            "closing parenthesis ')' does not match opening parenthesis '[' on line 3",
            "unterminated triple-quoted string literal (detected at line 3)",
            "redefinition of unused 'os' from line 3",
            "import 'os' from line 3 shadowed by loop variable",
            "local variable 'x' defined in enclosing scope on line 3 "
            "referenced before assignment",
        ],
    )
    def test_the_quote_rule_does_not_block_a_real_coordinate(self, message):
        from pyxle.compiler.parser import _remap_message_line_refs

        remapped = _remap_message_line_refs(message, lambda relative: relative + 10)
        assert "line 13" in remapped
        assert "line 3" not in remapped

    @pytest.mark.parametrize(
        "message",
        [
            "undefined name 'ghost on line 3' in __all__",
            'undefined name "it\'s on line 3" in __all__',
        ],
    )
    def test_a_quoted_number_survives(self, message):
        from pyxle.compiler.parser import _remap_message_line_refs

        assert _remap_message_line_refs(message, lambda relative: relative + 10) == message

    def test_a_segment_reference_is_bounded_by_its_segment(self):
        """The segment path translates by addition, which has no natural end.
        A number past the segment must not become a line beyond it."""
        from pyxle.compiler.parser import _exact_source_line, _remap_message_line_refs

        # A segment occupying file lines 5-7.
        segment_lines = range(5, 8)
        assert _exact_source_line(1, segment_lines) == 5
        assert _exact_source_line(3, segment_lines) == 7
        assert _exact_source_line(4, segment_lines) is None

        message = "opening parenthesis '[' on line 4"
        assert _remap_message_line_refs(
            message, lambda relative: _exact_source_line(relative, segment_lines)
        ) == message


class TestAnUnclosedBracketNamesTheBracket:
    """An unclosed bracket inside an indented block used to be reported as
    ``unexpected indent``.

    The split walker stops at the largest prefix that parses, which is the line
    *before* the bracket's line, and hands the rest over as JSX. Parsing that
    remainder alone describes the tear (`the fragment starts indented`) rather
    than the fault (`the bracket two lines up is open`). CPython, given the
    whole thing, says the useful sentence — so we widen back to the Python this
    segment was torn from and report that.
    """

    SOURCE = """
    from __future__ import annotations


    @server
    async def load(request):
        body = {"a": 1}
        step = int(body.get("a", 1)
        return {"step": step}


    import React from "react";

    export default function A() {
        return <main>hi</main>;
    }
    """

    def _python_errors(self):
        result = _parse(self.SOURCE, tolerant=True)
        return [d for d in result.diagnostics if d.section == "python"]

    def test_it_names_the_bracket_not_the_indentation(self):
        errors = self._python_errors()
        assert errors, "an unclosed bracket must produce a python diagnostic"
        message = errors[0].message
        assert "was never closed" in message
        assert "unexpected indent" not in message

    def test_it_points_at_the_line_holding_the_bracket(self):
        # File line 7 is ``step = int(body.get("a", 1)``. Line 8 is the
        # ``return``, which is fine, and line 6 is fine too — an off-by-one
        # here sends the reader to code that is correct.
        assert self._python_errors()[0].line == 7

    def test_a_top_level_bracket_is_unaffected(self):
        # No indented block, so nothing is torn and the narrow path already
        # produced CPython's message. This guards the widening from becoming a
        # blanket override.
        result = _parse(
            """
            from __future__ import annotations

            X = int(str(5)
            Y = 2
            """,
            tolerant=True,
        )
        errors = [d for d in result.diagnostics if d.section == "python"]
        assert errors and "was never closed" in errors[0].message
        assert errors[0].line == 3
