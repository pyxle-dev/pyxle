"""Global stylesheet helpers for the Pyxle dev server."""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence


class GlobalStyleConfigError(ValueError):
    """Raised when global stylesheet configuration is invalid."""


@dataclass(frozen=True, slots=True)
class GlobalStylesheet:
    """Descriptor for a configured global stylesheet file."""

    source_path: Path
    relative_path: Path
    identifier: str

    @property
    def client_relative_path(self) -> Path:
        suffix = self.relative_path.suffix or ".css"
        return Path("styles") / f"{self.identifier}{suffix}"

    @property
    def import_specifier(self) -> str:
        return f"./{self.client_relative_path.as_posix()}"

    @property
    def vite_url(self) -> str:
        return f"/{self.client_relative_path.as_posix()}"

    def as_dict(self) -> dict[str, str]:
        return {
            "source_path": str(self.source_path),
            "relative_path": self.relative_path.as_posix(),
            "identifier": self.identifier,
            "client_relative_path": self.client_relative_path.as_posix(),
        }


def resolve_global_stylesheets(
    project_root: Path,
    entries: Sequence[str] | Iterable[str] | None,
) -> tuple[GlobalStylesheet, ...]:
    """Validate and resolve configured global stylesheet paths."""

    if not entries:
        return ()

    root = project_root.expanduser().resolve()
    resolved: list[GlobalStylesheet] = []
    seen: set[str] = set()

    for raw_entry in entries:
        if raw_entry is None:
            continue
        if not isinstance(raw_entry, str):
            raise GlobalStyleConfigError(
                f"Global stylesheet entries must be strings; got {type(raw_entry).__name__}."
            )
        entry = raw_entry.strip()
        if not entry:
            continue
        relative = _normalize_relative_path(entry)
        key = relative.as_posix()
        if key in seen:
            continue
        seen.add(key)
        source_path = (root / relative).resolve()
        if not source_path.exists():
            raise GlobalStyleConfigError(
                f"Global stylesheet '{entry}' was not found under project root '{root}'."
            )
        if not source_path.is_file():
            raise GlobalStyleConfigError(
                f"Global stylesheet '{entry}' must be a file, not a directory."
            )
        identifier = _make_identifier(key)
        resolved.append(
            GlobalStylesheet(
                source_path=source_path,
                relative_path=relative,
                identifier=identifier,
            )
        )

    return tuple(resolved)



#: Where a project's own source lives when ``jsconfig.json`` does not say.
#: ``pages`` is deliberately absent: it is compiled into the Vite root, so
#: Tailwind already finds it.
_DEFAULT_SOURCE_DIRS: tuple[str, ...] = ("components", "lib")

_TAILWIND_IMPORT = re.compile(r"""^[ \t]*@import[ \t]+["']tailwindcss["'][^\n]*$""", re.M)

#: Marks our injected block so a second copy does not stack another one.
_SOURCE_MARKER = "/* pyxle: Tailwind source roots (generated) */"


def _jsconfig_include(project_root: Path) -> list[str]:
    """Directory names listed in ``jsconfig.json``'s ``include``."""
    try:
        data = json.loads((project_root / "jsconfig.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    include = data.get("include")
    if not isinstance(include, list):
        return []
    return [entry for entry in include if isinstance(entry, str)]


def project_source_dirs(project_root: Path) -> list[Path]:
    """Existing directories holding the project's own source files.

    Taken from ``jsconfig.json``'s ``include`` — the same declaration the editor
    and shadcn/ui read — falling back to a conventional default. ``pages`` is
    dropped: its compiled output already sits under the Vite root.
    """
    names = _jsconfig_include(project_root) or list(_DEFAULT_SOURCE_DIRS)
    dirs: list[Path] = []
    for name in names:
        if name == "pages":
            continue
        candidate = project_root / name
        if candidate.is_dir() and candidate not in dirs:
            dirs.append(candidate)
    return dirs


def inject_tailwind_sources(css: str, *, destination: Path, project_root: Path) -> str:
    """Point Tailwind at the project's own source directories.

    Vite's root is the generated client directory, which holds the *compiled*
    pages and nothing else. Tailwind v4 auto-detects its sources from that root,
    so a utility class used only in the project's own ``components/`` — which is
    exactly where ``shadcn/ui`` puts every component it installs — is never
    generated, and the component renders unstyled with no error anywhere.

    The stylesheet is copied into the build directory, so a hand-written
    ``@source "../../components"`` in the project's CSS resolves *inside* that
    directory and silently matches nothing. The paths therefore have to be
    rewritten at copy time, which is what this does. Returns *css* unchanged
    when it does not import Tailwind.
    """
    if _SOURCE_MARKER in css:
        return css
    match = _TAILWIND_IMPORT.search(css)
    if match is None:
        return css
    dirs = project_source_dirs(project_root)
    if not dirs:
        return css
    block = [_SOURCE_MARKER]
    for directory in dirs:
        relative = os.path.relpath(directory, destination.parent).replace(os.sep, "/")
        block.append(f'@source "{relative}";')
    return css[: match.end()] + "\n" + "\n".join(block) + css[match.end() :]


def sync_global_stylesheets(
    stylesheets: Sequence[GlobalStylesheet],
    *,
    client_root: Path,
) -> list[str]:
    """Copy configured stylesheets into the client build directory.

    Returns a list of relative stylesheet identifiers that were updated.
    """

    updated: list[str] = []
    for sheet in stylesheets:
        destination = client_root / sheet.client_relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            payload = sheet.source_path.read_bytes()
        except OSError as exc:  # pragma: no cover - surface helpful context
            raise GlobalStyleConfigError(
                f"Unable to read global stylesheet '{sheet.source_path}': {exc}"
            ) from exc

        if destination.exists():
            try:
                existing = destination.read_bytes()
            except OSError:
                existing = b""
            if existing == payload:
                continue

        destination.write_bytes(payload)
        updated.append(sheet.relative_path.as_posix())

    return updated


def load_inline_stylesheets(
    stylesheets: Sequence[GlobalStylesheet],
) -> list[tuple[GlobalStylesheet, str]]:
    """Read stylesheet contents for inlining within SSR documents."""

    payloads: list[tuple[GlobalStylesheet, str]] = []
    for sheet in stylesheets:
        try:
            contents = sheet.source_path.read_text(encoding="utf-8")
        except OSError:
            continue
        payloads.append((sheet, contents))
    return payloads


def _normalize_relative_path(value: str) -> Path:
    candidate = Path(value)
    if candidate.is_absolute():
        raise GlobalStyleConfigError(
            f"Global stylesheet '{value}' must be a relative path inside the project root."
        )

    parts: list[str] = []
    for part in candidate.parts:
        if part in ("", "."):
            continue
        if part == "..":
            raise GlobalStyleConfigError(
                f"Global stylesheet '{value}' cannot navigate outside the project root."
            )
        parts.append(part)

    if not parts:
        raise GlobalStyleConfigError(
            "Global stylesheet paths must reference a file (received only separators)."
        )

    return Path(*parts)


def _make_identifier(value: str) -> str:
    digest = hashlib.blake2s(value.encode("utf-8"), digest_size=12).hexdigest()
    safe = value.replace("/", "-").replace(".", "-")
    safe = "-".join(filter(None, safe.split("-")))
    return f"pyxle-style-{safe}-{digest}"


__all__ = [
    "GlobalStyleConfigError",
    "GlobalStylesheet",
    "resolve_global_stylesheets",
    "sync_global_stylesheets",
    "inject_tailwind_sources",
    "project_source_dirs",
    "load_inline_stylesheets",
]
