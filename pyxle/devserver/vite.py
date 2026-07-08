"""Management helpers for the Vite development server subprocess."""

from __future__ import annotations

import asyncio
import os
import shutil
import time
from asyncio.subprocess import PIPE
from contextlib import suppress
from typing import Awaitable, Callable, Iterable

from pyxle.cli.logger import ConsoleLogger

from .client_files import VITE_CONFIG_FILENAME
from .settings import DevServerSettings

_ViteProbe = Callable[[str, int], Awaitable[bool]]

#: Maximum consecutive relaunch attempts after an unexpected Vite exit.
#: Three attempts (with the exponential backoff below) are enough to ride out
#: transient causes — e.g. a rebuild burst rewriting generated files while
#: Vite is mid config-restart — while a persistently broken setup (bad
#: ``vite.config.js``, missing dependency) still fails fast and loudly instead
#: of crash-looping forever.
DEFAULT_RESTART_ATTEMPTS = 3

#: Base delay in seconds before relaunching Vite after an unexpected exit.
#: Doubles on every consecutive failure (0.5s → 1s → 2s) so the condition that
#: killed Vite (an in-flight rebuild, a port not yet released) has time to
#: clear, while the first relaunch is quick enough that HMR is back before the
#: developer notices.
DEFAULT_RESTART_DELAY = 0.5


class ViteSupervisionError(RuntimeError):
    """Raised when Vite exits unexpectedly and cannot be relaunched.

    Produced after the supervisor exhausts its relaunch budget
    (:data:`DEFAULT_RESTART_ATTEMPTS` by default). The dev server is still
    running at that point but can no longer serve client assets, so the error
    is surfaced prominently instead of leaving a silently dead asset server.
    """

    def __init__(self, attempts: int) -> None:
        super().__init__(
            "Vite dev server exited unexpectedly and could not be relaunched "
            f"after {attempts} attempt(s). Client assets can no longer be "
            "served — restart `pyxle dev`. Check the [vite] log output above "
            "for the underlying failure."
        )
        self.attempts = attempts


class ViteProcess:
    """Launch and supervise the Vite dev server.

    Supervision: any exit the supervisor did not initiate via :meth:`stop` —
    including a "clean" exit code 0, which Vite produces when e.g. its config
    file disappears mid config-reload — is treated as unexpected and answered
    with a bounded, exponentially backed-off relaunch. Exhausting the relaunch
    budget records a :class:`ViteSupervisionError` (see :attr:`fatal_error`)
    and logs it as a fatal error.
    """

    def __init__(
        self,
        settings: DevServerSettings,
        *,
        logger: ConsoleLogger | None = None,
        command: Iterable[str] | None = None,
        process_factory=None,
        stop_timeout: float = 5.0,
        readiness_timeout: float = 10.0,
        readiness_interval: float = 0.1,
        probe: _ViteProbe | None = None,
        restart_delay: float = DEFAULT_RESTART_DELAY,
        max_restart_attempts: int = DEFAULT_RESTART_ATTEMPTS,
    ) -> None:
        self._settings = settings
        self._logger = logger or ConsoleLogger()
        self._custom_command = list(command) if command is not None else None
        self._process_factory = process_factory or asyncio.create_subprocess_exec
        self._process: asyncio.subprocess.Process | None = None
        self._monitor_task: asyncio.Task[None] | None = None
        self._stop_timeout = stop_timeout
        self._readiness_timeout = readiness_timeout
        self._readiness_interval = readiness_interval
        self._probe = probe or self._default_probe
        self._latest_ready_elapsed: float | None = None
        self._stopping: bool = False
        self._restart_task: asyncio.Task[None] | None = None
        self._restart_delay = restart_delay
        self._max_restart_attempts = max_restart_attempts
        self._restart_attempts = 0
        self._fatal_error: ViteSupervisionError | None = None
        self._command_override: list[str] | None = None
        self._npm_install_attempted = False

    @property
    def running(self) -> bool:
        process = self._process
        return process is not None and process.returncode is None

    @property
    def fatal_error(self) -> ViteSupervisionError | None:
        """The supervision failure that ended relaunch attempts, if any."""
        return self._fatal_error

    async def start(self) -> None:
        if self.running:
            return

        self._stopping = False
        restart_task = self._restart_task
        current_task = asyncio.current_task()
        if restart_task is not None and restart_task is not current_task:
            restart_task.cancel()
            with suppress(asyncio.CancelledError):
                await restart_task
            self._restart_task = None
        if current_task is not restart_task or restart_task is None:
            # A manual (re)start expresses fresh intent: clear any previous
            # supervision failure so the relaunch budget starts over. The
            # supervisor's own relaunch loop must NOT reset the counter here,
            # or the retry budget could never be exhausted.
            self._fatal_error = None
            self._restart_attempts = 0

        command = self._build_launch_command()
        self._logger.debug("Launching Vite dev server: " + " ".join(command))
        env = self._build_env()

        try:
            process = await self._process_factory(
                *command,
                stdout=PIPE,
                stderr=PIPE,
                cwd=str(self._settings.project_root),
                env=env,
            )
        except FileNotFoundError as exc:
            if not await self._recover_missing_vite():
                raise RuntimeError(
                    "Unable to find 'vite'. Install Node.js dependencies with 'npm install' or provide a custom command."
                ) from exc

            command = self._build_launch_command()
            self._logger.debug("Retrying Vite launch with resolved command: " + " ".join(command))
            process = await self._process_factory(
                *command,
                stdout=PIPE,
                stderr=PIPE,
                cwd=str(self._settings.project_root),
                env=env,
            )

        self._process = process
        self._monitor_task = asyncio.create_task(self._monitor_process(process))

    async def wait_until_ready(self) -> None:
        """Block until Vite accepts TCP connections or the timeout elapses."""

        if not self.running:
            raise RuntimeError("Vite process is not running")

        host = self._settings.vite_host
        port = self._settings.vite_port
        timeout = self._readiness_timeout
        interval = self._readiness_interval
        deadline = asyncio.get_running_loop().time() + timeout
        logged_wait = False
        start = time.perf_counter()
        already_reported = self._latest_ready_elapsed is not None

        while True:
            if await self._probe(host, port):
                if not already_reported:
                    self._latest_ready_elapsed = time.perf_counter() - start
                    # The curated startup summary reports the Vite URL and total
                    # ready time; keep this per-probe confirmation at debug so
                    # the default console stays clean while `--verbose` still
                    # surfaces it.
                    self._logger.debug(
                        f"Vite dev server ready at http://{host}:{port} "
                        f"({self._latest_ready_elapsed:.2f}s)"
                    )
                return

            if not self.running:
                raise RuntimeError("Vite process exited before becoming ready")

            now = asyncio.get_running_loop().time()
            if now >= deadline:
                raise RuntimeError(
                    f"Timed out waiting for Vite dev server on http://{host}:{port}"
                )

            if not logged_wait:
                self._logger.info(
                    f"Waiting for Vite dev server on http://{host}:{port}"
                )
                logged_wait = True

            await asyncio.sleep(interval)

    async def stop(self) -> None:
        self._stopping = True

        if self._restart_task is not None:
            self._restart_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._restart_task
            self._restart_task = None

        process = self._process
        if process is None:
            return

        if process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=self._stop_timeout)
            except asyncio.TimeoutError:
                self._logger.warning("Vite process did not exit after SIGTERM; killing")
                process.kill()
                await process.wait()

        if self._monitor_task is not None:
            with suppress(asyncio.CancelledError):
                await self._monitor_task

        self._logger.debug("Vite dev server stopped")
        self._process = None
        self._monitor_task = None

    def _build_command(self) -> tuple[str, ...]:
        config_path = self._settings.client_build_dir / VITE_CONFIG_FILENAME
        return (
            "vite",
            "dev",
            "--config",
            str(config_path),
            "--host",
            self._settings.vite_host,
            "--port",
            str(self._settings.vite_port),
        )

    def _build_launch_command(self) -> list[str]:
        if self._custom_command is not None:
            return list(self._custom_command)
        if self._command_override is not None:
            return list(self._command_override)
        return list(self._build_command())

    async def _recover_missing_vite(self) -> bool:
        if self._custom_command is not None:
            return False

        base_command = list(self._build_command())
        local_command = self._local_vite_command()
        if local_command is not None:
            self._command_override = [*local_command, *base_command[1:]]
            return True

        project_root = self._settings.project_root
        package_json = project_root / "package.json"
        if (
            not self._npm_install_attempted
            and package_json.exists()
        ):
            self._npm_install_attempted = True
            await self._run_npm_install()
            local_command = self._local_vite_command()
            if local_command is not None:
                self._command_override = [*local_command, *base_command[1:]]
                return True

        npx_prefix = self._npx_prefix()
        if npx_prefix is not None:
            self._command_override = [*npx_prefix, *base_command[1:]]
            return True

        return False

    def _local_vite_command(self) -> list[str] | None:
        project_root = self._settings.project_root
        node_exec = shutil.which("node")

        vite_bin = project_root / "node_modules" / "vite" / "bin" / "vite.js"
        if node_exec is not None and vite_bin.exists():
            return [node_exec, str(vite_bin)]

        candidates = [
            project_root / "node_modules" / ".bin" / "vite",
            project_root / "node_modules" / ".bin" / "vite.cmd",
        ]
        for candidate in candidates:
            if candidate.exists():
                return [str(candidate)]

        return None

    async def _run_npm_install(self) -> bool:
        npm_exec = shutil.which("npm")
        if npm_exec is None:
            self._logger.error("Cannot run 'npm install': 'npm' executable not found in PATH.")
            return False

        self._logger.info("Installing Node dependencies via 'npm install'")
        try:
            process = await self._process_factory(
                npm_exec,
                "install",
                stdout=PIPE,
                stderr=PIPE,
                cwd=str(self._settings.project_root),
            )
        except FileNotFoundError:
            self._logger.error("Failed to execute 'npm install': 'npm' executable is unavailable.")
            return False

        stdout_bytes, stderr_bytes = await process.communicate()
        self._log_process_output(stdout_bytes, stderr_bytes, prefix="npm")

        if process.returncode not in (0, None):
            self._logger.error(f"'npm install' exited with code {process.returncode}")
            return False

        self._logger.success("npm install completed successfully")
        return True

    def _npx_prefix(self) -> tuple[str, ...] | None:
        npx_exec = shutil.which("npx")
        if npx_exec is None:
            return None
        return (npx_exec, "--yes", "vite")

    def _build_env(self) -> dict[str, str]:
        env = dict(os.environ)
        env.setdefault("PYXLE_VITE_BASE", "/")
        # Ensure Node can resolve modules from the project root's node_modules
        # even when the Vite config lives in .pyxle-build/client/.
        node_modules = str(self._settings.project_root / "node_modules")
        existing = env.get("NODE_PATH", "")
        env["NODE_PATH"] = f"{node_modules}:{existing}" if existing else node_modules
        return env

    def _log_process_output(self, stdout: bytes, stderr: bytes, *, prefix: str) -> None:
        stdout_text = stdout.decode(errors="ignore") if stdout else ""
        stderr_text = stderr.decode(errors="ignore") if stderr else ""

        for line in stdout_text.splitlines():
            line = line.strip()
            if line:
                self._logger.debug(f"[{prefix}] {line}")

        for line in stderr_text.splitlines():
            line = line.strip()
            if line:
                self._logger.error(f"[{prefix}] {line}")

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

        if returncode not in (0, None):
            self._logger.error(f"[vite] process exited with code {returncode}")
        else:
            self._logger.debug("[vite] process exited")

        if self._stopping:
            return

        # Any exit the supervisor did not initiate is unexpected — including a
        # "clean" exit code 0 (Vite exits 0 when, e.g., its config file
        # vanishes during a config-reload). Without a relaunch the dev server
        # would keep running with no asset server behind it, which looks like
        # a dead page in the browser.
        self._logger.warning("Vite process exited unexpectedly; attempting restart")
        self._process = None
        if self._fatal_error is None and (
            self._restart_task is None or self._restart_task.done()
        ):
            self._restart_task = asyncio.create_task(self._restart_after_exit())

    async def _pipe_stream(self, stream: asyncio.StreamReader, *, is_error: bool) -> None:
        while True:
            line = await stream.readline()
            if not line:
                break
            message = line.decode(errors="replace").rstrip()
            if not message:
                continue
            if is_error:
                self._logger.error(f"[vite] {message}")
            else:
                # Vite's per-line stdout (startup banner, HMR updates, transform
                # logs) is the noisy firehose. Keep it at debug so the default
                # `pyxle dev` console stays clean; `--verbose` restores it.
                self._logger.debug(f"[vite] {message}")

    async def _restart_after_exit(self) -> None:
        """Relaunch Vite after an unexpected exit, with backoff and a budget.

        Each consecutive failure doubles the delay; a relaunch that reaches
        readiness resets the budget (the supervisor is healthy again).
        Exhausting :attr:`_max_restart_attempts` records a
        :class:`ViteSupervisionError` and logs it as fatal.
        """
        try:
            while not self._stopping:
                self._restart_attempts += 1
                if self._restart_attempts > self._max_restart_attempts:
                    self._fatal_error = ViteSupervisionError(self._max_restart_attempts)
                    self._logger.error(str(self._fatal_error))
                    # Never leave a hung child running unsupervised past the
                    # budget — it would hold the port and confuse the next
                    # manual start().
                    await self._terminate_unready_child()
                    return
                delay = self._restart_delay * (2 ** (self._restart_attempts - 1))
                self._logger.warning(
                    f"Relaunching Vite dev server in {delay:.1f}s "
                    f"(attempt {self._restart_attempts}/{self._max_restart_attempts})"
                )
                await asyncio.sleep(delay)
                if self._stopping:
                    return
                try:
                    await self.start()
                    await self.wait_until_ready()
                except Exception as exc:
                    self._logger.warning(f"Vite relaunch attempt failed: {exc}")
                    # A child that launched but never became ready (hung Vite)
                    # must not survive the attempt: ``start()`` would no-op on
                    # it next iteration and the budget would burn re-probing
                    # the same dead-end process.
                    await self._terminate_unready_child()
                    continue
                # The relaunched process accepts connections again; treat the
                # supervisor as healthy and forget the failure streak.
                self._restart_attempts = 0
                return
        finally:
            if self._restart_task is asyncio.current_task():
                self._restart_task = None

    async def _terminate_unready_child(self) -> None:
        """Terminate a live child without entering shutdown state.

        Used only by the supervisor when a relaunch left Vite running but not
        accepting connections. ``_stopping`` stays False (this is not a
        shutdown) — the monitor task skips rescheduling because the restart
        task is still the current task.
        """
        process = self._process
        if process is None or process.returncode is not None:
            return
        self._logger.warning(
            "Vite is running but not accepting connections; terminating the hung process"
        )
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=self._stop_timeout)
        except asyncio.TimeoutError:
            self._logger.warning("Hung Vite process did not exit after SIGTERM; killing")
            process.kill()
            await process.wait()
        self._process = None

    @staticmethod
    async def _default_probe(host: str, port: int) -> bool:
        try:
            reader, writer = await asyncio.open_connection(host, port)
        except OSError:
            return False
        else:
            writer.close()
            with suppress(Exception):
                await writer.wait_closed()
            return True


__all__ = [
    "DEFAULT_RESTART_ATTEMPTS",
    "DEFAULT_RESTART_DELAY",
    "ViteProcess",
    "ViteSupervisionError",
]
