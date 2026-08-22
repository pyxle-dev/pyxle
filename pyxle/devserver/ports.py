"""Deciding whether a TCP port is free, and saying so usefully when it is not.

An occupied port is the most common way a dev server fails to start, and it was
the least legible failure we shipped. uvicorn's own
``[Errno 98] error while attempting to bind ... address already in use`` arrived
with no framework voice, no remedy, and no suggestion of a port that *would*
work. Worse, it arrived at the wrong moment in both commands:

* ``pyxle dev`` had already spawned Vite, so the **last** line on screen was
  ``[vite] process exited with code 143`` — the SIGTERM of an innocent child
  during teardown. The eye lands on the last line, and it named the one
  component that had done nothing wrong.
* ``pyxle serve`` had already rebuilt the project, and had already printed
  ``Serving Pyxle build on http://host:port`` — a success line, with a URL the
  developer could click, for a server that never bound.

So the check happens *first*, before Vite starts and before anything is built:
a failure that costs nothing to detect should not cost a rebuild to discover.

Stdlib only, deliberately — this is imported on the startup path of every
command that binds a socket.
"""

from __future__ import annotations

import socket
import sys
from typing import Final

#: How far above the requested port to look for a free one to suggest.
_SUGGESTION_SEARCH_LIMIT: Final[int] = 20

#: How long to wait for a connection probe before calling the port free. A
#: listener on the loopback or LAN answers far inside this.
_PROBE_TIMEOUT_SECONDS: Final[float] = 0.1


def is_port_available(host: str, port: int) -> bool:
    """Whether nothing is currently listening on ``host:port``.

    Probes by connecting rather than binding: binding to test would either race
    with the real bind moments later, or (with ``SO_REUSEADDR``) succeed against
    a socket in ``TIME_WAIT`` that a server could still legitimately use.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(_PROBE_TIMEOUT_SECONDS)
        return sock.connect_ex((host, port)) != 0


def find_free_port(
    host: str, start: int, limit: int = _SUGGESTION_SEARCH_LIMIT
) -> int | None:
    """The first free port at or above ``start``, or ``None`` if there is none.

    Used only to *suggest* a port. Nothing is reserved — by the time the
    developer runs the suggested command the port could be taken again, which is
    no worse than the guess they would otherwise make themselves.
    """
    for candidate in range(start, start + limit):
        if candidate > 65535:
            return None
        if is_port_available(host, candidate):
            return candidate
    return None


def _holder_hint(port: int) -> str:
    """A command that names the process holding ``port``, for this platform."""
    if sys.platform.startswith("win"):
        return f"netstat -ano | findstr :{port}"
    return f"lsof -i :{port}"


def port_in_use_message(host: str, port: int, *, command: str) -> str:
    """Explain that ``host:port`` is taken, and what to do about it.

    ``command`` is the command the developer ran (``"pyxle dev"``), so the
    remedy is a line they can paste rather than a flag they have to place.
    """
    lines = [
        f"Port {port} is already in use, so {command} cannot start.",
        "",
        f"Something is already listening on {host}:{port} — most often a "
        f"{command} from an earlier session that is still running, or another "
        "application using the same port.",
    ]

    free_port = find_free_port(host, port + 1)
    if free_port is not None:
        lines += ["", f"Start on a free port:   {command} --port {free_port}"]

    lines += [f"See what is holding it: {_holder_hint(port)}"]
    return "\n".join(lines)
