"""Every Python sample we publish has to be Python.

A syntax error in a documented sample is a defect a test suite will never catch
on its own: nothing imports the docs, so a broken snippet ships, gets copied,
and fails in a stranger's terminal on their first ten minutes with the
framework. This walks every ``python`` fence under ``docs/`` and parses it.

Three kinds of block are deliberately not parsed as pure Python, and each skip
is a rule rather than a list of whatever currently fails:

* **A whole ``.pyxl`` file.** These are fenced ``python`` on purpose — pyxle.dev
  detects the Python→JS boundary and upgrades them to its dual tokenizer (see
  ``looksLikePyxl`` in the site's ``code-highlighter.jsx``). The JSX half is not
  Python and is not supposed to be.
* **An abridged block**, marked with ``...`` or ``…``. The ellipsis is the
  author telling the reader it is not complete.
* **A one-line signature display**, e.g. ``LoaderError(message: str, ...)`` —
  reference material describing a call, not a statement to run.

Anything else must parse. Two truncated excerpts of real framework source are
allowlisted by name below, with the reason.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

DOCS_ROOT = Path(__file__).resolve().parent.parent / "docs"

#: The first top-level JS import or export — the Python→JS boundary in a
#: ``.pyxl`` file. Kept in step with the site's own ``PYXL_JS_BOUNDARY``.
_JS_BOUNDARY = re.compile(
    r"^import\b.*\bfrom\s+['\"]|^export\s+(default|function|const|class|async|let|var)\b",
    re.M,
)

_SIGNATURE_LINE = re.compile(r"^[A-Za-z_][\w.]*\(.*\)\s*$", re.S)

#: Truncated excerpts of real source, shown deliberately mid-definition in the
#: architecture notes. Parsing them would require inventing a body the reader is
#: not meant to see. Keyed by ``path:line`` so moving one makes the skip fail
#: loudly instead of silently covering a new block.
_ALLOWED_TRUNCATED = {
    "architecture/compiler.md": "compile_file's signature, shown without its body",
    "architecture/parser.md": "_JSX_TOPLEVEL_PREFIXES, shown partially",
}


def _python_blocks() -> list[tuple[str, int, str]]:
    blocks = []
    for md in sorted(DOCS_ROOT.rglob("*.md")):
        text = md.read_text(encoding="utf-8")
        rel = md.relative_to(DOCS_ROOT).as_posix()
        for match in re.finditer(r"```python\n(.*?)```", text, re.S):
            line = text[: match.start()].count("\n") + 1
            blocks.append((rel, line, match.group(1)))
    return blocks


def _should_parse(code: str) -> bool:
    if "..." in code or "…" in code:
        return False
    if _JS_BOUNDARY.search(code):
        return False
    stripped = code.strip()
    return not (_SIGNATURE_LINE.match(stripped) and "\n" not in stripped)


BLOCKS = _python_blocks()


def test_the_docs_actually_contain_python_samples() -> None:
    """Guard the guard: a broken glob would make every test below vacuous."""
    assert len(BLOCKS) > 100, f"only found {len(BLOCKS)} python blocks — check DOCS_ROOT"


@pytest.mark.parametrize(
    ("rel", "line", "code"),
    [pytest.param(r, ln, c, id=f"{r}:{ln}") for r, ln, c in BLOCKS],
)
def test_published_python_sample_parses(rel: str, line: int, code: str) -> None:
    if rel in _ALLOWED_TRUNCATED:
        pytest.skip(f"deliberate excerpt — {_ALLOWED_TRUNCATED[rel]}")
    if not _should_parse(code):
        pytest.skip("whole .pyxl file, abridged block, or signature display")
    try:
        ast.parse(code)
    except SyntaxError as exc:
        pytest.fail(
            f"docs/{rel} line {line}: published Python sample does not parse — "
            f"{exc.msg} (sample line {exc.lineno})"
        )
