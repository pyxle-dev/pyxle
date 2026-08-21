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


#: pytest-cov's subprocess hook, plus coverage's own multiprocessing hook.
_CHILD_COVERAGE_ENV = (
    "COV_CORE_SOURCE",
    "COV_CORE_CONFIG",
    "COV_CORE_DATAFILE",
    "COV_CORE_BRANCH",
    "COV_CORE_CONTEXT",
    "COVERAGE_PROCESS_START",
)


@pytest.fixture
def uninstrumented_child(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep a subprocess spawned with a foreign ``cwd`` out of the coverage data.

    pytest-cov instruments every subprocess via a ``.pth`` hook driven by
    ``COV_CORE_*``. It does **not** pass the parent's branch setting down —
    ``pytest_cov.embed.init`` only enables branch coverage when
    ``COV_CORE_BRANCH=enabled``, which pytest-cov does not set — so the child
    falls back to *discovering* a config file relative to its **current
    directory**. A child that inherits the repo root finds ``pyproject.toml``,
    picks up ``branch = true``, and merges cleanly. A child launched with
    ``cwd=tmp_path`` finds nothing, measures statements only, and writes a
    statement-only file next to the parent's branch data — at which point
    ``coverage combine`` refuses the merge and the run dies with an
    ``INTERNALERROR`` *after* every test has passed. That reads as flaky
    infrastructure rather than as one test's doing, which is the expensive way
    to fail, and it only shows up in a run that reaches the combine step.

    So: use this in any test that spawns a real process from a temp directory
    which executes **no** framework code — a stand-in binary, a scripted stub.
    Nothing measurable is lost by leaving such a child uninstrumented. A
    subprocess that genuinely runs ``pyxle`` code must stay instrumented; give
    it the project root as its ``cwd`` instead, so it discovers the same
    config the parent uses.
    """

    for name in _CHILD_COVERAGE_ENV:
        monkeypatch.delenv(name, raising=False)
