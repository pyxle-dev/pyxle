"""Utilities supporting the ``pyxle init`` scaffolding command."""

from __future__ import annotations

import re
import shutil
import unicodedata
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "InvalidProjectName",
    "InvalidImportAlias",
    "slugify_project_name",
    "validate_project_name",
    "validate_import_alias",
    "FilesystemWriter",
]


class InvalidProjectName(ValueError):
    """Raised when the provided project name is not filesystem safe."""


class InvalidImportAlias(ValueError):
    """Raised when the provided import alias is not a valid module prefix."""


_SLUG_PATTERN = re.compile(r"[^a-z0-9-]+")
_MULTIPLE_HYPHENS = re.compile(r"-{2,}")
_ALIAS_PREFIX_PATTERN = re.compile(r"^[A-Za-z0-9@~._-]+$")


def slugify_project_name(value: str) -> str:
    """Convert arbitrary input into a filesystem-safe slug.

    The slug contains lowercase ASCII letters, digits, and ``-``. Leading and
    trailing hyphens are stripped. An empty slug raises :class:`InvalidProjectName`.
    """

    if not value or not value.strip():
        raise InvalidProjectName("Project name cannot be blank.")

    normalized = unicodedata.normalize("NFKD", value)
    ascii_only = normalized.encode("ascii", "ignore").decode("ascii")
    cleaned = ascii_only.lower().replace("_", "-").replace(" ", "-")
    cleaned = _SLUG_PATTERN.sub("-", cleaned)
    cleaned = _MULTIPLE_HYPHENS.sub("-", cleaned).strip("-")

    if not cleaned:
        raise InvalidProjectName("Project name must contain alphanumeric characters.")

    return cleaned


def validate_project_name(value: str) -> str:
    """Validate the project name and return the filesystem-safe slug."""

    stripped = value.strip()
    # A path is not a name. Slugifying one turns every separator into a hyphen,
    # so `pyxle init apps/my-app` used to create a directory literally called
    # `apps-my-app` in the current directory — silently, and nowhere near where
    # the user pointed. Checked before the '.'/'-' rule so `./my-app` and
    # `../my-app` get this message rather than a misleading one about the
    # leading character.
    if stripped not in (".", "..") and ("/" in stripped or "\\" in stripped):
        raise InvalidProjectName(
            f"'{value.strip()}' looks like a path, and `pyxle init` takes a name — "
            "it creates a directory of that name in the current directory. "
            "Run it from the parent (e.g. `cd apps && pyxle init my-app`), "
            "or use `pyxle init .` to scaffold into the current directory."
        )
    if stripped.startswith(".") or stripped.startswith("-"):
        raise InvalidProjectName("Project name cannot start with '.' or '-'.")

    slug = slugify_project_name(value)
    if slug in {"con", "prn", "aux", "nul"}:
        raise InvalidProjectName("Project name conflicts with reserved system names.")
    return slug


def validate_import_alias(value: str) -> str:
    """Validate an import alias and return it normalised to ``<prefix>/*``.

    Accepts the bare prefix (``@``), a trailing slash (``@/``), or the full
    glob (``@/*``); all normalise to ``@/*``. The prefix may contain letters,
    digits, and ``@ ~ . _ -`` — anything else (notably a path separator) is
    rejected so it maps cleanly to a single Vite/jsconfig alias entry.
    """

    stripped = value.strip()
    if not stripped:
        raise InvalidImportAlias("Import alias cannot be blank.")

    if stripped.endswith("/*"):
        prefix = stripped[:-2]
    elif stripped.endswith("/"):
        prefix = stripped[:-1]
    else:
        prefix = stripped

    if not prefix or not _ALIAS_PREFIX_PATTERN.match(prefix):
        raise InvalidImportAlias(
            f"Invalid import alias '{value}'. Use a prefix such as '@/*' "
            "(letters, digits, and @ ~ . _ - only)."
        )

    return f"{prefix}/*"


@dataclass
class FilesystemWriter:
    """Helper encapsulating safe file and directory writes."""

    root: Path

    def ensure_root(self, force: bool = False, keep_root: bool = False) -> None:
        if keep_root:
            # Scaffolding into an existing directory (e.g. ``pyxle init .``):
            # never delete the directory itself. Require it to be empty unless
            # ``force`` is set, in which case existing files may be overwritten.
            self.root.mkdir(parents=True, exist_ok=True)
            if not force and any(self.root.iterdir()):
                raise FileExistsError(f"Target directory '{self.root}' is not empty.")
            return
        if self.root.exists() and not force:
            raise FileExistsError(f"Target directory '{self.root}' already exists.")
        if force and self.root.exists():
            if self.root.is_dir():
                shutil.rmtree(self.root)
            else:
                self.root.unlink()
        self.root.mkdir(parents=True, exist_ok=True)

    def write(
        self,
        relative_path: str | Path,
        content: bytes | str,
        *,
        binary: bool = False,
        overwrite: bool = False,
    ) -> None:
        path = self.root / Path(relative_path)
        if path.exists() and not overwrite:
            raise FileExistsError(f"File '{path}' already exists.")
        path.parent.mkdir(parents=True, exist_ok=True)
        if binary:
            data = content if isinstance(content, bytes) else str(content).encode("utf-8")
            path.write_bytes(data)
        else:
            text = content.decode("utf-8") if isinstance(content, (bytes, bytearray)) else str(content)
            path.write_text(text, encoding="utf-8")

    def touch_directory(self, relative_path: str | Path) -> Path:
        path = self.root / Path(relative_path)
        path.mkdir(parents=True, exist_ok=True)
        return path
