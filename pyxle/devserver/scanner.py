"""Source scanning utilities for the Pyxle development server."""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import List

from .settings import DevServerSettings


class SourceKind(str, Enum):
    """Types of source files discovered within the project."""

    PAGE = "page"
    API = "api"
    CLIENT_ASSET = "client_asset"


class ReservedApiDirectoryError(RuntimeError):
    """Raised when a ``.pyxl`` page sits inside an ``api`` directory.

    A directory named ``api`` is server ground at every level of the stack, so
    it cannot also hold pages. Refusing the file names the problem where the
    author can act on it; without the check the same project failed later, in
    Vite, as an unresolvable import inside ``.pyxle-build/``.
    """


@dataclass(frozen=True, slots=True)
class SourceFile:
    """Representation of a discovered source file."""

    kind: SourceKind
    absolute_path: Path
    relative_path: Path
    content_hash: str

    def as_dict(self) -> dict[str, str]:
        """Serialise the source description into primitives."""

        return {
            "kind": self.kind.value,
            "absolute_path": str(self.absolute_path),
            "relative_path": self.relative_path.as_posix(),
            "content_hash": self.content_hash,
        }


_HASH_CHUNK_SIZE = 64 * 1024  # 64KiB
_CLIENT_ASSET_SUFFIXES = {".jsx", ".js", ".tsx", ".ts", ".mjs", ".cjs", ".json", ".css"}

#: Every file suffix :func:`scan_source_tree` recognises as project source.
#: Shared with the watcher so "is this a file Pyxle builds?" has one answer.
SOURCE_SUFFIXES = frozenset({".pyxl", ".py", *_CLIENT_ASSET_SUFFIXES})


def is_source_file(path: Path) -> bool:
    """Whether *path* names a file Pyxle would build, judged by suffix alone.

    Used to keep editor scratch files out of user-facing output. Several
    editors save by writing a temporary sibling (``pages/sedo1AOsO``) and
    renaming it into place; the watcher must still react to those events —
    that rename *is* the save — but naming the temporary file in the rebuild
    log made it look like Pyxle had compiled it.
    """

    return path.suffix.lower() in SOURCE_SUFFIXES


def _in_api_directory(relative_path: Path) -> bool:
    """Return ``True`` when the file sits inside an ``api`` directory.

    One rule decides three things, so that a reader can predict all of them
    from the directory name alone: an ``api`` directory is **server ground**.
    Its ``.py`` files are endpoints, its client assets are never copied into
    the client build, and a ``.pyxl`` page in it is refused
    (:class:`ReservedApiDirectoryError`) rather than published as a page whose
    neighbouring components silently fail to resolve. The client router reads
    the same rule off the URL and leaves such links to the browser.

    The directory may be at any depth, so ``pages/api/health.py`` and
    ``pages/s/[slug]/api/v2/summary.json.py`` are both endpoints. The rule
    reads off the URL: a ``.py`` file serves the path it maps to whenever
    that path contains an ``api`` segment.

    Only directories count, never the file itself — ``pages/api.py`` is not
    an endpoint and ``pages/api.pyxl`` is an ordinary page, the same as before.
    """

    return "api" in relative_path.parts[:-1]


def _is_api_module(relative_path: Path) -> bool:
    """Return ``True`` when a ``.py`` file under ``pages/`` is an HTTP endpoint.

    It has to sit in an ``api`` directory, and its name must not mark it
    private. Python's own convention decides the second half: a leading
    underscore means "not part of the public surface", so ``api/_shared.py``,
    ``api/__init__.py`` and everything under ``api/_internal/`` are helper
    modules. They serve no URL and stay importable by the endpoints beside
    them, which is the whole point of colocating them. Without the rule such a
    file is registered as a route, exports no handler, and the app refuses to
    start.

    Only the segments at or below the ``api`` directory are read as Python.
    Above it the path is a URL, where an underscore carries no meaning, so
    ``pages/_admin/api/health.py`` still serves ``/_admin/api/health``.
    """

    if not _in_api_directory(relative_path):
        return False
    api_index = relative_path.parts[:-1].index("api")
    return not any(part.startswith("_") for part in relative_path.parts[api_index:])


def _reserved_api_directory_message(pages: Sequence[Path]) -> str:
    """Compose the error shown for ``.pyxl`` pages inside an ``api`` directory."""

    listed = "\n".join(f"  pages/{page.as_posix()}" for page in pages)
    if len(pages) == 1:
        opening = "this page sits inside one"
        closing = "move the page out of it"
    else:
        opening = "these pages sit inside one"
        closing = "move the pages out of it"
    return (
        f"A directory named 'api' holds endpoints, not pages, but {opening}:\n"
        f"{listed}\n"
        "An 'api' directory is server ground throughout: its .py files serve "
        "URLs, its client assets (.jsx, .css, .json) are never shipped to the "
        "browser, and links to its URLs are never client-side navigations — so "
        "a page there loads without the components beside it. Rename the "
        f"directory (for example 'reference/' or 'api-docs/'), or {closing}."
    )


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(_HASH_CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()


def scan_source_tree(settings: DevServerSettings) -> List[SourceFile]:
    """Walk the project's ``pages/`` directory and record relevant sources.

    Raises :class:`ReservedApiDirectoryError` when a ``.pyxl`` page is found
    inside an ``api`` directory — see :func:`_in_api_directory`.
    """

    pages_dir = settings.pages_dir
    if not pages_dir.exists():
        return []

    entries: list[SourceFile] = []
    reserved_pages: list[Path] = []

    for file_path in pages_dir.rglob("*"):
        if not file_path.is_file():
            continue

        relative_path = file_path.relative_to(pages_dir)

        # Ignore build artefacts that may live under the source tree (e.g. legacy
        # `.pyxle-build/` directories from older workflows). These files mirror
        # compiled output and should not be treated as fresh sources.
        if any(part == ".pyxle-build" for part in relative_path.parts):
            continue

        suffix = file_path.suffix.lower()
        if suffix == ".pyxl":
            if _in_api_directory(relative_path):
                # Collected rather than raised on sight: rglob's order is
                # filesystem order, so reporting every offender (sorted) makes
                # the message the same on every machine and on every run.
                reserved_pages.append(relative_path)
                continue
            kind = SourceKind.PAGE
        elif suffix == ".py":
            if not _is_api_module(relative_path):
                continue
            kind = SourceKind.API
        elif suffix in _CLIENT_ASSET_SUFFIXES:
            if _in_api_directory(relative_path):
                # An api directory is server ground: its client assets are not
                # copied to the client build.
                continue
            kind = SourceKind.CLIENT_ASSET
        else:
            continue

        entries.append(
            SourceFile(
                kind=kind,
                absolute_path=file_path,
                relative_path=relative_path,
                content_hash=_hash_file(file_path),
            )
        )

    if reserved_pages:
        reserved_pages.sort(key=lambda page: page.as_posix())
        raise ReservedApiDirectoryError(_reserved_api_directory_message(reserved_pages))

    entries.sort(key=lambda entry: entry.relative_path.as_posix())
    return entries
