from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Iterable

import pytest

from pyxle.cli.logger import ConsoleLogger
from pyxle.devserver.settings import DevServerSettings
from pyxle.devserver.vite import ViteProcess, ViteSupervisionError

pytestmark = pytest.mark.anyio("asyncio")


@pytest.fixture
def settings(tmp_path: Path) -> DevServerSettings:
	root = tmp_path / "project"
	(root / "pages").mkdir(parents=True)
	(root / "public").mkdir()
	(root / ".pyxle-build" / "client").mkdir(parents=True)
	return DevServerSettings.from_project_root(root)


@pytest.fixture
def anyio_backend() -> str:  # pragma: no cover - fixture wiring
	return "asyncio"


class FakeProcess:
	def __init__(
		self,
		stdout_lines: Iterable[str] | None,
		stderr_lines: Iterable[str] | None,
		exit_code: int,
		*,
		auto_exit: bool = True,
	) -> None:
		self.stdout = asyncio.StreamReader() if stdout_lines is not None else None
		self.stderr = asyncio.StreamReader() if stderr_lines is not None else None
		self._stdout_lines = list(stdout_lines or [])
		self._stderr_lines = list(stderr_lines or [])
		self._exit_code = exit_code
		self.returncode: int | None = None
		self._feed_tasks: list[asyncio.Task[None]] = []
		self._auto_exit = auto_exit
		self._exit_event: asyncio.Event | None = None
		if not auto_exit:
			self._exit_event = asyncio.Event()

	def start(self) -> None:
		loop = asyncio.get_running_loop()

		if self.stdout is not None:
			async def feed_stdout() -> None:
				for line in self._stdout_lines:
					self.stdout.feed_data(f"{line}\n".encode())
					await asyncio.sleep(0)
				self.stdout.feed_eof()

			self._feed_tasks.append(loop.create_task(feed_stdout()))

		if self.stderr is not None:
			async def feed_stderr() -> None:
				for line in self._stderr_lines:
					self.stderr.feed_data(f"{line}\n".encode())
					await asyncio.sleep(0)
				self.stderr.feed_eof()

			self._feed_tasks.append(loop.create_task(feed_stderr()))

	async def wait(self) -> int:
		if self._feed_tasks:
			await asyncio.gather(*self._feed_tasks, return_exceptions=True)
		if not self._auto_exit and self._exit_event is not None:
			await self._exit_event.wait()
		if self.returncode is None:
			self.returncode = self._exit_code
		return self.returncode

	def terminate(self) -> None:
		if self.returncode is None:
			self.returncode = self._exit_code
		for stream in (self.stdout, self.stderr):
			if stream is not None and not stream.at_eof():
				stream.feed_eof()
		if self._exit_event is not None:
			self._exit_event.set()

	def kill(self) -> None:
		self.returncode = -9
		self.terminate()

	def allow_exit(self, exit_code: int | None = None) -> None:
		if exit_code is not None:
			self._exit_code = exit_code
		if self._exit_event is not None:
			self._exit_event.set()

	async def communicate(self) -> tuple[bytes, bytes]:
		await self.wait()
		stdout = "\n".join(self._stdout_lines).encode()
		stderr = "\n".join(self._stderr_lines).encode()
		return stdout, stderr


class ProcessStub:
	def __init__(
		self,
		*,
		stdout_lines: Iterable[str] | None = None,
		stderr_lines: Iterable[str] | None = None,
		exit_code: int = 0,
	) -> None:
		self.stdout_lines = list(stdout_lines or [])
		self.stderr_lines = list(stderr_lines or [])
		self.exit_code = exit_code
		self.created_commands: list[list[str]] = []
		self.processes: list[FakeProcess] = []

	async def __call__(self, *cmd: str, **_: Any) -> FakeProcess:
		process = FakeProcess(self.stdout_lines, self.stderr_lines, self.exit_code)
		process.start()
		self.created_commands.append(list(cmd))
		self.processes.append(process)
		return process


async def test_vite_process_start_and_stop(settings: DevServerSettings) -> None:
	stub = ProcessStub(stdout_lines=["ready"], stderr_lines=["warning"])
	capture: list[str] = []
	logger = ConsoleLogger(secho=lambda msg, **_: capture.append(msg))

	vite = ViteProcess(
		settings,
		logger=logger,
		process_factory=stub,
		stop_timeout=0.1,
	)

	await vite.start()
	await asyncio.sleep(0)
	assert stub.created_commands[0][:2] == ["vite", "dev"]
	assert vite.running is True

	await vite.stop()
	assert vite.running is False
	assert any("ready" in msg for msg in capture)
	assert any("warning" in msg for msg in capture)


async def test_vite_process_is_idempotent(settings: DevServerSettings) -> None:
	stub = ProcessStub()
	vite = ViteProcess(settings, process_factory=stub)

	await vite.start()
	await vite.start()

	assert len(stub.created_commands) == 1

	await vite.stop()


async def test_vite_process_stop_without_start(settings: DevServerSettings) -> None:
	vite = ViteProcess(settings)
	await vite.stop()  # should not raise


async def test_vite_process_detects_non_zero_exit(settings: DevServerSettings) -> None:
	stub = ProcessStub(stderr_lines=["fail"], exit_code=1)
	capture: list[str] = []
	logger = ConsoleLogger(secho=lambda msg, **_: capture.append(msg))

	vite = ViteProcess(settings, logger=logger, process_factory=stub, stop_timeout=0.1)
	await vite.start()
	await asyncio.sleep(0)

	await vite.stop()
	assert any("fail" in msg for msg in capture)
	assert any("exited with code 1" in msg for msg in capture)


async def test_vite_process_handles_missing_streams(settings: DevServerSettings) -> None:
	async def factory(*cmd: str, **_: Any) -> FakeProcess:
		process = FakeProcess(stdout_lines=None, stderr_lines=None, exit_code=0)
		process.start()
		return process

	vite = ViteProcess(settings, process_factory=factory)

	await vite.start()
	await vite.stop()


async def test_vite_process_ignores_blank_stream_lines(settings: DevServerSettings) -> None:
	stub = ProcessStub(stdout_lines=["", "ready"], stderr_lines=["", "error"])
	capture: list[str] = []
	logger = ConsoleLogger(secho=lambda msg, **_: capture.append(msg))

	vite = ViteProcess(settings, logger=logger, process_factory=stub, stop_timeout=0.1)

	await vite.start()
	await asyncio.sleep(0.05)
	await vite.stop()

	assert any("ready" in msg for msg in capture)
	assert any("error" in msg for msg in capture)
	assert not any(msg.rstrip().endswith("[vite]") for msg in capture)


async def test_vite_process_restarts_after_crash(settings: DevServerSettings) -> None:
	class RestartingFactory:
		def __init__(self) -> None:
			self.calls = 0
			self.processes: list[FakeProcess] = []

		async def __call__(self, *cmd: str, **_: Any) -> FakeProcess:
			if self.calls == 0:
				exit_code = 1
				auto_exit = True
			else:
				exit_code = 0
				auto_exit = False

			process = FakeProcess(
				stdout_lines=None,
				stderr_lines=None,
				exit_code=exit_code,
				auto_exit=auto_exit,
			)
			process.start()
			self.calls += 1
			self.processes.append(process)
			return process

	factory = RestartingFactory()
	capture: list[str] = []
	logger = ConsoleLogger(secho=lambda msg, **_: capture.append(msg))
	probe_calls: list[tuple[str, int]] = []

	async def probe(host: str, port: int) -> bool:
		probe_calls.append((host, port))
		return True

	vite = ViteProcess(
		settings,
		logger=logger,
		process_factory=factory,
		restart_delay=0,
		readiness_interval=0.01,
		probe=probe,
		stop_timeout=0.1,
	)

	await vite.start()

	async def wait_for_restart() -> None:
		while True:
			if (
				factory.calls >= 2
				and vite.running
				and probe_calls
				and vite._restart_task is None
			):
				return
			await asyncio.sleep(0.01)

	await asyncio.wait_for(wait_for_restart(), timeout=1)

	assert factory.calls == 2
	assert vite.running is True
	assert probe_calls
	assert any("attempting restart" in msg for msg in capture)

	await vite.stop()


async def test_vite_process_stop_cancels_pending_restart(settings: DevServerSettings) -> None:
	class RestartingFactory:
		def __init__(self) -> None:
			self.calls = 0

		async def __call__(self, *cmd: str, **_: Any) -> FakeProcess:
			if self.calls == 0:
				process = FakeProcess(stdout_lines=None, stderr_lines=None, exit_code=1)
			else:
				process = FakeProcess(
					stdout_lines=None,
					stderr_lines=None,
					exit_code=0,
					auto_exit=False,
				)
			process.start()
			self.calls += 1
			return process

	factory = RestartingFactory()
	probe_calls: list[tuple[str, int]] = []

	async def probe(host: str, port: int) -> bool:
		probe_calls.append((host, port))
		return False

	vite = ViteProcess(
		settings,
		process_factory=factory,
		probe=probe,
		restart_delay=0,
		stop_timeout=0.1,
	)

	await vite.start()

	async def wait_for_restart_task() -> None:
		while vite._restart_task is None:
			await asyncio.sleep(0.01)

	await asyncio.wait_for(wait_for_restart_task(), timeout=1)
	assert probe_calls  # restart attempted readiness probe

	await vite.stop()

	assert vite._restart_task is None
	assert vite.running is False


async def test_vite_process_stop_kills_after_timeout(settings: DevServerSettings) -> None:
	class HangingProcess(FakeProcess):
		def __init__(self) -> None:
			super().__init__(stdout_lines=None, stderr_lines=None, exit_code=0, auto_exit=False)
			self.terminated = False
			self.killed = False

		def terminate(self) -> None:
			self.terminated = True
			# intentional: do not allow exit so wait blocks

		def kill(self) -> None:
			self.killed = True
			self.allow_exit(exit_code=-9)

	class HangingFactory:
		def __init__(self) -> None:
			self.process: HangingProcess | None = None

		async def __call__(self, *cmd: str, **_: Any) -> HangingProcess:
			process = HangingProcess()
			process.start()
			self.process = process
			return process

	factory = HangingFactory()
	capture: list[str] = []
	logger = ConsoleLogger(secho=lambda msg, **_: capture.append(msg))
	vite = ViteProcess(settings, logger=logger, process_factory=factory, stop_timeout=0.01)

	await vite.start()
	assert factory.process is not None
	await asyncio.sleep(0)
	await vite.stop()

	process = factory.process
	assert process is not None and process.terminated is True
	assert process is not None and process.killed is True
	assert any("did not exit after SIGTERM" in msg for msg in capture)


async def test_vite_process_manual_start_cancels_scheduled_restart(settings: DevServerSettings) -> None:
	class RestartingFactory:
		def __init__(self) -> None:
			self.calls = 0

		async def __call__(self, *cmd: str, **_: Any) -> FakeProcess:
			if self.calls == 0:
				process = FakeProcess(stdout_lines=None, stderr_lines=None, exit_code=1)
			else:
				process = FakeProcess(
					stdout_lines=None,
					stderr_lines=None,
					exit_code=0,
					auto_exit=False,
				)
			process.start()
			self.calls += 1
			return process

	factory = RestartingFactory()

	vite = ViteProcess(
		settings,
		process_factory=factory,
		restart_delay=0.05,
		stop_timeout=0.1,
	)

	await vite.start()

	async def wait_for_restart_task() -> None:
		while vite._restart_task is None:
			await asyncio.sleep(0.01)

	await asyncio.wait_for(wait_for_restart_task(), timeout=1)
	assert factory.calls == 1

	await vite.start()  # manual restart should cancel pending task

	assert vite._restart_task is None
	assert factory.calls == 2
	assert vite.running is True

	await vite.stop()


async def test_vite_process_restart_aborts_when_stopping(settings: DevServerSettings) -> None:
	class RestartingFactory:
		def __init__(self) -> None:
			self.calls = 0

		async def __call__(self, *cmd: str, **_: Any) -> FakeProcess:
			process = FakeProcess(stdout_lines=None, stderr_lines=None, exit_code=1 if self.calls == 0 else 0)
			process.start()
			self.calls += 1
			return process

	factory = RestartingFactory()

	vite = ViteProcess(
		settings,
		process_factory=factory,
		restart_delay=0.05,
	)

	await vite.start()

	async def wait_for_restart_task() -> None:
		while vite._restart_task is None:
			await asyncio.sleep(0.005)

	await asyncio.wait_for(wait_for_restart_task(), timeout=1)
	assert factory.calls == 1

	vite._stopping = True  # simulate external stop without cancelling task
	await asyncio.sleep(0.06)

	assert vite._restart_task is None
	assert factory.calls == 1  # restart skipped

	await vite.stop()


async def test_vite_process_installs_dependencies_when_vite_missing(
	settings: DevServerSettings,
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	(settings.project_root / "package.json").write_text("{}", encoding="utf-8")
	commands: list[list[str]] = []

	class Factory:
		async def __call__(self, *cmd: str, **_: Any) -> FakeProcess:
			commands.append(list(cmd))
			if cmd[0] == "vite":
				raise FileNotFoundError("vite")
			if Path(cmd[0]).name == "npm":
				process = FakeProcess(stdout_lines=["added 1 package"], stderr_lines=None, exit_code=0)
				process.start()
				bin_dir = settings.project_root / "node_modules" / ".bin"
				bin_dir.mkdir(parents=True, exist_ok=True)
				(bin_dir / "vite").write_text("#!/usr/bin/env node", encoding="utf-8")
				return process
			process = FakeProcess(stdout_lines=None, stderr_lines=None, exit_code=0, auto_exit=False)
			process.start()
			return process

	monkeypatch.setattr(
		"pyxle.devserver.vite.shutil.which",
		lambda name: name if name == "npm" else None,
	)

	factory = Factory()
	vite = ViteProcess(settings, process_factory=factory, stop_timeout=0.1)

	await vite.start()
	await asyncio.sleep(0)

	assert commands and commands[0][0] == "vite"
	assert any(Path(cmd[0]).name == "npm" for cmd in commands)
	assert any("node_modules/.bin" in cmd[0] and Path(cmd[0]).name == "vite" for cmd in commands)

	await vite.stop()


async def test_vite_process_falls_back_to_npx_when_install_fails(
	settings: DevServerSettings,
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	(settings.project_root / "package.json").write_text("{}", encoding="utf-8")
	commands: list[list[str]] = []

	class Factory:
		async def __call__(self, *cmd: str, **_: Any) -> FakeProcess:
			commands.append(list(cmd))
			if cmd[0] == "vite":
				raise FileNotFoundError("vite")
			if Path(cmd[0]).name == "npm":
				process = FakeProcess(stdout_lines=None, stderr_lines=["npm error"], exit_code=1)
				process.start()
				return process
			if Path(cmd[0]).name == "npx":
				process = FakeProcess(stdout_lines=None, stderr_lines=None, exit_code=0, auto_exit=False)
				process.start()
				return process
			raise AssertionError(f"unexpected command: {cmd}")

	monkeypatch.setattr(
		"pyxle.devserver.vite.shutil.which",
		lambda name: name if name in {"npm", "npx"} else None,
	)

	factory = Factory()
	vite = ViteProcess(settings, process_factory=factory, stop_timeout=0.1)

	await vite.start()
	await asyncio.sleep(0)

	assert commands and commands[0][0] == "vite"
	assert any(Path(cmd[0]).name == "npm" for cmd in commands)
	assert any(Path(cmd[0]).name == "npx" for cmd in commands)

	await vite.stop()


async def test_vite_process_wait_until_ready_success(settings: DevServerSettings) -> None:
	stub = ProcessStub()
	capture: list[str] = []
	logger = ConsoleLogger(secho=lambda msg, **_: capture.append(msg))
	probe_calls: list[tuple[str, int]] = []

	async def probe(host: str, port: int) -> bool:
		probe_calls.append((host, port))
		return True

	vite = ViteProcess(
		settings,
		logger=logger,
		process_factory=stub,
		probe=probe,
	)

	await vite.start()
	await vite.wait_until_ready()
	await vite.stop()

	assert probe_calls == [(settings.vite_host, settings.vite_port)]
	assert any("ready" in message for message in capture)


async def test_vite_process_wait_until_ready_reuses_elapsed(settings: DevServerSettings) -> None:
	stub = ProcessStub()
	logger = ConsoleLogger()
	probe_calls: list[tuple[str, int]] = []

	async def probe(host: str, port: int) -> bool:
		probe_calls.append((host, port))
		return True

	vite = ViteProcess(
		settings,
		logger=logger,
		process_factory=stub,
		probe=probe,
	)

	await vite.start()
	await vite.wait_until_ready()
	first_elapsed = vite._latest_ready_elapsed
	assert first_elapsed is not None

	probe_calls.clear()
	await vite.wait_until_ready()

	assert vite._latest_ready_elapsed == first_elapsed
	assert probe_calls == [(settings.vite_host, settings.vite_port)]

	await vite.stop()


async def test_vite_process_wait_until_ready_times_out(settings: DevServerSettings) -> None:
	stub = ProcessStub()

	async def probe(*_: Any) -> bool:
		return False

	vite = ViteProcess(
		settings,
		process_factory=stub,
		probe=probe,
		readiness_timeout=0.2,
		readiness_interval=0.05,
	)

	await vite.start()
	with pytest.raises(RuntimeError):
		await vite.wait_until_ready()
	await vite.stop()


async def test_vite_process_wait_until_ready_requires_running(settings: DevServerSettings) -> None:
	vite = ViteProcess(settings)
	with pytest.raises(RuntimeError):
		await vite.wait_until_ready()


async def test_vite_process_wait_until_ready_logs_wait(settings: DevServerSettings) -> None:
	stub = ProcessStub()
	attempts: list[int] = []
	capture: list[str] = []
	logger = ConsoleLogger(secho=lambda msg, **_: capture.append(msg))

	async def probe(host: str, port: int) -> bool:
		attempts.append(1)
		return len(attempts) > 1

	vite = ViteProcess(
		settings,
		logger=logger,
		process_factory=stub,
		probe=probe,
		readiness_interval=0.01,
		readiness_timeout=1.0,
	)

	await vite.start()
	await vite.wait_until_ready()
	await vite.stop()

	assert len(attempts) >= 2
	assert any("Waiting for Vite dev server" in message for message in capture)
	assert any("ready" in message for message in capture)


@pytest.mark.anyio
async def test_vite_process_default_probe(monkeypatch) -> None:
	results: list[bool] = []

	async def failing_open_connection(host: str, port: int):
		raise OSError("boom")

	monkeypatch.setattr("asyncio.open_connection", failing_open_connection)
	result = await ViteProcess._default_probe("127.0.0.1", 1234)
	results.append(result)

	class DummyWriter:
		def __init__(self) -> None:
			self.closed = False
			self.waited = False

		def close(self) -> None:
			self.closed = True

		async def wait_closed(self) -> None:
			self.waited = True

	async def succeeding_open_connection(host: str, port: int):
		return asyncio.StreamReader(), DummyWriter()

	monkeypatch.setattr("asyncio.open_connection", succeeding_open_connection)
	result_success = await ViteProcess._default_probe("127.0.0.1", 1235)
	results.append(result_success)

	assert results == [False, True]


async def test_vite_process_relaunches_after_unexpected_clean_exit(settings: DevServerSettings) -> None:
	"""An exit the supervisor didn't initiate — even code 0 — is relaunched."""

	class Factory:
		def __init__(self) -> None:
			self.calls = 0

		async def __call__(self, *cmd: str, **_: Any) -> FakeProcess:
			if self.calls == 0:
				process = FakeProcess(stdout_lines=None, stderr_lines=None, exit_code=0)
			else:
				process = FakeProcess(
					stdout_lines=None,
					stderr_lines=None,
					exit_code=0,
					auto_exit=False,
				)
			process.start()
			self.calls += 1
			return process

	factory = Factory()
	capture: list[str] = []
	logger = ConsoleLogger(secho=lambda msg, **_: capture.append(msg))

	async def probe(host: str, port: int) -> bool:
		return True

	vite = ViteProcess(
		settings,
		logger=logger,
		process_factory=factory,
		restart_delay=0,
		readiness_interval=0.01,
		probe=probe,
		stop_timeout=0.1,
	)

	await vite.start()

	async def wait_for_relaunch() -> None:
		while not (factory.calls >= 2 and vite.running and vite._restart_task is None):
			await asyncio.sleep(0.01)

	await asyncio.wait_for(wait_for_relaunch(), timeout=1)

	assert factory.calls == 2
	assert vite.running is True
	assert vite.fatal_error is None
	assert vite._restart_attempts == 0  # a ready relaunch resets the budget
	assert any("exited unexpectedly" in msg for msg in capture)

	await vite.stop()


async def test_vite_process_shutdown_exit_does_not_relaunch(settings: DevServerSettings) -> None:
	"""A stop()-initiated exit must never schedule a relaunch."""

	class Factory:
		def __init__(self) -> None:
			self.calls = 0

		async def __call__(self, *cmd: str, **_: Any) -> FakeProcess:
			process = FakeProcess(
				stdout_lines=None,
				stderr_lines=None,
				exit_code=0,
				auto_exit=False,
			)
			process.start()
			self.calls += 1
			return process

	factory = Factory()
	capture: list[str] = []
	logger = ConsoleLogger(secho=lambda msg, **_: capture.append(msg))

	vite = ViteProcess(settings, logger=logger, process_factory=factory, stop_timeout=0.1)

	await vite.start()
	await asyncio.sleep(0)
	await vite.stop()
	await asyncio.sleep(0.05)

	assert factory.calls == 1
	assert vite._restart_task is None
	assert vite.fatal_error is None
	assert not any("attempting restart" in msg for msg in capture)


async def test_vite_process_restart_exhaustion_surfaces_structured_error(
	settings: DevServerSettings,
) -> None:
	"""Relaunch attempts are bounded; exhaustion records a ViteSupervisionError."""

	class CrashingFactory:
		def __init__(self) -> None:
			self.calls = 0

		async def __call__(self, *cmd: str, **_: Any) -> FakeProcess:
			process = FakeProcess(stdout_lines=None, stderr_lines=None, exit_code=1)
			process.start()
			self.calls += 1
			return process

	factory = CrashingFactory()
	capture: list[str] = []
	logger = ConsoleLogger(secho=lambda msg, **_: capture.append(msg))

	async def probe(host: str, port: int) -> bool:
		return False

	vite = ViteProcess(
		settings,
		logger=logger,
		process_factory=factory,
		restart_delay=0,
		readiness_interval=0.01,
		readiness_timeout=0.2,
		probe=probe,
		max_restart_attempts=2,
		stop_timeout=0.1,
	)

	await vite.start()

	async def wait_for_fatal() -> None:
		while vite.fatal_error is None:
			await asyncio.sleep(0.01)

	await asyncio.wait_for(wait_for_fatal(), timeout=5)

	error = vite.fatal_error
	assert isinstance(error, ViteSupervisionError)
	assert error.attempts == 2
	assert "could not be relaunched" in str(error)
	assert "pyxle dev" in str(error)
	assert factory.calls == 3  # the initial launch + 2 relaunch attempts
	assert vite.running is False
	assert any("could not be relaunched" in msg for msg in capture)
	assert any("relaunch attempt failed" in msg.lower() for msg in capture)

	await vite.stop()


async def test_vite_process_manual_start_clears_fatal_error(settings: DevServerSettings) -> None:
	"""After exhaustion, an explicit start() resets the supervision budget."""

	class Factory:
		def __init__(self) -> None:
			self.calls = 0

		async def __call__(self, *cmd: str, **_: Any) -> FakeProcess:
			process = FakeProcess(
				stdout_lines=None,
				stderr_lines=None,
				exit_code=0,
				auto_exit=False,
			)
			process.start()
			self.calls += 1
			return process

	factory = Factory()
	vite = ViteProcess(settings, process_factory=factory, stop_timeout=0.1)
	vite._fatal_error = ViteSupervisionError(3)
	vite._restart_attempts = 4

	await vite.start()

	assert vite.fatal_error is None
	assert vite._restart_attempts == 0
	assert vite.running is True

	await vite.stop()


async def test_vite_process_restart_loop_no_ops_when_already_stopping(
	settings: DevServerSettings,
) -> None:
	"""A relaunch racing stop() exits immediately without spawning anything."""
	stub = ProcessStub()
	vite = ViteProcess(settings, process_factory=stub)
	vite._stopping = True

	await vite._restart_after_exit()

	assert stub.created_commands == []
	assert vite._restart_task is None
	assert vite.fatal_error is None


async def test_vite_supervisor_terminates_hung_child_and_stops_leak(
	settings: DevServerSettings,
) -> None:
	"""A relaunched Vite that stays alive but never listens is terminated on
	each failed attempt (so every attempt launches a FRESH process) and again
	on budget exhaustion — no orphan survives the supervisor."""

	class HungFactory:
		def __init__(self) -> None:
			self.calls = 0
			self.processes: list[FakeProcess] = []

		async def __call__(self, *cmd: str, **_: Any) -> FakeProcess:
			# First process crashes to trigger the supervisor; every relaunch
			# hangs: alive, but the probe below never reports ready.
			auto_exit = self.calls == 0
			process = FakeProcess(
				stdout_lines=None,
				stderr_lines=None,
				exit_code=1 if auto_exit else 0,
				auto_exit=auto_exit,
			)
			process.start()
			self.calls += 1
			self.processes.append(process)
			return process

	factory = HungFactory()
	capture: list[str] = []
	logger = ConsoleLogger(secho=lambda msg, **_: capture.append(msg))

	async def never_ready(host: str, port: int) -> bool:
		return False

	vite = ViteProcess(
		settings,
		logger=logger,
		process_factory=factory,
		restart_delay=0,
		readiness_timeout=0.05,
		readiness_interval=0.01,
		probe=never_ready,
		stop_timeout=0.1,
	)

	await vite.start()

	async def wait_for_exhaustion() -> None:
		while vite.fatal_error is None:
			await asyncio.sleep(0.01)

	await asyncio.wait_for(wait_for_exhaustion(), timeout=5)

	# One fresh process per attempt: the hung child never survives into the
	# next iteration, and none survives exhaustion.
	assert factory.calls == 1 + vite._max_restart_attempts
	assert all(process.returncode is not None for process in factory.processes)
	assert vite.running is False
	assert any("terminating the hung process" in msg for msg in capture)

	await vite.stop()
