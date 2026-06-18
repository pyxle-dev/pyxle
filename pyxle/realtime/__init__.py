"""Realtime primitives for Pyxle WebSocket handlers.

Public surface:

* :func:`channel` — subscribe a WebSocket to a named channel (room) for the
  life of an ``async with`` block, with a ``.publish()`` handle.
* :class:`Broker` / :class:`InProcessBroker` — the pub/sub contract and its
  default in-process implementation. :class:`RedisBroker` (the
  ``pyxle-framework[redis]`` extra) spans worker processes for multi-worker
  realtime; :func:`build_broker` selects between them from the environment.
* :func:`authenticate_websocket` — resolve the session user inside a WS handler
  (the auth middleware never runs for WebSocket scope).
* :func:`origin_allowed` — the WS equivalent of CSRF: check the upgrade Origin.

Standalone module — imports nothing from ``pyxle.devserver`` or
``pyxle.compiler``.
"""

from __future__ import annotations

from pyxle.realtime.auth import authenticate_websocket, origin_allowed
from pyxle.realtime.channels import (
    Broker,
    ChannelHandle,
    InProcessBroker,
    Message,
    broker_for,
    build_broker,
    channel,
)

__all__ = [
    "Broker",
    "ChannelHandle",
    "InProcessBroker",
    "Message",
    "authenticate_websocket",
    "broker_for",
    "build_broker",
    "channel",
    "origin_allowed",
]
