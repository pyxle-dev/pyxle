"""Tests for the CLI toolchain pre-flight checks."""

from __future__ import annotations

import dataclasses
from pathlib import Path
from types import SimpleNamespace

import pytest
import typer
from typer.testing import CliRunner

import pyxle.cli as cli
from pyxle.cli import app, preflight
from pyxle.cli.preflight import (
    NODE_FLOOR,
    ToolchainError,
    check_node,
    detect_node_version,
    node_floor_message,
    node_meets_floor,
)
from pyxle.config import PyxleConfig

runner = CliRunner()


def _fake_node(monkeypatch: pytest.MonkeyPatch, *, which: str | None, stdout: str) -> None:
    monkeypatch.setattr(preflight.shutil, "which", lambda name: which)
    monkeypatch.setattr(
        preflight.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(stdout=stdout, returncode=0),
    )


@pytest.mark.parametrize(
    ("stdout", "expected"),
    [
        ("v20.19.0\n", (20, 19, 0)),
        ("v24.9.0", (24, 9, 0)),
        ("20.18.1", (20, 18, 1)),
        ("v22.14.0\n", (22, 14, 0)),
    ],
)
def test_detect_node_version_parses(monkeypatch, stdout, expected) -> None:
    _fake_node(monkeypatch, which="/usr/bin/node", stdout=stdout)
    assert detect_node_version() == expected


def test_detect_node_version_missing_node(monkeypatch) -> None:
    monkeypatch.setattr(preflight.shutil, "which", lambda name: None)
    assert detect_node_version() is None


def test_detect_node_version_unparseable(monkeypatch) -> None:
    _fake_node(monkeypatch, which="/usr/bin/node", stdout="not a version")
    assert detect_node_version() is None


def test_detect_node_version_subprocess_error(monkeypatch) -> None:
    monkeypatch.setattr(preflight.shutil, "which", lambda name: "/usr/bin/node")

    def boom(*a, **k):
        raise OSError("exec failed")

    monkeypatch.setattr(preflight.subprocess, "run", boom)
    assert detect_node_version() is None


def test_detect_node_version_honors_explicit_executable(monkeypatch) -> None:
    # When an explicit path is given, PATH lookup is skipped.
    monkeypatch.setattr(
        preflight.shutil,
        "which",
        lambda name: pytest.fail("shutil.which should not be called"),
    )
    monkeypatch.setattr(
        preflight.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(stdout="v20.19.0", returncode=0),
    )
    assert detect_node_version("/opt/node/bin/node") == (20, 19, 0)


@pytest.mark.parametrize(
    ("version", "expected"),
    [
        ((20, 19, 0), True),
        ((20, 19, 5), True),
        ((20, 20, 0), True),
        ((21, 0, 0), True),
        ((24, 9, 0), True),
        ((20, 18, 9), False),
        ((20, 16, 0), False),
        ((19, 99, 0), False),
        (None, False),
    ],
)
def test_node_meets_floor(version, expected) -> None:
    assert node_meets_floor(version) is expected


def test_node_floor_message_missing_mentions_floor_and_install() -> None:
    message = node_floor_message(None)
    assert f"{NODE_FLOOR[0]}.{NODE_FLOOR[1]}" in message
    assert "not found" in message
    assert "nodejs.org" in message


def test_node_floor_message_old_mentions_current_and_floor() -> None:
    message = node_floor_message((20, 16, 0))
    assert "20.16.0" in message
    assert f"{NODE_FLOOR[0]}.{NODE_FLOOR[1]}" in message


def test_check_node_ok(monkeypatch) -> None:
    monkeypatch.setattr(preflight, "detect_node_version", lambda: (20, 19, 0))
    assert check_node() is True


def test_check_node_required_raises_when_old(monkeypatch) -> None:
    monkeypatch.setattr(preflight, "detect_node_version", lambda: (20, 16, 0))
    with pytest.raises(ToolchainError) as excinfo:
        check_node(required=True)
    assert "20.16.0" in str(excinfo.value)


def test_check_node_required_raises_when_missing(monkeypatch) -> None:
    monkeypatch.setattr(preflight, "detect_node_version", lambda: None)
    with pytest.raises(ToolchainError):
        check_node(required=True)


def test_check_node_warns_when_not_required(monkeypatch) -> None:
    monkeypatch.setattr(preflight, "detect_node_version", lambda: (20, 16, 0))
    warnings: list[str] = []
    logger = SimpleNamespace(warning=warnings.append)
    result = check_node(required=False, logger=logger)
    assert result is False
    assert len(warnings) == 1
    assert "20.16.0" in warnings[0]


def test_check_node_not_required_ok_is_silent(monkeypatch) -> None:
    monkeypatch.setattr(preflight, "detect_node_version", lambda: (22, 0, 0))
    warnings: list[str] = []
    logger = SimpleNamespace(warning=warnings.append)
    assert check_node(required=False, logger=logger) is True
    assert warnings == []


# --- CLI wiring: the gate translates a ToolchainError into a clean exit -------


def test_dev_exits_when_toolchain_below_floor(monkeypatch) -> None:
    def raise_old(*, required=True, logger=None):
        if required:
            raise ToolchainError("Node.js 20.19+ is required, but 20.16.0 is installed.")
        return False

    # Override the autouse fixture's permissive stub for this test.
    monkeypatch.setattr(cli, "check_node", raise_old)

    with runner.isolated_filesystem():
        Path("demo/pages").mkdir(parents=True)
        result = runner.invoke(app, ["dev", "demo"], catch_exceptions=False)

    assert result.exit_code == 1
    assert "20.19+ is required" in result.output


def test_build_exits_when_toolchain_below_floor(monkeypatch) -> None:
    def raise_missing(*, required=True, logger=None):
        if required:
            raise ToolchainError("Node.js 20.19+ is required but was not found on your PATH.")
        return False

    monkeypatch.setattr(cli, "check_node", raise_missing)

    with runner.isolated_filesystem():
        Path("demo/pages").mkdir(parents=True)
        result = runner.invoke(app, ["build", "demo"], catch_exceptions=False)

    assert result.exit_code == 1
    assert "not found on your PATH" in result.output


# --- Production secret gate (`pyxle serve` refuses to boot without a key) -------


def _fake_logger() -> SimpleNamespace:
    errors: list[str] = []
    return SimpleNamespace(error=errors.append, errors=errors)


def test_require_production_secret_raises_without_key(monkeypatch) -> None:
    monkeypatch.delenv("PYXLE_SECRET_KEY", raising=False)
    logger = _fake_logger()
    with pytest.raises(typer.Exit) as excinfo:
        cli._require_production_secret(PyxleConfig(), logger)
    assert excinfo.value.exit_code == 1
    assert any("PYXLE_SECRET_KEY is not set" in m for m in logger.errors)


def test_require_production_secret_passes_with_key(monkeypatch) -> None:
    monkeypatch.setenv("PYXLE_SECRET_KEY", "x" * 32)
    logger = _fake_logger()
    cli._require_production_secret(PyxleConfig(), logger)  # must not raise
    assert logger.errors == []


def test_require_production_secret_skipped_when_csrf_disabled(monkeypatch) -> None:
    monkeypatch.delenv("PYXLE_SECRET_KEY", raising=False)
    config = PyxleConfig()
    disabled_csrf = dataclasses.replace(config.csrf, enabled=False)
    config = dataclasses.replace(config, csrf=disabled_csrf)
    logger = _fake_logger()
    cli._require_production_secret(config, logger)  # must not raise
    assert logger.errors == []


def test_serve_exits_without_secret_key(monkeypatch) -> None:
    monkeypatch.delenv("PYXLE_SECRET_KEY", raising=False)
    with runner.isolated_filesystem():
        Path("demo/pages").mkdir(parents=True)
        result = runner.invoke(
            app, ["serve", "demo", "--skip-build"], catch_exceptions=False
        )
    assert result.exit_code == 1
    assert "PYXLE_SECRET_KEY is not set" in result.output
