"""Report errors against the file the author wrote, not the one Pyxle generated.

A ``.pyxl`` file is compiled into a ``.jsx`` module under ``.pyxle-build/``, and
the tools that report syntax errors — esbuild, Babel, Rollup — necessarily name
that generated file: ``pages/about.jsx:8:8``. The author never created
``pages/about.jsx``, does not know ``.pyxle-build/`` exists, and cannot open
line 8 of it to find their mistake. Worse, the line number is wrong for their
file: a page's JSX half starts wherever its Python half ended, so JSX line 1 is
often line 19 or line 40 of the ``.pyxl``.

The compiler already records the mapping, one entry per page, in the
``pyxl-sourcemaps.json`` sidecar it writes next to the generated modules (see
``pyxle.compiler.writers``). This module reads it and rewrites those positions
back to the source.

**Not every ``.jsx`` in the client tree is generated.** A ``.jsx`` component the
developer wrote beside their pages is *copied* there byte for byte (see
:func:`pyxle.devserver.builder.build_once`), so line 4 of the copy is line 4 of
their file. Such a position is reported against the file they wrote, with the
line and column untouched — there is nothing to translate and nothing to warn
about.

Where a position in a genuinely generated module cannot be mapped, the path is
**labelled as generated** rather than silently presented as the author's file: a
reader who knows a position is approximate can still work with it, while a
reader who believes ``about.jsx`` is their own file cannot.

**Only a path carrying a ``:line`` is touched.** A bare ``.jsx`` path is
ambiguous in a way no amount of resolution settles: it is equally the shape of
an import specifier the author typed and of a line of their source echoed back
inside an esbuild code frame. Rewriting those corrupts the developer's own text
— the same false label this module exists to prevent, pointed at the author
instead of the artifact. The cost is that a failure genuinely carrying no
coordinate (Rollup's unresolved import) still names the build artifact; that is
a documented limitation, and it is the cheaper of the two mistakes.

**And a ``.jsx`` inside a URL is never touched, coordinate or not.** A URL is a
link rather than a location, and it can carry a coordinate of its own
(``http://localhost:5176/pages/index.jsx:3:9``). Rewriting the path inside it
destroys the link: the pattern that matches a plain path cannot see a scheme,
so its match starts after the ``:`` and the result is
``http://localhost:pages/index.pyxl:21:9``, with the port eaten. See
:data:`_URL_LOCATION`.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

__all__ = ["remap_generated_locations"]

#: Written by the compiler alongside the generated client modules.
SOURCEMAP_SIDECAR = "pyxl-sourcemaps.json"

#: Client-tree subdirectory holding both compiled pages and the client assets
#: copied verbatim beside them. Mirrors the destination
#: :func:`pyxle.devserver.builder.build_once` copies a ``CLIENT_ASSET`` to.
_CLIENT_PAGES_DIR = "pages"

#: A ``.jsx`` reached over a URL rather than named as a path:
#: ``http://localhost:5176/pages/index.jsx:3:9`` — the shape of a stack frame
#: or a diagnostic naming a module the browser loaded over HTTP.
#:
#: What makes it a URL is the scheme and the ``//`` that follow it — the two
#: things the path pattern below is structurally unable to see, because its
#: character class excludes ``:``. Left to that pattern the match would begin
#: *after* the scheme, on ``5176/pages/index.jsx:3:9``, and rewriting it would
#: leave ``http://localhost:pages/index.pyxl:21:9``: the port eaten, the URL
#: destroyed, and a link presented as a source location. Matching the whole URL
#: first — and handing it back untouched — is what prevents that, and it is
#: general rather than a list of hostnames: ``https``, ``file://`` and Vite's
#: ``/@fs/`` are all URLs before they are paths, and all three are covered by
#: the same rule.
#:
#: A path with no scheme is *not* a URL and is left to the pattern below, which
#: is the point: ``/@fs/…/pages/index.jsx:3:9`` stripped of its origin still
#: names a real generated module, and remapping it is exactly right.
#:
#: The path portion is greedy on purpose. A URL glued to a following position by
#: a character this class does not exclude swallows it, and that position goes
#: unmapped. Excluding more characters trades that for a URL *containing* one
#: failing to match here and having its own tail rewritten by the pattern below
#: — a broken link rather than a missed translation, which is the worse of the
#: two.
_URL_LOCATION = r"""[A-Za-z][A-Za-z0-9+.\-]*://[^\s'"`()\[\]]*\.jsx(?::\d+)*"""

#: ``pages/about.jsx:8:8`` / ``pages/about.jsx:8``. The path class deliberately
#: excludes whitespace, quotes and colons so a position embedded in prose or in
#: a quoted message is still matched at its boundaries.
#:
#: The ``:line`` is **required**, and that is the whole safety property: it is
#: what separates a compiler-reported position from a ``.jsx`` the author merely
#: wrote down. Making it optional matches an import specifier in a code frame
#: (``import Other from './components/Other.jsx';``) and rewrites it — editing
#: the developer's own source text in the message meant to help them read it.
_LOCATION_RE = re.compile(
    rf"""(?P<url>{_URL_LOCATION})"""
    rf"""|(?P<path>[^\s'"`:()\[\]]+\.jsx):(?P<line>\d+)(?::(?P<column>\d+))?"""
)

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


def _suffixes(jsx_path: str) -> list[str]:
    """Every trailing path fragment of *jsx_path*, longest first.

    A reported path may carry extra leading segments — an absolute path into the
    build directory, or one relative to the project rather than the client root
    — so both the sidecar lookup and the copied-asset lookup match on the
    longest suffix they recognise.
    """
    normalized = jsx_path.replace("\\", "/").lstrip("./")
    parts = normalized.split("/")
    return ["/".join(parts[start:]) for start in range(len(parts))]


def _lookup(entries: dict, jsx_path: str) -> tuple[str, list] | None:
    """Find the sidecar entry for *jsx_path*, i.e. the ``.pyxl`` it came from."""
    for candidate in _suffixes(jsx_path):
        entry = entries.get(candidate)
        if isinstance(entry, dict):
            source = entry.get("pyxl")
            lines = entry.get("lines")
            if isinstance(source, str) and isinstance(lines, list):
                return source, lines
    return None


def _source_display_path(source: str) -> str:
    """Render a client-root-relative path as the author sees it.

    The sidecar stores ``../../pages/about.pyxl`` (relative to the client root,
    so no absolute paths land in build artifacts); a developer knows the file as
    ``pages/about.pyxl``.
    """
    cleaned = source.replace("\\", "/")
    while cleaned.startswith("../"):
        cleaned = cleaned[3:]
    return cleaned.lstrip("./")


def _copied_author_file(
    jsx_path: str, client_root: Path | None, pages_root: Path | None
) -> str | None:
    """The author's own file, when *jsx_path* names a verbatim copy of it.

    A ``.jsx`` component written beside the pages is copied into the client
    tree unchanged, at the same relative path under ``pages/``. Nothing
    about such a position is generated: the line and column are the author's
    own. This finds the source file so the build directory is never named and
    the position is never labelled as compiler output.

    Returns the path as the developer knows it, or ``None`` when *jsx_path* is
    not a copied asset — including whenever ``pages_root`` is unknown, because
    the answer is then unprovable and a guess here would re-create the bug this
    exists to prevent.
    """
    if client_root is None or pages_root is None:
        return None

    prefix = f"{_CLIENT_PAGES_DIR}/"
    for candidate in _suffixes(jsx_path):
        if not candidate.startswith(prefix):
            continue
        relative = candidate[len(prefix) :]
        author_file = pages_root / relative
        # Both halves must be on disk: the copy under the client root proves the
        # reported path really is that artifact, and the source proves it was
        # copied rather than compiled.
        if not author_file.is_file() or not (client_root / candidate).is_file():
            continue
        try:
            display = Path(os.path.relpath(author_file, client_root)).as_posix()
        except ValueError:  # pragma: no cover - Windows, different drives
            return author_file.as_posix()
        return _source_display_path(display)
    return None


def remap_generated_locations(
    text: str, client_root: Path | None, pages_root: Path | None = None
) -> str:
    """Rewrite ``<generated>.jsx:line[:col]`` positions in *text* to their source.

    ``pages/about.jsx:3:8`` becomes ``pages/about.pyxl:21:8``. A ``.jsx`` the
    developer wrote themselves is copied into the build tree unchanged, so it
    keeps its line and column and is reported against their own file. A position
    in a generated module that cannot be mapped keeps its path and is marked
    ``(generated)``, so the reader is never told an artifact is their own file.

    Only ``.jsx`` paths carrying a line number are considered. A bare path is
    left exactly as written, whatever it resolves to: it is indistinguishable
    from an import specifier the author typed, and rewriting it would edit their
    source text rather than describe it. An unresolved-import failure therefore
    keeps naming the build artifact — see ``docs/architecture/build-and-serve.md``.

    A ``.jsx`` inside a URL is left alone even when it carries one, because a
    URL is a link and rewriting its path breaks it:
    ``at HomePage (http://localhost:5176/pages/index.jsx:3:9)`` survives byte
    for byte.

    ``pages_root`` is the project's pages directory — the source side of the
    verbatim copy. Omitting it costs only the copied-asset case, which then
    falls back to the ``(generated)`` label.

    Returns *text* unchanged when it holds no such position — the overwhelmingly
    common case, and the reason this costs nothing on a message that does not
    need it.
    """
    if not text or ".jsx" not in text:
        return text

    entries = _load_sidecar(client_root) if client_root is not None else {}

    def replace(match: re.Match[str]) -> str:
        if match.group("url") is not None:
            # A URL is a link, not a location. Its path belongs to whoever
            # serves it, and rewriting that path breaks the link outright.
            return match.group(0)

        jsx_path = match.group("path")
        line_text, column_text = match.group("line"), match.group("column")
        suffix = f":{column_text}" if column_text else ""

        found = _lookup(entries, jsx_path)
        if found is None:
            author_file = _copied_author_file(jsx_path, client_root, pages_root)
            if author_file is not None:
                # Byte-for-byte copy: the coordinate is already the author's.
                return f"{author_file}:{line_text}{suffix}"
            return f"{match.group(0)} (generated)"

        source, lines = found
        index = int(line_text) - 1
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
