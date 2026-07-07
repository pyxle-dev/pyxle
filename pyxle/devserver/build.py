"""Helpers for preparing the `.pyxle-build` cache."""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict

from .settings import DevServerSettings

CACHE_METADATA_FILENAME = "meta.json"
BUILD_CACHE_SCHEMA_VERSION = "1"


@dataclass(frozen=True, slots=True)
class BuildPaths:
    """Key directories inside the `.pyxle-build` cache."""

    build_root: Path
    client_root: Path
    server_root: Path
    metadata_root: Path


@dataclass(frozen=True, slots=True)
class CachedSourceRecord:
    """Stored hash information for a single source file."""

    kind: str
    content_hash: str

    def to_dict(self) -> Dict[str, str]:
        return {"kind": self.kind, "hash": self.content_hash}

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "CachedSourceRecord":
        kind = data.get("kind")
        hash_value = data.get("hash") or data.get("content_hash")
        if not isinstance(kind, str) or not isinstance(hash_value, str):
            raise ValueError("Invalid cached source record")
        return cls(kind=kind, content_hash=hash_value)


@dataclass(slots=True)
class BuildMetadata:
    """Metadata persisted alongside the build cache."""

    schema_version: str
    sources: Dict[str, CachedSourceRecord]

    @classmethod
    def empty(cls, schema_version: str) -> "BuildMetadata":
        return cls(schema_version=schema_version, sources={})

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "sources": {
                path: record.to_dict() for path, record in sorted(self.sources.items())
            },
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "BuildMetadata":
        schema_raw = data.get("schema_version")
        schema_version = str(schema_raw) if schema_raw is not None else BUILD_CACHE_SCHEMA_VERSION
        raw_sources = data.get("sources", {})
        sources: Dict[str, CachedSourceRecord] = {}
        if isinstance(raw_sources, dict):
            for key, value in raw_sources.items():
                if not isinstance(key, str) or not isinstance(value, dict):
                    continue
                try:
                    sources[key] = CachedSourceRecord.from_dict(value)
                except ValueError:
                    continue
        return cls(schema_version=schema_version, sources=sources)


def initialize_build_directories(settings: DevServerSettings) -> BuildPaths:
    """Create the standard `.pyxle-build` directory structure.

    The helper is idempotent—existing directories are left intact. All
    directories are created with ``parents=True`` to support nested paths.
    """

    build_root = settings.build_root
    client_root = settings.client_build_dir
    server_root = settings.server_build_dir
    metadata_root = settings.metadata_build_dir

    for path in (build_root, client_root, server_root, metadata_root):
        path.mkdir(parents=True, exist_ok=True)

    # Ensure the canonical `pages/` folders exist for downstream writers.
    (client_root / "pages").mkdir(parents=True, exist_ok=True)
    (server_root / "pages").mkdir(parents=True, exist_ok=True)

    return BuildPaths(
        build_root=build_root,
        client_root=client_root,
        server_root=server_root,
        metadata_root=metadata_root,
    )


def _metadata_path(build_root: Path) -> Path:
    return build_root / CACHE_METADATA_FILENAME


def _load_metadata(path: Path) -> BuildMetadata | None:
    if not path.exists():
        return None

    try:
        with path.open("r", encoding="utf-8") as file:
            data = json.load(file)
    except (OSError, json.JSONDecodeError):
        return None

    if not isinstance(data, dict):
        return None

    return BuildMetadata.from_dict(data)


def _write_metadata(path: Path, metadata: BuildMetadata) -> None:
    """Persist ``metadata`` to ``path`` — write-if-changed and atomic.

    Identical content is never rewritten, so a no-op build pass leaves the
    file's mtime untouched (the same guard every other stable generated
    artifact uses). Changed content lands via a temp file + ``os.replace`` so
    a concurrent reader can never observe a truncated ``meta.json`` — a torn
    read here used to be misdiagnosed as a schema mismatch, wiping the entire
    build cache.
    """
    payload = json.dumps(metadata.to_dict(), indent=2, sort_keys=True) + "\n"

    if path.exists():
        try:
            current = path.read_text(encoding="utf-8")
        except OSError:  # pragma: no cover - unreadable cache file; rewrite it
            current = None
        if current == payload:
            return

    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(path.name + ".tmp")
    try:
        temp_path.write_text(payload, encoding="utf-8")
        os.replace(temp_path, path)
    finally:
        if temp_path.exists():  # pragma: no cover - only on a failed write
            temp_path.unlink(missing_ok=True)


def load_build_metadata(build_root: Path, *, schema_version: str = BUILD_CACHE_SCHEMA_VERSION) -> BuildMetadata:
    """Read the cached build metadata, falling back to an empty structure."""

    metadata = _load_metadata(_metadata_path(build_root))
    if metadata is None:
        return BuildMetadata.empty(schema_version)
    return metadata


def ensure_fresh_build_cache(
    settings: DevServerSettings,
    *,
    schema_version: str = BUILD_CACHE_SCHEMA_VERSION,
) -> tuple[BuildPaths, BuildMetadata]:
    """Ensure the `.pyxle-build` cache matches the expected schema.

    If the on-disk metadata indicates a different schema version (or is
    missing/corrupt), the entire cache directory is removed and recreated.
    The resulting structure is guaranteed to contain a `meta.json` file with
    the provided ``schema_version``.
    """

    build_root = settings.build_root
    metadata_path = _metadata_path(build_root)
    metadata = _load_metadata(metadata_path)
    current_version = metadata.schema_version if metadata else None

    if current_version != schema_version and build_root.exists():
        shutil.rmtree(build_root)
        metadata = None

    paths = initialize_build_directories(settings)

    if metadata is None or metadata.schema_version != schema_version:
        metadata = BuildMetadata.empty(schema_version)

    _write_metadata(metadata_path, metadata)

    return paths, metadata


def save_build_metadata(build_root: Path, metadata: BuildMetadata) -> None:
    """Persist updated build metadata to disk."""

    _write_metadata(_metadata_path(build_root), metadata)
