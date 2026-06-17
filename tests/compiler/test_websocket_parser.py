"""Tests for ``async def websocket(ws)`` detection in the .pyxl parser.

A page may declare a module-scope coroutine named ``websocket`` to also serve a
WebSocket route at its path. Detection is by **convention** (the function name),
not a decorator — matching how API modules already expose a ``websocket``
callable, and keeping ``pyxle.runtime`` free of a new symbol. These tests pin
the detection rules and the error messages, since the parser is the most
fragile module in the codebase.
"""

from __future__ import annotations

from textwrap import dedent

import pytest

from pyxle.compiler.exceptions import CompilationError
from pyxle.compiler.parser import PyxParser, WebsocketDetails


def parse(text: str) -> object:
    return PyxParser().parse_text(dedent(text).strip())


# ---------------------------------------------------------------------------
# Happy-path detection
# ---------------------------------------------------------------------------


def test_detects_websocket_handler() -> None:
    result = parse(
        """
        async def websocket(ws):
            await ws.accept()

        import React from 'react';
        export default function Page() { return <div/>; }
        """
    )
    assert result.websocket is not None
    assert isinstance(result.websocket, WebsocketDetails)
    assert result.websocket.name == "websocket"
    assert result.websocket.is_async is True
    assert list(result.websocket.parameters) == ["ws"]


def test_websocket_first_arg_any_name() -> None:
    # Unlike @server/@action (which require 'request'), the WS handler's first
    # argument can be named anything — it's a Starlette WebSocket, not a Request.
    for arg in ("ws", "socket", "connection", "websocket_conn"):
        result = parse(
            f"""
            async def websocket({arg}):
                await {arg}.accept()

            import React from 'react';
            export default function Page() {{ return <div/>; }}
            """
        )
        assert result.websocket is not None
        assert list(result.websocket.parameters) == [arg]


def test_no_websocket_is_none() -> None:
    result = parse(
        """
        from pyxle.runtime import server

        @server
        async def load(request):
            return {}

        import React from 'react';
        export default function Page() { return <div/>; }
        """
    )
    assert result.websocket is None


def test_websocket_coexists_with_loader_and_action() -> None:
    result = parse(
        """
        from pyxle.runtime import server, action

        @server
        async def load(request):
            return {"room": "lobby"}

        @action
        async def post_message(request):
            return {"ok": True}

        async def websocket(ws):
            await ws.accept()

        import React from 'react';
        export default function Page() { return <div/>; }
        """
    )
    assert result.loader is not None and result.loader.name == "load"
    assert len(result.actions) == 1 and result.actions[0].name == "post_message"
    assert result.websocket is not None and result.websocket.name == "websocket"


def test_websocket_with_extra_params() -> None:
    result = parse(
        """
        async def websocket(ws, *, broker=None):
            await ws.accept()

        import React from 'react';
        export default function Page() { return <div/>; }
        """
    )
    assert result.websocket is not None
    assert list(result.websocket.parameters) == ["ws"]


def test_websocket_line_number_mapped() -> None:
    # The reported line must point at the original .pyxl source, not the
    # extracted Python.
    result = parse(
        """
        # a comment line
        # another comment

        async def websocket(ws):
            await ws.accept()

        import React from 'react';
        export default function Page() { return <div/>; }
        """
    )
    assert result.websocket is not None
    assert result.websocket.line_number == 4


# ---------------------------------------------------------------------------
# Rejections
# ---------------------------------------------------------------------------


def test_sync_websocket_raises() -> None:
    with pytest.raises(CompilationError, match="must be declared as async"):
        parse(
            """
            def websocket(ws):
                pass

            import React from 'react';
            export default function Page() { return <div/>; }
            """
        )


def test_websocket_class_raises() -> None:
    with pytest.raises(CompilationError, match="must be an async function"):
        parse(
            """
            class websocket:
                pass

            import React from 'react';
            export default function Page() { return <div/>; }
            """
        )


def test_websocket_without_args_raises() -> None:
    with pytest.raises(CompilationError, match="must accept a WebSocket argument"):
        parse(
            """
            async def websocket():
                pass

            import React from 'react';
            export default function Page() { return <div/>; }
            """
        )


def test_multiple_websocket_handlers_raises() -> None:
    with pytest.raises(CompilationError, match="Multiple `websocket` handlers"):
        parse(
            """
            async def websocket(ws):
                pass

            async def websocket(ws):  # noqa: F811 - intentional duplicate
                pass

            import React from 'react';
            export default function Page() { return <div/>; }
            """
        )


def test_nested_websocket_is_ignored() -> None:
    # A function named ``websocket`` nested inside another function is NOT the
    # page handler (it isn't a module attribute, so it can't be served). It is
    # silently ignored — not an error — so a local helper name never breaks a
    # page.
    result = parse(
        """
        from pyxle.runtime import server

        @server
        async def load(request):
            async def websocket(ws):
                pass
            return {}

        import React from 'react';
        export default function Page() { return <div/>; }
        """
    )
    assert result.websocket is None
