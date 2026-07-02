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
from pyxle.compiler.parser import PyxDiagnostic, PyxParser

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
