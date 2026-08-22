"""Every module has to be importable on its own, from a cold interpreter.

``pyxle.devserver`` and ``pyxle.ssr`` import each other: the dev server builds
the Starlette app out of SSR, and SSR reaches back for ``dev_origins`` and
``error_pages``. While ``pyxle/devserver/__init__.py`` pulled in
``starlette_app`` at module scope that cycle was real, and entering it from the
SSR side raised — ``import pyxle.ssr.view`` and ``import pyxle.ssr.template``
both failed on a partially initialised module.

It stayed invisible because ``import pyxle`` and the CLI both enter from the
other side, so nothing we run day to day ever tripped it. What it broke was a
contributor importing one module to look at it, and any tool that does the same.

These run in a **subprocess** on purpose: inside the pytest process the modules
are already in ``sys.modules``, and the cycle only bites on a cold import.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

#: One per edge of the cycle, plus the modules either side of it.
STANDALONE_MODULES = [
    "pyxle.ssr.view",
    "pyxle.ssr.template",
    "pyxle.ssr.renderer",
    "pyxle.ssr.worker_pool",
    "pyxle.devserver",
    "pyxle.devserver.starlette_app",
    "pyxle.devserver.error_pages",
]


@pytest.mark.parametrize("module", STANDALONE_MODULES)
def test_module_imports_standalone_in_a_cold_interpreter(module: str) -> None:
    result = subprocess.run(
        [sys.executable, "-c", f"import {module}"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"`import {module}` failed in a fresh interpreter:\n{result.stderr}"
    )
