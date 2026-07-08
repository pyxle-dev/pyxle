"""JSX component extraction using Babel AST parsing."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class JSXComponent:
    """Represents a JSX component usage found in code."""

    name: str  # Component name (e.g., "Script", "Image", "Head")
    props: dict[str, Any]  # Props/attributes
    children: str | None  # Text content for container components like <Head>
    self_closing: bool  # Whether it's self-closing
    line: int | None
    column: int | None


@dataclass(frozen=True)
class JSXParseResult:
    """Result of parsing JSX code."""

    components: tuple[JSXComponent, ...]
    error: str | None
    #: A machine-readable tag for the error, when the extractor sets one.
    #: ``"ts_in_client_block"`` means TypeScript syntax was found in the JSX —
    #: the compiler surfaces this as a clear, source-located error rather than
    #: letting it fail later in the bundler.
    error_code: str | None = None
    #: 1-indexed line of the error within the JSX source, when known.
    error_line: int | None = None


def parse_jsx_components(jsx_code: str, *, target_components: set[str] | None = None) -> JSXParseResult:
    """
    Parse JSX code using Babel and extract specific component usages.

    Args:
        jsx_code: The JSX source code to parse
        target_components: Set of component names to extract (e.g., {"Script", "Image", "Head"}).
                          If None, extracts all components.

    Returns:
        JSXParseResult with extracted components or error information.
    """
    if not jsx_code.strip():
        return JSXParseResult(components=(), error=None)

    # Use TemporaryDirectory for automatic cleanup and better isolation
    # on shared systems (avoids predictable temp file paths).
    with tempfile.TemporaryDirectory() as temp_dir:
        temp_path = Path(temp_dir) / "input.jsx"
        temp_path.write_text(jsx_code, encoding="utf-8")
        return _run_babel_parser(str(temp_path), target_components)


def _langkit_js_base() -> Path | None:
    """Locate the ``js/`` directory of the installed ``pyxle_langkit`` package.

    Uses ``importlib.util.find_spec`` so the package is *located without being
    imported* — ``pyxle_langkit`` imports ``pyxle.compiler``, so an actual
    import from here would be a runtime cycle. Returns ``None`` when the
    package isn't installed (the sibling-path heuristics below still apply).
    """
    try:
        spec = importlib.util.find_spec("pyxle_langkit")
    except (ImportError, ValueError):
        # A broken or half-uninstalled distribution must degrade to the
        # sibling-path heuristics, never crash ``pyxle check``.
        return None
    if spec is None or not spec.submodule_search_locations:
        return None
    return Path(next(iter(spec.submodule_search_locations))) / "js"


def _run_babel_parser(source_path: str, target_components: set[str] | None) -> JSXParseResult:
    """Run the Node.js Babel parser script."""
    # Resolve the extractor script. Prefer the self-contained bundle
    # (``*.bundle.mjs`` — Babel inlined, so it works with zero npm setup on a
    # clean ``pip install``); fall back to the source ``.mjs`` (which needs
    # @babel/* in node_modules — the dev/CI layout). Each location, in order:
    #   1. the installed pyxle_langkit distribution (a default dependency
    #      since 0.7.0), resolved via importlib — works for every install
    #      layout, including an editable pyxle with langkit in site-packages
    #   2. nested in the installed pyxle package
    #   3. installed pyxle_langkit sibling (the pre-importlib heuristic for
    #      a flat site-packages layout)
    #   4. a sibling pyxle-langkit checkout (workspace dev)
    _installed_base = _langkit_js_base()
    _js_bases = (
        *((_installed_base,) if _installed_base is not None else ()),
        Path(__file__).parent.parent / "pyxle_langkit" / "js",
        Path(__file__).parent.parent.parent / "pyxle_langkit" / "js",
        Path(__file__).resolve().parent.parent.parent.parent / "pyxle-langkit" / "pyxle_langkit" / "js",
    )
    script_path: Path | None = None
    for _base in _js_bases:
        for _candidate in (
            _base / "jsx_component_extractor.bundle.mjs",
            _base / "jsx_component_extractor.mjs",
        ):
            if _candidate.exists():
                script_path = _candidate
                break
        if script_path is not None:
            break

    if script_path is None:
        return JSXParseResult(
            components=(),
            error=(
                "JSX checker unavailable: the language toolkit isn't installed. "
                "Install it with `pip install 'pyxle-framework[langkit]'` (it also "
                "needs @babel/parser and @babel/traverse available to Node)."
            ),
        )

    # Prepare command
    components_arg = json.dumps(list(target_components)) if target_components else "null"
    command = ["node", str(script_path), source_path, components_arg]

    try:
        proc = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except FileNotFoundError:
        return JSXParseResult(
            components=(),
            error="Node.js not found. Install Node.js >=20.19 to parse JSX components.",
        )
    except subprocess.TimeoutExpired:
        return JSXParseResult(components=(), error="JSX parser timed out.")

    # Surface a clear message when the extractor's parser dependencies
    # (@babel/parser, @babel/traverse) aren't installed — the most common
    # clean-install failure. Node exits non-zero with ERR_MODULE_NOT_FOUND and an
    # empty stdout, which would otherwise degrade into an opaque "invalid output".
    stderr = (proc.stderr or "").strip()
    if proc.returncode != 0 and ("ERR_MODULE_NOT_FOUND" in stderr or "@babel/" in stderr):
        return JSXParseResult(
            components=(),
            error=(
                "The JSX checker could not load its parser dependencies "
                "(@babel/parser, @babel/traverse). Install them where the extractor "
                "lives — e.g. `npm install @babel/parser @babel/traverse` in the "
                "pyxle-langkit package — then re-run."
            ),
        )

    # Parse JSON output
    try:
        payload = json.loads(proc.stdout.strip() or stderr or "{}")
    except json.JSONDecodeError:
        detail = (stderr or proc.stdout or "(no output)").strip()
        return JSXParseResult(
            components=(),
            error=f"JSX parser produced invalid output: {detail[:200]}",
        )

    # Check for errors
    if not payload.get("ok", False):
        error_msg = payload.get("message", "Unknown JSX parsing error")
        error_line = payload.get("line")
        return JSXParseResult(
            components=(),
            error=error_msg,
            error_code=payload.get("code"),
            error_line=error_line if isinstance(error_line, int) else None,
        )

    # Convert payload to JSXComponent objects
    components = []
    for comp_data in payload.get("components", []):
        component = JSXComponent(
            name=comp_data.get("name", "unknown"),
            props=comp_data.get("props", {}),
            children=comp_data.get("children"),
            self_closing=comp_data.get("selfClosing", False),
            line=comp_data.get("line"),
            column=comp_data.get("column"),
        )
        components.append(component)

    return JSXParseResult(components=tuple(components), error=None)
