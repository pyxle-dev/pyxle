from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from pyxle.build import vite
from pyxle.build.vite import (
    ViteBuildError,
    _attempt_npm_install,
    _resolve_npm_build_command,
    _resolve_vite_command,
    _verify_command,
    run_vite_build,
)
from pyxle.cli.logger import ConsoleLogger


def make_logger():
    messages: list[str] = []

    def fake_secho(message: str, fg=None, bold=False):  # noqa: ANN001, D401 - test helper
        messages.append(message)

    return ConsoleLogger(fake_secho), messages


def test_run_vite_build_success(monkeypatch, tmp_path):
    project_root = tmp_path / "project"
    client_build_dir = project_root / ".pyxle-build" / "client"
    output_dir = project_root / "dist" / "client"
    client_build_dir.mkdir(parents=True)
    output_dir.mkdir(parents=True)

    manifest_path = output_dir / "manifest.json"

    def fake_resolve(root: Path, logger: ConsoleLogger):  # noqa: ARG001
        assert root == project_root
        return ["/usr/local/bin/node", "vite.js"]

    executed: dict[str, object] = {}

    def fake_run(command, *, cwd, capture_output, text, check, env=None, start_new_session=None):  # noqa: ANN001, ARG001
        executed["command"] = command
        executed["cwd"] = cwd
        executed["capture_output"] = capture_output
        executed["text"] = text
        executed["check"] = check
        executed["start_new_session"] = start_new_session
        assert env is not None
        assert env.get("PYXLE_VITE_BASE") == "/client/"
        manifest_path.write_text("{}", encoding="utf-8")
        return SimpleNamespace(
            returncode=0,
            stdout="build ok\nall good",
            stderr="warn line",
        )

    monkeypatch.setattr(vite, "_resolve_npm_build_command", lambda *_: None)
    monkeypatch.setattr(vite, "_resolve_vite_command", fake_resolve)
    monkeypatch.setattr(vite.subprocess, "run", fake_run)

    logger, messages = make_logger()

    result = run_vite_build(
        project_root=project_root,
        client_build_dir=client_build_dir,
        output_dir=output_dir,
        logger=logger,
    )

    assert result == manifest_path
    assert executed["command"][:2] == ["/usr/local/bin/node", "vite.js"]
    assert "build" in executed["command"]
    assert executed["cwd"] == str(project_root)
    assert executed["capture_output"] is True
    assert executed["text"] is True
    assert executed["check"] is False
    assert executed["start_new_session"] is True
    assert any("Running Vite production build" in message for message in messages)
    assert any("[vite] build ok" in message for message in messages)
    assert any("[vite] warn line" in message for message in messages)
    assert any("Vite build completed" in message for message in messages)


def test_run_vite_build_failure(monkeypatch, tmp_path):
    project_root = tmp_path / "project"
    client_build_dir = project_root / ".pyxle-build" / "client"
    output_dir = project_root / "dist" / "client"
    client_build_dir.mkdir(parents=True)
    output_dir.mkdir(parents=True)

    manifest_path = output_dir / "manifest.json"
    manifest_path.write_text("{}", encoding="utf-8")

    monkeypatch.setattr(vite, "_resolve_npm_build_command", lambda *_: None)
    monkeypatch.setattr(vite, "_resolve_vite_command", lambda *_: ["vite"])

    def fake_run(command, *, cwd, capture_output, text, check, env=None, start_new_session=None):  # noqa: ANN001, ARG001
        assert command[0] == "vite"
        return SimpleNamespace(returncode=1, stdout="", stderr="failure")

    monkeypatch.setattr(vite.subprocess, "run", fake_run)

    logger, messages = make_logger()

    with pytest.raises(ViteBuildError):
        run_vite_build(
            project_root=project_root,
            client_build_dir=client_build_dir,
            output_dir=output_dir,
            logger=logger,
        )

    assert any("[vite] failure" in message for message in messages)


def test_run_vite_build_manifest_missing(monkeypatch, tmp_path):
    project_root = tmp_path / "project"
    client_build_dir = project_root / ".pyxle-build" / "client"
    output_dir = project_root / "dist" / "client"
    client_build_dir.mkdir(parents=True)
    output_dir.mkdir(parents=True)

    monkeypatch.setattr(vite, "_resolve_npm_build_command", lambda *_: None)
    monkeypatch.setattr(vite, "_resolve_vite_command", lambda *_: ["vite"])

    def fake_run(command, *, cwd, capture_output, text, check, env=None, start_new_session=None):  # noqa: ANN001, ARG001
        return SimpleNamespace(returncode=0, stdout="done", stderr="")

    monkeypatch.setattr(vite.subprocess, "run", fake_run)

    logger, _ = make_logger()

    with pytest.raises(ViteBuildError) as exc:
        run_vite_build(
            project_root=project_root,
            client_build_dir=client_build_dir,
            output_dir=output_dir,
            logger=logger,
        )

    assert "manifest" in str(exc.value)


def test_run_vite_build_prefers_npm_script(monkeypatch, tmp_path):
    project_root = tmp_path / "project"
    client_build_dir = project_root / ".pyxle-build" / "client"
    output_dir = project_root / "dist" / "client"
    client_build_dir.mkdir(parents=True)
    output_dir.mkdir(parents=True)

    manifest_path = output_dir / "manifest.json"

    npm_command = ["npm", "run", "build", "--"]
    monkeypatch.setattr(vite, "_resolve_npm_build_command", lambda *_: npm_command)

    def fake_resolve(*_):  # pragma: no cover - should not run
        raise AssertionError("_resolve_vite_command should not be called when npm script exists")

    monkeypatch.setattr(vite, "_resolve_vite_command", fake_resolve)

    recorded: dict[str, object] = {}

    def fake_run(command, *, cwd, capture_output, text, check, env=None, start_new_session=None):  # noqa: ANN001, ARG001
        recorded["command"] = command
        recorded["cwd"] = cwd
        manifest_path.write_text("{}", encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr(vite.subprocess, "run", fake_run)

    logger, _ = make_logger()

    result = run_vite_build(
        project_root=project_root,
        client_build_dir=client_build_dir,
        output_dir=output_dir,
        logger=logger,
    )

    assert result == manifest_path
    assert recorded["command"][:4] == npm_command
    assert recorded["command"][4] == "--config"


def test_attempt_npm_install_runs_install(monkeypatch, tmp_path):
    project_root = tmp_path / "project"
    project_root.mkdir()
    (project_root / "package.json").write_text("{}", encoding="utf-8")

    commands: list[list[str]] = []

    def fake_which(name: str) -> str | None:
        if name == "npm":
            return "/usr/bin/npm"
        return None

    def fake_run(command, *, cwd, capture_output, text, check, start_new_session=None):  # noqa: ANN001
        commands.append(command)
        assert start_new_session is True
        assert cwd == str(project_root)
        assert capture_output is True
        assert text is True
        assert check is False
        return SimpleNamespace(returncode=0, stdout="install ok", stderr="")

    monkeypatch.setattr(vite.shutil, "which", fake_which)
    monkeypatch.setattr(vite.subprocess, "run", fake_run)

    logger, messages = make_logger()

    result = _attempt_npm_install(project_root, logger)

    assert result is True
    assert commands == [["/usr/bin/npm", "install"]]
    assert any("npm install completed" in message for message in messages)


def test_attempt_npm_install_missing_npm(monkeypatch, tmp_path):
    project_root = tmp_path / "project"
    project_root.mkdir()
    (project_root / "package.json").write_text("{}", encoding="utf-8")

    monkeypatch.setattr(vite.shutil, "which", lambda name: None)

    logger, messages = make_logger()

    result = _attempt_npm_install(project_root, logger)

    assert result is False
    assert any("npm executable not found" in message for message in messages)


def test_attempt_npm_install_handles_failure(monkeypatch, tmp_path):
    project_root = tmp_path / "project"
    project_root.mkdir()
    (project_root / "package.json").write_text("{}", encoding="utf-8")

    def fake_which(name: str) -> str | None:
        return "/usr/bin/npm" if name == "npm" else None

    def fake_run(command, *, cwd, capture_output, text, check, start_new_session=None):  # noqa: ANN001
        return SimpleNamespace(returncode=1, stdout="", stderr="fatal")

    monkeypatch.setattr(vite.shutil, "which", fake_which)
    monkeypatch.setattr(vite.subprocess, "run", fake_run)

    logger, messages = make_logger()

    result = _attempt_npm_install(project_root, logger)

    assert result is False
    assert any("npm install" in message for message in messages)


def test_resolve_vite_command_prefers_local_vite(monkeypatch, tmp_path):
    project_root = tmp_path / "project"
    vite_js = project_root / "node_modules" / "vite" / "bin" / "vite.js"
    vite_js.parent.mkdir(parents=True)
    vite_js.write_text("", encoding="utf-8")

    def fake_which(name: str) -> str | None:
        return "/usr/local/bin/node" if name == "node" else None

    observed: list[list[str]] = []

    def fake_verify(command, root: Path) -> bool:
        observed.append(list(command))
        return True

    monkeypatch.setattr(vite.shutil, "which", fake_which)
    monkeypatch.setattr(vite, "_verify_command", fake_verify)
    monkeypatch.setattr(vite, "_attempt_npm_install", lambda *_: False)

    logger, _ = make_logger()

    command = _resolve_vite_command(project_root, logger)

    expected = ["/usr/local/bin/node", str(vite_js)]
    assert command == expected
    assert observed[0] == expected


def test_resolve_vite_command_installs_then_prefers_local_vite(monkeypatch, tmp_path):
    """When ``package.json`` exists but ``node_modules`` is absent, resolution
    runs ``npm install`` once and then prefers the freshly-installed *pinned*
    local vite over the unpinned ``npx`` fallback (keeps builds reproducible).
    """

    project_root = tmp_path / "project"
    project_root.mkdir()
    (project_root / "package.json").write_text("{}", encoding="utf-8")

    node_exec = "/usr/local/bin/node"
    vite_js = project_root / "node_modules" / "vite" / "bin" / "vite.js"

    def fake_which(name: str) -> str | None:
        # node is on PATH; npx exists too, but must NOT be chosen because the
        # local install resolves first.
        if name == "node":
            return node_exec
        if name == "npx":
            return "/usr/bin/npx"
        return None

    def fake_attempt(root: Path, logger: ConsoleLogger) -> bool:  # noqa: ARG001
        # Simulate ``npm install`` materialising the pinned local vite.
        vite_js.parent.mkdir(parents=True, exist_ok=True)
        vite_js.write_text("", encoding="utf-8")
        return True

    monkeypatch.setattr(vite.shutil, "which", fake_which)
    monkeypatch.setattr(vite, "_verify_command", lambda command, root: True)
    monkeypatch.setattr(vite, "_attempt_npm_install", fake_attempt)

    logger, _ = make_logger()

    command = _resolve_vite_command(project_root, logger)

    assert command == [node_exec, str(vite_js)]


def test_resolve_vite_command_falls_back_to_npx(monkeypatch, tmp_path):
    """With no local or global vite and nothing installable, ``npx --yes vite``
    is the last-resort path that keeps zero-config projects buildable."""

    project_root = tmp_path / "project"
    project_root.mkdir()  # no package.json -> the install tiers are skipped

    def fake_which(name: str) -> str | None:
        return "/usr/bin/npx" if name == "npx" else None

    monkeypatch.setattr(vite.shutil, "which", fake_which)
    monkeypatch.setattr(
        vite, "_verify_command", lambda command, root: command[:1] == ["/usr/bin/npx"]
    )
    monkeypatch.setattr(vite, "_attempt_npm_install", lambda *_: False)

    logger, _ = make_logger()

    command = _resolve_vite_command(project_root, logger)

    assert command == ["/usr/bin/npx", "--yes", "vite"]


def test_resolve_vite_command_raises_when_unavailable(monkeypatch, tmp_path):
    project_root = tmp_path / "project"
    project_root.mkdir()

    monkeypatch.setattr(vite.shutil, "which", lambda name: None)
    monkeypatch.setattr(vite, "_attempt_npm_install", lambda *_: False)

    logger, _ = make_logger()

    with pytest.raises(ViteBuildError):
        _resolve_vite_command(project_root, logger)


def test_resolve_npm_build_command_detects_script(monkeypatch, tmp_path):
    project_root = tmp_path / "project"
    project_root.mkdir()
    (project_root / "package.json").write_text(
        '{"scripts": {"build": "npm run build:css && vite build"}}',
        encoding="utf-8",
    )

    monkeypatch.setattr(vite.shutil, "which", lambda name: "/usr/bin/npm" if name == "npm" else None)
    # Avoid running npm install during the test
    monkeypatch.setattr(vite, "_attempt_npm_install", lambda *_: True)

    command = _resolve_npm_build_command(project_root, ConsoleLogger())
    assert command == ["/usr/bin/npm", "run", "build", "--"]


def test_resolve_npm_build_command_returns_none_without_script(monkeypatch, tmp_path):
    project_root = tmp_path / "project"
    project_root.mkdir()
    (project_root / "package.json").write_text("{}", encoding="utf-8")

    monkeypatch.setattr(vite.shutil, "which", lambda name: "/usr/bin/npm" if name == "npm" else None)

    command = _resolve_npm_build_command(project_root, ConsoleLogger())
    assert command is None


def test_verify_command_handles_missing_binary(monkeypatch, tmp_path):
    project_root = tmp_path / "project"
    project_root.mkdir()

    def fake_run(command, *, cwd, capture_output, check):  # noqa: ANN001, ARG001
        raise FileNotFoundError()

    monkeypatch.setattr(vite.subprocess, "run", fake_run)

    assert _verify_command(["vite"], project_root) is False


def test_verify_command_success(monkeypatch, tmp_path):
    project_root = tmp_path / "project"
    project_root.mkdir()

    def fake_run(command, *, cwd, capture_output, check):  # noqa: ANN001, ARG001
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(vite.subprocess, "run", fake_run)

    assert _verify_command(["vite"], project_root) is True


def test_verify_command_failure(monkeypatch, tmp_path):
    project_root = tmp_path / "project"
    project_root.mkdir()

    def fake_run(command, *, cwd, capture_output, check):  # noqa: ANN001, ARG001
        return SimpleNamespace(returncode=1)

    monkeypatch.setattr(vite.subprocess, "run", fake_run)

    assert _verify_command(["vite"], project_root) is False