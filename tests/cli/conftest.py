"""Shared fixtures for CLI command tests."""

from __future__ import annotations

import pytest

import pyxle.cli as cli


@pytest.fixture(autouse=True)
def _satisfy_node_toolchain(monkeypatch: pytest.MonkeyPatch) -> None:
    """Neutralize the Node.js pre-flight so CLI tests don't depend on the runner.

    The real check shells out to ``node --version``; unit tests must not depend
    on the runner's installed Node version (CLAUDE.md rule 28 — mock Node).
    Tests that specifically exercise the gate re-patch ``cli.check_node``.
    """

    monkeypatch.setattr(
        cli, "check_node", lambda *, required=True, logger=None: True
    )


@pytest.fixture(autouse=True)
def _provide_production_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    """Provide a signing secret so ``pyxle serve`` tests pass the production gate.

    Production serving refuses to start without ``PYXLE_SECRET_KEY``; tests that
    exercise that gate delete the variable themselves.
    """

    monkeypatch.setenv("PYXLE_SECRET_KEY", "test-secret-key")
