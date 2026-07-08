"""Tests for the Tailwind CSS watcher subprocess manager."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any
import pytest

from pyxle.cli.logger import ConsoleLogger
from pyxle.devserver.settings import DevServerSettings
from pyxle.devserver.tailwind import (
    TailwindProcess,
    _parse_tailwind_paths_from_package_json,
    detect_postcss_config,
    detect_tailwind_config,
    resolve_tailwind_paths,
)

pytestmark = pytest.mark.anyio("asyncio")


@pytest.fixture
def anyio_backend() -> str:  # pragma: no cover - fixture wiring
    return "asyncio"


class LogCapture:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def __call__(self, message: str, fg: str | None = None, bold: bool = False) -> None:
        self.messages.append(message)


# --- detect_tailwind_config ---


def test_detect_tailwind_config_finds_cjs(tmp_path: Path) -> None:
    (tmp_path / "tailwind.config.cjs").write_text("module.exports = {}")
    assert detect_tailwind_config(tmp_path) == tmp_path / "tailwind.config.cjs"


def test_detect_tailwind_config_finds_js(tmp_path: Path) -> None:
    (tmp_path / "tailwind.config.js").write_text("export default {}")
    assert detect_tailwind_config(tmp_path) == tmp_path / "tailwind.config.js"


def test_detect_tailwind_config_finds_ts(tmp_path: Path) -> None:
    (tmp_path / "tailwind.config.ts").write_text("export default {}")
    assert detect_tailwind_config(tmp_path) == tmp_path / "tailwind.config.ts"


def test_detect_tailwind_config_finds_mjs(tmp_path: Path) -> None:
    (tmp_path / "tailwind.config.mjs").write_text("export default {}")
    assert detect_tailwind_config(tmp_path) == tmp_path / "tailwind.config.mjs"


def test_detect_tailwind_config_returns_none_when_missing(tmp_path: Path) -> None:
    assert detect_tailwind_config(tmp_path) is None


def test_detect_tailwind_config_prefers_cjs_first(tmp_path: Path) -> None:
    (tmp_path / "tailwind.config.cjs").write_text("cjs")
    (tmp_path / "tailwind.config.js").write_text("js")
    assert detect_tailwind_config(tmp_path) == tmp_path / "tailwind.config.cjs"


# --- detect_postcss_config ---


def test_detect_postcss_config_finds_cjs(tmp_path: Path) -> None:
    (tmp_path / "postcss.config.cjs").write_text(
        "module.exports = { plugins: { tailwindcss: {} } }"
    )
    assert detect_postcss_config(tmp_path) == tmp_path / "postcss.config.cjs"


def test_detect_postcss_config_finds_js(tmp_path: Path) -> None:
    (tmp_path / "postcss.config.js").write_text("export default {}")
    assert detect_postcss_config(tmp_path) == tmp_path / "postcss.config.js"


def test_detect_postcss_config_finds_mjs(tmp_path: Path) -> None:
    (tmp_path / "postcss.config.mjs").write_text("export default {}")
    assert detect_postcss_config(tmp_path) == tmp_path / "postcss.config.mjs"


def test_detect_postcss_config_finds_ts(tmp_path: Path) -> None:
    (tmp_path / "postcss.config.ts").write_text("export default {}")
    assert detect_postcss_config(tmp_path) == tmp_path / "postcss.config.ts"


def test_detect_postcss_config_returns_none_when_missing(tmp_path: Path) -> None:
    assert detect_postcss_config(tmp_path) is None


def test_detect_postcss_config_prefers_cjs_first(tmp_path: Path) -> None:
    (tmp_path / "postcss.config.cjs").write_text("cjs")
    (tmp_path / "postcss.config.js").write_text("js")
    (tmp_path / "postcss.config.mjs").write_text("mjs")
    (tmp_path / "postcss.config.ts").write_text("ts")
    assert detect_postcss_config(tmp_path) == tmp_path / "postcss.config.cjs"


def test_detect_postcss_config_ignores_directory_with_same_name(tmp_path: Path) -> None:
    (tmp_path / "postcss.config.cjs").mkdir()
    assert detect_postcss_config(tmp_path) is None


# --- _parse_tailwind_paths_from_package_json ---


def test_parse_paths_from_dev_css_script(tmp_path: Path) -> None:
    package = {
        "scripts": {
            "dev:css": "tailwindcss -i ./pages/styles/tw.css -o ./public/styles/tw.css --watch"
        }
    }
    (tmp_path / "package.json").write_text(json.dumps(package))
    input_p, output_p = _parse_tailwind_paths_from_package_json(tmp_path)
    assert input_p == Path("pages/styles/tw.css")
    assert output_p == Path("public/styles/tw.css")


def test_parse_paths_from_build_css_script(tmp_path: Path) -> None:
    package = {
        "scripts": {
            "build:css": "tailwindcss -i ./src/input.css -o ./dist/output.css --minify"
        }
    }
    (tmp_path / "package.json").write_text(json.dumps(package))
    input_p, output_p = _parse_tailwind_paths_from_package_json(tmp_path)
    assert input_p == Path("src/input.css")
    assert output_p == Path("dist/output.css")


def test_parse_paths_returns_none_no_package_json(tmp_path: Path) -> None:
    assert _parse_tailwind_paths_from_package_json(tmp_path) == (None, None)


def test_parse_paths_returns_none_no_tailwind_script(tmp_path: Path) -> None:
    package = {"scripts": {"dev": "vite"}}
    (tmp_path / "package.json").write_text(json.dumps(package))
    assert _parse_tailwind_paths_from_package_json(tmp_path) == (None, None)


def test_parse_paths_returns_none_invalid_json(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text("not json")
    assert _parse_tailwind_paths_from_package_json(tmp_path) == (None, None)


def test_parse_paths_returns_none_no_scripts_key(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text(json.dumps({"name": "test"}))
    assert _parse_tailwind_paths_from_package_json(tmp_path) == (None, None)


# --- resolve_tailwind_paths ---


def test_resolve_tailwind_paths_with_config_and_input(tmp_path: Path) -> None:
    (tmp_path / "tailwind.config.cjs").write_text("module.exports = {}")
    styles_dir = tmp_path / "pages" / "styles"
    styles_dir.mkdir(parents=True)
    (styles_dir / "tailwind.css").write_text("@tailwind base;")

    result = resolve_tailwind_paths(tmp_path)
    assert result is not None
    input_css, output_css = result
    assert input_css == Path("pages/styles/tailwind.css")
    assert output_css == Path("public/styles/tailwind.css")


def test_resolve_tailwind_paths_uses_package_json_paths(tmp_path: Path) -> None:
    (tmp_path / "tailwind.config.js").write_text("export default {}")
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    (src_dir / "app.css").write_text("@tailwind base;")
    package = {
        "scripts": {"dev:css": "tailwindcss -i ./src/app.css -o ./dist/app.css --watch"}
    }
    (tmp_path / "package.json").write_text(json.dumps(package))

    result = resolve_tailwind_paths(tmp_path)
    assert result is not None
    input_css, output_css = result
    assert input_css == Path("src/app.css")
    assert output_css == Path("dist/app.css")


def test_resolve_tailwind_paths_none_without_config(tmp_path: Path) -> None:
    styles_dir = tmp_path / "pages" / "styles"
    styles_dir.mkdir(parents=True)
    (styles_dir / "tailwind.css").write_text("@tailwind base;")
    assert resolve_tailwind_paths(tmp_path) is None


def test_resolve_tailwind_paths_none_without_input_file(tmp_path: Path) -> None:
    (tmp_path / "tailwind.config.cjs").write_text("module.exports = {}")
    assert resolve_tailwind_paths(tmp_path) is None


# --- TailwindProcess ---


def _make_settings(tmp_path: Path) -> DevServerSettings:
    return DevServerSettings.from_project_root(tmp_path)


def _make_stub_process(returncode: int = 0):
    """Create a stub subprocess that simulates a Tailwind process."""

    class StubStream:
        def __init__(self) -> None:
            self._done = False

        async def readline(self) -> bytes:
            if not self._done:
                self._done = True
                return b"Rebuilding...\n"
            return b""

    class StubProcess:
        def __init__(self) -> None:
            self.returncode: int | None = None
            self.stdout = StubStream()
            self.stderr = StubStream()
            self._final_code = returncode

        def terminate(self) -> None:
            self.returncode = self._final_code

        def kill(self) -> None:
            self.returncode = -9

        async def wait(self) -> int:
            if self.returncode is None:
                self.returncode = self._final_code
            return self.returncode

    return StubProcess()


async def test_tailwind_process_start_and_stop(tmp_path: Path) -> None:
    (tmp_path / "tailwind.config.cjs").write_text("module.exports = {}")
    styles_dir = tmp_path / "pages" / "styles"
    styles_dir.mkdir(parents=True)
    (styles_dir / "tailwind.css").write_text("@tailwind base;")

    settings = _make_settings(tmp_path)
    capture = LogCapture()
    logger = ConsoleLogger(secho=capture)

    stub_proc = _make_stub_process()

    async def factory(*args: Any, **kwargs: Any) -> Any:
        return stub_proc

    tw = TailwindProcess(
        settings,
        logger=logger,
        process_factory=factory,
    )

    assert not tw.running
    await tw.start()
    assert tw.running

    # Let the monitor task process the output
    await asyncio.sleep(0.05)

    await tw.stop()
    assert not tw.running
    assert any("Tailwind CSS watcher started" in m for m in capture.messages)
    assert any("Tailwind CSS watcher stopped" in m for m in capture.messages)


async def test_tailwind_process_start_idempotent(tmp_path: Path) -> None:
    settings = _make_settings(tmp_path)
    capture = LogCapture()
    logger = ConsoleLogger(secho=capture)
    call_count = 0

    stub_proc = _make_stub_process()

    async def factory(*args: Any, **kwargs: Any) -> Any:
        nonlocal call_count
        call_count += 1
        return stub_proc

    tw = TailwindProcess(
        settings,
        logger=logger,
        input_css=Path("input.css"),
        output_css=Path("output.css"),
        process_factory=factory,
    )

    await tw.start()
    await tw.start()  # second call should be a no-op
    assert call_count == 1
    await tw.stop()


async def test_tailwind_process_handles_file_not_found(tmp_path: Path) -> None:
    settings = _make_settings(tmp_path)
    capture = LogCapture()
    logger = ConsoleLogger(secho=capture)

    async def factory(*args: Any, **kwargs: Any) -> Any:
        raise FileNotFoundError("tailwindcss not found")

    tw = TailwindProcess(
        settings,
        logger=logger,
        input_css=Path("input.css"),
        output_css=Path("output.css"),
        process_factory=factory,
    )

    await tw.start()
    assert not tw.running
    assert any("not found" in m for m in capture.messages)


async def test_tailwind_process_stop_when_not_started(tmp_path: Path) -> None:
    settings = _make_settings(tmp_path)
    tw = TailwindProcess(
        settings,
        input_css=Path("input.css"),
        output_css=Path("output.css"),
    )
    # Should not raise
    await tw.stop()


async def test_tailwind_process_uses_explicit_paths(tmp_path: Path) -> None:
    settings = _make_settings(tmp_path)
    tw = TailwindProcess(
        settings,
        input_css=Path("my/input.css"),
        output_css=Path("my/output.css"),
    )
    assert tw.input_css == Path("my/input.css")
    assert tw.output_css == Path("my/output.css")


async def test_tailwind_process_detects_paths_from_project(tmp_path: Path) -> None:
    (tmp_path / "tailwind.config.cjs").write_text("module.exports = {}")
    styles_dir = tmp_path / "pages" / "styles"
    styles_dir.mkdir(parents=True)
    (styles_dir / "tailwind.css").write_text("@tailwind base;")

    settings = _make_settings(tmp_path)
    tw = TailwindProcess(settings)
    assert tw.input_css == Path("pages/styles/tailwind.css")
    assert tw.output_css == Path("public/styles/tailwind.css")


def test_tailwind_process_find_binary_local_bin(tmp_path: Path) -> None:
    bin_dir = tmp_path / "node_modules" / ".bin"
    bin_dir.mkdir(parents=True)
    tw_bin = bin_dir / "tailwindcss"
    tw_bin.write_text("#!/bin/sh")
    tw_bin.chmod(0o755)

    settings = _make_settings(tmp_path)
    tw = TailwindProcess(
        settings,
        input_css=Path("input.css"),
        output_css=Path("output.css"),
    )
    command = tw._build_command()
    assert command[0] == str(tw_bin)
    assert "-i" in command
    assert "--watch" in command


def test_tailwind_process_find_binary_fallback(tmp_path: Path) -> None:
    settings = _make_settings(tmp_path)
    tw = TailwindProcess(
        settings,
        input_css=Path("input.css"),
        output_css=Path("output.css"),
    )
    command = tw._build_command()
    # Should either use npx or bare tailwindcss
    assert "tailwindcss" in command[0] or "npx" in command[0]


def test_tailwind_process_defaults_when_no_paths_detected(tmp_path: Path) -> None:
    """When project has no tailwind config or package.json, use default paths."""

    settings = _make_settings(tmp_path)
    tw = TailwindProcess(settings)
    assert tw.input_css == Path("pages/styles/tailwind.css")
    assert tw.output_css == Path("public/styles/tailwind.css")


def test_tailwind_process_find_binary_node_cli_js(tmp_path: Path, monkeypatch) -> None:
    """When node_modules/.bin doesn't have tailwindcss, fall back to node + cli.js."""

    cli_dir = tmp_path / "node_modules" / "tailwindcss" / "lib"
    cli_dir.mkdir(parents=True)
    (cli_dir / "cli.js").write_text("#!/usr/bin/env node")

    monkeypatch.setattr("shutil.which", lambda cmd: "/usr/local/bin/node" if cmd == "node" else None)

    settings = _make_settings(tmp_path)
    tw = TailwindProcess(
        settings,
        input_css=Path("input.css"),
        output_css=Path("output.css"),
    )
    command = tw._build_command()
    assert command[0] == "/usr/local/bin/node"
    assert str(cli_dir / "cli.js") in command[1]


def test_tailwind_process_find_binary_bare_fallback(tmp_path: Path, monkeypatch) -> None:
    """When nothing else is available, fall back to bare tailwindcss command."""

    monkeypatch.setattr("shutil.which", lambda cmd: None)

    settings = _make_settings(tmp_path)
    tw = TailwindProcess(
        settings,
        input_css=Path("input.css"),
        output_css=Path("output.css"),
    )
    command = tw._build_command()
    assert command[0] == "tailwindcss"


async def test_tailwind_process_stop_kills_on_timeout(tmp_path: Path) -> None:
    """When process doesn't exit after SIGTERM, force kill."""

    settings = _make_settings(tmp_path)
    capture = LogCapture()
    logger = ConsoleLogger(secho=capture)

    class HangingProcess:
        def __init__(self) -> None:
            self.returncode: int | None = None
            self.stdout = None
            self.stderr = None
            self._terminate_called = False
            self._kill_called = False

        def terminate(self) -> None:
            self._terminate_called = True
            # Simulate process that doesn't terminate

        def kill(self) -> None:
            self._kill_called = True
            self.returncode = -9

        async def wait(self) -> int:
            if self._kill_called:
                return -9
            # Simulate hanging — wait forever
            await asyncio.sleep(100)
            return 0

    stub_proc = HangingProcess()

    async def factory(*args: Any, **kwargs: Any) -> Any:
        return stub_proc

    tw = TailwindProcess(
        settings,
        logger=logger,
        input_css=Path("input.css"),
        output_css=Path("output.css"),
        process_factory=factory,
        stop_timeout=0.1,
    )

    await tw.start()
    await tw.stop()

    assert stub_proc._kill_called is True
    assert any("killing" in m.lower() for m in capture.messages)


async def test_tailwind_process_logs_exit_code_on_crash(tmp_path: Path) -> None:
    settings = _make_settings(tmp_path)
    capture = LogCapture()
    logger = ConsoleLogger(secho=capture)

    stub_proc = _make_stub_process(returncode=1)

    async def factory(*args: Any, **kwargs: Any) -> Any:
        return stub_proc

    tw = TailwindProcess(
        settings,
        logger=logger,
        input_css=Path("input.css"),
        output_css=Path("output.css"),
        process_factory=factory,
    )

    await tw.start()
    # Wait for monitor to pick up exit
    await asyncio.sleep(0.1)

    assert any("exited with code 1" in m for m in capture.messages)
    await tw.stop()
