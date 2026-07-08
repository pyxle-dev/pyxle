"""Management helpers for the Tailwind CSS watcher subprocess."""

from __future__ import annotations

import asyncio
import json
import shutil
from asyncio.subprocess import PIPE
from contextlib import suppress
from pathlib import Path
from typing import Awaitable, Callable, Sequence

from pyxle.cli.logger import ConsoleLogger

from .settings import DevServerSettings

_TAILWIND_CONFIG_FILENAMES: Sequence[str] = (
    "tailwind.config.cjs",
    "tailwind.config.js",
    "tailwind.config.ts",
    "tailwind.config.mjs",
)

_POSTCSS_CONFIG_FILENAMES: Sequence[str] = (
    "postcss.config.cjs",
    "postcss.config.js",
    "postcss.config.mjs",
    "postcss.config.ts",
)

_DEFAULT_INPUT_CSS = Path("pages") / "styles" / "tailwind.css"
_DEFAULT_OUTPUT_CSS = Path("public") / "styles" / "tailwind.css"


def detect_tailwind_config(project_root: Path) -> Path | None:
    """Return the path to the Tailwind config file, or ``None`` if not found."""

    for filename in _TAILWIND_CONFIG_FILENAMES:
        candidate = project_root / filename
        if candidate.is_file():
            return candidate
    return None


def detect_postcss_config(project_root: Path) -> Path | None:
    """Return the path to the project's PostCSS config, or ``None`` if absent.

    When a PostCSS config is present, Pyxle defers CSS processing to Vite's
    PostCSS pipeline rather than running the standalone Tailwind CLI watcher.
    Stylesheets imported from a JSX module are then compiled, content-hashed,
    and listed in the Vite manifest, so the SSR template can emit a
    cache-busting ``<link rel="stylesheet" href="/client/dist/assets/{name}-{hash}.css" />``
    on every request without any manual ``?v=N`` workaround.
    """

    for filename in _POSTCSS_CONFIG_FILENAMES:
        candidate = project_root / filename
        if candidate.is_file():
            return candidate
    return None


def _parse_tailwind_paths_from_package_json(
    project_root: Path,
) -> tuple[Path | None, Path | None]:
    """Try to extract input/output CSS paths from the ``dev:css`` npm script."""

    package_json = project_root / "package.json"
    if not package_json.is_file():
        return None, None

    try:
        data = json.loads(package_json.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None, None

    scripts: dict[str, str] = data.get("scripts", {})
    dev_css = scripts.get("dev:css", "") or scripts.get("build:css", "")
    if not dev_css or "tailwindcss" not in dev_css:
        return None, None

    input_path: Path | None = None
    output_path: Path | None = None

    parts = dev_css.split()
    for i, part in enumerate(parts):
        if part == "-i" and i + 1 < len(parts):
            input_path = Path(parts[i + 1].lstrip("./"))
        elif part == "-o" and i + 1 < len(parts):
            output_path = Path(parts[i + 1].lstrip("./"))

    return input_path, output_path


def resolve_tailwind_paths(
    project_root: Path,
) -> tuple[Path, Path] | None:
    """Determine the Tailwind CSS input and output paths for a project.

    Returns a ``(input, output)`` tuple of project-relative paths, or ``None``
    if the project does not appear to use Tailwind CSS.
    """

    if detect_tailwind_config(project_root) is None:
        return None

    pkg_input, pkg_output = _parse_tailwind_paths_from_package_json(project_root)
    input_css = pkg_input or _DEFAULT_INPUT_CSS
    output_css = pkg_output or _DEFAULT_OUTPUT_CSS

    absolute_input = project_root / input_css
    if not absolute_input.is_file():
        return None

    return input_css, output_css


class TailwindProcess:
    """Launch and supervise the Tailwind CSS watcher subprocess."""

    def __init__(
        self,
        settings: DevServerSettings,
        *,
        logger: ConsoleLogger | None = None,
        input_css: Path | None = None,
        output_css: Path | None = None,
        process_factory: Callable[..., Awaitable[asyncio.subprocess.Process]] | None = None,
        stop_timeout: float = 5.0,
    ) -> None:
        self._settings = settings
        self._logger = logger or ConsoleLogger()
        self._process_factory = process_factory or asyncio.create_subprocess_exec
        self._process: asyncio.subprocess.Process | None = None
        self._monitor_task: asyncio.Task[None] | None = None
        self._stop_timeout = stop_timeout
        self._stopping = False

        paths = resolve_tailwind_paths(settings.project_root)
        if input_css is not None and output_css is not None:
            self._input_css = input_css
            self._output_css = output_css
        elif paths is not None:
            self._input_css = paths[0]
            self._output_css = paths[1]
        else:
            self._input_css = _DEFAULT_INPUT_CSS
            self._output_css = _DEFAULT_OUTPUT_CSS

    @property
    def running(self) -> bool:
        process = self._process
        return process is not None and process.returncode is None

    @property
    def input_css(self) -> Path:
        return self._input_css

    @property
    def output_css(self) -> Path:
        return self._output_css

    async def start(self) -> None:
        """Start the Tailwind CSS watcher process."""

        if self.running:
            return

        self._stopping = False
        command = self._build_command()
        self._logger.info("Starting Tailwind CSS watcher: " + " ".join(command))

        try:
            process = await self._process_factory(
                *command,
                stdout=PIPE,
                stderr=PIPE,
                cwd=str(self._settings.project_root),
            )
        except FileNotFoundError as exc:
            self._logger.warning(
                f"Tailwind CSS CLI not found ({exc}); skipping automatic CSS compilation. "
                "Run 'npm install' to install tailwindcss."
            )
            return

        self._process = process
        self._monitor_task = asyncio.create_task(self._monitor_process(process))
        self._logger.success(
            f"Tailwind CSS watcher started ({self._input_css} -> {self._output_css})"
        )

    async def stop(self) -> None:
        """Stop the Tailwind CSS watcher process."""

        self._stopping = True
        process = self._process
        if process is None:
            return

        if process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=self._stop_timeout)
            except asyncio.TimeoutError:
                self._logger.warning("Tailwind process did not exit after SIGTERM; killing")
                process.kill()
                await process.wait()

        if self._monitor_task is not None:
            with suppress(asyncio.CancelledError):
                await self._monitor_task

        self._logger.info("Tailwind CSS watcher stopped")
        self._process = None
        self._monitor_task = None

    def _build_command(self) -> list[str]:
        tailwind_bin = self._find_tailwind_binary()
        return [
            *tailwind_bin,
            "-i",
            str(self._input_css),
            "-o",
            str(self._output_css),
            "--watch",
        ]

    def _find_tailwind_binary(self) -> list[str]:
        project_root = self._settings.project_root
        node_exec = shutil.which("node")

        # Try local node_modules bin first
        candidates = [
            project_root / "node_modules" / ".bin" / "tailwindcss",
            project_root / "node_modules" / ".bin" / "tailwindcss.cmd",
        ]
        for candidate in candidates:
            if candidate.exists():
                return [str(candidate)]

        # Try node + tailwindcss CLI entry point
        tailwind_cli = project_root / "node_modules" / "tailwindcss" / "lib" / "cli.js"
        if node_exec is not None and tailwind_cli.exists():
            return [node_exec, str(tailwind_cli)]

        # Fall back to npx
        npx_exec = shutil.which("npx")
        if npx_exec is not None:
            return [npx_exec, "--yes", "tailwindcss"]

        # Last resort: bare command name (will raise FileNotFoundError at launch)
        return ["tailwindcss"]

    async def _monitor_process(self, process: asyncio.subprocess.Process) -> None:
        stdout = process.stdout
        stderr = process.stderr

        tasks: list[asyncio.Task[None]] = []
        if stdout is not None:
            tasks.append(asyncio.create_task(self._pipe_stream(stdout, is_error=False)))
        if stderr is not None:
            tasks.append(asyncio.create_task(self._pipe_stream(stderr, is_error=True)))

        try:
            if tasks:
                await asyncio.gather(*tasks)
        finally:
            for task in tasks:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task

        returncode = await process.wait()
        if returncode not in (0, None) and not self._stopping:
            self._logger.warning(
                f"Tailwind CSS watcher exited with code {returncode}"
            )

    async def _pipe_stream(self, stream: asyncio.StreamReader, *, is_error: bool) -> None:
        while True:
            line = await stream.readline()
            if not line:
                break
            message = line.decode(errors="replace").rstrip()
            if not message:
                continue
            if is_error:
                self._logger.warning(f"[tailwind] {message}")
            else:
                self._logger.info(f"[tailwind] {message}")


__all__ = [
    "TailwindProcess",
    "detect_postcss_config",
    "detect_tailwind_config",
    "resolve_tailwind_paths",
]
