from __future__ import annotations

import sys
import time
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Callable, List, Tuple

import pytest

from pyxle.cli.logger import ConsoleLogger, Verbosity
from pyxle.devserver.builder import BuildSummary, build_once
from pyxle.devserver.settings import DevServerSettings
from pyxle.devserver.watcher import (
    ProjectWatcher,
    WatcherStatistics,
    _default_timer_factory,
    _invalidate_python_modules,
    _is_generated_output,
    _module_name_from_path,
    _ProjectEventHandler,
)


class ManualTimerHandle:
    def __init__(self, callback: Callable[[], None]) -> None:
        self._callback = callback
        self._cancelled = False

    def trigger(self) -> None:
        if not self._cancelled:
            self._callback()

    def cancel(self) -> None:  # pragma: no cover - exercised indirectly
        self._cancelled = True


class DummyObserver:
    def __init__(self) -> None:
        self.scheduled: List[tuple[object, str, bool]] = []
        self.started = False
        self.stopped = False
        self.join_called = False

    def schedule(self, handler, path: str, recursive: bool) -> None:  # pragma: no cover - simple forwarding
        self.scheduled.append((handler, path, recursive))

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:  # pragma: no cover - unused in tests
        self.stopped = True

    def join(self, timeout: float | None = None) -> None:  # pragma: no cover - unused in tests
        self.join_called = True


@dataclass
class LogCapture:
    messages: List[str]

    def __call__(self, message: str, fg: str | None = None, bold: bool = False) -> None:  # pragma: no cover - formatting only
        self.messages.append(message)


@pytest.fixture
def project(tmp_path: Path) -> DevServerSettings:
    root = tmp_path / "project"
    (root / "pages").mkdir(parents=True)
    (root / "public").mkdir()
    return DevServerSettings.from_project_root(root)


@pytest.fixture
def timer_factory() -> Tuple[Callable[[float, Callable[[], None]], ManualTimerHandle], List[ManualTimerHandle]]:
    handles: List[ManualTimerHandle] = []

    def factory(delay: float, callback: Callable[[], None]) -> ManualTimerHandle:
        handle = ManualTimerHandle(callback)
        handles.append(handle)
        return handle

    return factory, handles


def make_logger() -> tuple[ConsoleLogger, List[str]]:
    capture = LogCapture(messages=[])
    logger = ConsoleLogger(secho=capture)
    return logger, capture.messages


def test_project_watcher_start_schedules_directories(project: DevServerSettings) -> None:
    observer = DummyObserver()
    logger, _ = make_logger()

    watcher = ProjectWatcher(
        project,
        logger=logger,
        observer_factory=lambda: observer,
        timer_factory=lambda delay, callback: ManualTimerHandle(callback),
        build_function=lambda settings, **_: BuildSummary(),
    )

    watcher.start()

    assert observer.started is True
    scheduled_paths = [Path(path) for _, path, _ in observer.scheduled]
    assert project.pages_dir in scheduled_paths
    assert project.public_dir in scheduled_paths


def test_project_watcher_start_is_idempotent(project: DevServerSettings) -> None:
    observer = DummyObserver()
    logger, _ = make_logger()

    watcher = ProjectWatcher(
        project,
        logger=logger,
        observer_factory=lambda: observer,
        timer_factory=lambda delay, callback: ManualTimerHandle(callback),
        build_function=lambda settings, **_: BuildSummary(),
    )

    watcher.start()
    watcher.start()  # second invocation should be a no-op

    # pages/, public/, and the project root (for pyxle.config.json).
    assert len(observer.scheduled) == 3


def test_project_watcher_watches_config_file(project: DevServerSettings) -> None:
    """start() schedules a dedicated single-file watch for pyxle.config.json on
    the project root (non-recursive)."""
    from pyxle.devserver.watcher import _SingleFileEventHandler

    observer = DummyObserver()
    logger, _ = make_logger()
    watcher = ProjectWatcher(
        project,
        logger=logger,
        observer_factory=lambda: observer,
        timer_factory=lambda delay, callback: ManualTimerHandle(callback),
        build_function=lambda settings, **_: BuildSummary(),
    )

    watcher.start()

    config_watches = [
        (handler, Path(path), recursive)
        for handler, path, recursive in observer.scheduled
        if isinstance(handler, _SingleFileEventHandler)
    ]
    assert len(config_watches) == 1
    _, path, recursive = config_watches[0]
    assert path == project.project_root
    assert recursive is False


def test_config_change_warns_to_restart(project: DevServerSettings, timer_factory) -> None:
    """A pyxle.config.json change emits one debounced 'restart' warning."""
    factory, handles = timer_factory
    logger, messages = make_logger()
    watcher = ProjectWatcher(
        project,
        logger=logger,
        observer_factory=DummyObserver,
        timer_factory=factory,
        build_function=lambda settings, **_: BuildSummary(),
    )

    watcher._handle_config_change()
    # Debounced — nothing surfaced until the timer fires.
    assert not any("pyxle.config.json" in m for m in messages)
    handles[-1].trigger()
    assert any("pyxle.config.json changed" in m and "restart" in m.lower() for m in messages)


def test_single_file_handler_fires_only_for_target(tmp_path: Path) -> None:
    from watchdog.events import FileModifiedEvent

    from pyxle.devserver.watcher import _SingleFileEventHandler

    target = tmp_path / "pyxle.config.json"
    target.write_text("{}", encoding="utf-8")
    sibling = tmp_path / "package.json"
    sibling.write_text("{}", encoding="utf-8")

    fired: list[bool] = []
    handler = _SingleFileEventHandler(target.resolve(), lambda: fired.append(True))

    handler.on_any_event(FileModifiedEvent(str(target)))
    assert fired == [True]

    fired.clear()
    handler.on_any_event(FileModifiedEvent(str(sibling)))
    assert fired == []


def test_project_watcher_watches_global_styles(tmp_path: Path) -> None:
    root = tmp_path / "project"
    (root / "pages").mkdir(parents=True)
    (root / "public").mkdir()
    style_dir = root / "styles"
    style_dir.mkdir()
    (style_dir / "global.css").write_text("body { color: tomato; }\n", encoding="utf-8")

    settings = DevServerSettings.from_project_root(
        root,
        global_stylesheets=("styles/global.css",),
    )

    observer = DummyObserver()
    logger, _ = make_logger()

    watcher = ProjectWatcher(
        settings,
        logger=logger,
        observer_factory=lambda: observer,
        timer_factory=lambda delay, callback: ManualTimerHandle(callback),
        build_function=lambda *_: BuildSummary(),
    )

    watcher.start()

    scheduled = {Path(path).resolve() for _, path, _ in observer.scheduled}
    assert style_dir.resolve() in scheduled


def test_project_watcher_watches_global_scripts(tmp_path: Path) -> None:
    root = tmp_path / "project"
    (root / "pages").mkdir(parents=True)
    (root / "public").mkdir()
    script_dir = root / "scripts"
    script_dir.mkdir()
    (script_dir / "analytics.js").write_text("console.log('analytics');\n", encoding="utf-8")

    settings = DevServerSettings.from_project_root(
        root,
        global_scripts=("scripts/analytics.js",),
    )

    observer = DummyObserver()
    logger, _ = make_logger()

    watcher = ProjectWatcher(
        settings,
        logger=logger,
        observer_factory=lambda: observer,
        timer_factory=lambda delay, callback: ManualTimerHandle(callback),
        build_function=lambda *_: BuildSummary(),
    )

    watcher.start()

    scheduled = {Path(path).resolve() for _, path, _ in observer.scheduled}
    assert script_dir.resolve() in scheduled


def test_project_watcher_stop_without_start(project: DevServerSettings) -> None:
    logger, _ = make_logger()

    watcher = ProjectWatcher(
        project,
        logger=logger,
        timer_factory=lambda delay, callback: ManualTimerHandle(callback),
        build_function=lambda settings, **_: BuildSummary(),
    )

    watcher.stop()  # should not raise and leaves watcher stopped


def test_project_watcher_start_and_close(project: DevServerSettings) -> None:
    observer = DummyObserver()
    logger, _ = make_logger()

    watcher = ProjectWatcher(
        project,
        logger=logger,
        observer_factory=lambda: observer,
        timer_factory=lambda delay, callback: ManualTimerHandle(callback),
        build_function=lambda settings, **_: BuildSummary(),
    )

    watcher.start()
    assert watcher.running is True

    watcher.close()

    assert watcher.running is False
    assert observer.stopped is True
    assert observer.join_called is True


def test_rebuild_triggers_once_after_debounce(
    project: DevServerSettings, timer_factory: Tuple[Callable[[float, Callable[[], None]], ManualTimerHandle], List[ManualTimerHandle]]
) -> None:
    factory, handles = timer_factory
    logger, messages = make_logger()
    rebuild_calls: List[int] = []

    def build(settings: DevServerSettings, **_: object) -> BuildSummary:
        rebuild_calls.append(1)
        assert settings.pages_dir == project.pages_dir
        return BuildSummary(compiled_pages=["index.pyxl"], copied_api_modules=["api/pulse.py"], removed=[])

    watcher = ProjectWatcher(
        project,
        logger=logger,
        timer_factory=factory,
        build_function=build,
    )

    watcher.notify_paths([project.pages_dir / "index.pyxl"])
    watcher.notify_paths([project.pages_dir / "api/pulse.py"])

    assert len(handles) == 2
    handles[-1].trigger()

    assert len(rebuild_calls) == 1
    # The curated rebuild output is a concise one-liner ("Rebuilt <files> in
    # <ms> ms"); the pre-step and per-category breakdown are debug-only.
    assert any(message.startswith("✅ Rebuilt ") for message in messages)
    assert not any("Rebuild breakdown" in message for message in messages)


def test_project_watcher_invokes_rebuild_callback(
    project: DevServerSettings,
    timer_factory: Tuple[Callable[[float, Callable[[], None]], ManualTimerHandle], List[ManualTimerHandle]],
) -> None:
    factory, handles = timer_factory
    stats: List[WatcherStatistics] = []

    watcher = ProjectWatcher(
        project,
        timer_factory=factory,
        build_function=lambda settings, **_: BuildSummary(compiled_pages=["index.pyxl"]),
        on_rebuild=lambda payload: stats.append(payload),
    )

    watcher.notify_paths([project.pages_dir / "index.pyxl"])
    handles[-1].trigger()

    assert stats, "expected callback invocation"
    assert stats[0].summary is not None
    assert stats[0].summary.compiled_pages == ["index.pyxl"]


def test_project_watcher_flush_without_pending(project: DevServerSettings) -> None:
    logger, _ = make_logger()

    watcher = ProjectWatcher(
        project,
        logger=logger,
        timer_factory=lambda delay, callback: ManualTimerHandle(callback),
        build_function=lambda settings, **_: BuildSummary(),
    )

    watcher.flush()  # exercise branch where buffer is empty


def test_rebuild_breakdown_shown_only_in_verbose(
    project: DevServerSettings, timer_factory: Tuple[Callable[[float, Callable[[], None]], ManualTimerHandle], List[ManualTimerHandle]]
) -> None:
    factory, handles = timer_factory
    logger, messages = make_logger()
    logger.set_verbosity(Verbosity.VERBOSE)

    watcher = ProjectWatcher(
        project,
        logger=logger,
        timer_factory=factory,
        build_function=lambda settings, **_: BuildSummary(
            compiled_pages=["index.pyxl"], copied_api_modules=["api/pulse.py"], removed=[]
        ),
    )

    watcher.notify_paths([project.pages_dir / "index.pyxl"])
    handles[-1].trigger()

    assert any(message.startswith("✅ Rebuilt ") for message in messages)
    assert any("Rebuild breakdown" in message for message in messages)


def test_rebuild_logs_no_changes_when_summary_empty(
    project: DevServerSettings, timer_factory: Tuple[Callable[[float, Callable[[], None]], ManualTimerHandle], List[ManualTimerHandle]]
) -> None:
    factory, handles = timer_factory
    logger, messages = make_logger()
    # The "no material changes" notice is debug-level (verbose-only).
    logger.set_verbosity(Verbosity.VERBOSE)

    watcher = ProjectWatcher(
        project,
        logger=logger,
        timer_factory=factory,
        build_function=lambda settings, **_: BuildSummary(),
    )

    watcher.notify_paths([project.public_dir / "favicon.ico"])
    handles[-1].trigger()

    assert any("no material changes" in message for message in messages)


def test_rebuild_reports_filesystem_error(
    project: DevServerSettings, timer_factory: Tuple[Callable[[float, Callable[[], None]], ManualTimerHandle], List[ManualTimerHandle]]
) -> None:
    factory, handles = timer_factory
    logger, messages = make_logger()

    def failing_build(settings: DevServerSettings, **_: object) -> BuildSummary:
        raise OSError("disk full")

    watcher = ProjectWatcher(
        project,
        logger=logger,
        timer_factory=factory,
        build_function=failing_build,
    )

    watcher.notify_paths([project.pages_dir / "index.pyxl"])
    handles[-1].trigger()

    assert any("Filesystem error" in message for message in messages)
    stats = watcher.latest_statistics
    assert isinstance(stats, WatcherStatistics)
    assert isinstance(stats.error, OSError)

def test_project_watcher_rebuilds_only_changed_page(
    tmp_path: Path,
    timer_factory: Tuple[Callable[[float, Callable[[], None]], ManualTimerHandle], List[ManualTimerHandle]],
) -> None:
    project_root = tmp_path / "project"
    project_root.mkdir()
    settings = DevServerSettings.from_project_root(project_root)

    settings.pages_dir.mkdir(parents=True, exist_ok=True)
    settings.public_dir.mkdir(parents=True, exist_ok=True)

    def write(path: Path, content: str) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    index_source = write(
        settings.pages_dir / "index.pyxl",
        "import React from 'react';\n\n"
        "export default function Index() {\n"
        "  return <div>Index</div>;\n"
        "}\n",
    )
    write(
        settings.pages_dir / "about.pyxl",
        "import React from 'react';\n\n"
        "export default function About() {\n"
        "  return <div>About</div>;\n"
        "}\n",
    )
    write(
        settings.pages_dir / "api/pulse.py",
        "async def endpoint(request):\n    return {'message': 'hi'}\n",
    )

    initial_summary = build_once(settings, force_rebuild=True)
    assert set(initial_summary.compiled_pages) == {"about.pyxl", "index.pyxl"}

    client_index = settings.client_build_dir / "pages/index.jsx"
    client_about = settings.client_build_dir / "pages/about.jsx"
    index_mtime = client_index.stat().st_mtime_ns
    about_mtime = client_about.stat().st_mtime_ns

    factory, handles = timer_factory
    logger, _ = make_logger()
    watcher = ProjectWatcher(
        settings,
        logger=logger,
        build_function=build_once,
        timer_factory=factory,
    )

    index_source.write_text(
        "import React from 'react';\n\n"
        "export default function Index() {\n"
        "  return <div>Updated</div>;\n"
        "}\n",
        encoding="utf-8",
    )

    watcher.notify_paths([index_source])
    assert handles, "expected debounce handle to be scheduled"
    handles[-1].trigger()

    stats = watcher.latest_statistics
    assert stats is not None
    summary = stats.summary
    assert summary is not None
    assert summary.compiled_pages == ["index.pyxl"]
    assert summary.copied_api_modules == []
    assert "about.pyxl" in summary.skipped
    assert "api/pulse.py" in summary.skipped

    assert client_index.stat().st_mtime_ns > index_mtime
    assert client_about.stat().st_mtime_ns == about_mtime


def test_project_event_handler_prefers_dest_path(project: DevServerSettings) -> None:
    captured: List[Path] = []
    handler = _ProjectEventHandler(lambda path: captured.append(path))

    class DummyEvent:
        is_directory = False
        src_path = "ignored"
        dest_path = str(project.pages_dir / "moved.pyxl")

    handler.on_any_event(DummyEvent())

    assert captured == [Path(DummyEvent.dest_path)]


def test_project_event_handler_uses_src_when_dest_empty(project: DevServerSettings) -> None:
    """A non-move event (empty ``dest_path``) falls back to ``src_path``."""
    captured: List[Path] = []
    handler = _ProjectEventHandler(lambda path: captured.append(path))

    class DummyEvent:
        is_directory = False
        src_path = str(project.pages_dir / "index.pyxl")
        dest_path = ""

    handler.on_any_event(DummyEvent())

    assert captured == [Path(DummyEvent.src_path)]


def test_project_event_handler_skips_ignored_paths(project: DevServerSettings) -> None:
    """The handler drops events for which the ignore predicate returns True."""
    captured: List[Path] = []
    tailwind_output = project.public_dir / "styles" / "tailwind.css"
    handler = _ProjectEventHandler(
        lambda path: captured.append(path),
        ignore=lambda path: path == tailwind_output,
    )

    class Ignored:
        is_directory = False
        src_path = str(tailwind_output)
        dest_path = ""

    class Kept:
        is_directory = False
        src_path = str(project.pages_dir / "index.pyxl")
        dest_path = ""

    handler.on_any_event(Ignored())
    handler.on_any_event(Kept())

    assert captured == [Path(Kept.src_path)]


def test_is_generated_output_ignores_build_artefacts(tmp_path: Path) -> None:
    root = tmp_path / "project"
    (root / "pages").mkdir(parents=True)
    (root / "public").mkdir()
    settings = DevServerSettings.from_project_root(root)
    watcher = ProjectWatcher(
        settings,
        logger=make_logger()[0],
        timer_factory=lambda delay, callback: ManualTimerHandle(callback),
        build_function=lambda s, **_: BuildSummary(),
    )

    ignored = [
        root / ".pyxle-build" / "client" / "pages" / "index.jsx",
        root / "pages" / "__pycache__" / "index.cpython-312.pyc",
        root / "pages" / "index.cpython-312.pyc",
        root / "data" / "pyxle.db",
        root / "data" / "pyxle.db-wal",
        root / "data" / "pyxle.db-shm",
    ]
    for candidate in ignored:
        assert watcher._should_ignore(candidate) is True, candidate


def test_is_generated_output_keeps_real_sources(tmp_path: Path) -> None:
    root = tmp_path / "project"
    (root / "pages").mkdir(parents=True)
    (root / "public").mkdir()
    settings = DevServerSettings.from_project_root(root)
    watcher = ProjectWatcher(
        settings,
        logger=make_logger()[0],
        timer_factory=lambda delay, callback: ManualTimerHandle(callback),
        build_function=lambda s, **_: BuildSummary(),
    )

    kept = [
        root / "pages" / "index.pyxl",
        root / "pages" / "api" / "pulse.py",
        root / "pages" / "styles" / "tailwind.css",
        root / "public" / "favicon.ico",
    ]
    for candidate in kept:
        assert watcher._should_ignore(candidate) is False, candidate


def test_is_generated_output_tolerates_unresolvable_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A path that can't be resolved (vanished mid-event / symlink loop) is
    still classified from its raw form rather than raising."""

    def _raise(self: Path, *args: object, **kwargs: object) -> Path:
        raise OSError("path vanished mid-event")

    monkeypatch.setattr(Path, "resolve", _raise)

    # Falls back to the unresolved path and still matches on suffix, so the
    # generated file is ignored instead of crashing the event handler.
    assert _is_generated_output(tmp_path / "pages" / "x.pyc") is True
    assert _is_generated_output(tmp_path / "pages" / "index.pyxl") is False


def test_public_change_refreshes_index_without_rebuild(tmp_path: Path) -> None:
    """A public/ change refreshes the static index but never rebuilds/reloads.

    Public assets are served live from disk (like Next.js), so a new or removed
    file is made discoverable by refreshing the index — no build_once, no
    browser reload. This is also the structural replacement for the old Tailwind
    reload-loop guard: the Tailwind CLI writing ``public/styles/tailwind.css``
    now lands on the public handler, which cannot rebuild.
    """
    root = tmp_path / "project"
    (root / "pages").mkdir(parents=True)
    (root / "public" / "styles").mkdir(parents=True)
    settings = DevServerSettings.from_project_root(root)

    build_calls: List[object] = []
    reload_calls: List[WatcherStatistics] = []
    refresh_calls: List[int] = []
    handles: List[ManualTimerHandle] = []

    def factory(delay: float, callback: Callable[[], None]) -> ManualTimerHandle:
        handle = ManualTimerHandle(callback)
        handles.append(handle)
        return handle

    watcher = ProjectWatcher(
        settings,
        logger=make_logger()[0],
        timer_factory=factory,
        build_function=lambda s, **_: build_calls.append(s) or BuildSummary(),
        on_rebuild=reload_calls.append,
        public_index_refresh=lambda: refresh_calls.append(1),
    )

    new_asset = settings.public_dir / "styles" / "tailwind.css"
    new_asset.write_text("/* generated */", encoding="utf-8")

    class PublicCreate:
        is_directory = False
        event_type = "created"
        src_path = str(new_asset)
        dest_path = ""

    # A structural public event schedules an index refresh — not a rebuild.
    watcher._public_handler.on_any_event(PublicCreate())
    watcher.flush()  # drain the (empty) rebuild buffer
    assert build_calls == []
    assert reload_calls == []
    # Fire the debounced refresh timer.
    handles[-1].trigger()
    assert refresh_calls == [1]


def test_public_modified_event_is_ignored(tmp_path: Path) -> None:
    """A plain 'modified' public event does not even refresh the index — the
    served set is unchanged, so there is nothing to do."""
    root = tmp_path / "project"
    (root / "pages").mkdir(parents=True)
    (root / "public").mkdir()
    settings = DevServerSettings.from_project_root(root)

    refresh_calls: List[int] = []
    watcher = ProjectWatcher(
        settings,
        logger=make_logger()[0],
        timer_factory=lambda delay, callback: ManualTimerHandle(callback),
        build_function=lambda s, **_: BuildSummary(),
        public_index_refresh=lambda: refresh_calls.append(1),
    )

    class PublicModify:
        is_directory = False
        event_type = "modified"
        src_path = str(settings.public_dir / "favicon.ico")
        dest_path = ""

    watcher._public_handler.on_any_event(PublicModify())
    assert refresh_calls == []


def test_public_handler_scheduled_not_rebuild_handler(project: DevServerSettings) -> None:
    """start() wires public/ to the lightweight index handler, and pages/ to the
    rebuild handler — a public change can never reach build_once."""
    from pyxle.devserver.watcher import _ProjectEventHandler, _PublicAssetEventHandler

    observer = DummyObserver()
    watcher = ProjectWatcher(
        project,
        logger=make_logger()[0],
        observer_factory=lambda: observer,
        timer_factory=lambda delay, callback: ManualTimerHandle(callback),
        build_function=lambda settings, **_: BuildSummary(),
    )
    watcher.start()

    by_path = {Path(path): handler for handler, path, _ in observer.scheduled}
    assert isinstance(by_path[project.pages_dir], _ProjectEventHandler)
    assert isinstance(by_path[project.public_dir], _PublicAssetEventHandler)


def test_dev_watch_dir_rebuilds_and_invalidates(tmp_path: Path) -> None:
    """An extra ``dev.watch`` directory joins the rebuild watch: a change there
    rebuilds, reloads, and invalidates the imported Python module."""
    root = tmp_path / "project"
    (root / "pages").mkdir(parents=True)
    (root / "public").mkdir()
    lib_dir = root / "lib"
    lib_dir.mkdir()
    (lib_dir / "util.py").write_text("VALUE = 1\n", encoding="utf-8")

    settings = DevServerSettings.from_project_root(root, dev_watch=("lib",))

    observer = DummyObserver()
    build_calls: List[object] = []
    reload_calls: List[WatcherStatistics] = []
    watcher = ProjectWatcher(
        settings,
        logger=make_logger()[0],
        observer_factory=lambda: observer,
        timer_factory=lambda delay, callback: ManualTimerHandle(callback),
        build_function=lambda s, **_: build_calls.append(s)
        or BuildSummary(compiled_pages=["index.pyxl"]),
        on_rebuild=reload_calls.append,
    )
    watcher.start()

    # The extra directory is scheduled on the rebuild handler.
    from pyxle.devserver.watcher import _ProjectEventHandler

    by_path = {Path(path).resolve(): handler for handler, path, _ in observer.scheduled}
    assert isinstance(by_path[lib_dir.resolve()], _ProjectEventHandler)

    # A change under it rebuilds + reloads + invalidates the module.
    sys.modules["lib.util"] = ModuleType("lib.util")
    watcher.notify_paths([lib_dir / "util.py"])
    watcher.flush()
    assert len(build_calls) == 1
    assert len(reload_calls) == 1
    assert "lib.util" not in sys.modules


def test_dev_watch_missing_dir_warns_and_is_skipped(tmp_path: Path) -> None:
    """A configured ``dev.watch`` directory that doesn't exist is skipped with a
    warning rather than silently ignored (catches typo'd paths)."""
    root = tmp_path / "project"
    (root / "pages").mkdir(parents=True)
    (root / "public").mkdir()

    settings = DevServerSettings.from_project_root(root, dev_watch=("nope",))

    observer = DummyObserver()
    logger, messages = make_logger()
    watcher = ProjectWatcher(
        settings,
        logger=logger,
        observer_factory=lambda: observer,
        timer_factory=lambda delay, callback: ManualTimerHandle(callback),
        build_function=lambda s, **_: BuildSummary(),
    )
    watcher.start()

    scheduled = {Path(path).resolve() for _, path, _ in observer.scheduled}
    assert (root / "nope").resolve() not in scheduled
    assert any("dev.watch directory does not exist" in m and "nope" in m for m in messages)


def test_dev_watch_inside_public_is_skipped(tmp_path: Path) -> None:
    """A ``dev.watch`` entry inside public/ is refused (public is served live,
    index-only) — rebuild-watching it could reintroduce the reload loop."""
    root = tmp_path / "project"
    (root / "pages").mkdir(parents=True)
    (root / "public" / "styles").mkdir(parents=True)

    settings = DevServerSettings.from_project_root(root, dev_watch=("public/styles",))

    observer = DummyObserver()
    logger, messages = make_logger()
    watcher = ProjectWatcher(
        settings,
        logger=logger,
        observer_factory=lambda: observer,
        timer_factory=lambda delay, callback: ManualTimerHandle(callback),
        build_function=lambda s, **_: BuildSummary(),
    )
    watcher.start()

    from pyxle.devserver.watcher import _ProjectEventHandler

    rebuild_watched = {
        Path(path).resolve()
        for handler, path, _ in observer.scheduled
        if isinstance(handler, _ProjectEventHandler)
    }
    assert (root / "public" / "styles").resolve() not in rebuild_watched
    assert any("inside public/" in m for m in messages)


def test_dev_ignore_is_additive_to_builtins(tmp_path: Path) -> None:
    """A user ``dev.ignore`` glob suppresses matching events, but the built-in
    generated-output ignores stay in force regardless of the user list."""
    root = tmp_path / "project"
    (root / "pages").mkdir(parents=True)
    (root / "public").mkdir()
    settings = DevServerSettings.from_project_root(root, dev_ignore=("pages/generated/*",))

    watcher = ProjectWatcher(
        settings,
        logger=make_logger()[0],
        timer_factory=lambda delay, callback: ManualTimerHandle(callback),
        build_function=lambda s, **_: BuildSummary(),
    )

    # User glob suppresses a matching source path.
    assert watcher._should_ignore(root / "pages" / "generated" / "schema.py") is True
    # A non-matching source is still watched.
    assert watcher._should_ignore(root / "pages" / "index.pyxl") is False
    # Built-in ignores remain in force even though a user list is present.
    assert watcher._should_ignore(root / "pages" / "index.cpython-312.pyc") is True
    assert watcher._should_ignore(root / "pages" / "__pycache__" / "x.pyc") is True


def test_format_paths_truncates_and_handles_external(project: DevServerSettings) -> None:
    logger, _ = make_logger()
    watcher = ProjectWatcher(
        project,
        logger=logger,
        timer_factory=lambda delay, callback: ManualTimerHandle(callback),
        build_function=lambda settings, **_: BuildSummary(),
    )

    external = Path("/tmp/elsewhere.txt")
    paths = [
        project.pages_dir / "file_0.pyxl",
        external,
        project.pages_dir / "file_1.pyxl",
        project.pages_dir / "file_2.pyxl",
        project.pages_dir / "file_3.pyxl",
        project.pages_dir / "file_4.pyxl",
        project.pages_dir / "file_5.pyxl",
    ]

    output = watcher._format_paths(paths)

    remaining = len(paths) - 5
    assert f"+{remaining} more" in output
    assert external.as_posix() in output


def test_default_timer_handle_cancel_prevents_callback() -> None:
    triggered: List[int] = []

    handle = _default_timer_factory(0.05, lambda: triggered.append(1))
    handle.cancel()
    time.sleep(0.1)

    assert triggered == []


def test_python_module_invalidation(
    tmp_path: Path,
    timer_factory: Tuple[Callable[[float, Callable[[], None]], ManualTimerHandle], List[ManualTimerHandle]],
) -> None:
    project_root = tmp_path / "proj"
    settings = DevServerSettings.from_project_root(project_root)
    module_path = settings.pages_dir / "components" / "head.py"
    module_path.parent.mkdir(parents=True, exist_ok=True)
    module_path.write_text("value = 1\n", encoding="utf-8")

    module_name = "pages.components.head"
    sys.modules[module_name] = ModuleType(module_name)

    factory, handles = timer_factory
    watcher = ProjectWatcher(
        settings,
        timer_factory=factory,
        build_function=lambda settings, **_: BuildSummary(),
    )

    watcher.notify_paths([module_path])
    assert handles, "expected debounce handle"
    handles[-1].trigger()

    assert module_name not in sys.modules


def test_module_name_from_path_handles_outside_root(tmp_path: Path) -> None:
    project_root = tmp_path / "proj"
    project_root.mkdir()
    external = tmp_path / "external" / "other.py"
    external.parent.mkdir(parents=True, exist_ok=True)
    external.write_text("value = 2\n", encoding="utf-8")

    result = _module_name_from_path(external, project_root)

    assert result is None


def test_module_name_from_path_handles_package_init(tmp_path: Path) -> None:
    project_root = tmp_path / "proj"
    pages_dir = project_root / "pages" / "components"
    target = pages_dir / "__init__.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("value = 3\n", encoding="utf-8")

    result = _module_name_from_path(target, project_root)

    assert result == "pages.components"


def test_module_name_from_path_strips_pycache(tmp_path: Path) -> None:
    project_root = tmp_path / "proj"
    pages_dir = project_root / "pages" / "__pycache__"
    target = pages_dir / "mod.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("value = 4\n", encoding="utf-8")

    result = _module_name_from_path(target, project_root)

    assert result == "pages.mod"


def test_module_name_from_path_returns_none_for_root_init(tmp_path: Path) -> None:
    project_root = tmp_path / "proj"
    target = project_root / "__init__.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("value = 5\n", encoding="utf-8")

    result = _module_name_from_path(target, project_root)

    assert result is None


def test_invalidate_python_modules_skips_unknown(tmp_path: Path) -> None:
    project_root = tmp_path / "proj"
    project_root.mkdir()
    external = tmp_path / "external" / "ghost.py"
    external.parent.mkdir(parents=True, exist_ok=True)
    external.write_text("value = 6\n", encoding="utf-8")

    sentinel = "sentinel.module"
    sys.modules[sentinel] = ModuleType(sentinel)

    _invalidate_python_modules([external], project_root)

    assert sentinel in sys.modules
    del sys.modules[sentinel]


def test_invalidate_python_modules_purges_parents(tmp_path: Path) -> None:
    project_root = tmp_path / "proj"
    target = project_root / "pages" / "components" / "head.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("value = 1\n", encoding="utf-8")

    sys.modules["pages"] = ModuleType("pages")
    sys.modules["pages.components"] = ModuleType("pages.components")
    sys.modules["pages.components.head"] = ModuleType("pages.components.head")

    _invalidate_python_modules([target], project_root)

    assert "pages" not in sys.modules
    assert "pages.components" not in sys.modules
    assert "pages.components.head" not in sys.modules


def test_failed_rebuild_forces_the_next_pass(tmp_path: Path) -> None:
    """After a failed build, the next trigger runs with force_rebuild=True so a
    file reverted to its last-good content (hash-identical to the cache) still
    rebuilds instead of stranding the terminal on 'Rebuild failed'."""
    root = tmp_path / "project"
    (root / "pages").mkdir(parents=True)
    (root / "public").mkdir()
    settings = DevServerSettings.from_project_root(root)

    calls: list[bool] = []
    fail_next = {"value": True}

    def build(settings_, *, force_rebuild: bool = False):
        calls.append(force_rebuild)
        if fail_next["value"]:
            fail_next["value"] = False
            raise ValueError("mid-edit syntax error")
        return BuildSummary(compiled_pages=["index.pyxl"])

    watcher = ProjectWatcher(
        settings,
        logger=make_logger()[0],
        timer_factory=lambda delay, callback: ManualTimerHandle(callback),
        build_function=build,
    )

    watcher.notify_paths([root / "pages" / "index.pyxl"])
    watcher.flush()  # fails
    watcher.notify_paths([root / "pages" / "index.pyxl"])
    watcher.flush()  # revert -> must force
    watcher.notify_paths([root / "pages" / "index.pyxl"])
    watcher.flush()  # healthy again -> back to incremental

    assert calls == [False, True, False]
    assert watcher.latest_statistics is not None and watcher.latest_statistics.error is None
