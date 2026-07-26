"""Incremental build orchestration for the Pyxle development server."""

from __future__ import annotations

import shutil
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict

from pyxle.compiler.core import compile_file
from pyxle.compiler.writers import reconcile_client_sourcemap_sidecar

from .build import (
    BuildMetadata,
    BuildPaths,
    CachedSourceRecord,
    ensure_fresh_build_cache,
    save_build_metadata,
)
from .client_files import write_client_bootstrap_files
from .layouts import compose_layout_templates
from .scanner import SourceKind, scan_source_tree
from .scripts import sync_global_scripts
from .settings import DevServerSettings
from .styles import sync_global_stylesheets


@dataclass(slots=True)
class BuildSummary:
    """Report describing the outcome of a build invocation."""

    compiled_pages: list[str] = field(default_factory=list)
    copied_api_modules: list[str] = field(default_factory=list)
    copied_client_assets: list[str] = field(default_factory=list)
    synced_stylesheets: list[str] = field(default_factory=list)
    synced_scripts: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)

    def any_changes(self) -> bool:
        return bool(
            self.compiled_pages
            or self.copied_api_modules
            or self.copied_client_assets
            or self.synced_stylesheets
            or self.synced_scripts
            or self.removed
        )


#: Serializes build passes within the process. The watcher debounces
#: filesystem events, but a debounce timer that fires while a previous build
#: is still running starts a second pass on another thread
#: (``threading.Timer.cancel()`` cannot stop a timer that has already fired).
#: Two interleaved passes read and rewrite the same ``meta.json`` and
#: generated artifacts; a torn read used to make
#: :func:`ensure_fresh_build_cache` mistake a mid-write ``meta.json`` for a
#: schema mismatch and wipe the whole build cache — deleting and recreating
#: ``vite.config.js``, which Vite answers with a full (and racy) dev-server
#: restart. Running one pass at a time keeps every pass' view of the cache
#: consistent.
_BUILD_PASS_LOCK = threading.Lock()


def build_once(settings: DevServerSettings, *, force_rebuild: bool = False) -> BuildSummary:
    """Run a single build pass for the project located at ``settings``.

    Passes are serialized process-wide (see :data:`_BUILD_PASS_LOCK`): a call
    that arrives while another pass is running blocks until that pass
    finishes, then rebuilds against the fresh metadata it left behind.
    """

    with _BUILD_PASS_LOCK:
        return _build_once_locked(settings, force_rebuild=force_rebuild)


def _build_once_locked(settings: DevServerSettings, *, force_rebuild: bool) -> BuildSummary:
    paths, previous_metadata = ensure_fresh_build_cache(settings)
    sources = scan_source_tree(settings)
    summary = BuildSummary()

    new_sources: Dict[str, CachedSourceRecord] = {}

    for source in sources:
        relative_key = source.relative_path.as_posix()
        cached = previous_metadata.sources.get(relative_key)
        changed = force_rebuild or _is_changed(source.kind, source.content_hash, cached)

        if source.kind is SourceKind.PAGE:
            if changed:
                compile_file(
                    source.absolute_path,
                    build_root=paths.build_root,
                    client_root=paths.client_root,
                    server_root=paths.server_root,
                )
                summary.compiled_pages.append(relative_key)
            else:
                summary.skipped.append(relative_key)
        elif source.kind is SourceKind.API:
            destination = paths.server_root / source.relative_path
            if changed:
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source.absolute_path, destination)
                summary.copied_api_modules.append(relative_key)
            else:
                summary.skipped.append(relative_key)
        else:
            destination = paths.client_root / "pages" / source.relative_path
            if changed:
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source.absolute_path, destination)
                summary.copied_client_assets.append(relative_key)
            else:
                summary.skipped.append(relative_key)

        new_sources[relative_key] = CachedSourceRecord(
            kind=source.kind.value,
            content_hash=source.content_hash,
        )

    removed_keys = sorted(set(previous_metadata.sources) - set(new_sources))
    for relative_key in removed_keys:
        record = previous_metadata.sources[relative_key]
        _remove_artifacts(paths, Path(relative_key), record.kind)
        summary.removed.append(relative_key)

    updated_metadata = BuildMetadata(
        schema_version=previous_metadata.schema_version,
        sources=new_sources,
    )
    save_build_metadata(paths.build_root, updated_metadata)

    # Prune source-map sidecar entries for pages removed this pass so the
    # client sourcemap manifest mirrors the live set of .jsx modules.
    reconcile_client_sourcemap_sidecar(paths.client_root)

    compose_layout_templates(settings)
    if settings.global_stylesheets:
        updated_styles = sync_global_stylesheets(
            settings.global_stylesheets,
            client_root=paths.client_root,
        )
        summary.synced_stylesheets.extend(updated_styles)
    if settings.global_scripts:
        updated_scripts = sync_global_scripts(
            settings.global_scripts,
            client_root=paths.client_root,
        )
        summary.synced_scripts.extend(updated_scripts)
    write_client_bootstrap_files(settings)

    return summary


def _is_changed(
    kind: SourceKind,
    content_hash: str,
    cached: CachedSourceRecord | None,
) -> bool:
    if cached is None:
        return True
    if cached.kind != kind.value:
        return True
    return cached.content_hash != content_hash


def _remove_artifacts(paths: BuildPaths, relative_path: Path, kind: str) -> None:
    if kind == SourceKind.PAGE.value:
        _remove_page_artifacts(paths, relative_path)
    elif kind == SourceKind.API.value:
        _remove_api_artifacts(paths, relative_path)
    elif kind == SourceKind.CLIENT_ASSET.value:
        _remove_client_assets(paths, relative_path)


def _remove_page_artifacts(paths: BuildPaths, relative_path: Path) -> None:
    server_file = paths.server_root / "pages" / relative_path.with_suffix(".py")
    client_file = paths.client_root / "pages" / relative_path.with_suffix(".jsx")
    metadata_file = paths.metadata_root / "pages" / relative_path.with_suffix(".json")

    for target in (server_file, client_file, metadata_file):
        if target.exists():
            target.unlink()


def _remove_api_artifacts(paths: BuildPaths, relative_path: Path) -> None:
    target = paths.server_root / relative_path
    if target.exists():
        target.unlink()


def _remove_client_assets(paths: BuildPaths, relative_path: Path) -> None:
    target = paths.client_root / "pages" / relative_path
    if target.exists():
        target.unlink()
