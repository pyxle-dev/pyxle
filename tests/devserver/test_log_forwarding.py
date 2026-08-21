"""Tests for the dev-only server-log → browser-console forwarding bridge."""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import List, Tuple

import pytest

from pyxle.devserver.log_forwarding import (
    BrowserConsoleLogHandler,
    _console_method,
    _default_scheduler,
)


class FakeOverlay:
    """Records every ``notify_log`` call and executes synchronously."""

    def __init__(self) -> None:
        self.calls: List[Tuple[str, str, str]] = []

    async def notify_log(self, *, level: str, message: str, logger_name: str = "") -> None:
        self.calls.append((level, message, logger_name))


def _make_record(
    *,
    name: str = "app.users",
    level: int = logging.INFO,
    msg: str = "hello",
    pathname: str = __file__,
) -> logging.LogRecord:
    return logging.LogRecord(name, level, pathname, 1, msg, None, None)


def _sync_scheduler(coro, loop) -> None:
    """Drive a (non-suspending) coroutine to completion synchronously."""
    try:
        coro.send(None)
    except StopIteration:
        pass


@pytest.fixture
def preserve_root_logger():
    """Save/restore the root logger level and handlers around a test."""
    root = logging.getLogger()
    prev_level = root.level
    prev_handlers = list(root.handlers)
    try:
        yield root
    finally:
        root.setLevel(prev_level)
        root.handlers[:] = prev_handlers


# -- level mapping -------------------------------------------------------


@pytest.mark.parametrize(
    ("levelno", "expected"),
    [
        (logging.DEBUG, "debug"),
        (logging.INFO, "info"),
        (logging.WARNING, "warn"),
        (logging.ERROR, "error"),
        (logging.CRITICAL, "error"),
    ],
)
def test_console_method_mapping(levelno: int, expected: str) -> None:
    assert _console_method(levelno) == expected


# -- forwarding at each level -------------------------------------------


@pytest.mark.parametrize(
    ("levelno", "expected_level"),
    [
        (logging.INFO, "info"),
        (logging.WARNING, "warn"),
        (logging.ERROR, "error"),
    ],
)
def test_emit_forwards_record_with_correct_shape(levelno: int, expected_level: str) -> None:
    overlay = FakeOverlay()
    handler = BrowserConsoleLogHandler(overlay, loop=None, scheduler=_sync_scheduler)

    handler.emit(_make_record(name="app.users", level=levelno, msg="payload"))

    assert overlay.calls == [(expected_level, "payload", "app.users")]


def test_emit_forwards_debug_only_in_verbose() -> None:
    overlay = FakeOverlay()
    handler = BrowserConsoleLogHandler(
        overlay, loop=None, verbose=True, scheduler=_sync_scheduler
    )

    handler.emit(_make_record(name="app.debugger", level=logging.DEBUG, msg="trace"))

    assert overlay.calls == [("debug", "trace", "app.debugger")]


# -- filtering -----------------------------------------------------------


def test_debug_dropped_by_default() -> None:
    overlay = FakeOverlay()
    handler = BrowserConsoleLogHandler(overlay, loop=None, scheduler=_sync_scheduler)

    handler.emit(_make_record(level=logging.DEBUG))

    assert overlay.calls == []


@pytest.mark.parametrize("name", ["pyxle", "pyxle.devserver.vite", "uvicorn.access"])
def test_internal_loggers_dropped_by_default(name: str) -> None:
    overlay = FakeOverlay()
    handler = BrowserConsoleLogHandler(overlay, loop=None, scheduler=_sync_scheduler)

    handler.emit(_make_record(name=name, level=logging.INFO, msg="internal"))

    assert overlay.calls == []


def test_internal_loggers_forwarded_in_verbose() -> None:
    overlay = FakeOverlay()
    handler = BrowserConsoleLogHandler(
        overlay, loop=None, verbose=True, scheduler=_sync_scheduler
    )

    handler.emit(_make_record(name="pyxle.devserver.vite", level=logging.INFO, msg="x"))

    assert overlay.calls == [("info", "x", "pyxle.devserver.vite")]


def test_non_internal_lookalike_logger_is_forwarded() -> None:
    """A user logger merely *starting with* an internal token is not internal."""
    overlay = FakeOverlay()
    handler = BrowserConsoleLogHandler(overlay, loop=None, scheduler=_sync_scheduler)

    # "pyxletools" is not "pyxle" nor "pyxle.*", so it must be forwarded.
    handler.emit(_make_record(name="pyxletools", level=logging.INFO, msg="ok"))

    assert overlay.calls == [("info", "ok", "pyxletools")]


class TestCompiledPagesAreUserCodeNotInternals:
    """``log = logging.getLogger(__name__)`` at the top of a ``.pyxl`` must work.

    Inside a compiled page ``__name__`` is the synthetic module key the dev
    server imports it under — ``pyxle.server.pages.about``. Matching that
    against the ``pyxle`` internals prefix classifies the developer's own page
    as framework noise and drops it, so the single most common line of Python
    logging anyone writes produces nothing in the browser at all.
    """

    @pytest.mark.parametrize(
        "name",
        [
            "pyxle.server.pages.about",
            "pyxle.server.pages.blog.slug_1a2b3c",
            "pyxle.server.api.orders",
            "pyxle.server",
        ],
    )
    def test_page_and_api_module_loggers_are_forwarded(self, name: str) -> None:
        overlay = FakeOverlay()
        handler = BrowserConsoleLogHandler(overlay, loop=None, scheduler=_sync_scheduler)

        handler.emit(_make_record(name=name, level=logging.INFO, msg="from my page"))

        assert [call[1] for call in overlay.calls] == ["from my page"]

    @pytest.mark.parametrize(
        "name",
        [
            "pyxle",
            "pyxle.ssr.renderer",
            "pyxle.devserver.vite",
            "pyxle.serverless",  # a lookalike: not the pyxle.server namespace
            "uvicorn.access",
            "watchfiles.main",
        ],
    )
    def test_real_internals_are_still_dropped(self, name: str) -> None:
        """The carve-out must not reopen the flood the filter exists to stop."""
        overlay = FakeOverlay()
        handler = BrowserConsoleLogHandler(overlay, loop=None, scheduler=_sync_scheduler)

        handler.emit(_make_record(name=name, level=logging.INFO, msg="noise"))

        assert overlay.calls == []


class TestBrowserPrefixNamesTheSourceFile:
    """The console prefix should name something the developer recognises."""

    def test_a_page_is_labelled_by_its_pyxl_source(self, tmp_path) -> None:
        overlay = FakeOverlay()
        handler = BrowserConsoleLogHandler(
            overlay, loop=None, project_root=tmp_path, scheduler=_sync_scheduler
        )

        handler.emit(
            _make_record(
                name="pyxle.server.pages.about",
                msg="hi",
                pathname=str(tmp_path / "pages" / "about.pyxl"),
            )
        )

        # Not "pyxle.server.pages.about" — the file they actually wrote.
        assert overlay.calls == [("info", "hi", "pages/about.pyxl")]

    def test_a_hand_named_logger_keeps_its_own_name(self, tmp_path) -> None:
        """Only the auto-generated module key is replaced."""
        overlay = FakeOverlay()
        handler = BrowserConsoleLogHandler(
            overlay, loop=None, project_root=tmp_path, scheduler=_sync_scheduler
        )

        handler.emit(
            _make_record(
                name="shopapp",
                msg="hi",
                pathname=str(tmp_path / "pages" / "about.pyxl"),
            )
        )

        assert overlay.calls == [("info", "hi", "shopapp")]

    def test_a_generated_artifact_path_is_never_shown(self, tmp_path) -> None:
        """Only pages get the ``.pyxl`` line remap.

        An API module runs from its copy under ``.pyxle-build``, so its records
        carry a generated path. Labelling with it would point the developer at
        a file they must not edit — worse than the module key.
        """
        overlay = FakeOverlay()
        handler = BrowserConsoleLogHandler(
            overlay,
            loop=None,
            project_root=tmp_path,
            build_root=tmp_path / ".pyxle-build",
            scheduler=_sync_scheduler,
        )

        handler.emit(
            _make_record(
                name="pyxle.server.api.pulse",
                msg="hi",
                pathname=str(tmp_path / ".pyxle-build" / "server" / "api" / "pulse.py"),
            )
        )

        assert overlay.calls == [("info", "hi", "pyxle.server.api.pulse")]

    @pytest.mark.parametrize(
        ("project_root", "pathname"),
        [
            (None, "/anywhere/pages/about.pyxl"),  # root not supplied
            ("tmp_path", "/elsewhere/pages/about.pyxl"),  # outside the project
            ("tmp_path", ""),  # no source on the record
        ],
    )
    def test_falls_back_to_the_logger_name_when_unresolvable(
        self, tmp_path, project_root, pathname: str
    ) -> None:
        root = tmp_path if project_root == "tmp_path" else None
        overlay = FakeOverlay()
        handler = BrowserConsoleLogHandler(
            overlay, loop=None, project_root=root, scheduler=_sync_scheduler
        )

        handler.emit(
            _make_record(name="pyxle.server.pages.about", msg="hi", pathname=pathname)
        )

        assert overlay.calls == [("info", "hi", "pyxle.server.pages.about")]


# -- throttling ----------------------------------------------------------


def test_rate_limit_drops_excess_records(monkeypatch) -> None:
    overlay = FakeOverlay()
    handler = BrowserConsoleLogHandler(
        overlay, loop=None, max_records_per_second=2, scheduler=_sync_scheduler
    )

    # Freeze the clock so every record lands in the same one-second window.
    monkeypatch.setattr(
        "pyxle.devserver.log_forwarding.time.monotonic", lambda: 1000.0
    )

    for index in range(5):
        handler.emit(_make_record(msg=f"m{index}"))

    assert [call[1] for call in overlay.calls] == ["m0", "m1"]


def test_rate_limit_window_resets(monkeypatch) -> None:
    overlay = FakeOverlay()
    handler = BrowserConsoleLogHandler(
        overlay, loop=None, max_records_per_second=1, scheduler=_sync_scheduler
    )

    clock = {"now": 1000.0}
    monkeypatch.setattr(
        "pyxle.devserver.log_forwarding.time.monotonic", lambda: clock["now"]
    )

    handler.emit(_make_record(msg="first"))
    handler.emit(_make_record(msg="dropped"))  # same window, over the cap
    clock["now"] += 1.5  # advance past the window
    handler.emit(_make_record(msg="second"))

    assert [call[1] for call in overlay.calls] == ["first", "second"]


# -- re-entrancy ---------------------------------------------------------


def test_no_reentrancy_when_forwarding_logs() -> None:
    overlay = FakeOverlay()
    handler = BrowserConsoleLogHandler(overlay, loop=None)
    reentry_logger = logging.getLogger("app.reentry")

    def logging_scheduler(coro, loop) -> None:
        # Drive the coroutine, and while it runs emit another record straight
        # back into this handler on the same thread. The re-entrancy guard must
        # drop it instead of recursing forever.
        handler.emit(_make_record(name="app.reentry", msg="recursive"))
        try:
            coro.send(None)
        except StopIteration:
            pass

    handler._scheduler = logging_scheduler  # type: ignore[attr-defined]
    reentry_logger.addHandler(handler)
    try:
        handler.emit(_make_record(name="app.reentry", msg="original"))
    finally:
        reentry_logger.removeHandler(handler)

    # Exactly one forward — the re-entrant emit was dropped by the guard.
    assert overlay.calls == [("info", "original", "app.reentry")]


# -- robustness ----------------------------------------------------------


def test_scheduler_runtime_error_is_swallowed_and_coro_closed() -> None:
    overlay = FakeOverlay()

    def raising_scheduler(coro, loop) -> None:
        coro.close()  # simulate the loop-closed cleanup path deterministically
        raise RuntimeError("event loop is closed")

    handler = BrowserConsoleLogHandler(
        overlay, loop=None, scheduler=raising_scheduler
    )

    # Must not raise even though scheduling failed.
    handler.emit(_make_record(msg="lost"))
    assert overlay.calls == []


def test_emit_calls_handle_error_on_failure(monkeypatch) -> None:
    overlay = FakeOverlay()
    handler = BrowserConsoleLogHandler(overlay, loop=None, scheduler=_sync_scheduler)

    def boom(_record):
        raise ValueError("format failed")

    handled: List[logging.LogRecord] = []
    monkeypatch.setattr(handler, "format", boom)
    monkeypatch.setattr(handler, "handleError", handled.append)

    record = _make_record(msg="x")
    handler.emit(record)

    assert handled == [record]
    # Re-entrancy flag is cleared even after an error, so the next emit works.
    monkeypatch.undo()
    handler.emit(_make_record(msg="after"))
    assert overlay.calls == [("info", "after", "app.users")]


def test_dispatch_default_runtime_error_closes_coro() -> None:
    overlay = FakeOverlay()
    closed_loop = asyncio.new_event_loop()
    closed_loop.close()
    handler = BrowserConsoleLogHandler(overlay, closed_loop)

    # The loop is closed, so the default scheduler's run_coroutine_threadsafe
    # raises RuntimeError; the handler must catch it and close the coroutine
    # (no "coroutine was never awaited" warning) instead of crashing.
    handler._dispatch("info", "x", "app")  # uses the default scheduler
    assert overlay.calls == []


# -- attach / detach -----------------------------------------------------


def test_attach_lowers_root_level_and_restores(preserve_root_logger) -> None:
    root = preserve_root_logger
    root.setLevel(logging.WARNING)
    overlay = FakeOverlay()
    handler = BrowserConsoleLogHandler(overlay, loop=None, scheduler=_sync_scheduler)

    handler.attach()
    assert handler in root.handlers
    assert root.level == logging.INFO

    handler.detach()
    assert handler not in root.handlers
    assert root.level == logging.WARNING


def test_attach_verbose_lowers_to_debug(preserve_root_logger) -> None:
    root = preserve_root_logger
    root.setLevel(logging.WARNING)
    overlay = FakeOverlay()
    handler = BrowserConsoleLogHandler(
        overlay, loop=None, verbose=True, scheduler=_sync_scheduler
    )

    handler.attach()
    assert root.level == logging.DEBUG
    handler.detach()
    assert root.level == logging.WARNING


def test_attach_preserves_already_verbose_root_level(preserve_root_logger) -> None:
    root = preserve_root_logger
    root.setLevel(logging.DEBUG)  # already more verbose than the target
    overlay = FakeOverlay()
    handler = BrowserConsoleLogHandler(overlay, loop=None, scheduler=_sync_scheduler)

    handler.attach()
    # Root was already at DEBUG (<= INFO target), so it is left untouched …
    assert root.level == logging.DEBUG
    handler.detach()
    # … and detach does not raise the level back up.
    assert root.level == logging.DEBUG


def test_attach_installs_stderr_fallback_when_root_is_bare(
    preserve_root_logger, capsys
) -> None:
    """Attaching must not silence what Python would otherwise have printed.

    :data:`logging.lastResort` sends WARNING+ to stderr only while a record
    finds no handler at all. Adding this bridge ends that fallback for the whole
    process — which is how a plugin whose ``on_startup`` raised could abort a
    boot while printing nothing to the terminal, its traceback going instead to
    a browser console that was not even open.
    """
    root = preserve_root_logger
    root.handlers[:] = []  # a bare root logger, as under `pyxle dev`
    overlay = FakeOverlay()
    handler = BrowserConsoleLogHandler(overlay, loop=None, scheduler=_sync_scheduler)

    handler.attach()
    try:
        assert len(root.handlers) == 2  # the stderr fallback plus the bridge
        logging.getLogger("uvicorn.error").error("Application startup failed")
    finally:
        handler.detach()

    assert "Application startup failed" in capsys.readouterr().err
    # Framework-internal namespaces stay out of the browser console …
    assert overlay.calls == []
    # … and detaching leaves the root logger exactly as it was found.
    assert root.handlers == []


def test_attach_leaves_an_already_configured_root_alone(preserve_root_logger) -> None:
    """An app that configured its own logging keeps exactly the handlers it set."""
    root = preserve_root_logger
    existing = logging.StreamHandler()
    root.handlers[:] = [existing]
    handler = BrowserConsoleLogHandler(FakeOverlay(), loop=None, scheduler=_sync_scheduler)

    handler.attach()
    try:
        assert root.handlers == [existing, handler]
    finally:
        handler.detach()
    assert root.handlers == [existing]


def test_stderr_fallback_ignores_internal_records_below_warning(
    preserve_root_logger, capsys
) -> None:
    """Framework internals below WARNING are not new terminal output.

    ``uvicorn`` prints its own startup lines; echoing its INFO records here
    would double them.
    """
    root = preserve_root_logger
    root.handlers[:] = []
    handler = BrowserConsoleLogHandler(FakeOverlay(), loop=None, scheduler=_sync_scheduler)

    handler.attach()
    try:
        logging.getLogger("uvicorn.error").info("Application startup complete")
    finally:
        handler.detach()

    assert capsys.readouterr().err == ""


class TestTheTerminalSeesWhatTheBrowserSees:
    """``attach`` lowers the root logger to INFO so those records reach the
    browser. A stderr fallback pinned at WARNING sends them *only* there — so a
    plain ``log.info(...)`` vanishes from the terminal the developer is already
    watching, which combined with the name filter made a page's logging silent
    in both places at once.
    """

    @pytest.mark.parametrize(
        "name", ["shopapp", "pyxle.server.pages.about", "pyxle.server.api.orders"]
    )
    def test_user_info_records_reach_the_terminal(
        self, preserve_root_logger, capsys, name: str
    ) -> None:
        root = preserve_root_logger
        root.handlers[:] = []
        overlay = FakeOverlay()
        handler = BrowserConsoleLogHandler(
            overlay, loop=None, scheduler=_sync_scheduler
        )

        handler.attach()
        try:
            logging.getLogger(name).info("order placed")
        finally:
            handler.detach()

        # Both destinations, not one or the other.
        assert "order placed" in capsys.readouterr().err
        assert [call[1] for call in overlay.calls] == ["order placed"]

    def test_debug_stays_off_the_terminal_even_in_verbose(
        self, preserve_root_logger, capsys
    ) -> None:
        """Verbose opens the browser firehose; it must not bury the terminal."""
        root = preserve_root_logger
        root.handlers[:] = []
        overlay = FakeOverlay()
        handler = BrowserConsoleLogHandler(
            overlay, loop=None, verbose=True, scheduler=_sync_scheduler
        )

        handler.attach()
        try:
            logging.getLogger("watchfiles.main").debug("1 change detected")
        finally:
            handler.detach()

        assert capsys.readouterr().err == ""
        assert [call[1] for call in overlay.calls] == ["1 change detected"]


def test_attached_handler_forwards_user_logs(preserve_root_logger) -> None:
    overlay = FakeOverlay()
    handler = BrowserConsoleLogHandler(overlay, loop=None, scheduler=_sync_scheduler)
    handler.attach()
    try:
        logging.getLogger("app.orders").info("order placed")
    finally:
        handler.detach()

    assert overlay.calls == [("info", "order placed", "app.orders")]


# -- default scheduler end-to-end ---------------------------------------


def test_default_scheduler_forwards_via_running_loop() -> None:
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    overlay = FakeOverlay()
    handler = BrowserConsoleLogHandler(overlay, loop, verbose=True)
    try:
        handler.emit(_make_record(name="app.metrics", level=logging.INFO, msg="tick"))
        deadline = time.monotonic() + 2.0
        while not overlay.calls and time.monotonic() < deadline:
            time.sleep(0.01)
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=2.0)
        loop.close()

    assert overlay.calls == [("info", "tick", "app.metrics")]


def test_default_scheduler_helper_raises_without_running_loop_is_caller_concern() -> None:
    # Direct smoke test of the default scheduler wrapper: with a fresh, closed
    # loop, run_coroutine_threadsafe raises RuntimeError which the handler
    # catches. Here we assert the wrapper itself surfaces that error.
    loop = asyncio.new_event_loop()
    loop.close()
    overlay = FakeOverlay()
    coro = overlay.notify_log(level="info", message="x")
    with pytest.raises(RuntimeError):
        _default_scheduler(coro, loop)
    coro.close()
