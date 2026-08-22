"""An occupied port has to explain itself — see :mod:`pyxle.devserver.ports`."""

from __future__ import annotations

import socket
from contextlib import contextmanager

from pyxle.devserver.ports import (
    find_free_port,
    is_port_available,
    port_in_use_message,
)


@contextmanager
def occupied_port():
    """Listen on an ephemeral port and yield it, so nothing is hard-coded."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        yield sock.getsockname()[1]
    finally:
        sock.close()


def test_is_port_available_sees_a_real_listener() -> None:
    with occupied_port() as port:
        assert is_port_available("127.0.0.1", port) is False


def test_is_port_available_is_true_once_the_listener_is_gone() -> None:
    with occupied_port() as port:
        pass
    assert is_port_available("127.0.0.1", port) is True


def test_find_free_port_steps_over_an_occupied_one() -> None:
    with occupied_port() as port:
        assert find_free_port("127.0.0.1", port) != port


def test_find_free_port_gives_up_rather_than_running_past_the_port_range() -> None:
    assert find_free_port("127.0.0.1", 65535, limit=5) in (65535, None)


def test_the_message_names_the_port_the_command_and_a_way_out() -> None:
    """The three things a developer needs, none of which the errno carried."""
    with occupied_port() as port:
        message = port_in_use_message("127.0.0.1", port, command="pyxle dev")

    assert str(port) in message
    assert "already in use" in message
    assert "pyxle dev" in message
    # A port that actually works, not just an instruction to pick one.
    assert "--port" in message
    # And a way to find the process squatting on it.
    assert str(port) in message.splitlines()[-1]


def test_the_message_suggests_a_port_that_is_genuinely_free() -> None:
    with occupied_port() as port:
        message = port_in_use_message("127.0.0.1", port, command="pyxle serve")
        suggested = int(message.split("--port ")[1].split()[0])

    assert suggested != port
    assert is_port_available("127.0.0.1", suggested)
