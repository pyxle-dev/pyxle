"""Toolchain pre-flight checks for Pyxle CLI commands.

Pyxle drives Vite 7 and renders React on the server through Node.js. When the
local Node.js is missing or older than the version Vite 7 requires, the failure
otherwise surfaces much later as an opaque Vite/esbuild crash — often *after* a
green "ready" banner — which reads as a broken framework rather than a stale
toolchain. These checks fail fast, up front, with a single actionable message.

The Python floor is enforced by ``requires-python`` at install time (pip refuses
to install Pyxle on an unsupported interpreter), so there is nothing to re-check
here at runtime; only the Node.js toolchain needs a CLI-side gate.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from typing import Optional, Protocol

#: Minimum supported Node.js version, as ``(major, minor)``. This is the single
#: source of truth for the CLI gate and must stay in sync with the scaffold
#: template's ``package.json`` ``engines.node`` field and the documented floor.
NODE_FLOOR: tuple[int, int] = (20, 19)

#: Minimum supported Python version, as ``(major, minor)``. Enforced by
#: ``requires-python`` in ``pyproject.toml``; recorded here for messaging/tests.
PYTHON_FLOOR: tuple[int, int] = (3, 10)

_NODE_VERSION_RE = re.compile(r"v?(\d+)\.(\d+)\.(\d+)")


class ToolchainError(RuntimeError):
    """Raised when a required toolchain component is missing or too old."""


class _Logger(Protocol):
    def warning(self, message: str) -> None: ...


def _format_floor(floor: tuple[int, int]) -> str:
    return f"{floor[0]}.{floor[1]}"


def detect_node_version(
    node_exec: Optional[str] = None,
) -> Optional[tuple[int, int, int]]:
    """Return the local Node.js version as ``(major, minor, patch)``.

    Returns ``None`` when Node.js is not installed or its ``--version`` output
    cannot be parsed. Never raises — a missing or misbehaving Node is reported
    as ``None`` so callers can decide whether that is fatal.
    """

    executable = node_exec or shutil.which("node")
    if executable is None:
        return None
    try:
        result = subprocess.run(
            [executable, "--version"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    match = _NODE_VERSION_RE.search(result.stdout.strip())
    if match is None:
        return None
    return (int(match.group(1)), int(match.group(2)), int(match.group(3)))


def node_floor_message(version: Optional[tuple[int, int, int]]) -> str:
    """Build the actionable message shown when the Node.js floor is not met."""

    floor = _format_floor(NODE_FLOOR)
    if version is None:
        return (
            f"Node.js {floor}+ is required but was not found on your PATH.\n"
            "  Pyxle uses Node.js to run Vite and render React on the server.\n"
            "  Install it from https://nodejs.org (or via nvm / fnm), then re-run this command."
        )
    current = ".".join(str(part) for part in version)
    return (
        f"Node.js {floor}+ is required, but {current} is installed.\n"
        f"  Pyxle's Vite 7 build stack needs Node.js {floor} or newer; older versions crash at startup.\n"
        "  Upgrade Node.js (https://nodejs.org, or `nvm install 20` / `fnm install 20`), then re-run this command."
    )


def node_meets_floor(version: Optional[tuple[int, int, int]]) -> bool:
    """Return whether a detected Node.js version satisfies :data:`NODE_FLOOR`."""

    return version is not None and version[:2] >= NODE_FLOOR


def check_node(*, required: bool = True, logger: Optional[_Logger] = None) -> bool:
    """Verify the local Node.js version meets Pyxle's supported floor.

    When ``required`` is ``True`` and Node.js is missing or below the floor,
    raises :class:`ToolchainError` with an actionable message. When ``required``
    is ``False``, logs the same message as a warning (if a ``logger`` is given)
    and returns without raising. Returns ``True`` when the toolchain is
    acceptable, ``False`` otherwise.
    """

    version = detect_node_version()
    if node_meets_floor(version):
        return True

    message = node_floor_message(version)
    if required:
        raise ToolchainError(message)
    if logger is not None:
        logger.warning(message)
    return False
