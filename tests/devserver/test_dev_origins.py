"""Tests for the single definition of "an origin this dev server may serve"."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from pyxle.devserver.dev_origins import (
    DEV_SESSION_FILENAME,
    VITE_DEFAULT_ORIGIN_PATTERN,
    active_dev_session,
    allowed_hostnames,
    allowed_origins,
    browser_vite_host,
    is_off_box_host,
    private_origin_pattern,
    resolve_vite_bind_host,
    unhydratable_origin_warning,
    vite_reachability_warning,
    vite_serves_origin,
    websocket_origins,
)


def write_session(
    build_root: Path,
    *,
    pid: int | None = None,
    server: object = None,
    vite: object = None,
    payload: object = None,
) -> Path:
    """Write a ``dev-server.json`` shaped like the one ``pyxle dev`` writes."""

    build_root.mkdir(parents=True, exist_ok=True)
    target = build_root / DEV_SESSION_FILENAME
    if payload is None:
        payload = {
            "pid": os.getpid() if pid is None else pid,
            "server": {"host": "0.0.0.0", "port": 3000} if server is None else server,
            "vite": {"host": "0.0.0.0", "port": 5173} if vite is None else vite,
            "url": "http://127.0.0.1:3000",
        }
    target.write_text(json.dumps(payload), encoding="utf-8")
    return target


def dead_pid() -> int:
    """A pid that named a real process and no longer names anything.

    Spawned and reaped rather than invented, so the liveness check is answering
    the question it will face in the field — a dev server that exited — and not
    rejecting an implausible number for some other reason.
    """

    process = subprocess.Popen([sys.executable, "-c", ""])
    process.wait()
    return process.pid


# --- origin policy ---------------------------------------------------------


def test_allowed_origins_covers_private_network_for_a_wildcard_bind() -> None:
    exact, pattern = allowed_origins("0.0.0.0", 3000)

    assert exact == ("http://localhost:3000", "http://127.0.0.1:3000")
    assert pattern is not None
    assert re.match(pattern, "http://192.168.1.11:3000")
    assert re.match(pattern, "http://10.0.0.4:3000")
    assert re.match(pattern, "http://172.16.9.9:3000")
    # A public address, and a private one on someone else's port, are not ours.
    assert not re.match(pattern, "http://203.0.113.9:3000")
    assert not re.match(pattern, "http://192.168.1.11:3001")


def test_private_origin_pattern_spans_every_port_it_is_given() -> None:
    """One pattern has to cover both dev ports for the WebSocket allow-list."""

    pattern = private_origin_pattern(3000, 5173)

    assert re.match(pattern, "http://192.168.1.11:3000")
    assert re.match(pattern, "http://192.168.1.11:5173")
    assert not re.match(pattern, "http://192.168.1.11:5174")


def test_websocket_origins_match_the_addresses_the_server_answers_on() -> None:
    """The dev socket trusts exactly the browsers the module server trusts.

    A dev server started with ``--host 0.0.0.0`` prints a ``Network:`` URL and
    invites a phone or a second laptop to use it. Refusing that origin's
    WebSocket costs it hot reload and the error overlay — and the build-failure
    page, which promises in its own text to reload itself once the rebuild
    succeeds, then never does.
    """

    exact, pattern = websocket_origins(
        starlette_host="0.0.0.0", starlette_port=3000, vite_port=5173
    )

    assert exact == (
        "http://localhost:3000",
        "http://127.0.0.1:3000",
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    )
    assert pattern is not None
    assert re.match(pattern, "http://192.168.1.11:3000")
    assert re.match(pattern, "http://192.168.1.11:5173")
    # Not "any origin": these sockets carry source paths and stack traces.
    assert not re.match(pattern, "http://evil.example.com")


def test_websocket_origins_do_not_repeat_a_shared_port() -> None:
    """Two ports that happen to be one still describe one set of origins."""

    exact, _pattern = websocket_origins(
        starlette_host="0.0.0.0", starlette_port=3000, vite_port=3000
    )

    assert exact == ("http://localhost:3000", "http://127.0.0.1:3000")


def test_websocket_origins_for_a_named_host_are_that_host() -> None:
    exact, pattern = websocket_origins(
        starlette_host="192.168.1.11", starlette_port=3000, vite_port=5173
    )

    assert exact == ("http://192.168.1.11:3000", "http://192.168.1.11:5173")
    assert pattern is None


def test_websocket_origins_for_a_loopback_bind_stay_loopback() -> None:
    exact, pattern = websocket_origins(
        starlette_host="127.0.0.1", starlette_port=3000, vite_port=5173
    )

    assert exact == (
        "http://localhost:3000",
        "http://127.0.0.1:3000",
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    )
    assert pattern is None


@pytest.mark.parametrize(
    "origin",
    [
        "http://localhost:5173",
        "http://app.localhost:3000",
        "http://127.0.0.1:3000",
        "http://[::1]:3000",
    ],
)
def test_vite_serves_loopback_origins_by_its_own_default(origin: str) -> None:
    """Vite's built-in allow-list is loopback on any port; predict it exactly.

    The generated config restates this pattern rather than replacing it, so an
    origin matching it is served whether or not Pyxle added anything.
    """

    assert vite_serves_origin(origin, starlette_host="127.0.0.1", starlette_port=3000)
    assert re.match(VITE_DEFAULT_ORIGIN_PATTERN, origin)


def test_vite_serves_private_origins_only_for_an_off_box_server() -> None:
    assert vite_serves_origin(
        "http://192.168.1.11:3000", starlette_host="0.0.0.0", starlette_port=3000
    )
    # The same origin against a loopback-only dev server is refused: Pyxle
    # writes no allow-list for it, so Vite's loopback default is all there is.
    assert not vite_serves_origin(
        "http://192.168.1.11:3000", starlette_host="127.0.0.1", starlette_port=3000
    )


def test_vite_serves_a_named_bind_host_exactly() -> None:
    assert vite_serves_origin(
        "http://dev.internal:3000",
        starlette_host="dev.internal",
        starlette_port=3000,
    )
    assert not vite_serves_origin(
        "http://other.internal:3000",
        starlette_host="dev.internal",
        starlette_port=3000,
    )


# --- the silence -----------------------------------------------------------


def test_unhydratable_origin_warning_names_the_origin_and_stays_quiet_otherwise() -> None:
    """The one place that can see both halves of a completely silent failure."""

    assert (
        unhydratable_origin_warning(
            document_origin="http://192.168.1.11:3000",
            starlette_host="0.0.0.0",
            starlette_port=3000,
            vite_port=5173,
        )
        is None
    )

    message = unhydratable_origin_warning(
        document_origin="http://dev.internal:3000",
        starlette_host="0.0.0.0",
        starlette_port=3000,
        vite_port=5173,
    )
    assert message is not None
    assert "http://dev.internal:3000" in message
    assert "5173" in message
    assert "--host" in message


# --- the running dev server ------------------------------------------------


def test_active_dev_session_reads_another_process_record(tmp_path: Path) -> None:
    """A live record is the answer to "what is this project's client config for".

    The pid is the parent's: any live process that is not this one stands in for
    a running ``pyxle dev``.
    """

    write_session(tmp_path, pid=os.getppid(), server={"host": "0.0.0.0", "port": 9610})

    session = active_dev_session(tmp_path)

    assert session is not None
    assert session.pid == os.getppid()
    assert (session.starlette_host, session.starlette_port) == ("0.0.0.0", 9610)
    assert (session.vite_host, session.vite_port) == ("0.0.0.0", 5173)


def test_active_dev_session_ignores_our_own_record(tmp_path: Path) -> None:
    """The dev server's own settings are the truth; its file only echoes them."""

    write_session(tmp_path, pid=os.getpid())

    assert active_dev_session(tmp_path) is None


def test_active_dev_session_ignores_a_dead_servers_leftovers(tmp_path: Path) -> None:
    """A crashed or ``kill -9``'d dev server leaves its record behind."""

    write_session(tmp_path, pid=dead_pid())

    assert active_dev_session(tmp_path) is None


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param({"server": {"host": "0.0.0.0", "port": 9610}}, id="no-pid"),
        pytest.param({"pid": "1234"}, id="pid-not-an-int"),
        pytest.param({"pid": True}, id="pid-is-a-bool"),
        pytest.param({"pid": -1}, id="pid-out-of-range"),
        pytest.param(["not", "a", "record"], id="not-an-object"),
    ],
)
def test_active_dev_session_ignores_malformed_records(
    tmp_path: Path, payload: object
) -> None:
    write_session(tmp_path, payload=payload)

    assert active_dev_session(tmp_path) is None


@pytest.mark.parametrize(
    ("server", "vite"),
    [
        pytest.param("0.0.0.0:9610", None, id="server-not-an-object"),
        pytest.param({"port": 9610}, None, id="server-without-host"),
        pytest.param({"host": "0.0.0.0"}, None, id="server-without-port"),
        pytest.param({"host": 0, "port": 9610}, None, id="host-not-a-string"),
        pytest.param({"host": "0.0.0.0", "port": "9610"}, None, id="port-not-an-int"),
        pytest.param({"host": "0.0.0.0", "port": True}, None, id="port-is-a-bool"),
        pytest.param({"host": "0.0.0.0", "port": 0}, None, id="port-zero"),
        pytest.param({"host": "0.0.0.0", "port": 70000}, None, id="port-too-large"),
        pytest.param(None, {"host": "0.0.0.0"}, id="vite-without-port"),
    ],
)
def test_active_dev_session_ignores_incomplete_endpoints(
    tmp_path: Path, server: object, vite: object
) -> None:
    write_session(tmp_path, pid=os.getppid(), server=server, vite=vite)

    assert active_dev_session(tmp_path) is None


def test_active_dev_session_keeps_a_record_it_may_not_signal(
    tmp_path: Path, monkeypatch
) -> None:
    """A dev server running as another user exists; it is simply not ours to poke."""

    def _refuse(_pid: int, _signal: int) -> None:
        raise PermissionError("not your process")

    monkeypatch.setattr(os, "kill", _refuse)
    write_session(tmp_path, pid=os.getppid())

    assert active_dev_session(tmp_path) is not None


def test_active_dev_session_without_a_record(tmp_path: Path) -> None:
    assert active_dev_session(tmp_path / "never-built") is None


def test_active_dev_session_survives_an_unreadable_record(tmp_path: Path) -> None:
    """Reading the record is a convenience; failing to must not fail a build."""

    (tmp_path / DEV_SESSION_FILENAME).write_text("{not json", encoding="utf-8")

    assert active_dev_session(tmp_path) is None


def test_active_dev_session_accepts_an_empty_bind_host(tmp_path: Path) -> None:
    """``""`` is a real bind address (every interface), not a missing field."""

    write_session(
        tmp_path, pid=os.getppid(), server={"host": "", "port": 9610}
    )

    session = active_dev_session(tmp_path)

    assert session is not None
    assert session.starlette_host == ""


# --- surrounding helpers, exercised directly -------------------------------


def test_resolve_vite_bind_host_only_widens_the_untouched_default() -> None:
    assert resolve_vite_bind_host("0.0.0.0", "127.0.0.1") == "0.0.0.0"
    # A host the developer named is a choice, and is left alone.
    assert resolve_vite_bind_host("0.0.0.0", "localhost") == "localhost"
    assert resolve_vite_bind_host("127.0.0.1", "127.0.0.1") == "127.0.0.1"


def test_browser_vite_host_prefers_the_name_the_browser_already_used() -> None:
    assert (
        browser_vite_host(
            vite_host="0.0.0.0",
            starlette_host="0.0.0.0",
            request_host="192.168.1.11",
        )
        == "192.168.1.11"
    )
    # An IPv6 literal needs its brackets back before it meets a port separator.
    assert (
        browser_vite_host(
            vite_host="0.0.0.0", starlette_host="0.0.0.0", request_host="fe80::1"
        )
        == "[fe80::1]"
    )
    assert browser_vite_host(vite_host="0.0.0.0", starlette_host="0.0.0.0") == "localhost"


def test_allowed_hostnames_declares_only_real_names() -> None:
    """Vite's host check already accepts IP literals and localhost."""

    assert allowed_hostnames("0.0.0.0", "127.0.0.1", "::1") == ()
    assert allowed_hostnames("192.168.1.11", "[fe80::1]") == ()
    assert allowed_hostnames("dev.internal", "dev.internal") == ("dev.internal",)


def test_is_off_box_host_counts_a_wildcard_bind() -> None:
    assert is_off_box_host("0.0.0.0") is True
    assert is_off_box_host("192.168.1.11") is True
    assert is_off_box_host("localhost") is False


def test_vite_reachability_warning_speaks_only_when_vite_is_out_of_reach() -> None:
    assert (
        vite_reachability_warning(
            starlette_host="0.0.0.0",
            starlette_port=3000,
            vite_host="0.0.0.0",
            vite_port=5173,
        )
        is None
    )
    message = vite_reachability_warning(
        starlette_host="0.0.0.0",
        starlette_port=3000,
        vite_host="localhost",
        vite_port=5173,
    )
    assert message is not None
    assert "--vite-host 0.0.0.0" in message


@pytest.mark.parametrize(
    ("starlette_host", "vite_host"),
    [
        pytest.param("127.0.0.1", "127.0.0.1", id="both-loopback"),
        pytest.param("0.0.0.0", "0.0.0.0", id="vite-is-everywhere-too"),
        pytest.param("192.168.1.11", "192.168.1.11", id="same-named-interface"),
        pytest.param("192.168.1.11", "10.0.0.4", id="both-pinned-off-box"),
    ],
)
def test_vite_reachability_warning_stays_quiet_when_vite_is_reachable(
    starlette_host: str, vite_host: str
) -> None:
    assert (
        vite_reachability_warning(
            starlette_host=starlette_host,
            starlette_port=3000,
            vite_host=vite_host,
            vite_port=5173,
        )
        is None
    )


def test_vite_reachability_warning_names_a_pinned_host_it_can_see() -> None:
    """A named bind host is an address the developer can be pointed at."""

    message = vite_reachability_warning(
        starlette_host="192.168.1.11",
        starlette_port=3000,
        vite_host="127.0.0.1",
        vite_port=5173,
    )

    assert message is not None
    assert "http://192.168.1.11:3000/" in message


# ---------------------------------------------------------------------------
# The client's scheme vs the socket's. One rule, used by both the CSRF cookie
# and the absolute URLs in /llms.txt.
# ---------------------------------------------------------------------------


def _scope(*, scheme: str = "http", forwarded: str | None = None) -> dict:
    headers = []
    if forwarded is not None:
        headers.append((b"x-forwarded-proto", forwarded.encode()))
    return {"type": "http", "scheme": scheme, "headers": headers}


class TestForwardedScheme:
    """Measured before this existed: with a proxy on *another* host, uvicorn
    ignores ``X-Forwarded-Proto`` (the peer is not in ``forwarded_allow_ips``,
    ``127.0.0.1`` by default) and ``/llms.txt`` emitted ``http://`` links on an
    HTTPS site. Reading the header ourselves is what makes the answer the same
    wherever the proxy runs."""

    def test_forwarded_https_wins_over_a_plain_socket(self):
        from pyxle.devserver.dev_origins import forwarded_scheme

        assert forwarded_scheme(_scope(scheme="http", forwarded="https")) == "https"

    def test_forwarded_http_is_not_upgraded(self):
        """A proxy that says http means http — never guess upwards."""
        from pyxle.devserver.dev_origins import forwarded_scheme

        assert forwarded_scheme(_scope(scheme="http", forwarded="http")) == "http"

    def test_first_hop_wins_in_a_comma_list(self):
        from pyxle.devserver.dev_origins import forwarded_scheme

        assert forwarded_scheme(_scope(forwarded="https, http")) == "https"

    def test_falls_back_to_the_socket_scheme(self):
        from pyxle.devserver.dev_origins import forwarded_scheme

        assert forwarded_scheme(_scope(scheme="https")) == "https"
        assert forwarded_scheme(_scope(scheme="http")) == "http"

    def test_csrf_uses_the_same_rule(self):
        """The two surfaces must never disagree about whether this was TLS."""
        from pyxle.devserver.csrf import _is_https
        from pyxle.devserver.dev_origins import request_is_https

        for scope in (
            _scope(scheme="http", forwarded="https"),
            _scope(scheme="http", forwarded="http"),
            _scope(scheme="https"),
            _scope(scheme="http"),
        ):
            assert _is_https(scope) is request_is_https(scope)
