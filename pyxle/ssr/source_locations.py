"""Report errors against the file the author wrote, not the one Pyxle generated.

A ``.pyxl`` file is compiled into a ``.jsx`` module under ``.pyxle-build/``, and
the tools that report syntax errors — esbuild, Babel — necessarily name that
generated file: ``pages/about.jsx:8:8``. The author never created
``pages/about.jsx``, does not know ``.pyxle-build/`` exists, and cannot open
line 8 of it to find their mistake. Worse, the line number is wrong for their
file: a page's JSX half starts wherever its Python half ended, so JSX line 1 is
often line 19 or line 40 of the ``.pyxl``.

The compiler already records the mapping, one entry per page, in the
``pyxl-sourcemaps.json`` sidecar it writes next to the generated modules (see
``pyxle.compiler.writers``). This module reads it and rewrites those positions
back to the source.

Where a position cannot be mapped, the generated path is **labelled as
generated** rather than silently presented as the author's file: a reader who
knows a position is approximate can still work with it, while a reader who
believes ``about.jsx`` is their own file cannot.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

__all__ = ["remap_generated_locations"]

#: Written by the compiler alongside the generated client modules.
SOURCEMAP_SIDECAR = "pyxl-sourcemaps.json"

#: ``pages/about.jsx:8:8`` / ``pages/about.jsx:8``. The path class deliberately
#: excludes whitespace, quotes and colons so a position embedded in prose or in
#: a quoted message is still matched at its boundaries.
_LOCATION_RE = re.compile(r"""([^\s'"`:()\[\]]+\.jsx):(\d+)(?::(\d+))?""")

#: Sidecars already read, keyed by ``(path, mtime_ns)`` so a rebuild is picked
#: up without a stat-per-lookup becoming a stale read. Bounded (rule 17): one
#: entry per client root in practice, and this runs only on the error path.
_sidecar_cache: dict[tuple[str, int], dict] = {}
_SIDECAR_CACHE_LIMIT = 8


def _load_sidecar(client_root: Path) -> dict:
    """Read the compiler's ``.jsx`` → ``.pyxl`` line map, or ``{}``."""
    sidecar = client_root / SOURCEMAP_SIDECAR
    try:
        mtime = sidecar.stat().st_mtime_ns
    except OSError:
        return {}

    key = (str(sidecar), mtime)
    cached = _sidecar_cache.get(key)
    if cached is not None:
        return cached

    try:
        entries = json.loads(sidecar.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(entries, dict):
        return {}

    if len(_sidecar_cache) >= _SIDECAR_CACHE_LIMIT:
        _sidecar_cache.clear()
    _sidecar_cache[key] = entries
    return entries


def _lookup(entries: dict, jsx_path: str) -> tuple[str, list] | None:
    """Find the sidecar entry for *jsx_path*, which may carry extra leading
    segments (an absolute path, or one relative to the project rather than the
    client root). Match on the longest suffix that the sidecar knows."""
    normalized = jsx_path.replace("\\", "/").lstrip("./")
    parts = normalized.split("/")
    for start in range(len(parts)):
        candidate = "/".join(parts[start:])
        entry = entries.get(candidate)
        if isinstance(entry, dict):
            source = entry.get("pyxl")
            lines = entry.get("lines")
            if isinstance(source, str) and isinstance(lines, list):
                return source, lines
    return None


def _source_display_path(source: str) -> str:
    """Render the sidecar's client-root-relative path as the author sees it.

    Stored as ``../../pages/about.pyxl`` (relative to the client root, so no
    absolute paths land in build artifacts); a developer knows the file as
    ``pages/about.pyxl``.
    """
    cleaned = source.replace("\\", "/")
    while cleaned.startswith("../"):
        cleaned = cleaned[3:]
    return cleaned.lstrip("./")


def remap_generated_locations(text: str, client_root: Path | None) -> str:
    """Rewrite ``<generated>.jsx:line[:col]`` positions in *text* to their source.

    Every position that maps becomes ``pages/about.pyxl:19:8``. A position that
    cannot be mapped keeps its generated path and is marked ``(generated)``, so
    the reader is never told an artifact is their own file.

    Returns *text* unchanged when it holds no such position — the overwhelmingly
    common case, and the reason this costs nothing on a message that does not
    need it.
    """
    if not text or ".jsx" not in text:
        return text

    entries = _load_sidecar(client_root) if client_root is not None else {}

    def replace(match: re.Match[str]) -> str:
        jsx_path, line_text, column_text = match.group(1), match.group(2), match.group(3)
        suffix = f":{column_text}" if column_text else ""

        found = _lookup(entries, jsx_path)
        if found is None:
            return f"{match.group(0)} (generated)"

        source, lines = found
        try:
            index = int(line_text) - 1
        except ValueError:  # pragma: no cover - the regex guarantees digits
            return f"{match.group(0)} (generated)"

        if not (0 <= index < len(lines)) or not isinstance(lines[index], int):
            # The file is known but this line is not — an error inside code the
            # compiler emitted rather than code the author wrote. Name the
            # source file so they know which page, and be explicit that the
            # position belongs to the generated module.
            return (
                f"{_source_display_path(source)} "
                f"(in generated output at {jsx_path}:{line_text}{suffix})"
            )

        return f"{_source_display_path(source)}:{lines[index]}{suffix}"

    return _LOCATION_RE.sub(replace, text)
