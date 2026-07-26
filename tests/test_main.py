"""Tests for the ``python -m pyxle`` entry point."""

from __future__ import annotations

import subprocess
import sys

import pyxle.__main__ as main_module


def test_main_reexports_the_cli_app() -> None:
    """``pyxle.__main__`` exposes the same Typer app as the console script."""
    from pyxle.cli import app

    assert main_module.app is app


def test_main_invokes_the_app(monkeypatch) -> None:
    """``main()`` runs the CLI app (the callable behind ``python -m pyxle``)."""
    called: list[bool] = []
    monkeypatch.setattr(main_module, "app", lambda: called.append(True))

    main_module.main()

    assert called == [True]


def test_python_dash_m_pyxle_runs() -> None:
    """``python -m pyxle --help`` works — the VS Code debugger launches the CLI
    through this module target, so it must be runnable."""
    result = subprocess.run(
        [sys.executable, "-m", "pyxle", "--help"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0
    assert "pyxle" in result.stdout.lower()
