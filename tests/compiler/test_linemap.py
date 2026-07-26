"""Unit tests for the compiler's source line maps (``pyxle/compiler/linemap.py``).

The line map is what makes the Pyxle debugger work: the writer persists an
emitted-line → ``.pyxl``-line map inside every generated server module, and the
dev server's import loader replays that map at import time so tracebacks and
debugger breakpoints reference the original ``.pyxl`` file.
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

import pyxle.compiler.linemap as linemap
from pyxle.compiler.linemap import (
    LINE_MAP_DUNDER,
    SOURCE_DUNDER,
    PyxlDebugInfo,
    PyxlSourceFileLoader,
    _SpanMapper,
    build_line_map,
    compress_to_spans,
    extract_debug_info,
    remap_code,
    render_debug_footer,
)


# ---------------------------------------------------------------------------
# _split_lines
# ---------------------------------------------------------------------------


def test_split_lines_drops_trailing_empty_line() -> None:
    # A final newline must not produce a phantom trailing "" line.
    assert linemap._split_lines("a\nb\n") == ["a", "b"]
    # …but a file with no trailing newline keeps its last line.
    assert linemap._split_lines("a\nb") == ["a", "b"]
    # Interior blank lines are preserved; only the single trailing "" is dropped.
    assert linemap._split_lines("a\n\nb\n") == ["a", "", "b"]


def test_split_lines_empty_string_has_no_lines() -> None:
    assert linemap._split_lines("") == []


def test_split_lines_ignores_form_feed_and_vertical_tab() -> None:
    # Unlike str.splitlines, a form-feed / vertical-tab inside a line is an
    # ordinary in-line character — the parser splits on "\n" only.
    assert linemap._split_lines("a\x0cb\nc") == ["a\x0cb", "c"]
    assert linemap._split_lines("a\x0bb\nc") == ["a\x0bb", "c"]
    # str.splitlines would have split the form-feed into two entries — the very
    # desync _split_lines exists to avoid.
    assert "a\x0cb".splitlines() == ["a", "b"]


# ---------------------------------------------------------------------------
# build_line_map
# ---------------------------------------------------------------------------


def test_build_line_map_identity_without_injections() -> None:
    pre = "a = 1\nb = 2\n"
    assert build_line_map(pre, (3, 4), pre) == (3, 4)


def test_build_line_map_marks_injected_lines_as_none() -> None:
    pre = "@server\nasync def load(request):\n    return {}\n"
    final = (
        "from pyxle.runtime import server\n"
        "@server\n"
        "async def load(request):\n"
        "    return {}\n"
    )
    assert build_line_map(pre, (5, 6, 7), final) == (None, 5, 6, 7)


def test_build_line_map_injection_after_docstring_and_future_prelude() -> None:
    """Imports injected mid-file (after the docstring + ``__future__`` prelude)
    leave an unmapped hole in the middle of an otherwise contiguous map."""
    pre = dedent(
        '''\
        """Doc."""
        from __future__ import annotations

        @server
        def load(request):
            return {}
        '''
    )
    final = dedent(
        '''\
        """Doc."""
        from __future__ import annotations
        from pyxle.runtime import server

        @server
        def load(request):
            return {}
        '''
    )
    # The .pyxl lines are non-contiguous, mimicking a multi-segment page.
    assert build_line_map(pre, (2, 3, 4, 8, 9, 10), final) == (
        2,
        3,
        None,
        4,
        8,
        9,
        10,
    )


def test_build_line_map_trailing_injected_lines() -> None:
    pre = "x = 1\n"
    final = "x = 1\n\n__pyxle_source__ = 'a.pyxl'\n__pyxle_line_map__ = ()\n"
    assert build_line_map(pre, (1,), final) == (1, None, None, None)


def test_build_line_map_empty_inputs() -> None:
    assert build_line_map("", (), "") == ()


def test_build_line_map_form_feed_in_string_literal_does_not_desync() -> None:
    """A form-feed inside a Python string literal is one line to the parser, so
    the map must treat it identically to a plain-newline baseline. If the map
    split on form-feeds (like ``str.splitlines``) the line accounting would
    desync against ``python_line_numbers``."""
    pre_ff = 'x = "a\x0cb"\ny = 2\n'
    pre_plain = 'x = "ab"\ny = 2\n'
    line_numbers = (5, 6)
    # No injections: final == pre, so every line maps one-to-one.
    ff_map = build_line_map(pre_ff, line_numbers, pre_ff)
    plain_map = build_line_map(pre_plain, line_numbers, pre_plain)
    assert ff_map == plain_map == (5, 6)


# ---------------------------------------------------------------------------
# compress_to_spans
# ---------------------------------------------------------------------------


def test_compress_to_spans_empty_and_all_unmapped() -> None:
    assert compress_to_spans(()) == ()
    assert compress_to_spans((None, None, None)) == ()


def test_compress_to_spans_single_contiguous_run() -> None:
    assert compress_to_spans((4, 5, 6)) == ((1, 4, 3),)


def test_compress_to_spans_splits_on_unmapped_gap() -> None:
    assert compress_to_spans((4, 5, None, None, 9, 10)) == ((1, 4, 2), (5, 9, 2))


def test_compress_to_spans_splits_on_source_discontinuity() -> None:
    # Emitted lines are consecutive but the source jumps (multi-segment page).
    assert compress_to_spans((5, 6, 9, 10)) == ((1, 5, 2), (3, 9, 2))


def test_compress_to_spans_leading_unmapped_lines() -> None:
    assert compress_to_spans((None, None, 7)) == ((3, 7, 1),)


# ---------------------------------------------------------------------------
# render_debug_footer
# ---------------------------------------------------------------------------


def test_render_debug_footer_empty_spans() -> None:
    footer = render_debug_footer("pages/a.pyxl", ())
    assert footer == (
        "\n__pyxle_source__ = 'pages/a.pyxl'\n__pyxle_line_map__ = ()\n"
    )


def test_render_debug_footer_single_span_is_a_tuple_literal() -> None:
    # The single-span form needs the trailing comma or the literal would
    # collapse to a plain 3-tuple instead of a tuple of spans.
    footer = render_debug_footer("a.pyxl", ((1, 2, 3),))
    assert f"{LINE_MAP_DUNDER} = ((1, 2, 3),)" in footer


def test_render_debug_footer_multiple_spans() -> None:
    footer = render_debug_footer("a.pyxl", ((1, 2, 3), (7, 20, 4)))
    assert f"{LINE_MAP_DUNDER} = ((1, 2, 3), (7, 20, 4))" in footer


def test_render_debug_footer_round_trips_through_extract() -> None:
    # Path repr must survive awkward characters (quotes in file names).
    source = "pages/it's a page.pyxl"
    spans = ((1, 4, 5), (9, 20, 2))
    text = "x = 1\ny = 2\n" + render_debug_footer(source, spans)
    info = extract_debug_info(text)
    assert info == PyxlDebugInfo(source_relative_posix=source, spans=spans)


# ---------------------------------------------------------------------------
# extract_debug_info
# ---------------------------------------------------------------------------


def test_extract_debug_info_absent_footer() -> None:
    assert extract_debug_info("x = 1\ny = 2\n") is None


def test_extract_debug_info_requires_both_dunders() -> None:
    assert extract_debug_info(f"{SOURCE_DUNDER} = 'a.pyxl'\n") is None
    assert extract_debug_info(f"{LINE_MAP_DUNDER} = ((1, 1, 1),)\n") is None


def test_extract_debug_info_accepts_empty_span_tuple() -> None:
    text = f"{SOURCE_DUNDER} = 'a.pyxl'\n{LINE_MAP_DUNDER} = ()\n"
    info = extract_debug_info(text)
    assert info is not None
    assert info.spans == ()


def test_extract_debug_info_ignores_trailing_blank_lines() -> None:
    # The footer is anchored to the last two NON-blank lines, so trailing
    # whitespace/newlines after it don't hide it.
    text = render_debug_footer("a.pyxl", ((1, 1, 2),)) + "\n\n   \n"
    info = extract_debug_info(text)
    assert info is not None
    assert info.source_relative_posix == "a.pyxl"
    assert info.spans == ((1, 1, 2),)


def test_extract_debug_info_prefers_trailing_footer_over_user_globals() -> None:
    """A page that defines same-named globals in user code must not shadow the
    real footer the writer appends after it — the scan runs from the end."""
    text = (
        f"{SOURCE_DUNDER} = 'decoy.pyxl'\n"
        f"{LINE_MAP_DUNDER} = ((9, 9, 9),)\n"
        "def f():\n"
        f"    {LINE_MAP_DUNDER} = 'local variable, not a footer'\n"
        "    return 1\n" + render_debug_footer("real.pyxl", ((1, 1, 5),))
    )
    info = extract_debug_info(text)
    assert info is not None
    assert info.source_relative_posix == "real.pyxl"
    assert info.spans == ((1, 1, 5),)


def test_extract_debug_info_malformed_real_footer_does_not_pick_up_decoy() -> None:
    """The scan commits to the FIRST line-map seen from EOF (the real footer)
    even when it fails to parse — it must never fall through to a valid decoy
    a page happened to assign earlier in its own module-level code."""
    text = (
        f"{SOURCE_DUNDER} = 'decoy.pyxl'\n"
        f"{LINE_MAP_DUNDER} = ((1, 1, 1),)\n"  # a valid decoy in user code
        "value = 1\n"
        f"{LINE_MAP_DUNDER} = ((9, 9, 9)\n"  # malformed real footer (SyntaxError)
    )
    # Committed to the malformed footer's map (None), not the earlier decoy.
    assert extract_debug_info(text) is None


def test_extract_debug_info_ignores_stray_line_map_above_the_footer() -> None:
    # A stray line-map assignment earlier in the module is not the footer — only
    # the final adjacent source+line-map pair (what the writer emits) is read.
    text = (
        f"{LINE_MAP_DUNDER} = ((2, 2, 2),)\n"  # stray, in user code
        f"{SOURCE_DUNDER} = 'a.pyxl'\n"
        f"{LINE_MAP_DUNDER} = ((1, 1, 1),)\n"  # the real footer
    )
    info = extract_debug_info(text)
    assert info is not None
    assert info.source_relative_posix == "a.pyxl"
    assert info.spans == ((1, 1, 1),)


def test_extract_debug_info_rejects_footerless_module_with_decoy_dunders() -> None:
    """A footerless module whose own code assigns both dunders (even in footer
    order) must not be mistaken for a real footer: the writer always appends the
    footer as the LAST two non-blank lines, so dunders with code after them are
    not a footer. Anchoring to that shape closes the footerless-decoy misread."""
    text = (
        f"{SOURCE_DUNDER} = 'somefile.py'\n"
        f"{LINE_MAP_DUNDER} = ((1, 2, 3),)\n"
        "def handler():\n"
        "    return 1\n"
    )
    assert extract_debug_info(text) is None


@pytest.mark.parametrize(
    "literal",
    [
        "[(1, 2, 3)]",  # not a tuple
        "(1, 2, 3)",  # tuple, but items are not span tuples
        "((1, 2),)",  # wrong arity
        "((1, 'a', 3),)",  # non-int member
        "((1, 2.0, 3),)",  # float is not an int
        "((True, 2, 3),)",  # bool is an int subclass but never a line number
        "((0, 1, 1),)",  # zero — line numbers are 1-based
        "((1, -2, 3),)",  # negative
        "((1, 2, 3)",  # malformed literal (SyntaxError)
        "make_spans()",  # non-literal expression (ValueError)
    ],
)
def test_extract_debug_info_rejects_invalid_span_literals(literal: str) -> None:
    text = f"x = 1\n{SOURCE_DUNDER} = 'a.pyxl'\n{LINE_MAP_DUNDER} = {literal}\n"
    assert extract_debug_info(text) is None


@pytest.mark.parametrize(
    "literal",
    [
        "42",  # not a string
        "b'bytes.pyxl'",  # bytes are not a path string
        "None",
        "'unterminated",  # malformed literal
    ],
)
def test_extract_debug_info_rejects_invalid_source_literals(literal: str) -> None:
    text = f"{SOURCE_DUNDER} = {literal}\n{LINE_MAP_DUNDER} = ((1, 1, 1),)\n"
    assert extract_debug_info(text) is None


def test_extract_debug_info_last_source_line_is_authoritative() -> None:
    # The scan stops at the LAST source dunder; if that one is invalid, an
    # earlier valid-looking pair must not resurrect a stale map.
    text = (
        f"{SOURCE_DUNDER} = 'stale.pyxl'\n"
        f"{LINE_MAP_DUNDER} = ((1, 1, 1),)\n"
        f"{SOURCE_DUNDER} = 42\n"
    )
    assert extract_debug_info(text) is None


# ---------------------------------------------------------------------------
# _SpanMapper
# ---------------------------------------------------------------------------


def test_span_mapper_maps_lines_inside_spans() -> None:
    mapper = _SpanMapper(((3, 10, 2), (7, 20, 3)))
    assert mapper.map_line(3) == 10
    assert mapper.map_line(4) == 11
    assert mapper.map_line(7) == 20
    assert mapper.map_line(9) == 22


def test_span_mapper_before_first_span_lands_on_first_mapped_line() -> None:
    # A breakpoint dragged onto an injected import (emitted before any user
    # code) lands on the first real statement.
    mapper = _SpanMapper(((3, 10, 2),))
    assert mapper.map_line(1) == 10
    assert mapper.map_line(2) == 10


def test_span_mapper_between_spans_prefers_following_span() -> None:
    mapper = _SpanMapper(((3, 10, 2), (7, 20, 3)))
    assert mapper.map_line(5) == 20
    assert mapper.map_line(6) == 20


def test_span_mapper_past_last_span_clamps_to_final_mapped_line() -> None:
    mapper = _SpanMapper(((3, 10, 2), (7, 20, 3)))
    # The footer's own lines sit past the last span.
    assert mapper.map_line(10) == 22
    assert mapper.map_line(99) == 22


def test_span_mapper_sorts_unordered_spans() -> None:
    mapper = _SpanMapper(((7, 20, 3), (3, 10, 2)))
    assert mapper.map_line(3) == 10
    assert mapper.map_line(8) == 21


# ---------------------------------------------------------------------------
# remap_code
# ---------------------------------------------------------------------------


def _write_pair(tmp_path: Path, module_body: str, spans, pyxl_text: str = "") -> tuple[Path, Path]:
    """Write a generated ``.py`` (body + footer) beside its ``.pyxl`` origin."""
    pyxl_path = tmp_path / "page.pyxl"
    pyxl_path.write_text(pyxl_text or "# placeholder .pyxl\n", encoding="utf-8")
    module_path = tmp_path / "page.py"
    module_path.write_text(
        module_body + render_debug_footer("page.pyxl", spans), encoding="utf-8"
    )
    return module_path, pyxl_path


def test_remap_code_returns_none_without_footer(tmp_path: Path) -> None:
    module_path = tmp_path / "plain.py"
    module_path.write_text("x = 1\n", encoding="utf-8")
    assert remap_code("x = 1\n", module_path) is None


def test_remap_code_returns_none_for_empty_spans(tmp_path: Path) -> None:
    module_path, _ = _write_pair(tmp_path, "x = 1\n", ())
    assert remap_code(module_path.read_text(encoding="utf-8"), module_path) is None


def test_remap_code_returns_none_when_pyxl_is_missing(tmp_path: Path) -> None:
    module_path, pyxl_path = _write_pair(tmp_path, "x = 1\n", ((1, 1, 1),))
    pyxl_path.unlink()
    assert remap_code(module_path.read_text(encoding="utf-8"), module_path) is None


def test_remap_code_rewrites_filename_and_line_numbers(tmp_path: Path) -> None:
    body = dedent(
        """\
        def outer():
            return inner()

        def inner():
            return 42
        """
    )
    # The .pyxl carried three header lines before this Python segment.
    module_path, pyxl_path = _write_pair(tmp_path, body, ((1, 4, 5),))
    code = remap_code(module_path.read_text(encoding="utf-8"), module_path)
    assert code is not None
    assert code.co_filename == str(pyxl_path.resolve())

    namespace: dict[str, object] = {}
    exec(code, namespace)  # noqa: S102 - executing the module under test
    outer = namespace["outer"]
    inner = namespace["inner"]
    assert outer.__code__.co_filename == str(pyxl_path.resolve())
    assert outer.__code__.co_firstlineno == 4
    assert inner.__code__.co_firstlineno == 7
    assert outer() == 42


def test_remap_code_clamps_multiline_node_ending_on_unmapped_line(tmp_path: Path) -> None:
    """A multi-line node whose last line is unmapped (e.g. absorbed by an
    injected region) must clamp its end to the span's final line — an end
    before the start would make ``compile`` reject the tree."""
    body = dedent(
        """\
        def f(
            a,
        ):
            return a
        """
    )
    # Only the first two emitted lines are mapped; the node's end_lineno (4)
    # falls past the span and resolves by clamping.
    module_path, pyxl_path = _write_pair(tmp_path, body, ((1, 10, 2),))
    code = remap_code(module_path.read_text(encoding="utf-8"), module_path)
    assert code is not None

    namespace: dict[str, object] = {}
    exec(code, namespace)  # noqa: S102 - executing the module under test
    f = namespace["f"]
    assert f.__code__.co_firstlineno == 10
    assert f(5) == 5


def test_loader_survives_adversarial_map_that_inverts_a_node_range(tmp_path: Path) -> None:
    # Adversarial map: the node's end line maps BELOW its start line, which
    # the clamp collapses onto one line with inverted columns — ``compile``
    # rejects that tree. The import loader's fault barrier must absorb it and
    # fall back to the stock import rather than failing the module.
    body = "x = [\n]\n"
    module_path, _ = _write_pair(tmp_path, body, ((1, 20, 1), (2, 5, 1)))
    loader = PyxlSourceFileLoader("inverted_mod", str(module_path))
    code = loader.get_code("inverted_mod")
    assert code.co_filename == str(module_path)


def test_remap_code_tolerates_node_without_end_lineno(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A node carrying ``lineno`` but no ``end_lineno`` (defensive: modern
    CPython always sets both) is still remapped without touching an end."""
    import ast

    body = "x = 1\n"
    module_path, pyxl_path = _write_pair(tmp_path, body, ((1, 9, 1),))
    module_text = module_path.read_text(encoding="utf-8")

    real_parse = ast.parse

    def parse_without_end_lineno(text: str, *args, **kwargs):
        tree = real_parse(text, *args, **kwargs)
        for node in ast.walk(tree):
            if hasattr(node, "end_lineno"):
                del node.end_lineno
        return tree

    monkeypatch.setattr(linemap.ast, "parse", parse_without_end_lineno)
    code = remap_code(module_text, module_path)
    assert code is not None
    assert code.co_filename == str(pyxl_path.resolve())


# ---------------------------------------------------------------------------
# PyxlSourceFileLoader
# ---------------------------------------------------------------------------


def test_loader_get_code_returns_remapped_code(tmp_path: Path) -> None:
    body = "def f():\n    return 1\n"
    module_path, pyxl_path = _write_pair(tmp_path, body, ((1, 6, 2),))
    loader = PyxlSourceFileLoader("page_mod", str(module_path))
    code = loader.get_code("page_mod")
    assert code.co_filename == str(pyxl_path.resolve())


def test_loader_get_code_falls_through_without_footer(tmp_path: Path) -> None:
    module_path = tmp_path / "api.py"
    module_path.write_text("value = 3\n", encoding="utf-8")
    loader = PyxlSourceFileLoader("api_mod", str(module_path))
    code = loader.get_code("api_mod")
    assert code.co_filename == str(module_path)


def test_loader_get_code_falls_through_on_remap_fault(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A transient read fault during remapping degrades to the stock import
    path instead of failing the import."""
    body = "def f():\n    return 1\n"
    module_path, _ = _write_pair(tmp_path, body, ((1, 6, 2),))
    loader = PyxlSourceFileLoader("flaky_mod", str(module_path))

    real_get_data = loader.get_data
    calls = {"count": 0}

    def flaky_get_data(path: str) -> bytes:
        calls["count"] += 1
        if calls["count"] == 1:
            raise OSError("transient read failure")
        return real_get_data(path)

    monkeypatch.setattr(loader, "get_data", flaky_get_data)
    code = loader.get_code("flaky_mod")
    # Remap was skipped, so the code compiles against the generated .py.
    assert code.co_filename == str(module_path)
    assert calls["count"] >= 2


def test_loader_get_code_preserves_stock_error_for_unreadable_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = "def f():\n    return 1\n"
    module_path, _ = _write_pair(tmp_path, body, ((1, 6, 2),))
    loader = PyxlSourceFileLoader("unreadable_mod", str(module_path))

    def broken_get_data(path: str) -> bytes:
        raise OSError("permission denied")

    monkeypatch.setattr(loader, "get_data", broken_get_data)
    # The remap fault is swallowed; the stock loader's own failure surfaces,
    # matching plain SourceFileLoader semantics.
    with pytest.raises(OSError, match="permission denied"):
        loader.get_code("unreadable_mod")
