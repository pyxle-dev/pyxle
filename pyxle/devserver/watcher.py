"""File watching utilities for the Pyxle development server."""

from __future__ import annotations

import fnmatch
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, List, Sequence

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from pyxle.cli.logger import ConsoleLogger

from .builder import BuildSummary, build_once
from .settings import DevServerSettings

_RebuildCallback = Callable[[Sequence[Path]], None]
_RebuildListener = Callable[["WatcherStatistics"], None]
_TimerFactory = Callable[[float, Callable[[], None]], "_TimerHandle"]

#: Directory names whose contents are generated build artefacts, never sources.
#: A write anywhere beneath one of these is a side effect of a rebuild (compiled
#: output, cached bytecode) and must not trigger another rebuild.
_GENERATED_DIR_NAMES = frozenset({".pyxle-build", "__pycache__"})

#: File suffixes that identify generated output the watcher must ignore: Python
#: bytecode and SQLite database journals written by the running application.
#: These guard the rebuild watch (``pages/`` and any ``dev.watch`` extras): the
#: rebuild imports user Python, so ``.pyc`` bytecode is written back into a
#: watched tree, and a watched extra directory may hold a runtime SQLite file.
_GENERATED_SUFFIXES = frozenset({".pyc", ".db", ".db-wal", ".db-shm"})


def _is_generated_output(path: Path) -> bool:
    """Return ``True`` when *path* is a build artefact the rebuild watch ignores.

    A rebuild writes files back into a watched tree — Python caches ``.pyc``
    bytecode, the app touches its SQLite journals. Treating those writes as
    source edits would create a self-sustaining reload loop: the rebuild
    regenerates the very file whose change triggered it. Paths are resolved
    (following symlinks, tolerating a path that vanished mid-event) so the check
    is robust to symlinked directories and to Windows separators.

    These built-in ignores are load-bearing and always apply; a user's
    ``dev.ignore`` list (see :func:`_matches_ignore_glob`) is only ever additive
    on top of them.
    """

    try:
        resolved = path.resolve()
    except OSError:
        # Path vanished mid-event or a symlink loop — classify the raw path.
        resolved = path
    if resolved.suffix.lower() in _GENERATED_SUFFIXES:
        return True
    return any(part in _GENERATED_DIR_NAMES for part in resolved.parts)


def _matches_ignore_glob(path: Path, project_root: Path, globs: Sequence[str]) -> bool:
    """Return ``True`` when *path* matches one of the user's ``dev.ignore`` globs.

    Patterns are matched against the file's POSIX project-relative path (falling
    back to the absolute POSIX path when it lies outside the project root), using
    :func:`fnmatch.fnmatch` semantics. This is purely additive to the built-in
    generated-output ignores — it can suppress extra events but never re-enables
    a built-in-ignored one.
    """

    if not globs:
        return False
    try:
        relative = path.resolve().relative_to(project_root.resolve()).as_posix()
    except (OSError, ValueError):
        relative = path.as_posix()
    return any(fnmatch.fnmatch(relative, pattern) for pattern in globs)


class _TimerHandle:
    """Lightweight wrapper around timer objects to allow cancellation."""

    def __init__(self, delay: float, callback: Callable[[], None]) -> None:
        timer = threading.Timer(delay, callback)
        timer.daemon = True
        timer.start()
        self._timer = timer

    def cancel(self) -> None:
        self._timer.cancel()


def _default_timer_factory(delay: float, callback: Callable[[], None]) -> _TimerHandle:
    return _TimerHandle(delay, callback)


class _DebouncedChangeBuffer:
    """Aggregate filesystem events and invoke callback after debounce window."""

    def __init__(
        self,
        callback: _RebuildCallback,
        *,
        debounce_seconds: float,
        timer_factory: _TimerFactory = _default_timer_factory,
    ) -> None:
        self._callback = callback
        self._debounce_seconds = debounce_seconds
        self._timer_factory = timer_factory
        self._lock = threading.Lock()
        self._pending: set[Path] = set()
        self._handle: _TimerHandle | None = None

    def enqueue(self, path: Path) -> None:
        with self._lock:
            self._pending.add(path)
            if self._handle is not None:
                self._handle.cancel()
            self._handle = self._timer_factory(self._debounce_seconds, self.flush)

    def flush(self) -> None:
        with self._lock:
            if not self._pending:
                self._handle = None
                return
            paths = sorted(self._pending)
            self._pending.clear()
            self._handle = None
        self._callback(paths)


class _ProjectEventHandler(FileSystemEventHandler):
    """Watchdog handler that feeds events into a debounced buffer."""

    def __init__(
        self,
        sink: Callable[[Path], None],
        *,
        ignore: Callable[[Path], bool] | None = None,
    ) -> None:
        super().__init__()
        self._sink = sink
        self._ignore = ignore

    def on_any_event(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        dest = getattr(event, "dest_path", "")
        target_path = Path(dest) if dest else Path(event.src_path)
        if self._ignore is not None and self._ignore(target_path):
            return
        self._sink(target_path)


class _SingleFileEventHandler(FileSystemEventHandler):
    """Watchdog handler that fires ``on_change`` only when one specific file
    changes, ignoring every sibling in the watched directory."""

    def __init__(self, target: Path, on_change: Callable[[], None]) -> None:
        super().__init__()
        self._target = target
        self._on_change = on_change

    def on_any_event(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        candidates = [event.src_path]
        dest = getattr(event, "dest_path", "")
        if dest:
            candidates.append(dest)
        for raw in candidates:
            try:
                if Path(raw).resolve() == self._target:
                    self._on_change()
                    return
            except OSError:  # pragma: no cover - path vanished mid-event
                continue


class _PublicAssetEventHandler(FileSystemEventHandler):
    """Watchdog handler for the ``public/`` tree that keeps the dev static-file
    index current — and never rebuilds or reloads.

    Public assets are served live from disk (like Next.js), so a *modified*
    existing file is picked up on the next request with no watcher action. Only
    the *set* of discoverable paths needs maintaining: a newly created or
    deleted file changes which URLs resolve, so those structural events refresh
    the index. This handler deliberately does not touch the rebuild pipeline or
    the browser-reload channel.
    """

    #: Event types that change which public URLs resolve. A plain ``modified``
    #: event leaves the served set unchanged, so it is ignored.
    _STRUCTURAL_EVENTS = frozenset({"created", "deleted", "moved"})

    def __init__(self, on_structural_change: Callable[[], None]) -> None:
        super().__init__()
        self._on_structural_change = on_structural_change

    def on_any_event(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        if event.event_type not in self._STRUCTURAL_EVENTS:
            return
        self._on_structural_change()


@dataclass(slots=True)
class WatcherStatistics:
    """Outcome details for a rebuild triggered by the watcher."""

    elapsed_seconds: float
    summary: BuildSummary | None
    error: Exception | None
    changed_paths: Sequence[Path]


class ProjectWatcher:
    """Observe project directories and invoke incremental rebuilds on change."""

    def __init__(
        self,
        settings: DevServerSettings,
        *,
        logger: ConsoleLogger | None = None,
        debounce_seconds: float = 0.25,
        build_function: Callable[..., BuildSummary] = build_once,
        observer_factory: Callable[[], Observer] | None = None,
        timer_factory: _TimerFactory = _default_timer_factory,
        on_rebuild: _RebuildListener | None = None,
        public_index_refresh: Callable[[], None] | None = None,
    ) -> None:
        self._settings = settings
        self._logger = logger or ConsoleLogger()
        self._debounce_seconds = debounce_seconds
        self._build_function = build_function
        self._observer_factory = observer_factory or Observer
        self._timer_factory = timer_factory
        self._on_rebuild = on_rebuild
        # Refreshes the dev static-file index when a public/ file is added or
        # removed, so it becomes discoverable without a restart. ``None`` when
        # no static index is wired (unit tests, static serving disabled) — the
        # public/ watch then simply does nothing on an event.
        self._public_index_refresh = public_index_refresh
        # User-supplied extra ignore globs (``dev.ignore``), additive to the
        # built-in generated-output ignores in ``_is_generated_output``.
        self._ignore_globs = tuple(settings.dev_ignore_globs)

        self._observer: Observer | None = None
        self._buffer = _DebouncedChangeBuffer(
            self._handle_paths,
            debounce_seconds=debounce_seconds,
            timer_factory=timer_factory,
        )
        self._handler = _ProjectEventHandler(
            self._buffer.enqueue,
            ignore=self._should_ignore,
        )
        self._public_handler = _PublicAssetEventHandler(self._schedule_index_refresh)
        self._latest_stats: WatcherStatistics | None = None
        self._config_warn_handle: _TimerHandle | None = None
        self._index_refresh_handle: _TimerHandle | None = None

    @property
    def running(self) -> bool:
        return self._observer is not None

    @property
    def latest_statistics(self) -> WatcherStatistics | None:
        return self._latest_stats

    def start(self) -> None:
        if self._observer is not None:
            return

        observer = self._observer_factory()
        pages_dir = self._settings.pages_dir
        public_dir = self._settings.public_dir
        observer.schedule(self._handler, str(pages_dir), recursive=True)
        # public/ gets the lightweight index-refresh handler — NOT the rebuild
        # handler. Public assets are served live from disk (like Next.js), so a
        # change never rebuilds or reloads; only newly added/removed files need
        # the static index refreshed so they become discoverable in dev.
        observer.schedule(self._public_handler, str(public_dir), recursive=True)
        pages_root = pages_dir.resolve()
        public_root = public_dir.resolve()
        watched_extras: set[Path] = set()
        # Extra ``dev.watch`` directories join the rebuild watch: a change under
        # one runs the normal rebuild + module-invalidation + reload path, so an
        # imported module outside ``pages/`` (e.g. ``lib/``) hot-reloads.
        for directory in self._settings.dev_watch_dirs:
            try:
                resolved = directory.resolve()
            except OSError:
                continue
            if not resolved.exists():
                # A typo'd or not-yet-created path would otherwise be watched by
                # nothing with no feedback — surface it so it isn't silent.
                self._logger.warning(
                    f"dev.watch directory does not exist and is skipped: "
                    f"{directory.as_posix()} (restart 'pyxle dev' after creating it)"
                )
                continue
            if resolved == pages_root:
                continue
            if resolved == public_root or resolved.is_relative_to(public_root):
                # public/ is served live and index-only — never rebuild-watched.
                # Rebuild-watching it (or a subdirectory) would defeat that and
                # could reintroduce the reload loop (e.g. a Tailwind write to
                # public/styles/tailwind.css).
                self._logger.warning(
                    f"dev.watch directory is inside public/ and is skipped: "
                    f"{directory.as_posix()} (public assets are served live, not rebuilt)"
                )
                continue
            if resolved in watched_extras:
                continue
            observer.schedule(self._handler, str(resolved), recursive=True)
            watched_extras.add(resolved)
        for directory in _global_stylesheet_directories(self._settings):
            try:
                resolved = directory.resolve()
            except OSError:
                continue
            if not resolved.exists():
                continue
            if resolved == pages_root or resolved == public_root:
                continue
            if resolved in watched_extras:
                continue
            observer.schedule(self._handler, str(resolved), recursive=True)
            watched_extras.add(resolved)
        for directory in _global_script_directories(self._settings):
            try:
                resolved = directory.resolve()
            except OSError:
                continue
            if not resolved.exists():
                continue
            if resolved == pages_root or resolved == public_root:
                continue
            if resolved in watched_extras:
                continue
            observer.schedule(self._handler, str(resolved), recursive=True)
            watched_extras.add(resolved)
        # Watch pyxle.config.json (at the project root) on a dedicated handler.
        # Config is wired into the app at startup, so a change can't hot-reload
        # — but silently ignoring it is worse than a clear "restart" nudge.
        config_path = (self._settings.project_root / "pyxle.config.json").resolve()
        observer.schedule(
            _SingleFileEventHandler(config_path, self._handle_config_change),
            str(self._settings.project_root),
            recursive=False,
        )
        observer.start()
        self._observer = observer

    def stop(self) -> None:
        observer = self._observer
        if observer is None:
            return
        observer.stop()
        observer.join(timeout=2)
        self._observer = None

    def close(self) -> None:
        self.stop()

    def flush(self) -> None:
        """Force any pending events to be processed immediately."""

        self._buffer.flush()

    def notify_paths(self, paths: Iterable[Path]) -> None:
        """Manually enqueue a collection of changed paths (primarily for tests)."""

        for path in paths:
            self._buffer.enqueue(path)

    # Internal orchestration -------------------------------------------------

    def _should_ignore(self, path: Path) -> bool:
        """Ignore predicate for the rebuild event handler.

        Combines the built-in generated-output ignores (always in force) with
        any user-supplied ``dev.ignore`` globs (additive only).
        """

        if _is_generated_output(path):
            return True
        return _matches_ignore_glob(
            path, self._settings.project_root, self._ignore_globs
        )

    def _schedule_index_refresh(self) -> None:
        """Debounce public/ structural events into one static-index refresh.

        Editors and some platforms emit several events per file operation, so
        coalesce them before rewalking the public tree.
        """
        if self._index_refresh_handle is not None:
            self._index_refresh_handle.cancel()
        self._index_refresh_handle = self._timer_factory(
            self._debounce_seconds, self._run_index_refresh
        )

    def _run_index_refresh(self) -> None:
        self._index_refresh_handle = None
        if self._public_index_refresh is None:
            return
        try:
            self._public_index_refresh()
        except Exception as error:  # pragma: no cover - defensive logging
            self._logger.warning(f"Static index refresh failed: {error}")

    def _handle_paths(self, paths: Sequence[Path]) -> None:
        start = time.perf_counter()
        formatted = self._format_paths(paths)
        files_changed = len(paths)
        self._logger.debug(f"Rebuild triggered — {files_changed} file(s) changed: {formatted}")

        try:
            summary = self._build_function(self._settings, force_rebuild=False)
        except OSError as error:
            elapsed = time.perf_counter() - start
            self._logger.error(f"Filesystem error during rebuild ({elapsed:.2f}s): {error}")
            stats = WatcherStatistics(
                elapsed_seconds=elapsed,
                summary=None,
                error=error,
                changed_paths=paths,
            )
            self._latest_stats = stats
            self._emit_rebuild(stats)
            return
        except Exception as error:  # pragma: no cover - defensive guard
            elapsed = time.perf_counter() - start
            self._logger.error(f"Rebuild failed ({elapsed:.2f}s): {error}")
            stats = WatcherStatistics(
                elapsed_seconds=elapsed,
                summary=None,
                error=error,
                changed_paths=paths,
            )
            self._latest_stats = stats
            self._emit_rebuild(stats)
            return

        elapsed = time.perf_counter() - start
        stats = WatcherStatistics(
            elapsed_seconds=elapsed,
            summary=summary,
            error=None,
            changed_paths=paths,
        )
        self._latest_stats = stats
        self._emit_rebuild(stats)
        _invalidate_python_modules(paths, self._settings.project_root)

        if summary.any_changes():
            # Curated one-liner: what rebuilt and how long it took. The full
            # per-category breakdown stays at debug (`--verbose`).
            self._logger.success(f"Rebuilt {formatted} in {elapsed * 1000:.0f} ms")
            self._logger.debug(
                "Rebuild breakdown — "
                f"pages: {len(summary.compiled_pages)}, "
                f"apis: {len(summary.copied_api_modules)}, "
                f"client assets: {len(summary.copied_client_assets)}, "
                f"global styles: {len(summary.synced_stylesheets)}, "
                f"global scripts: {len(summary.synced_scripts)}, "
                f"removed: {len(summary.removed)}"
            )
        else:
            self._logger.debug(
                f"Rebuild finished in {elapsed:.2f}s with no material changes (debounced {files_changed} event(s))"
            )

    def _format_paths(self, paths: Sequence[Path]) -> str:
        project_root = self._settings.project_root
        rendered: List[str] = []
        for path in paths[:5]:
            try:
                rendered.append(path.relative_to(project_root).as_posix())
            except ValueError:
                rendered.append(path.as_posix())
        remaining = len(paths) - len(rendered)
        if remaining > 0:
            rendered.append(f"+{remaining} more")
        return ", ".join(rendered)

    def _emit_rebuild(self, stats: WatcherStatistics) -> None:
        if self._on_rebuild is None:
            return
        try:
            self._on_rebuild(stats)
        except Exception as error:  # pragma: no cover - defensive logging
            self._logger.warning(f"Rebuild listener raised error: {error}")

    def _handle_config_change(self) -> None:
        """Debounce ``pyxle.config.json`` events into one 'restart' warning.

        Editors emit several events per save, so coalesce them before warning.
        Config is never rebuilt — it's wired into the app at startup.
        """
        if self._config_warn_handle is not None:
            self._config_warn_handle.cancel()
        self._config_warn_handle = self._timer_factory(
            self._debounce_seconds, self._emit_config_warning
        )

    def _emit_config_warning(self) -> None:
        self._config_warn_handle = None
        self._logger.warning(
            "pyxle.config.json changed — restart `pyxle dev` to apply. "
            "Configuration (middleware, plugins, ports, CORS/CSRF, cache) is "
            "wired at startup and is not hot-reloaded."
        )


def _invalidate_python_modules(paths: Sequence[Path], project_root: Path) -> None:
    purged: set[str] = set()
    for path in paths:
        if path.suffix != ".py":
            continue
        module_name = _module_name_from_path(path, project_root)
        if not module_name:
            continue
        for target in _expand_module_hierarchy(module_name):
            if target in purged:
                continue
            if target in sys.modules:
                del sys.modules[target]
            purged.add(target)


def _module_name_from_path(path: Path, project_root: Path) -> str | None:
    try:
        relative = path.relative_to(project_root)
    except ValueError:
        return None
    if relative.name == "__init__.py":
        relative = relative.parent
    else:
        relative = relative.with_suffix("")
    parts = [part for part in relative.parts if part not in (
        "",
        "__pycache__",
    )]
    if not parts:
        return None
    return ".".join(parts)


def _expand_module_hierarchy(module_name: str) -> list[str]:
    parts = module_name.split(".")
    hierarchy: list[str] = []
    for index in range(len(parts), 0, -1):
        hierarchy.append(".".join(parts[:index]))
    return hierarchy


def _global_stylesheet_directories(settings: DevServerSettings) -> list[Path]:
    directories: list[Path] = []
    seen: set[Path] = set()
    for sheet in settings.global_stylesheets:
        parent = sheet.source_path.parent
        if parent in seen:
            continue
        seen.add(parent)
        directories.append(parent)
    return directories


def _global_script_directories(settings: DevServerSettings) -> list[Path]:
    directories: list[Path] = []
    seen: set[Path] = set()
    for script in settings.global_scripts:
        parent = script.source_path.parent
        if parent in seen:
            continue
        seen.add(parent)
        directories.append(parent)
    return directories


__all__ = ["ProjectWatcher", "WatcherStatistics"]
