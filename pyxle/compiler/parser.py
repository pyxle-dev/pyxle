"""AST-driven parser that splits ``.pyxl`` files into Python and JSX segments.

The parser is purely AST-driven: no fence markers, no string-based
directives, no per-line heuristics. The Python/JSX boundary is found
by walking the source greedily with :func:`ast.parse` — at each cursor
position the parser tries to grow the largest valid Python prefix; if
none is possible, it grows a JSX segment until Python resumes. This
cleanly handles arbitrary alternation of Python and JSX blocks
(``python | jsx | python | jsx | ...``) in any order, including
JSX-first files, single-section files, empty files.

The parser exposes a structured syntax error reporting mechanism:
``PyxDiagnostic`` entries on ``PyxParseResult.diagnostics``, populated
in tolerant mode so IDEs and ``pyxle check`` can surface every error
per file at once instead of stopping at the first.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Literal, Sequence

from .exceptions import CompilationError
from .head_elements import find_discarded_head_content

# ---------------------------------------------------------------------------
# Public dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LoaderDetails:
    """Metadata about an ``@server``-decorated loader function."""

    name: str
    line_number: int
    is_async: bool
    parameters: Sequence[str]


@dataclass(frozen=True)
class ActionDetails:
    """Metadata about an ``@action``-decorated function."""

    name: str
    line_number: int
    is_async: bool
    parameters: Sequence[str]


@dataclass(frozen=True)
class WebsocketDetails:
    """Metadata about a page's ``async def websocket(ws)`` handler.

    Detected by convention (a module-scope coroutine named ``websocket``),
    not a decorator — mirroring how API modules expose a ``websocket``
    callable. A page that declares one also serves a WebSocket route at its
    path.
    """

    name: str
    line_number: int
    is_async: bool
    parameters: Sequence[str]


@dataclass(frozen=True)
class PyxDiagnostic:
    """A syntax or structural error found during parsing.

    Diagnostics are populated when ``tolerant=True``. In strict mode the
    parser raises :class:`CompilationError` on the first error and never
    populates ``PyxParseResult.diagnostics``.

    Attributes
    ----------
    section:
        Which part of the file the error originated in: ``"python"`` for
        Python AST/semantic errors, ``"jsx"`` for JSX/Babel errors.
    severity:
        Either ``"error"`` or ``"warning"``.
    message:
        Human-readable error message.
    line:
        1-indexed line number in the original ``.pyxl`` source, or
        ``None`` if the position is unknown.
    column:
        1-indexed column number, or ``None``.
    """

    section: Literal["python", "jsx"]
    severity: Literal["error", "warning"]
    message: str
    line: int | None
    column: int | None = None


@dataclass(frozen=True)
class PyxParseResult:
    """The product of parsing a ``.pyxl`` file."""

    python_code: str
    jsx_code: str
    loader: LoaderDetails | None
    python_line_numbers: Sequence[int]
    jsx_line_numbers: Sequence[int]
    head_elements: tuple[str, ...]
    head_is_dynamic: bool
    script_declarations: tuple[dict, ...] = ()
    image_declarations: tuple[dict, ...] = ()
    head_jsx_blocks: tuple[str, ...] = ()
    actions: tuple[ActionDetails, ...] = ()
    websocket: WebsocketDetails | None = None
    cache_revalidate: float | None = None
    standalone: bool = False
    uses_suspense: bool = False
    diagnostics: tuple[PyxDiagnostic, ...] = ()


# ---------------------------------------------------------------------------
# Module constants
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _Segment:
    """A contiguous span of source classified as Python or JSX.

    ``start`` is the 0-indexed line where the segment begins. ``end`` is
    exclusive.
    """

    kind: Literal["python", "jsx"]
    start: int
    end: int


@dataclass(slots=True)
class _DiagnosticCollector:
    """Routes errors to either ``CompilationError`` or a diagnostic list."""

    tolerant: bool
    diagnostics: list[PyxDiagnostic] = field(default_factory=list)

    def emit(
        self,
        message: str,
        line: int | None,
        *,
        section: Literal["python", "jsx"] = "python",
        column: int | None = None,
        severity: Literal["error", "warning"] = "error",
    ) -> None:
        if self.tolerant:
            self.diagnostics.append(
                PyxDiagnostic(
                    section=section,
                    severity=severity,
                    message=message,
                    line=line,
                    column=column,
                )
            )
            return
        raise CompilationError(message, line, column)


def _normalize_newlines(text: str) -> list[str]:
    """Normalize newlines and strip a leading UTF-8 BOM if present.

    CRLF and bare CR are normalised to LF, then a leading ``U+FEFF`` is
    removed (Python's :func:`ast.parse` rejects in-string BOMs even
    though most file encodings strip them transparently). Returns the
    text split on LF without a trailing empty line.
    """
    if text.startswith("\ufeff"):
        text = text[1:]
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    return normalized.split("\n")


def _join_lines(lines: Iterable[str]) -> str:
    """Join lines back with newlines, adding a trailing newline if non-empty."""
    materialized = list(lines)
    if not materialized:
        return ""
    return "\n".join(materialized) + "\n"


def _segment_has_content(lines: Sequence[str], segment: _Segment) -> bool:
    return any(lines[i].strip() for i in range(segment.start, segment.end))


def _find_largest_python_at(lines: Sequence[str], start: int, n: int) -> int:
    """Return the largest k such that ``lines[start:k]`` is valid Python.

    Returns ``start`` when no Python statement begins at ``start``. Returns
    ``n`` when the entire remainder of the file is valid Python.

    The algorithm tries to parse the entire suffix first; on
    :class:`SyntaxError`, it walks back from ``exc.lineno`` line by line
    until a valid prefix is found. Typically terminates in 1-2 attempts
    because the SyntaxError lineno usually points exactly at the JSX
    boundary.

    May propagate :class:`MemoryError` or :class:`RecursionError` if
    CPython's parser stack overflows on a deeply-nested expression.
    The outer :meth:`PyxParser.parse_text` catches both and converts
    them into a structured diagnostic.
    """
    if start >= n:
        return start

    rest = "\n".join(lines[start:n])
    if not rest.strip():
        return n

    try:
        ast.parse(rest)
        return n
    except SyntaxError as exc:
        first_failure = (exc.lineno or 1) - 1

    # Walk back from the first failing line to find the largest valid
    # prefix. ``upper`` always reaches ``0`` eventually, where the empty
    # prefix triggers the ``not prefix.strip()`` branch.
    upper = min(first_failure + 1, n - start)
    while True:
        prefix = "\n".join(lines[start : start + upper])
        if not prefix.strip():
            return start
        try:
            ast.parse(prefix)
            return start + upper
        except SyntaxError:
            upper -= 1


def _find_jsx_end_at(lines: Sequence[str], start: int, n: int) -> int:
    """Return the smallest k > ``start`` where ``lines[k:]`` resumes Python.

    Walks forward from ``start + 1`` while tracking JS structural state
    (string literals, block comments, AND brace/paren/bracket depth).
    The walker first consumes ``lines[start]`` to seed the state, then
    at each subsequent non-blank line checks whether the state is at a
    clean top-level JS position — no open string/comment AND all
    brace/paren/bracket depths zero — and only then attempts to find a
    Python segment starting there. The first such line at which a
    non-empty Python segment can begin is the end of the JSX block.

    Tracking brace/paren/bracket depth is essential to fix the bug
    where content inside an open JSX function body that happens to look
    like Python (e.g. ``@action`` decorators embedded in a JSX
    component body, or ``import os`` shapes inside template literals)
    would otherwise be incorrectly extracted as a Python segment,
    splitting the JSX function in half.

    Returns ``n`` when no Python resumes — the rest of the file is JSX.
    """
    state = _JsState()
    # Seed the state with the starting JSX line so subsequent
    # iterations see its open braces / strings.
    state.advance(lines[start])
    for k in range(start + 1, n):
        if not lines[k].strip():
            # Blank lines don't change state and don't trigger a section
            # switch; advance to the next iteration.
            continue
        # The next line must be at a clean top-level JS position — no
        # open string/comment AND all brace/paren/bracket depths zero.
        if state.is_clean() and _find_largest_python_at(lines, k, n) > k:
            return k
        state.advance(lines[k])
    return n


@dataclass(slots=True)
class _JsState:
    """Mutable JS-aware state for the segmentation walker.

    Tracks string state (single/double-quoted strings reset at EOL,
    backtick template literals span lines), ``/* */`` block comments,
    AND brace/paren/bracket nesting depth. The walker uses
    :meth:`is_clean` to ask "are we at a top-level position where Python
    could plausibly resume?" — the answer is no while inside any open
    string, comment, or nesting.
    """

    # Maximum allowed nesting depth for braces, parentheses, and brackets.
    # Prevents CPU exhaustion from deeply-nested expressions in malicious
    # or auto-generated .pyxl files.
    MAX_NESTING_DEPTH: int = 256

    string: str | None = None  # ', ", `, or None
    block_comment: bool = False
    brace_depth: int = 0
    paren_depth: int = 0
    bracket_depth: int = 0

    def is_clean(self) -> bool:
        return (
            self.string is None
            and not self.block_comment
            and self.brace_depth == 0
            and self.paren_depth == 0
            and self.bracket_depth == 0
        )

    def advance(self, line: str) -> None:
        """Update state by walking *line* character by character."""
        length = len(line)
        j = 0
        while j < length:
            ch = line[j]
            if self.string is not None:
                if self.string == "`":
                    if ch == "\\" and j + 1 < length:
                        j += 2
                        continue
                    if ch == "`":
                        self.string = None
                    j += 1
                    continue
                if ch == "\\" and j + 1 < length:
                    j += 2
                    continue
                if ch == self.string:
                    self.string = None
                j += 1
                continue
            if self.block_comment:
                if ch == "*" and j + 1 < length and line[j + 1] == "/":
                    self.block_comment = False
                    j += 2
                    continue
                j += 1
                continue
            # Free state.
            if ch in ("'", '"', "`"):
                self.string = ch
                j += 1
                continue
            if ch == "/" and j + 1 < length:
                next_ch = line[j + 1]
                if next_ch == "/":
                    break  # Line comment to EOL.
                if next_ch == "*":
                    self.block_comment = True
                    j += 2
                    continue
            if ch == "{":
                self.brace_depth += 1
                if self.brace_depth > self.MAX_NESTING_DEPTH:
                    from pyxle.compiler.exceptions import CompilationError

                    raise CompilationError(
                        f"Maximum nesting depth exceeded ({self.MAX_NESTING_DEPTH})"
                    )
            elif ch == "}":
                self.brace_depth = max(0, self.brace_depth - 1)
            elif ch == "(":
                self.paren_depth += 1
                if self.paren_depth > self.MAX_NESTING_DEPTH:
                    from pyxle.compiler.exceptions import CompilationError

                    raise CompilationError(
                        f"Maximum nesting depth exceeded ({self.MAX_NESTING_DEPTH})"
                    )
            elif ch == ")":
                self.paren_depth = max(0, self.paren_depth - 1)
            elif ch == "[":
                self.bracket_depth += 1
                if self.bracket_depth > self.MAX_NESTING_DEPTH:
                    from pyxle.compiler.exceptions import CompilationError

                    raise CompilationError(
                        f"Maximum nesting depth exceeded ({self.MAX_NESTING_DEPTH})"
                    )
            elif ch == "]":
                self.bracket_depth = max(0, self.bracket_depth - 1)
            j += 1
        # Single/double-quoted JS strings reset at EOL.
        if self.string in ("'", '"'):
            self.string = None


def _jsx_state_clean_between(
    lines: Sequence[str], start: int, end: int
) -> bool:
    """Check if JS content in ``lines[start:end]`` ends at a clean state.

    Convenience wrapper around :class:`_JsState` for tests and external
    callers. Returns ``True`` when, after walking ``lines[start:end]``,
    no string/comment is open and all brace/paren/bracket depths are
    back to zero.
    """
    state = _JsState()
    for i in range(start, end):
        state.advance(lines[i])
    return state.is_clean()


def _auto_detect_segments(lines: Sequence[str]) -> list[_Segment]:
    """Walk *lines*, alternating Python and JSX segments based on AST validity.

    The walker uses a greedy strategy: at each cursor position, it tries
    to grow the largest possible Python segment (via ``ast.parse``); if
    none is possible, it grows a JSX segment until Python resumes. This
    cleanly handles arbitrary alternation: ``python | jsx | python | jsx``,
    JSX-first files, pure-Python files, pure-JSX files, and empty files.

    Auto-detected segments are inherently consistent — Python segments
    parse cleanly and JSX segments don't — so they don't need explicit
    validation in Layer 3.
    """
    segments: list[_Segment] = []
    n = len(lines)
    if n == 0:
        return segments

    cursor = 0
    while cursor < n:
        # Skip leading blank lines (assigned to the next segment).
        if not lines[cursor].strip():
            cursor += 1
            continue

        py_end = _find_largest_python_at(lines, cursor, n)
        if py_end > cursor:
            segments.append(_Segment(kind="python", start=cursor, end=py_end))
            cursor = py_end
            continue

        jsx_end = _find_jsx_end_at(lines, cursor, n)
        segments.append(_Segment(kind="jsx", start=cursor, end=jsx_end))
        cursor = jsx_end

    # Trim segments down to the lines that actually contain non-blank
    # content. Trailing blank lines are absorbed into the next segment by
    # the cursor advance, but they shouldn't sit at the END of an output.
    return [seg for seg in segments if _segment_has_content(lines, seg)]


_PYTHON_ONLY_FIRST_TOKENS = frozenset(
    {
        "def",
        "class",
        "from",
        "with",
        "elif",
        "except",
        "finally",
        "raise",
        "yield",
        "pass",
        "global",
        "nonlocal",
        "assert",
        "del",
        "lambda",
    }
)


# Prefixes that a JSX/JS top-level statement may legitimately start with.
# Any segment whose first non-blank line doesn't begin with one of these
# (after accounting for ``async function``) is suspicious — it may be
# broken Python that the auto-detect walker silently absorbed into a
# JSX bucket because ``ast.parse`` failed before the walker could claim
# it as Python.
_JSX_TOPLEVEL_PREFIXES: tuple[str, ...] = (
    "import ",
    "import{",
    "import(",
    "import*",
    'import"',
    "import'",
    "export ",
    "export{",
    "export*",
    "export(",
    "const ",
    "let ",
    "var ",
    "function ",
    "function(",
    "function*",
    "class ",
    "class{",
    "//",
    "/*",
    "<",
    "{",
    "}",
    "(",
    ")",
    "[",
    "]",
    ";",
)


def _contains_jsx_element_marker(line: str) -> bool:
    """Return True if *line* contains a JSX element tag marker.

    A JSX element begins with ``<`` immediately followed by a letter
    (opening tag like ``<Provider``), ``/`` (closing tag like
    ``</div``), or ``>`` (fragment ``<>``). The ``<`` must not be
    followed by whitespace — that would be a less-than operator, not
    a tag. This is a cheap but surprisingly robust way to distinguish
    a JSX-carrying line from broken Python.
    """
    i = line.find("<")
    while i != -1 and i + 1 < len(line):
        next_ch = line[i + 1]
        if next_ch.isalpha() or next_ch in ("/", ">"):
            return True
        i = line.find("<", i + 1)
    return False


def _looks_like_jsx_toplevel(line: str) -> bool:
    """Return True if *line* plausibly starts a JSX/JS top-level statement.

    Checks the first non-blank character(s) of *line* against the known
    set of JSX top-level statement starters, and falls back to checking
    for an embedded JSX element tag marker (``<TagName`` style) so
    that bare assignments like ``config = <Provider />`` are still
    recognized as JSX. Handles the ``async function`` special case
    where the starter spans two tokens. Called only on non-blank
    lines (the detector iterates via :func:`_segment_has_content`).
    """
    stripped = line.lstrip()
    for prefix in _JSX_TOPLEVEL_PREFIXES:
        if stripped.startswith(prefix):
            return True
    # ``async function`` is the only JS top-level starter that spans
    # two tokens. Python's ``async def`` is handled via the
    # Python-keyword heuristic instead.
    if stripped.startswith("async ") and stripped[6:].lstrip().startswith(
        "function"
    ):
        return True
    # A bare-identifier assignment to a JSX expression
    # (``config = <Provider />``) is legitimate JSX. Accept any line
    # that contains a JSX element tag marker.
    return _contains_jsx_element_marker(stripped)


# ---------------------------------------------------------------------------
# Coordinate translation for line numbers *inside* an error message
# ---------------------------------------------------------------------------

#: Phrases after which a compiler-facing tool writes a *second* line number into
#: the body of its own message. Every tool the parser calls is handed one
#: extracted block — a segment, the joined Python stream, the joined JSX stream —
#: so any line number it names is numbered from the start of that block, not the
#: start of the ``.pyxl`` file. The known producers:
#:
#: * CPython — ``closing parenthesis ')' does not match opening parenthesis '['
#:   on line 3`` and ``unterminated string literal (detected at line 9)``.
#: * pyflakes — ``redefinition of unused 'os' from line 1``, ``import 'os' from
#:   line 1 shadowed by loop variable``, ``local variable 'x' defined in
#:   enclosing scope on line 4 referenced before assignment``.
#:
#: The pattern is anchored to these three prepositions rather than any ``line
#: N``, which narrows it to the shapes a tool actually writes.
_MESSAGE_LINE_REFERENCE = re.compile(r"\b(on|from|at) line (\d+)\b")


def _reference_is_quoted(message: str, index: int) -> bool:
    """Whether the reference at *index* sits inside a quoted span of *message*.

    Every producer above writes its coordinate as plain prose and quotes only
    the *name* it is talking about — ``redefinition of unused 'os' from line
    1``. So a ``line N`` that falls inside quotes is not a coordinate at all: it
    is the developer's own text echoed back. ``__all__ = ["ghost on line 999"]``
    yields ``undefined name 'ghost on line 999' in __all__``, where 999 is part
    of a string they wrote, names no line, and must survive untouched — the one
    fragment of the message they would otherwise recognise.

    An odd number of either quote character before the match means the match is
    inside one. When that heuristic is wrong the reference is left raw, which is
    the same fallback an unmappable number gets.
    """
    prefix = message[:index]
    return prefix.count("'") % 2 == 1 or prefix.count('"') % 2 == 1


def _remap_message_line_refs(
    message: str, to_source_line: Callable[[int], int | None]
) -> str:
    """Rewrite block-relative line numbers *inside* ``message`` to file lines.

    The position a diagnostic carries structurally is already translated by the
    caller. This handles the other one — the line number a tool wrote into its
    own prose — which is otherwise reported raw, in the coordinates of an
    extracted block the developer never sees. An error pointing at the wrong
    line of the right file is worse than one that points nowhere: it sends the
    developer to read innocent code and costs them their trust in the compiler.

    ``to_source_line`` maps one block-relative line to its ``.pyxl`` line;
    returning ``None`` (no mapping possible) leaves that reference untouched
    rather than inventing a number. Callers must supply a mapper that answers
    ``None`` outside its block rather than clamping to an edge — a clamped
    number is exactly the confident wrong answer this is here to avoid.
    """

    def _replace(match: re.Match[str]) -> str:
        if _reference_is_quoted(message, match.start()):
            return match.group(0)
        mapped = to_source_line(int(match.group(2)))
        if mapped is None:
            return match.group(0)
        return f"{match.group(1)} line {mapped}"

    return _MESSAGE_LINE_REFERENCE.sub(_replace, message)


def _exact_source_line(relative: int, line_numbers: Sequence[int]) -> int | None:
    """The ``.pyxl`` line for block-relative line *relative*, or ``None``.

    Deliberately unlike :func:`_map_lineno`, which clamps an out-of-range
    number to the block's last line. Clamping is right for a diagnostic's
    structural position — some line has to be reported, and the last one is the
    closest true statement available. It is wrong for a number embedded in
    prose, where "no answer" is representable: a reference the block cannot
    account for is not a line of the developer's file, and answering with the
    nearest one would present a fabricated location as a real one.
    """
    if 1 <= relative <= len(line_numbers):
        return line_numbers[relative - 1]
    return None


def _detect_broken_python_in_jsx_segments(
    segments: Sequence[_Segment],
    lines: Sequence[str],
    *,
    collector: _DiagnosticCollector,
) -> None:
    """Flag JSX segments that look like broken Python and raise/diagnose.

    JSX top-level statements always start at column 0 with one of a
    small set of tokens (``import``, ``export``, ``const``, ``let``,
    ``var``, ``function``, ``async function``, ``class``, ``//``,
    ``/*``, ``<Component``, ``{``, etc). When an auto-detected JSX
    segment begins with content that doesn't match any of those
    starters, it almost certainly came from a Python block whose
    ``ast.parse`` failed and the walker silently absorbed the bad
    lines into a JSX bucket.

    The signal fires in three overlapping cases:
      1. The first non-blank line is indented (JSX top-level never is).
      2. The first non-blank line starts with a Python-only keyword
         (``def``, ``class``, ``from``, decorators, etc.).
      3. The first non-blank line doesn't match any known JSX
         top-level starter (catches bare assignments like
         ``x = "unterminated``, which look like neither Python
         keywords nor JSX keywords but are syntactically Python).

    When any signal fires, we re-run ``ast.parse`` on the segment in
    isolation to recover the precise Python error message and report
    it as a structured Python diagnostic instead of letting broken
    Python silently flow to the JSX compiler downstream.
    """
    # ``_segment_has_content`` filtering upstream guarantees every
    # segment has at least one non-blank line, so the empty-segment
    # defensive branch that earlier revisions had is unreachable.
    for index, segment in enumerate(segments):
        if segment.kind != "jsx":
            continue

        # Find the first non-blank line of the segment. _segment_has_content
        # filtering upstream guarantees there is at least one.
        first_line_idx = next(
            idx
            for idx in range(segment.start, segment.end)
            if lines[idx].strip()
        )

        first_line = lines[first_line_idx]
        is_indented = first_line[0] in (" ", "\t")
        first_token = first_line.lstrip().split(None, 1)[0]
        looks_python_keyword = (
            first_token.startswith("@")
            or first_token in _PYTHON_ONLY_FIRST_TOKENS
            # 'async' followed by 'def' is Python; 'async function' is JS.
            or (
                first_token == "async"
                and "def " in first_line
                and "function " not in first_line
            )
        )
        is_unknown_jsx_starter = not _looks_like_jsx_toplevel(first_line)

        if not (is_indented or looks_python_keyword or is_unknown_jsx_starter):
            continue

        # Try to ast.parse the segment in isolation to recover the precise
        # Python error message. If the segment unexpectedly parses cleanly
        # we just don't report — control flows to the next iteration.
        segment_text = "\n".join(lines[segment.start : segment.end])
        try:
            ast.parse(segment_text)
        except SyntaxError as exc:
            base = segment.start
            if isinstance(exc, IndentationError):
                # The narrow error is about the tear, not the fault — see
                # ``_reparse_with_python_context``. Only this class is
                # overridden: any other message is genuinely about the segment.
                widened = _reparse_with_python_context(segments, index, lines)
                if widened is not None:
                    exc, base = widened
            relative_line = exc.lineno or 1
            absolute_line = base + relative_line
            # The segment's own lines, in ``.pyxl`` coordinates: it is a
            # contiguous run, so line N of it is file line ``start + N``. A
            # number outside that run is not a line of this segment and gets no
            # answer rather than one extrapolated past its end.
            segment_lines = range(base + 1, segment.end + 1)
            collector.emit(
                # ``exc.msg`` is CPython's, written against the isolated
                # segment: "does not match opening parenthesis '[' on line 3"
                # means the third line *of the segment*. Translate it the same
                # way the position above is translated, or the message sends
                # the developer to a line that is perfectly fine.
                _remap_message_line_refs(
                    exc.msg or "invalid syntax",
                    lambda relative: _exact_source_line(relative, segment_lines),
                ),
                absolute_line,
                section="python",
                # ``SyntaxError.offset`` is already 1-indexed within its line
                # and needs no segment adjustment (segments only shift lines).
                # Passing it through is what lets the rebuild print a
                # ``pages/about.pyxl:7:5`` location an editor can jump to.
                column=exc.offset,
            )


def _reparse_with_python_context(
    segments: Sequence[_Segment],
    index: int,
    lines: Sequence[str],
) -> tuple[SyntaxError, int] | None:
    """Re-parse a torn segment together with the Python it was torn from.

    A segment whose first line is *indented* is not a statement anyone wrote at
    top level — it is the tail of a Python block that ``_find_largest_python_at``
    could not finish. That walker stops at the largest prefix which parses, so an
    unclosed bracket makes it stop on the line *before* the bracket's line and
    hand the remainder over as JSX. Parsing that remainder alone then reports
    ``unexpected indent``: a true description of the fragment, and a useless one
    about the file, because the indent is not the mistake — the unclosed bracket
    two lines up is.

    So widen back over the Python segment this one was torn from and report what
    CPython says about *that*, which is the text the developer actually wrote.
    Returns the error and the file line its line numbers are relative to, or
    ``None`` when there is no preceding Python to widen into or the wider text
    parses cleanly (in which case the narrow error is still the best available).
    """
    previous = segments[index - 1] if index > 0 else None
    if previous is None or previous.kind == "jsx":
        return None

    widened = "\n".join(lines[previous.start : segments[index].end])
    try:
        ast.parse(widened)
    except SyntaxError as exc:
        return exc, previous.start
    return None


def _concat_segments(
    segments: Sequence[_Segment], lines: Sequence[str]
) -> tuple[str, list[int], str, list[int]]:
    """Concatenate segments by kind, returning code blobs and line maps.

    Each Python segment's lines are appended to the python output, with
    their original 1-indexed line numbers tracked in ``python_line_numbers``.
    Same for JSX. The line maps let downstream code (notably the loader/
    action validators) translate line numbers in the joined output back
    to the original ``.pyxl`` source.
    """
    python_lines: list[str] = []
    python_line_numbers: list[int] = []
    jsx_lines: list[str] = []
    jsx_line_numbers: list[int] = []

    for segment in segments:
        for i in range(segment.start, segment.end):
            line = lines[i]
            line_no = i + 1  # 1-indexed
            if segment.kind == "python":
                python_lines.append(line)
                python_line_numbers.append(line_no)
            else:
                jsx_lines.append(line)
                jsx_line_numbers.append(line_no)

    return (
        _join_lines(python_lines),
        python_line_numbers,
        _join_lines(jsx_lines),
        jsx_line_numbers,
    )


def _map_lineno(lineno: int | None, line_numbers: Sequence[int]) -> int | None:
    """Translate a line number in joined output back to the original source."""
    if lineno is None or lineno < 1:
        return lineno
    if not line_numbers:
        return lineno
    index = min(lineno - 1, len(line_numbers) - 1)
    return line_numbers[index]


# ---------------------------------------------------------------------------
# AST metadata extraction (loader, actions, head)
# ---------------------------------------------------------------------------


def _has_decorator_named(decorators: Sequence[ast.expr], name: str) -> bool:
    for deco in decorators:
        target = deco
        if isinstance(deco, ast.Call):
            target = deco.func
        if isinstance(target, ast.Name) and target.id == name:
            return True
        if isinstance(target, ast.Attribute) and target.attr == name:
            return True
    return False


def _has_server_decorator(decorators: Sequence[ast.expr]) -> bool:
    return _has_decorator_named(decorators, "server")


def _has_action_decorator(decorators: Sequence[ast.expr]) -> bool:
    return _has_decorator_named(decorators, "action")


def _detect_loader(
    tree: ast.Module | None,
    python_line_numbers: Sequence[int],
    *,
    collector: _DiagnosticCollector,
) -> LoaderDetails | None:
    if tree is None:
        return None

    loader_node: ast.AsyncFunctionDef | None = None

    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and _has_server_decorator(
            node.decorator_list
        ):
            line = _map_lineno(node.lineno, python_line_numbers)
            collector.emit("@server loader must be declared as async", line)
            return None

        if isinstance(node, ast.ClassDef) and _has_server_decorator(
            node.decorator_list
        ):
            line = _map_lineno(node.lineno, python_line_numbers)
            collector.emit(
                "@server decorator can only be applied to functions", line
            )
            return None

        if isinstance(node, ast.AsyncFunctionDef) and _has_server_decorator(
            node.decorator_list
        ):
            if loader_node is not None:
                line = _map_lineno(node.lineno, python_line_numbers)
                collector.emit("Multiple @server loaders detected", line)
                return None
            loader_node = node

    if loader_node is None:
        return None

    if loader_node.col_offset != 0:
        line = _map_lineno(loader_node.lineno, python_line_numbers)
        collector.emit("@server loader must be defined at module scope", line)
        return None

    # Combine positional-only and regular positional args so loaders defined
    # like ``async def loader(request, /):`` are accepted.
    all_pos_args = list(loader_node.args.posonlyargs) + list(loader_node.args.args)

    if not all_pos_args:
        line = _map_lineno(loader_node.lineno, python_line_numbers)
        collector.emit("@server loader must accept a `request` argument", line)
        return None

    first_arg = all_pos_args[0].arg
    if first_arg != "request":
        line = _map_lineno(loader_node.lineno, python_line_numbers)
        collector.emit(
            "First argument of @server loader must be named 'request'", line
        )
        return None

    parameters = tuple(arg.arg for arg in all_pos_args)
    line = _map_lineno(loader_node.lineno, python_line_numbers)
    return LoaderDetails(
        name=loader_node.name,
        line_number=line,
        is_async=True,
        parameters=parameters,
    )


def _detect_actions(
    tree: ast.Module | None,
    python_line_numbers: Sequence[int],
    *,
    collector: _DiagnosticCollector,
) -> tuple[ActionDetails, ...]:
    """Return metadata for every ``@action``-decorated function in *tree*.

    Errors during validation (sync function, wrong arg name, duplicate name,
    etc.) are routed through *collector*.
    """
    if tree is None:
        return ()

    seen_names: set[str] = set()
    actions: list[ActionDetails] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and _has_action_decorator(
            node.decorator_list
        ):
            line = _map_lineno(node.lineno, python_line_numbers)
            collector.emit("@action must be declared as async", line)
            continue

        if isinstance(node, ast.ClassDef) and _has_action_decorator(
            node.decorator_list
        ):
            line = _map_lineno(node.lineno, python_line_numbers)
            collector.emit(
                "@action decorator can only be applied to functions", line
            )
            continue

        if not isinstance(node, ast.AsyncFunctionDef):
            continue
        if not _has_action_decorator(node.decorator_list):
            continue

        line = _map_lineno(node.lineno, python_line_numbers)

        if _has_server_decorator(node.decorator_list):
            collector.emit(
                "@action and @server cannot both be applied to the same "
                "function",
                line,
            )
            continue

        if node.col_offset != 0:
            collector.emit(
                "@action function must be defined at module scope", line
            )
            continue

        all_pos_args = list(node.args.posonlyargs) + list(node.args.args)

        if not all_pos_args:
            collector.emit(
                "@action function must accept a `request` argument", line
            )
            continue

        first_arg = all_pos_args[0].arg
        if first_arg != "request":
            collector.emit(
                "First argument of @action function must be named 'request'",
                line,
            )
            continue

        if node.name in seen_names:
            collector.emit(
                f"Duplicate @action name '{node.name}' — action names must be "
                "unique per page",
                line,
            )
            continue

        seen_names.add(node.name)
        parameters = tuple(arg.arg for arg in all_pos_args)
        actions.append(
            ActionDetails(
                name=node.name,
                line_number=line,
                is_async=True,
                parameters=parameters,
            )
        )

    return tuple(actions)


def _detect_websocket(
    tree: ast.Module | None,
    python_line_numbers: Sequence[int],
    *,
    collector: _DiagnosticCollector,
) -> WebsocketDetails | None:
    """Detect a module-scope ``async def websocket(ws)`` handler.

    Detection is by **convention** — a single coroutine named ``websocket`` at
    module scope — not a decorator. We scan only the module's direct children
    (``ast.iter_child_nodes``), so a local helper named ``websocket`` nested
    inside another function never false-matches and never breaks a page; only
    a module-level definition (the one that can actually be served) counts.

    A mis-shaped definition is reported so the developer isn't left with a
    silent 404: a sync ``def websocket`` or a ``class websocket`` at module
    scope is almost certainly a mistyped handler.
    """
    if tree is None:
        return None

    websocket_node: ast.AsyncFunctionDef | None = None

    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "websocket":
            line = _map_lineno(node.lineno, python_line_numbers)
            collector.emit(
                "`websocket` handler must be declared as async", line
            )
            return None
        if isinstance(node, ast.ClassDef) and node.name == "websocket":
            line = _map_lineno(node.lineno, python_line_numbers)
            collector.emit(
                "`websocket` must be an async function, not a class", line
            )
            return None
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "websocket":
            if websocket_node is not None:
                line = _map_lineno(node.lineno, python_line_numbers)
                collector.emit("Multiple `websocket` handlers detected", line)
                return None
            websocket_node = node

    if websocket_node is None:
        return None

    all_pos_args = (
        list(websocket_node.args.posonlyargs) + list(websocket_node.args.args)
    )
    if not all_pos_args:
        line = _map_lineno(websocket_node.lineno, python_line_numbers)
        collector.emit(
            "`websocket` handler must accept a WebSocket argument", line
        )
        return None

    parameters = tuple(arg.arg for arg in all_pos_args)
    line = _map_lineno(websocket_node.lineno, python_line_numbers)
    return WebsocketDetails(
        name=websocket_node.name,
        line_number=line,
        is_async=True,
        parameters=parameters,
    )


def _preview_discarded(discarded: str) -> str:
    """Shorten dropped markup for an error message without losing its shape."""
    collapsed = " ".join(discarded.split())
    if len(collapsed) > 80:
        collapsed = collapsed[:77] + "..."
    return collapsed


def _check_head_entries(
    entries: Sequence[str], line: int | None, collector: _DiagnosticCollector
) -> None:
    """Refuse a ``HEAD`` entry that holds more than one element.

    Only the first element of an entry survives sanitisation — the pass that
    also discards markup injected after an attribute quote breakout, so it is a
    security boundary rather than a limitation to work around. An entry with a
    second element is therefore content the author wrote and no visitor will
    ever receive. Caught here, while it is a literal in front of them, rather
    than months later in a rich-results report.
    """
    for entry in entries:
        discarded = find_discarded_head_content(entry)
        if discarded is None:
            continue
        collector.emit(
            "A HEAD entry may contain only one element; everything after the "
            "first is dropped. Split it into separate list entries. "
            f"Dropped: {_preview_discarded(discarded)}",
            line,
        )


def _extract_head_literal(
    value: ast.AST, line: int | None, collector: _DiagnosticCollector
) -> list[str] | None:
    """Return literal HEAD entries, or ``None`` for dynamic assignments."""
    if isinstance(value, ast.Constant):
        literal = value.value
        if literal is None:
            return []
        if isinstance(literal, str):
            _check_head_entries([literal], line, collector)
            return [literal]
        collector.emit(
            "HEAD must be assigned a string or list of strings", line
        )
        return None

    if isinstance(value, (ast.List, ast.Tuple)):
        normalized: list[str] = []
        for element in value.elts:
            if not isinstance(element, ast.Constant) or not isinstance(
                element.value, str
            ):
                return None
            normalized.append(element.value)
        _check_head_entries(normalized, line, collector)
        return normalized

    return None


def _collect_head_elements(
    tree: ast.Module | None,
    python_line_numbers: Sequence[int],
    *,
    collector: _DiagnosticCollector,
) -> tuple[tuple[str, ...], bool]:
    """Extract literal ``HEAD = ...`` assignments from the Python AST."""
    if tree is None:
        return tuple(), False

    elements: list[str] = []
    head_is_dynamic = False

    for node in tree.body:
        if isinstance(
            node, (ast.FunctionDef, ast.AsyncFunctionDef)
        ) and node.name == "HEAD":
            elements = []
            head_is_dynamic = True
            continue

        if not isinstance(node, ast.Assign):
            continue

        if not any(
            isinstance(target, ast.Name) and target.id == "HEAD"
            for target in node.targets
        ):
            continue

        line = _map_lineno(node.lineno, python_line_numbers)
        literal = _extract_head_literal(node.value, line, collector)
        if literal is None:
            elements = []
            head_is_dynamic = True
            continue

        elements = literal
        head_is_dynamic = False

    return tuple(elements), head_is_dynamic


def _extract_cache_revalidate(
    value: ast.AST, line: int | None, collector: _DiagnosticCollector
) -> float | None:
    """Pull the ``revalidate`` seconds out of a ``CACHE = {...}`` value."""
    if not isinstance(value, ast.Dict):
        collector.emit(
            'CACHE must be a dict literal, e.g. CACHE = {"revalidate": 60}', line
        )
        return None

    found: float | None = None
    for key_node, val_node in zip(value.keys, value.values):
        if not (isinstance(key_node, ast.Constant) and key_node.value == "revalidate"):
            continue
        if (
            isinstance(val_node, ast.Constant)
            and isinstance(val_node.value, (int, float))
            and not isinstance(val_node.value, bool)
            and val_node.value >= 0
        ):
            found = float(val_node.value)
        else:
            collector.emit(
                "CACHE 'revalidate' must be a non-negative number of seconds", line
            )
            return None

    if found is None:
        collector.emit('CACHE must contain a "revalidate" key', line)
        return None
    return found


def _detect_cache_directive(
    tree: ast.Module | None,
    python_line_numbers: Sequence[int],
    *,
    collector: _DiagnosticCollector,
) -> float | None:
    """Extract a module-level ``CACHE = {"revalidate": N}`` page-cache directive.

    Returns the revalidate window in seconds, or ``None`` when no (valid)
    directive is present. An invalid directive is reported as a compile
    diagnostic and otherwise ignored (the page is treated as uncached).
    """
    if tree is None:
        return None

    revalidate: float | None = None
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(
            isinstance(target, ast.Name) and target.id == "CACHE"
            for target in node.targets
        ):
            continue
        line = _map_lineno(node.lineno, python_line_numbers)
        revalidate = _extract_cache_revalidate(node.value, line, collector)
    return revalidate


def _extract_standalone(
    tree: ast.Module | None,
    python_line_numbers: Sequence[int],
    *,
    collector: _DiagnosticCollector,
) -> bool:
    """Extract a module-level ``STANDALONE = True`` directive.

    Only meaningful on a ``layout.pyxl``. It means "this layout is the root of
    its own chain" — layouts in ancestor directories are not applied to pages
    beneath it, and neither are their loaders.

    The case it exists for is a section of a site that is not part of the app
    around it: a public status page inside an admin console, a print view, an
    embedded widget. Without it the only options are to wrap that section in
    the app's chrome, or to teach the root layout to recognise the section and
    render nothing — a conditional that grows a branch per section and puts
    knowledge of every child in the parent.
    """
    if tree is None:
        return False

    standalone = False
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(
            isinstance(target, ast.Name) and target.id == "STANDALONE"
            for target in node.targets
        ):
            continue
        line = _map_lineno(node.lineno, python_line_numbers)
        value = node.value
        if isinstance(value, ast.Constant) and isinstance(value.value, bool):
            standalone = value.value
        else:
            collector.emit(
                "STANDALONE must be True or False, e.g. STANDALONE = True", line
            )
    return standalone


# ---------------------------------------------------------------------------
# JSX metadata extraction (Babel-backed)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _JsxMetadata:
    """JSX component metadata extracted from a single Babel pass."""

    script_declarations: tuple[dict, ...]
    image_declarations: tuple[dict, ...]
    head_jsx_blocks: tuple[str, ...]
    uses_suspense: bool
    #: ``(message, jsx_relative_line)`` when the extractor reported TypeScript
    #: syntax in the client block, else ``None``. Surfaced by ``parse_text`` as a
    #: source-located diagnostic once the Python section is confirmed clean.
    ts_violation: tuple[str, int | None] | None = None
    #: ``(message, jsx_relative_line)`` when Babel could not parse the JSX at
    #: all, else ``None``. Costs nothing to know: the extractor pass that reads
    #: ``<Head>``/``<Script>``/``<Image>``/``<Suspense>`` runs on every compile
    #: and has to parse the section to do it, so a failure here is a judgement
    #: it has already made and used to return empty metadata.
    syntax_error: tuple[str, int | None] | None = None


# Element names that opt a page into streaming SSR. ``React.Suspense`` is the
# member-expression form Babel reports for ``<React.Suspense>``.
_SUSPENSE_ELEMENT_NAMES = ("Suspense", "React.Suspense")


def _detect_jsx_metadata(jsx_code: str) -> _JsxMetadata:
    """Extract all JSX component metadata the compiler needs in one Babel pass.

    Scripts, images, ``<Head>`` blocks, and whether the page uses
    ``<Suspense>`` (the implicit streaming-SSR opt-in) are all derived from a
    single ``parse_jsx_components`` call. Babel is a Node.js subprocess, so
    consolidating the targets keeps the compile to one spawn instead of one
    per component kind.
    """
    from .jsx_parser import parse_jsx_components

    result = parse_jsx_components(
        jsx_code,
        target_components={"Script", "Image", "Head", *_SUSPENSE_ELEMENT_NAMES},
    )
    if result.error:
        # TypeScript syntax in the client block is a real, surfaceable user
        # error (Babel accepts it but esbuild later fails opaquely). Carry it
        # out so ``parse_text`` can emit a source-located diagnostic.
        if result.error_code == "ts_in_client_block":
            return _JsxMetadata(
                (), (), (), False, ts_violation=(result.error, result.error_line)
            )
        if not result.toolchain_available:
            # The *checker* failed, not the page. Degrade silently: turning a
            # missing Node install into a compile error would fail every file
            # in the project on a machine that has no Node at all.
            return _JsxMetadata((), (), (), False)
        # Babel ran and could not parse the section. Carry it out the same way:
        # the alternative is what shipped before — returning empty metadata, so
        # the page's <Head>, <Script>, <Image> and <Suspense> were all silently
        # dropped and the failure only appeared later, from the bundler, against
        # the generated .jsx.
        return _JsxMetadata(
            (), (), (), False, syntax_error=(result.error, result.error_line)
        )

    components = result.components
    scripts = tuple(
        component.props
        for component in components
        if component.name == "Script" and component.props
    )
    images = tuple(
        component.props
        for component in components
        if component.name == "Image" and component.props
    )
    head_blocks = tuple(
        component.children.strip()
        for component in components
        if component.name == "Head"
        and component.children
        and component.children.strip()
    )
    uses_suspense = any(
        component.name in _SUSPENSE_ELEMENT_NAMES for component in components
    )
    return _JsxMetadata(scripts, images, head_blocks, uses_suspense)


def _map_jsx_line(
    jsx_relative_line: int | None, jsx_line_numbers: Sequence[int]
) -> int | None:
    """Translate a line within the extracted JSX section to a ``.pyxl`` line.

    Every JSX-side tool — Babel here, esbuild later — numbers lines from the
    start of the *section* it was handed, not from the start of the file the
    developer is editing. Reporting that number unmapped points at the wrong
    line of the right file. Falls back to the section's first line when there
    is no line or it is out of range, so a diagnostic never points nowhere.
    """

    if (
        jsx_relative_line is not None
        and 0 <= jsx_relative_line - 1 < len(jsx_line_numbers)
    ):
        return jsx_line_numbers[jsx_relative_line - 1]
    return jsx_line_numbers[0] if jsx_line_numbers else None


def _remap_jsx_message(message: str, jsx_line_numbers: Sequence[int]) -> str:
    """Translate any line number written into a JSX tool's own message.

    The extractor already strips Babel's trailing ``(line:column)`` — that
    coordinate is section-relative and the compiler reports the real one
    separately. This covers the rest: any message that names a line in prose
    (its own, or one a future extractor rule adds) gets the same section →
    file translation the structural position gets. A reference outside the
    section is left alone rather than clamped to its first line, so a stray
    number can never masquerade as a real location.
    """
    return _remap_message_line_refs(
        message, lambda relative: _exact_source_line(relative, jsx_line_numbers)
    )


def _validate_jsx_syntax(
    jsx_code: str,
    jsx_line_numbers: Sequence[int],
    *,
    collector: _DiagnosticCollector,
) -> None:
    """Run Babel on the full JSX section. On failure, emit a diagnostic.

    Opt-in via ``validate_jsx=True``. Babel is a Node.js subprocess
    (~200ms per call) and is skipped on the fast build path. When the
    Babel script itself isn't available (no Node.js, missing langkit),
    the call returns an error message that we treat as ``"unknown"`` —
    we don't fail loud in that case because the diagnostic was opt-in.
    """
    from .jsx_parser import parse_jsx_components

    result = parse_jsx_components(jsx_code, target_components=set())
    if not result.error:
        return

    # Map Babel's 1-indexed line (within the extracted JSX section) back to the
    # real .pyxl line via ``jsx_line_numbers`` — mirroring the TS-violation
    # mapping above. Fall back to the section's first line when Babel reports no
    # line or one out of range, so a diagnostic never points nowhere.
    line: int | None = jsx_line_numbers[0] if jsx_line_numbers else None
    if (
        result.error_line is not None
        and 0 <= result.error_line - 1 < len(jsx_line_numbers)
    ):
        line = jsx_line_numbers[result.error_line - 1]
    collector.emit(
        f"JSX syntax error: {_remap_jsx_message(result.error, jsx_line_numbers)}",
        line,
        section="jsx",
    )


#: Runtime names the compiler auto-injects into the server module (see
#: ``compiler/writers.py``). pyflakes must treat them as defined so ``@server`` /
#: ``@action`` / ``raise ActionError(...)`` / ``raise LoaderError(...)`` never
#: read as undefined even when the user hasn't written an import.
#:
#: Public so editor tooling (pyxle-langkit) whitelists exactly the same names
#: the compiler injects, instead of keeping a copy that can drift.
INJECTED_RUNTIME_NAMES = frozenset(
    {
        "server",
        "action",
        "ActionError",
        "ValidationActionError",
        "LoaderError",
        "invalidate_routes",
    }
)


#: pyflakes message classes that describe code which will **fail when it runs**
#: — a NameError, an UnboundLocalError, a TypeError from a bad format string, or
#: a construct CPython rejects outright. These are the only semantic findings
#: reported as ``severity="error"``.
#:
#: Everything pyflakes can report that is *not* listed here is a warning: the
#: code runs correctly, it is merely untidy (an unused import, a dead local, a
#: duplicate dict key, an f-string with no placeholders). That split is what
#: lets ``pyxle check`` gate a deploy on real breakage without a leftover
#: ``import json`` blocking a release.
#:
#: An unrecognised message class — a rule added by a future pyflakes — is
#: treated as a **warning**, deliberately. A new hygiene rule must never turn
#: into a surprise deploy blocker on a dependency upgrade; the finding is still
#: printed, it just doesn't fail the run.
_PYFLAKES_ERROR_MESSAGES: frozenset[str] = frozenset(
    {
        # Unresolved references — NameError / UnboundLocalError at runtime.
        "UndefinedName",
        "UndefinedLocal",
        "UndefinedExport",
        # Constructs CPython itself rejects (normally caught by ast.parse
        # first; listed so they stay errors on any path that reaches here).
        "BreakOutsideLoop",
        "ContinueOutsideLoop",
        "ReturnOutsideFunction",
        "YieldOutsideFunction",
        "DefaultExceptNotLast",
        "DuplicateArgument",
        "FutureFeatureNotDefined",
        "LateFutureImport",
        "ImportStarNotPermitted",
        "TooManyExpressionsInStarredAssignment",
        "TwoStarredExpressions",
        "ForwardAnnotationSyntaxError",
        "DoctestSyntaxError",
        # Raises the moment the line executes.
        "RaiseNotImplemented",
        "InvalidPrintSyntax",
        "PercentFormatInvalidFormat",
        "PercentFormatExpectedMapping",
        "PercentFormatExpectedSequence",
        "PercentFormatExtraNamedArguments",
        "PercentFormatMissingArgument",
        "PercentFormatMixedPositionalAndNamed",
        "PercentFormatPositionalCountMismatch",
        "PercentFormatStarRequiresSequence",
        "PercentFormatUnsupportedFormatCharacter",
        "StringDotFormatInvalidFormat",
        "StringDotFormatMissingArgument",
        "StringDotFormatMixingAutomatic",
    }
)


def _pyflakes_severity(message: object) -> Literal["error", "warning"]:
    """Classify one pyflakes message as an error or a warning.

    See :data:`_PYFLAKES_ERROR_MESSAGES` for the rule and its rationale.
    """
    if type(message).__name__ in _PYFLAKES_ERROR_MESSAGES:
        return "error"
    return "warning"


def _validate_python_semantics(
    tree: ast.Module | None,
    python_line_numbers: Sequence[int],
    *,
    collector: _DiagnosticCollector,
) -> None:
    """Run pyflakes over the Python section for semantic issues.

    Opt-in via ``validate_semantics=True``. This is the layer beyond
    ``ast.parse``'s syntax check: it catches undefined names (e.g. a handler
    that ``raise``s a symbol it never imported), unused imports, redefinitions,
    and the rest of pyflakes' analysis. Compiler-injected runtime names are
    whitelisted so the idiomatic decorators and error classes never read as
    undefined.

    pyflakes is imported lazily so the fast compile/build path never pays for
    it; if it isn't installed the check is silently skipped.
    """
    if tree is None:
        return
    try:
        from pyflakes.checker import Checker  # lazy: only for `pyxle check`
    except ImportError:  # pragma: no cover - pyflakes is a declared dependency
        return

    try:
        checker = Checker(tree, filename="<pyxl>", builtins=INJECTED_RUNTIME_NAMES)
    except Exception:  # noqa: BLE001 — never let a linter crash the parse
        return

    for message in checker.messages:
        try:
            text = message.message % message.message_args
        except (TypeError, ValueError):  # pragma: no cover - defensive
            text = str(message.message)
        # Several pyflakes findings name a *second* location in their prose
        # ("redefinition of unused 'os' from line 1"), taken from the joined
        # Python stream this checker was handed — not from the ``.pyxl``.
        text = _remap_message_line_refs(
            text, lambda relative: _exact_source_line(relative, python_line_numbers)
        )
        line = _map_lineno(getattr(message, "lineno", None), python_line_numbers)
        collector.emit(
            text, line, section="python", severity=_pyflakes_severity(message)
        )


def _decorator_names(node: ast.AST) -> set[str]:
    """Every decorator's trailing name — ``@action``, ``@action()``, ``@pyxle.action``."""
    names: set[str] = set()
    for decorator in getattr(node, "decorator_list", ()):
        target = decorator.func if isinstance(decorator, ast.Call) else decorator
        if isinstance(target, ast.Name):
            names.add(target.id)
        elif isinstance(target, ast.Attribute):
            names.add(target.attr)
    return names


def _validate_action_signatures(
    tree: ast.Module | None,
    python_line_numbers: Sequence[int],
    *,
    collector: _DiagnosticCollector,
) -> None:
    """Flag an ``@action`` whose body parameter can never be filled.

    ``async def act(request, payload)`` asks Pyxle to supply ``payload`` from
    the request body while saying nothing about its shape, so the call fails
    the first time anyone triggers it. ``pyxle openapi`` already refuses this
    file; ``pyxle check`` — the command the deploy guide names as the gate —
    used to pass it, which is worse than having no gate at all.

    This reads the shape off the AST rather than importing the user's module,
    so ``check`` stays static: no import-time side effects, no import errors as
    a new failure class, no slower gate. It therefore sees only what is written
    literally in the file, which is the shape the mistake actually takes. The
    parameter rule and the message are :func:`resolve_body_model`'s, imported
    from ``pyxle.runtime`` so the gate and the dispatcher cannot drift.
    """
    if tree is None:
        return
    from pyxle.runtime import UnannotatedActionBodyError

    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if "action" not in _decorator_names(node):
            continue

        args = node.args
        # Mirrors ``resolve_body_model``: the body is the first parameter other
        # than ``request`` that the dispatcher could pass by name. Positional-only
        # and ``*args``/``**kwargs`` are excluded there, so they are excluded here.
        positional = list(args.args)
        # A default makes the parameter optional, so it can never be the thing
        # that breaks the call. ``args.defaults`` aligns to the tail of the
        # positional list; ``kw_defaults`` aligns 1:1 with ``kwonlyargs``, where
        # ``None`` means "no default".
        optional = set(map(id, positional[len(positional) - len(args.defaults):])) if args.defaults else set()
        optional |= {
            id(arg)
            for arg, default in zip(args.kwonlyargs, args.kw_defaults)
            if default is not None
        }
        candidates = [
            arg
            for arg in positional + args.kwonlyargs
            if arg.arg not in ("request", "self", "cls")
        ]
        if not candidates:
            continue
        body_arg = candidates[0]
        required = id(body_arg) not in optional
        if body_arg.annotation is not None or not required:
            # Annotated is the dispatcher's problem (it may still need Pydantic
            # installed, which is an environment fact this static gate cannot
            # know). Optional simply keeps its default.
            continue

        collector.emit(
            str(UnannotatedActionBodyError(param=body_arg.arg, action=node.name)),
            _map_lineno(getattr(body_arg, "lineno", None), python_line_numbers),
            section="python",
            severity="error",
        )


# ---------------------------------------------------------------------------
# Public parser
# ---------------------------------------------------------------------------


class PyxParser:
    """Parses ``.pyxl`` files into Python and JSX segments plus metadata."""

    def parse(
        self,
        source_path: Path,
        *,
        tolerant: bool = False,
        validate_jsx: bool = False,
        validate_semantics: bool = False,
        report_jsx_syntax: bool = False,
    ) -> PyxParseResult:
        """Parse a ``.pyxl`` file from disk into a :class:`PyxParseResult`.

        Parameters
        ----------
        source_path:
            Path to the ``.pyxl`` file. Read with ``utf-8-sig`` so a
            leading byte-order mark is consumed transparently.
        tolerant:
            When True, syntax and semantic errors are collected as
            :class:`PyxDiagnostic` entries on the result instead of
            raising :class:`CompilationError`. Used by IDE/LSP
            integrations that need partial results from incomplete code.
        validate_jsx:
            When True, the JSX section is also passed through Babel via
            the existing ``parse_jsx_components`` helper. Babel parse
            failures contribute ``PyxDiagnostic(section="jsx", ...)``
            entries (or raise :class:`CompilationError` in strict mode).
            Off by default because Babel is a Node.js subprocess
            (~200ms per call).
        """
        text = source_path.read_text(encoding="utf-8-sig")
        return self.parse_text(
            text,
            tolerant=tolerant,
            validate_jsx=validate_jsx,
            validate_semantics=validate_semantics,
            report_jsx_syntax=report_jsx_syntax,
        )

    def parse_text(
        self,
        text: str,
        *,
        tolerant: bool = False,
        validate_jsx: bool = False,
        validate_semantics: bool = False,
        report_jsx_syntax: bool = False,
    ) -> PyxParseResult:
        """Parse a ``.pyxl`` source string into a :class:`PyxParseResult`.

        ``report_jsx_syntax`` turns a JSX section Babel cannot parse into a
        source-located error instead of silently-empty JSX metadata. It reuses
        the extractor pass that already runs, so it spawns nothing extra; it is
        a flag only so the production build path keeps its current behaviour.
        """
        lines = _normalize_newlines(text)
        collector = _DiagnosticCollector(tolerant=tolerant)

        # Segment the file purely via AST-driven auto-detection. Deeply
        # nested expressions can exhaust CPython's parser stack and
        # raise MemoryError/RecursionError from inside ``ast.parse``.
        # We catch these at the outer boundary, emit a structured
        # diagnostic, and return an empty-but-valid PyxParseResult so
        # the CLI can keep scanning the rest of the project.
        try:
            segments = _auto_detect_segments(lines)
        except (MemoryError, RecursionError) as exc:
            collector.emit(
                f"Python parser exhausted ({type(exc).__name__}): "
                f"source is too deeply nested or too large for "
                f"CPython to parse",
                line=1,
                section="python",
            )
            return PyxParseResult(
                python_code="",
                jsx_code="",
                loader=None,
                python_line_numbers=(),
                jsx_line_numbers=(),
                head_elements=(),
                head_is_dynamic=False,
                script_declarations=(),
                image_declarations=(),
                head_jsx_blocks=(),
                actions=(),
                diagnostics=tuple(collector.diagnostics),
            )

        # Catch the case where broken Python was silently absorbed into
        # a JSX segment. The signal is a JSX segment whose first
        # non-blank line is indented or starts with a Python-only
        # keyword — JSX top-level statements never have either shape.
        _detect_broken_python_in_jsx_segments(
            segments, lines, collector=collector
        )

        # Concatenate segments by kind and extract metadata.
        (
            python_code,
            python_line_numbers,
            jsx_code,
            jsx_line_numbers,
        ) = _concat_segments(segments, lines)

        tree = self._parse_python_safely(python_code)

        loader = _detect_loader(
            tree, python_line_numbers, collector=collector
        )
        actions = _detect_actions(
            tree, python_line_numbers, collector=collector
        )
        websocket = _detect_websocket(
            tree, python_line_numbers, collector=collector
        )
        head_elements, head_is_dynamic = _collect_head_elements(
            tree, python_line_numbers, collector=collector
        )
        cache_revalidate = _detect_cache_directive(
            tree, python_line_numbers, collector=collector
        )
        standalone = _extract_standalone(
            tree, python_line_numbers, collector=collector
        )

        # Layer 5: JSX metadata + optional Babel validation.
        jsx_metadata = _detect_jsx_metadata(jsx_code)
        script_declarations = jsx_metadata.script_declarations
        image_declarations = jsx_metadata.image_declarations
        head_jsx_blocks = jsx_metadata.head_jsx_blocks

        # Only run JSX validation when the Python section is clean.
        # If Python already has diagnostics, the broken Python content
        # has almost certainly been absorbed into ``jsx_code`` by the
        # walker — rerunning Babel on it produces a cascade of noisy
        # ``[jsx]`` errors that are really just symptoms of the
        # underlying ``[python]`` problem. Fix Python first; JSX
        # validation becomes meaningful again on the next run.
        has_python_errors = any(
            d.section == "python" for d in collector.diagnostics
        )
        # TypeScript syntax in the client block is surfaced on every compile
        # (not just the opt-in ``validate_jsx`` path) because Babel accepts it
        # but esbuild later fails opaquely — catching it here gives a clear,
        # source-located error instead. Gated on a clean Python section so a
        # mis-split (broken Python absorbed into ``jsx_code``) can't be misread
        # as a type annotation.
        if jsx_metadata.ts_violation is not None and not has_python_errors:
            ts_message, ts_jsx_line = jsx_metadata.ts_violation
            collector.emit(
                _remap_jsx_message(ts_message, jsx_line_numbers),
                _map_jsx_line(ts_jsx_line, jsx_line_numbers),
                section="jsx",
            )
        # A JSX section Babel could not parse. Reported from the metadata pass
        # that already parsed it, so this costs no extra Node subprocess — the
        # spawn happens on every compile regardless, to read <Head>/<Script>/
        # <Image>/<Suspense>. Opt-in (``report_jsx_syntax``) purely so the
        # production build path keeps its existing behaviour and cannot be
        # newly broken by a parser disagreement between Babel and esbuild.
        reported_jsx_syntax = False
        if (
            report_jsx_syntax
            and jsx_metadata.syntax_error is not None
            and not has_python_errors
        ):
            jsx_message, jsx_error_line = jsx_metadata.syntax_error
            located_jsx_message = _remap_jsx_message(jsx_message, jsx_line_numbers)
            collector.emit(
                f"JSX syntax error: {located_jsx_message}",
                _map_jsx_line(jsx_error_line, jsx_line_numbers),
                section="jsx",
            )
            reported_jsx_syntax = True
        if (
            validate_jsx
            and jsx_code.strip()
            and not has_python_errors
            and not reported_jsx_syntax
        ):
            # Skipped when the metadata pass already reported the same failure:
            # it would spawn Babel a second time to rediscover it and emit a
            # duplicate diagnostic for one error.
            _validate_jsx_syntax(
                jsx_code, jsx_line_numbers, collector=collector
            )
        # Semantic (name-level) analysis of the Python section — the layer past
        # ``ast.parse``. Gated on a clean Python parse so pyflakes analyses real
        # code, not content a mis-split absorbed into the wrong section.
        if validate_semantics and python_code.strip() and not has_python_errors:
            _validate_python_semantics(
                tree, python_line_numbers, collector=collector
            )
            _validate_action_signatures(
                tree, python_line_numbers, collector=collector
            )

        diagnostics = tuple(
            sorted(
                collector.diagnostics,
                key=lambda d: (d.line or 0, d.column or 0),
            )
        )

        return PyxParseResult(
            python_code=python_code,
            jsx_code=jsx_code,
            loader=loader,
            python_line_numbers=tuple(python_line_numbers),
            jsx_line_numbers=tuple(jsx_line_numbers),
            head_elements=head_elements,
            head_is_dynamic=head_is_dynamic,
            script_declarations=script_declarations,
            image_declarations=image_declarations,
            head_jsx_blocks=head_jsx_blocks,
            actions=actions,
            websocket=websocket,
            cache_revalidate=cache_revalidate,
            standalone=standalone,
            uses_suspense=jsx_metadata.uses_suspense,
            diagnostics=diagnostics,
        )

    def _parse_python_safely(
        self,
        python_code: str,
    ) -> ast.Module | None:
        """Parse the Python segment with ``ast.parse``.

        Returns the AST module on success, or ``None`` if the segment is
        empty. ``_find_largest_python_at`` upstream guarantees that any
        non-empty Python text reaching this point parses cleanly, so an
        ``ast.parse`` failure here would indicate a bug elsewhere in
        the parser pipeline rather than user error — we let any such
        :class:`SyntaxError` propagate naturally so the bug surfaces.
        """
        if not python_code.strip():
            return None
        return ast.parse(python_code, mode="exec", type_comments=True)
