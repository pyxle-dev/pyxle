"""Which browser origins a Pyxle dev server may serve its client assets to.

``pyxle dev`` runs two HTTP servers on one machine: Pyxle serves the HTML
document, Vite serves the JavaScript modules that document loads. Same host,
different port is still a *different origin*, so every ``<script type="module">``
on the page is a cross-origin request that Vite answers only when the requesting
origin is on its CORS allow-list. Vite's own default list is loopback only
(``defaultAllowedOrigins`` — ``/^https?:\\/\\/(?:(?:[^:]+\\.)?localhost|127\\.0\\.0\\.1|\\[::1\\])(?::\\d+)?$/``,
introduced in Vite 6.0.9). The moment a developer runs ``pyxle dev --host
0.0.0.0`` and opens the page from a phone or another laptop, the document
renders perfectly and every module request is blocked: a complete page that
never hydrates.

This module is the single definition of "an origin this dev server may serve".
Every place that has to answer the question reads it, so the answer cannot
drift between them:

* :mod:`pyxle.devserver.client_files` — writes ``server.cors`` and
  ``server.allowedHosts`` into the generated ``vite.config.js``, and resolves
  the coordinates that config describes (:func:`active_dev_session`).
* :mod:`pyxle.devserver.starlette_app` — builds Pyxle's own dev-mode CORS
  middleware for the mirror-image direction (Vite's origin calling Pyxle), and
  the allow-list the dev overlay WebSocket accepts
  (:func:`websocket_origins`).
* :mod:`pyxle.ssr.template` — picks the host the ``<script src>`` points at.
* :mod:`pyxle.ssr.view` — warns when a document is served to an origin Vite
  will refuse (:func:`unhydratable_origin_warning`).
* :mod:`pyxle.devserver` — the startup banner and the unreachable-Vite warning.

**Why not ``cors: true``.** A dev server that answers every origin lets any page
the developer happens to have open — an ad iframe, a malicious tab left in
another window — read the source of the project they are working on, because the
browser will hand that page the response body. What we allow instead is exactly
the set of origins the dev server can itself be reached at: loopback, or, when
it is bound to every interface, private-network addresses **on Pyxle's own
port**. A public website can hold neither, so it never gets a
``Access-Control-Allow-Origin`` header. The same reasoning governs the dev-only
WebSocket endpoints, which hand out source paths and stack traces.

**One running dev server, one answer.** The policy above is a function of the
addresses ``pyxle dev`` was started on — which the *other* commands
(``pyxle routes``, ``pyxle check``, ``pyxle build``…) know nothing about. They
regenerate the same client files from the config file's defaults, and a
regenerated ``vite.config.js`` is a config change Vite acts on: it restarts and
adopts it. Narrowing it back to loopback kills every remote browser attached to
the running server, silently. So the record ``pyxle dev`` already writes for
editor tooling (``<build_root>/dev-server.json``) is also the answer to "which
dev server is this project's client config for" — see :func:`active_dev_session`.
"""

from __future__ import annotations

import json
import os
import re
import socket
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

#: Bind addresses that mean "every interface". Valid for ``listen(2)``; a
#: browser cannot connect to any of them.
ALL_INTERFACES: Final[tuple[str, ...]] = ("0.0.0.0", "::", "")

#: Bind addresses that mean "this machine only".
LOOPBACK_HOSTS: Final[tuple[str, ...]] = ("localhost", "127.0.0.1", "::1")

#: ``vite.host`` as it ships in :class:`pyxle.config.PyxleConfig`. A value equal
#: to this is a framework default nobody chose, which is what makes it safe to
#: widen (see :func:`resolve_vite_bind_host`).
DEFAULT_VITE_HOST: Final[str] = "127.0.0.1"

#: Vite's own ``defaultAllowedOrigins`` (Vite 6.0.9+): loopback on any port,
#: including ``*.localhost`` subdomains and the IPv6 literal. Regex source valid
#: in both Python's :mod:`re` and JavaScript's ``RegExp``.
#:
#: It appears here rather than only in the generated config because two callers
#: need the same fact: the writer restates it (setting ``server.cors.origin``
#: *replaces* Vite's default, so an unrestated default would silently drop the
#: developer's own browser), and :func:`vite_serves_origin` has to predict what
#: Vite will do with an origin Pyxle did not list.
VITE_DEFAULT_ORIGIN_PATTERN: Final[str] = (
    r"^https?://(?:(?:[^:]+\.)?localhost|127\.0\.0\.1|\[::1\])(?::\d+)?$"
)

#: The record ``pyxle dev`` writes under ``<build_root>/`` describing the server
#: it is running. Consumed by editor tooling (the VS Code extension) and by
#: :func:`active_dev_session`.
DEV_SESSION_FILENAME: Final[str] = "dev-server.json"


def forwarded_scheme(scope: Any) -> str:
    """The scheme the *client* used, not the one our socket saw.

    Reads ``X-Forwarded-Proto`` first, because the overwhelmingly common
    production shape is a TLS-terminating proxy speaking plain HTTP to the app:
    the ASGI scheme says ``http`` while the browser's connection was HTTPS all
    along.

    We do this ourselves rather than leaning on uvicorn's proxy-header support,
    which only rewrites the scheme when the peer address is in
    ``forwarded_allow_ips`` -- ``127.0.0.1`` by default. That covers a proxy on
    the same host and silently does nothing for the equally ordinary shape of
    an nginx / load balancer / ingress on a different host or container, where
    the header arrives and is ignored.
    """
    for name, value in scope.get("headers", ()):
        if name == b"x-forwarded-proto":
            first = value.decode("latin-1").split(",")[0].strip().lower()
            return "https" if first == "https" else "http"
    scheme = str(scope.get("scheme", "")).lower()
    return "https" if scheme in ("https", "wss") else "http"


def request_is_https(scope: Any) -> bool:
    """Whether the client's connection was TLS -- see :func:`forwarded_scheme`."""
    return forwarded_scheme(scope) == "https"


def is_wildcard_host(host: str) -> bool:
    """Whether ``host`` binds every interface rather than a named address."""

    return host in ALL_INTERFACES


def is_loopback_host(host: str) -> bool:
    """Whether ``host`` binds this machine only."""

    return host in LOOPBACK_HOSTS


def is_off_box_host(host: str) -> bool:
    """Whether a server bound to ``host`` answers requests from other machines.

    A wildcard bind counts: ``0.0.0.0`` answers on every interface, including
    the LAN one.
    """

    return not is_loopback_host(host)


def local_ipv4_addresses() -> tuple[str, ...]:
    """Best-effort list of this machine's own non-loopback IPv4 addresses.

    Used only to *name* a URL in terminal output — never to decide what is
    allowed — so a partial or empty answer degrades the message, not the
    behaviour. Two sources, because either alone is commonly wrong: resolving
    the hostname misses machines whose name maps to ``127.0.1.1`` (Debian's
    default ``/etc/hosts``), and the routing-table probe returns only the one
    address that would carry outbound traffic.
    """

    addresses: list[str] = []

    def _add(candidate: str) -> None:
        if candidate and candidate not in addresses and not candidate.startswith("127."):
            addresses.append(candidate)

    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            _add(info[4][0])
    except (OSError, UnicodeError):  # pragma: no cover - depends on host DNS
        pass

    # Ask the routing table which local address would be used to reach the
    # outside world. TEST-NET-1 is reserved and never routed, and a UDP
    # ``connect`` sends no packets — this is a local lookup, not network I/O.
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("192.0.2.1", 1))
            _add(probe.getsockname()[0])
    except OSError:  # pragma: no cover - no route configured
        pass

    return tuple(addresses)


def private_origin_pattern(*ports: int) -> str:
    """Regex source matching loopback and RFC 1918 origins on ``ports``.

    Valid in both Python's :mod:`re` and JavaScript's ``RegExp``, because the
    same policy is enforced by Pyxle's CORS middleware and by the generated
    ``vite.config.js``. Anchored at both ends and pinned to the given ports: an
    attacker-controlled site is neither on a private address nor on a port the
    developer's own dev server occupies.
    """

    port_alternation = "|".join(str(port) for port in ports)
    return (
        r"^https?://(?:localhost|127\.0\.0\.1"
        r"|10\.\d{1,3}\.\d{1,3}\.\d{1,3}"
        r"|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}"
        rf"|192\.168\.\d{{1,3}}\.\d{{1,3}}):(?:{port_alternation})$"
    )


def allowed_origins(host: str, port: int) -> tuple[tuple[str, ...], str | None]:
    """Origins a server reachable at ``host:port`` may serve cross-origin.

    Returns ``(exact_origins, pattern)`` where ``pattern`` is a
    :func:`private_origin_pattern` source or ``None``. Callers that can only
    express one of the two (Vite takes both; a plain list is enough for a named
    host) use whichever parts apply.

    * A wildcard bind is reachable under many names, so loopback is listed
      exactly and the private-network ranges are covered by the pattern.
    * A loopback bind is reachable as ``localhost`` and ``127.0.0.1``, which
      browsers treat as distinct origins — both are listed.
    * A named host is reachable as exactly itself.
    """

    if is_wildcard_host(host):
        return (
            (f"http://localhost:{port}", f"http://127.0.0.1:{port}"),
            private_origin_pattern(port),
        )
    if is_loopback_host(host):
        return ((f"http://localhost:{port}", f"http://127.0.0.1:{port}"), None)
    return ((f"http://{host}:{port}",), None)


def websocket_origins(
    *, starlette_host: str, starlette_port: int, vite_port: int
) -> tuple[tuple[str, ...], str | None]:
    """Origins the dev-only WebSocket endpoints may accept.

    ``(exact_origins, pattern)``, same shape as :func:`allowed_origins`. The dev
    overlay socket is opened from the page's own origin, so the allow-list has
    to be the one the page was served to — the very origins Vite is told to
    serve modules to. A dev server reachable off-box that then refuses the
    overlay leaves a remote browser with no hot reload and a build-failure page
    that promises to reload itself and never does.

    Both ports are covered: the document usually comes from Pyxle, but a page
    opened directly on Vite's port is still this developer's own dev server.

    Not "any origin": these sockets stream source paths, stack traces and
    forwarded server logs. Any page the developer has open would be able to
    read them.
    """

    exact: list[str] = []
    for port in (starlette_port, vite_port):
        for origin in allowed_origins(starlette_host, port)[0]:
            if origin not in exact:
                exact.append(origin)
    pattern = (
        private_origin_pattern(starlette_port, vite_port)
        if is_wildcard_host(starlette_host)
        else None
    )
    return tuple(exact), pattern


def vite_serves_origin(
    origin: str, *, starlette_host: str, starlette_port: int
) -> bool:
    """Whether Vite will answer module requests from ``origin``.

    Predicts the effective allow-list of the ``vite.config.js`` Pyxle generates
    for this dev server: Vite's own loopback default (which the generated config
    restates rather than replaces), plus the origins Pyxle adds for a server
    that answers off-box. A ``False`` here is a page that will render and never
    hydrate — see :func:`unhydratable_origin_warning`.
    """

    if re.match(VITE_DEFAULT_ORIGIN_PATTERN, origin):
        return True
    if not is_off_box_host(starlette_host):
        return False
    exact, pattern = allowed_origins(starlette_host, starlette_port)
    if origin in exact:
        return True
    return bool(pattern and re.match(pattern, origin))


def allowed_hostnames(*hosts: str) -> tuple[str, ...]:
    """Named (non-IP) hosts among ``hosts``, for Vite's ``server.allowedHosts``.

    Vite's host-header check already accepts any IP literal — DNS rebinding
    needs a name to rebind — plus ``localhost`` and its subdomains. Only a real
    hostname such as ``dev.internal`` or ``my-laptop.local`` has to be declared,
    so this returns just those and lets the default cover everything else.
    """

    names: list[str] = []
    for host in hosts:
        if is_wildcard_host(host) or is_loopback_host(host):
            continue
        if _is_ip_literal(host):
            continue
        if host not in names:
            names.append(host)
    return tuple(names)


def _is_ip_literal(host: str) -> bool:
    """Whether ``host`` is an IPv4/IPv6 literal rather than a name."""

    bare = host[1:-1] if host.startswith("[") and host.endswith("]") else host
    for family in (socket.AF_INET, socket.AF_INET6):
        try:
            socket.inet_pton(family, bare)
        except (OSError, ValueError):
            continue
        return True
    return False


@dataclass(frozen=True, slots=True)
class DevSession:
    """The network coordinates of the dev server running for a project.

    Read back from ``<build_root>/dev-server.json``. Only the four addresses the
    generated client config is a function of; everything else in that file
    belongs to editor tooling.
    """

    pid: int
    starlette_host: str
    starlette_port: int
    vite_host: str
    vite_port: int


def active_dev_session(build_root: Path) -> DevSession | None:
    """The dev server another process is running for ``build_root``, if any.

    ``None`` — meaning "nothing to defer to, this process's own settings are the
    truth" — for all of: no record, an unreadable or malformed one, a record
    this process wrote itself, and a record whose process is gone (a crashed or
    ``kill -9``'d dev server leaves its file behind).

    A live record is what stops a second command from silently reconfiguring a
    running server. ``pyxle routes`` in a project whose dev server is on
    ``0.0.0.0`` knows only the config file's ``127.0.0.1`` default; writing that
    into ``vite.config.js`` makes Vite restart onto a loopback-only allow-list,
    and every browser that is not on this machine gets a page that renders, does
    not hydrate, and reports nothing anywhere. The generated config describes
    *the running server*, so whoever regenerates it asks here first.

    Never raises: a dev-server record is a convenience, and failing to read one
    must not fail a build.
    """

    path = Path(build_root) / DEV_SESSION_FILENAME
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None

    pid = payload.get("pid")
    if not isinstance(pid, int) or isinstance(pid, bool):
        return None
    if pid == os.getpid():
        # Our own record. The caller is the dev server (or its watcher thread);
        # its settings are authoritative and are already what we would return.
        return None
    if not _process_is_alive(pid):
        return None

    server = payload.get("server")
    vite = payload.get("vite")
    starlette_host = _session_host(server)
    starlette_port = _session_port(server)
    vite_host = _session_host(vite)
    vite_port = _session_port(vite)
    if None in (starlette_host, starlette_port, vite_host, vite_port):
        return None
    return DevSession(
        pid=pid,
        starlette_host=starlette_host,  # type: ignore[arg-type]
        starlette_port=starlette_port,  # type: ignore[arg-type]
        vite_host=vite_host,  # type: ignore[arg-type]
        vite_port=vite_port,  # type: ignore[arg-type]
    )


def _session_host(entry: Any) -> str | None:
    """The ``host`` of a ``dev-server.json`` endpoint entry, if it has one."""

    if not isinstance(entry, dict):
        return None
    host = entry.get("host")
    # An empty string is a real bind address (every interface), so only the
    # type is checked.
    return host if isinstance(host, str) else None


def _session_port(entry: Any) -> int | None:
    """The ``port`` of a ``dev-server.json`` endpoint entry, if it has one."""

    if not isinstance(entry, dict):
        return None
    port = entry.get("port")
    if isinstance(port, bool) or not isinstance(port, int):
        return None
    return port if 0 < port < 65536 else None


def _process_is_alive(pid: int) -> bool:
    """Whether ``pid`` names a process that still exists.

    POSIX asks the kernel with the null signal. Windows cannot: ``os.kill``
    there maps signal ``0`` onto ``CTRL_C_EVENT``, so the "harmless" probe would
    interrupt the very dev server it is asking about — it opens a query handle
    instead.
    """

    if pid <= 0:
        return False
    if os.name == "nt":  # pragma: no cover - Windows-only branch
        return _windows_process_is_alive(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # It exists; it just belongs to another user.
        return True
    except OSError:  # pragma: no cover - defensive
        return False
    return True


def _windows_process_is_alive(pid: int) -> bool:  # pragma: no cover - Windows-only
    """Whether ``pid`` is a live process, via ``OpenProcess``/``GetExitCodeProcess``."""

    import ctypes  # noqa: PLC0415 - Windows-only dependency

    process_query_limited_information = 0x1000
    still_active = 259
    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
    if not handle:
        return False
    try:
        exit_code = ctypes.c_ulong()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return True
        return exit_code.value == still_active
    finally:
        kernel32.CloseHandle(handle)


def resolve_vite_bind_host(starlette_host: str, vite_host: str) -> str:
    """The address Vite should bind so the page it feeds is not born dead.

    A Pyxle server that answers off-box hands out documents whose scripts come
    from Vite. If Vite is still on loopback those documents can never hydrate
    for anyone but the developer's own machine — a page that renders and does
    nothing, which is worse than a page that fails.

    So a Vite host that is *still the framework default* follows Pyxle onto the
    same address. A host the developer actually named — including ``localhost``
    — is left exactly as written; that is a choice, and the dev server warns
    about it (:func:`vite_reachability_warning`) instead of overruling it.
    """

    if vite_host == DEFAULT_VITE_HOST and is_off_box_host(starlette_host):
        return starlette_host
    return vite_host


def browser_vite_host(
    *,
    vite_host: str,
    starlette_host: str,
    request_host: str | None = None,
) -> str:
    """A host the *browser* can use to reach Vite.

    ``0.0.0.0`` is a bind address, not a destination — a ``<script src>``
    pointing at it never loads. When Vite binds every interface it answers under
    every name this machine has, so the honest answer is the name the browser
    already used to reach Pyxle: a page served at ``http://192.168.1.11:3000``
    loads its scripts from ``http://192.168.1.11:5173``, and one served at
    ``http://localhost:3000`` (a port-mapped container, say) still loads them
    from ``localhost``. Without a request to read, fall back to Pyxle's own bind
    host and finally to ``localhost``.
    """

    if not is_wildcard_host(vite_host):
        return vite_host
    if request_host:
        # ``Request.url.hostname`` strips the brackets from an IPv6 literal;
        # a URL needs them back or the address runs into the port separator.
        if ":" in request_host and not request_host.startswith("["):
            return f"[{request_host}]"
        return request_host
    if not is_wildcard_host(starlette_host):
        return starlette_host
    return "localhost"


def vite_reachability_warning(
    *,
    starlette_host: str,
    starlette_port: int,
    vite_host: str,
    vite_port: int,
) -> str | None:
    """A terminal warning when Pyxle is reachable where Vite is not.

    Returns ``None`` when every address that can reach Pyxle can also reach
    Vite. Otherwise returns a message naming a URL that will render a complete
    page and never hydrate — the failure mode that is otherwise completely
    silent, in the browser as well as the terminal.
    """

    if not is_off_box_host(starlette_host):
        return None  # Both servers are loopback-only; they share one machine.
    if is_wildcard_host(vite_host):
        return None  # Vite answers everywhere Pyxle does.
    if vite_host == starlette_host:
        return None  # Same named interface.
    if is_off_box_host(vite_host) and not is_wildcard_host(starlette_host):
        return None  # Both pinned off-box; assume the developer wired it.

    if is_wildcard_host(starlette_host):
        lan = local_ipv4_addresses()
        example = lan[0] if lan else "your-lan-address"
    else:
        example = starlette_host

    return (
        f"Vite is bound to {vite_host}:{vite_port}, but this server answers on "
        f"{starlette_host}:{starlette_port}. Pages opened at "
        f"http://{example}:{starlette_port}/ will render and then never become "
        f"interactive — their scripts are served from a host only this machine "
        f"can reach. Start with --vite-host {starlette_host} (or set "
        f'"vite": {{"host": "{starlette_host}"}} in pyxle.config.json) to serve '
        f"them too."
    )


def unhydratable_origin_warning(
    *,
    document_origin: str,
    starlette_host: str,
    starlette_port: int,
    vite_port: int,
) -> str | None:
    """A terminal warning for a document served to an origin Vite will refuse.

    Returns ``None`` when the browser that just fetched a page can also fetch
    its modules. Otherwise returns a message naming that browser's origin.

    This is the last stop for the framework's most silent failure. A blocked
    module leaves the document intact — correct markup, correct styles, correct
    text — and every interactive part of it dead. The browser reports a CORS
    refusal to nothing but its own network panel; Vite answers ``200`` and logs
    nothing, because from its side nothing went wrong. Pyxle is the only party
    that sees both halves: it knows the origin it just served the document to,
    and it knows the allow-list it wrote for Vite.
    """

    if vite_serves_origin(
        document_origin, starlette_host=starlette_host, starlette_port=starlette_port
    ):
        return None
    return (
        f"This page was served to {document_origin}, an origin the Vite dev "
        f"server on port {vite_port} will not serve modules to. It will render "
        f"and never become interactive, and the browser will not say why. "
        f"Reach the dev server at one of the addresses it printed at startup, "
        f"or restart it with --host so it answers on the address you are using."
    )


__all__ = [
    "ALL_INTERFACES",
    "DEFAULT_VITE_HOST",
    "DEV_SESSION_FILENAME",
    "LOOPBACK_HOSTS",
    "VITE_DEFAULT_ORIGIN_PATTERN",
    "DevSession",
    "active_dev_session",
    "allowed_hostnames",
    "allowed_origins",
    "browser_vite_host",
    "is_loopback_host",
    "is_off_box_host",
    "is_wildcard_host",
    "local_ipv4_addresses",
    "private_origin_pattern",
    "resolve_vite_bind_host",
    "unhydratable_origin_warning",
    "vite_reachability_warning",
    "vite_serves_origin",
    "websocket_origins",
]
