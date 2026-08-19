"""Incremental build orchestration for the Pyxle development server."""

from __future__ import annotations

import shutil
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Sequence

from pyxle.compiler.core import compile_file
from pyxle.compiler.exceptions import CompilationError
from pyxle.compiler.writers import reconcile_client_sourcemap_sidecar
from pyxle.routing import route_path_variants_from_relative

from .build import (
    BuildMetadata,
    BuildPaths,
    CachedSourceRecord,
    ensure_fresh_build_cache,
    save_build_metadata,
)
from .build_errors import BuildFailure, build_code_frame, format_failures
from .client_files import write_client_bootstrap_files
from .layouts import compose_layout_templates
from .scanner import SourceFile, SourceKind, scan_source_tree
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

    #: Sources this pass could not compile. Every other file in the pass was
    #: still built, so a summary can carry both real changes and failures.
    failures: list[BuildFailure] = field(default_factory=list)

    def any_changes(self) -> bool:
        return bool(
            self.compiled_pages
            or self.copied_api_modules
            or self.copied_client_assets
            or self.synced_stylesheets
            or self.synced_scripts
            or self.removed
        )


class BuildFailed(Exception):
    """Raised when a build pass could not compile one or more sources.

    Carries the partial :class:`BuildSummary` as well as the failures, because
    the pass does not stop at the first broken file: everything that still
    compiles is built and written. The dev server needs both halves — what to
    reload, and what to refuse to serve — and a production build needs only the
    message, which names every failing file.
    """

    def __init__(self, failures: Sequence[BuildFailure], summary: BuildSummary) -> None:
        self.failures: tuple[BuildFailure, ...] = tuple(failures)
        self.summary = summary
        super().__init__(format_failures(self.failures))

    def __str__(self) -> str:
        return format_failures(self.failures)


#: Content hash stored for a source that failed to compile. It can never equal
#: a real digest, so the next pass always retries the file instead of skipping
#: it as unchanged while its artifacts on disk are still the previous version's.
#: Deliberately non-empty: an empty string does not survive the metadata
#: round-trip (:meth:`CachedSourceRecord.from_dict` reads it as missing) and
#: would make the following pass discard the whole build cache.
_FAILED_SOURCE_HASH = "!build-failed"


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

    Raises :class:`BuildFailed` when any source could not be compiled. The pass
    does not stop at the first one — every other file is built first — so the
    exception's ``summary`` describes real work that landed and its ``failures``
    name every file that did not.
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

        content_hash = source.content_hash
        if source.kind is SourceKind.PAGE:
            if changed:
                try:
                    compile_file(
                        source.absolute_path,
                        build_root=paths.build_root,
                        client_root=paths.client_root,
                        server_root=paths.server_root,
                        # Dev only. `pyxle build` and `pyxle serve` run with
                        # debug=False and keep their existing behaviour, so a
                        # disagreement between Babel and esbuild can never newly
                        # break a production build that works today.
                        report_jsx_syntax=settings.debug,
                    )
                except CompilationError as error:
                    # One unparseable file must not cost the developer the
                    # rebuild of every other file in the pass: collect it and
                    # keep going, so an edit elsewhere still hot-reloads while
                    # this one is being fixed.
                    summary.failures.append(_describe_failure(settings, source, error))
                    content_hash = _FAILED_SOURCE_HASH
                else:
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
            content_hash=content_hash,
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

    if summary.failures:
        # Raised only once the pass has finished writing everything that *did*
        # compile, so the partial summary the exception carries is accurate.
        raise BuildFailed(summary.failures, summary)

    return summary


def _describe_failure(
    settings: DevServerSettings, source: SourceFile, error: CompilationError
) -> BuildFailure:
    """Turn a compiler error into a located, self-contained failure record.

    The code frame is read here, at failure time, rather than when the page is
    later requested: the request path must not do file I/O, and by then the
    developer may already be mid-way through another edit, which would print a
    frame that does not match the error above it.
    """

    try:
        display_path = source.absolute_path.relative_to(settings.project_root).as_posix()
    except ValueError:  # pragma: no cover - source outside the project root
        display_path = source.absolute_path.as_posix()
    try:
        contents = source.absolute_path.read_text(encoding="utf-8-sig")
    except OSError:  # pragma: no cover - file vanished between compile and read
        contents = ""
    return BuildFailure(
        page_relative_path=source.relative_path,
        display_path=display_path,
        message=error.message,
        line=error.line_number,
        column=error.column,
        code_frame=build_code_frame(contents, error.line_number, error.column),
        url_paths=_static_url_paths(source.relative_path),
    )


def _static_url_paths(page_relative: Path) -> tuple[str, ...]:
    """The parameterless URLs *page_relative* would serve if it compiled.

    Only used for a source with no compiled route — one that has never built —
    where the URL is the only link back to the file. Paths carrying a route
    parameter are left out: matching those means matching patterns, and a page
    that has never compiled *and* is dynamic *and* is shadowed by another
    dynamic route is not worth a pattern matcher on the request path.
    """

    name = page_relative.name.lower()
    if name in {"layout.pyxl", "template.pyxl", "error.pyxl", "not-found.pyxl", "loading.pyxl"}:
        # None of these serve a URL of their own.
        return ()
    spec = route_path_variants_from_relative(page_relative)
    return tuple(
        path for path in (spec.primary, *spec.aliases) if "{" not in path
    )


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
