"""Source line maps between compiled artifacts and their ``.pyxl`` origins.

The compiler emits each ``.pyxl`` segment verbatim, so every line of a
generated server module either came from a known ``.pyxl`` line or was
injected by the writer (auto-imported runtime names). This module owns that
relationship end to end:

* :func:`build_line_map` — derive, at write time, the ``.pyxl`` line for every
  line of the final server module (``None`` for injected lines).
* :func:`render_debug_footer` / :func:`extract_debug_info` — persist the map
  *inside* the generated module as two trailing dunder assignments
  (``__pyxle_source__``, ``__pyxle_line_map__``). Embedding it in the artifact
  keeps map and code atomically in sync across rebuilds — a map read from the
  file always describes exactly that file.
* :func:`remap_code` / :class:`PyxlSourceFileLoader` — at import time (dev
  only), compile the module with ``co_filename`` set to the original ``.pyxl``
  and every line number mapped back to it. Tracebacks then point at ``.pyxl``
  sources, and debuggers (debugpy) bind breakpoints set in ``.pyxl`` files
  natively — no custom debug adapter required.

Stdlib-only on purpose: the compiler package imports nothing from the rest of
Pyxle, and both the dev server and the SSR view import this module.
"""

from __future__ import annotations

import ast
import importlib.machinery
import importlib.util
from bisect import bisect_left
from dataclasses import dataclass
from pathlib import Path
from types import CodeType
from typing import Optional, Sequence

SOURCE_DUNDER = "__pyxle_source__"
LINE_MAP_DUNDER = "__pyxle_line_map__"


def _split_lines(text: str) -> list[str]:
    """Split on ``\\n`` only, matching the parser's line accounting.

    Unlike :meth:`str.splitlines`, this ignores form-feed, vertical-tab, and
    Unicode line separators — which the parser also treats as ordinary in-line
    characters — and drops the trailing empty entry a final newline produces.
    """
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return lines

#: One mapped region: ``length`` consecutive emitted lines starting at
#: ``emitted_start`` correspond to consecutive ``.pyxl`` lines starting at
#: ``source_start``. All line numbers are 1-based.
Span = tuple[int, int, int]


def build_line_map(
    pre_injection_text: str,
    pre_injection_line_numbers: Sequence[int],
    final_text: str,
) -> tuple[Optional[int], ...]:
    """Map every line of *final_text* to its 1-based ``.pyxl`` line.

    *pre_injection_text* is the parser's Python stream (whose line ``i``
    originates from ``.pyxl`` line ``pre_injection_line_numbers[i]``);
    *final_text* is that stream after the writer injected auto-import lines.
    Injections are always whole-line insertions of lines that cannot collide
    with user code (an injected import is only added when the user does *not*
    already own the name), so a forward two-pointer walk recovers the origin
    of every final line; injected lines map to ``None``.

    Both texts are split on ``"\n"`` (not :meth:`str.splitlines`) so the line
    accounting matches the parser, which derives ``pre_injection_line_numbers``
    from ``\n``-delimited lines — a form-feed or vertical-tab inside a string
    literal would otherwise desync the two.
    """
    pre_lines = _split_lines(pre_injection_text)
    final_lines = _split_lines(final_text)
    mapping: list[Optional[int]] = []
    pre_index = 0
    for line in final_lines:
        if pre_index < len(pre_lines) and line == pre_lines[pre_index]:
            if pre_index < len(pre_injection_line_numbers):
                mapping.append(pre_injection_line_numbers[pre_index])
            else:  # pragma: no cover - parser guarantees equal lengths
                mapping.append(None)
            pre_index += 1
        else:
            mapping.append(None)
    return tuple(mapping)


def compress_to_spans(line_map: Sequence[Optional[int]]) -> tuple[Span, ...]:
    """Compress a per-line map into maximal contiguous spans."""
    spans: list[Span] = []
    start_emitted = start_source = length = 0
    for index, source_line in enumerate(line_map, start=1):
        if (
            source_line is not None
            and length > 0
            and source_line == start_source + length
            and index == start_emitted + length
        ):
            length += 1
            continue
        if length > 0:
            spans.append((start_emitted, start_source, length))
            length = 0
        if source_line is not None:
            start_emitted, start_source, length = index, source_line, 1
    if length > 0:
        spans.append((start_emitted, start_source, length))
    return tuple(spans)


def render_debug_footer(source_relative_posix: str, spans: Sequence[Span]) -> str:
    """The trailing lines that persist the map inside a generated module.

    ``source_relative_posix`` locates the ``.pyxl`` relative to the generated
    module's own directory, so the artifact stays relocatable — no absolute
    paths are baked into the build.
    """
    spans_literal = (
        "(" + ", ".join(f"({a}, {b}, {c})" for a, b, c in spans) + ("," if len(spans) == 1 else "") + ")"
        if spans
        else "()"
    )
    return (
        f"\n{SOURCE_DUNDER} = {source_relative_posix!r}\n"
        f"{LINE_MAP_DUNDER} = {spans_literal}\n"
    )


@dataclass(frozen=True, slots=True)
class PyxlDebugInfo:
    """The persisted map, as read back from a generated module's footer."""

    source_relative_posix: str
    spans: tuple[Span, ...]


def extract_debug_info(module_text: str) -> Optional[PyxlDebugInfo]:
    """Read the debug footer from generated module source, if present.

    :func:`render_debug_footer` appends the footer as the *final two non-blank
    lines* of the module — ``SOURCE_DUNDER`` immediately followed by
    ``LINE_MAP_DUNDER`` — so extraction is anchored to that exact shape: the
    last non-blank line must be the line-map assignment and the line directly
    above it the source assignment. A page that merely assigns same-named
    globals somewhere in its own code (even both, in footer order) therefore
    cannot be mistaken for a real footer, and a malformed authoritative footer
    never falls through to an earlier decoy.
    """
    lines = module_text.splitlines()
    end = len(lines)
    while end > 0 and not lines[end - 1].strip():
        end -= 1
    if end < 2:
        return None
    line_map_line = lines[end - 1]
    source_line = lines[end - 2]
    if not line_map_line.startswith(f"{LINE_MAP_DUNDER} = "):
        return None
    if not source_line.startswith(f"{SOURCE_DUNDER} = "):
        return None
    spans = _parse_spans_literal(line_map_line[len(LINE_MAP_DUNDER) + 3 :])
    source = _parse_str_literal(source_line[len(SOURCE_DUNDER) + 3 :])
    if source is None or spans is None:
        return None
    return PyxlDebugInfo(source_relative_posix=source, spans=spans)


def _parse_str_literal(text: str) -> Optional[str]:
    try:
        value = ast.literal_eval(text.strip())
    except (ValueError, SyntaxError):
        return None
    return value if isinstance(value, str) else None


def _parse_spans_literal(text: str) -> Optional[tuple[Span, ...]]:
    try:
        value = ast.literal_eval(text.strip())
    except (ValueError, SyntaxError):
        return None
    if not isinstance(value, tuple):
        return None
    spans: list[Span] = []
    for item in value:
        if (
            not isinstance(item, tuple)
            or len(item) != 3
            or not all(
                isinstance(part, int) and not isinstance(part, bool) and part > 0
                for part in item
            )
        ):
            return None
        spans.append(item)  # type: ignore[arg-type]
    return tuple(spans)


class _SpanMapper:
    """Resolve emitted line numbers to ``.pyxl`` lines via binary search.

    Unmapped lines (writer-injected imports, the footer itself) resolve to the
    nearest *following* mapped line — a breakpoint dragged onto an injected
    line lands on the first real statement after it — falling back to the
    nearest preceding line at the end of the file.

    Because :func:`remap_code` compiles the whole module under a single
    ``co_filename`` (the ``.pyxl``), a line has no way to say "this came from
    generated glue, not the source." A runtime error *in* an injected line —
    e.g. an ``ImportError`` from an auto-injected ``from pyxle.runtime import
    ...`` when the environment's Pyxle install is broken — therefore inherits
    the nearest mapped ``.pyxl`` line and is reported against innocent user
    source. This only affects import-time failures in generated glue (user
    statements and their breakpoints always sit inside a span and map exactly);
    it is inherent to source-level remapping, not a lookup bug here.
    """

    def __init__(self, spans: Sequence[Span]) -> None:
        self._spans = sorted(spans)
        self._starts = [span[0] for span in self._spans]

    def map_line(self, emitted_line: int) -> int:
        index = bisect_left(self._starts, emitted_line + 1) - 1
        if index >= 0:
            start_emitted, start_source, length = self._spans[index]
            offset = emitted_line - start_emitted
            if offset < length:
                return start_source + offset
        # Not inside a span: prefer the start of the next span.
        if index + 1 < len(self._spans):
            return self._spans[index + 1][1]
        if index >= 0:  # past the last span — clamp to its final line
            start_emitted, start_source, length = self._spans[index]
            return start_source + length - 1
        return emitted_line  # pragma: no cover - empty span list is filtered earlier


def remap_code(module_text: str, module_path: Path) -> Optional[CodeType]:
    """Compile *module_text* against its ``.pyxl`` origin, if it has one.

    Returns a code object whose ``co_filename`` is the resolved ``.pyxl`` path
    and whose line numbers are the ``.pyxl`` lines, or ``None`` when the module
    carries no footer (plain API modules, static stubs) or the ``.pyxl`` source
    no longer exists — callers then import the artifact unchanged.
    """
    info = extract_debug_info(module_text)
    if info is None or not info.spans:
        return None
    pyxl_path = (module_path.parent / info.source_relative_posix).resolve()
    if not pyxl_path.is_file():
        return None

    tree = ast.parse(module_text)
    mapper = _SpanMapper(info.spans)
    for node in ast.walk(tree):
        lineno = getattr(node, "lineno", None)
        if lineno is None:
            continue
        mapped = mapper.map_line(lineno)
        node.lineno = mapped
        end_lineno = getattr(node, "end_lineno", None)
        if end_lineno is not None:
            # A multi-line node may end on an unmapped line; never let the end
            # precede the start or the compile step rejects the tree.
            node.end_lineno = max(mapper.map_line(end_lineno), mapped)
    return compile(tree, str(pyxl_path), "exec")


class PyxlSourceFileLoader(importlib.machinery.SourceFileLoader):
    """Import loader that execs generated page modules as their ``.pyxl``.

    Used by the dev server's module importers (debug mode only): when the
    generated module carries a debug footer, :meth:`get_code` returns the
    line-remapped code object so stack traces, ``inspect``, and debuggers all
    see the ``.pyxl`` file. Modules without a footer — user API modules,
    static-page stubs — fall through to the stock loader unchanged.
    """

    def get_code(self, fullname: str) -> CodeType:
        try:
            source_bytes = self.get_data(self.path)
            module_text = importlib.util.decode_source(source_bytes)
            code = remap_code(module_text, Path(self.path))
        except Exception:  # noqa: BLE001 — any remap fault falls back to stock import
            code = None
        if code is not None:
            return code
        return super().get_code(fullname)


__all__ = [
    "SOURCE_DUNDER",
    "LINE_MAP_DUNDER",
    "Span",
    "PyxlDebugInfo",
    "PyxlSourceFileLoader",
    "build_line_map",
    "compress_to_spans",
    "extract_debug_info",
    "remap_code",
    "render_debug_footer",
]
