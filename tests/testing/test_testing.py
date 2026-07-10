"""Tests for the pyxle.testing helpers."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from pyxle.testing import load_loader, load_page

_LOADER_PAGE = """@server
async def load_home(request):
    return {"hello": "world"}

# --- JavaScript/PSX (Client + Server) ---

import React from 'react';

export default function Home({ data }) {
    return <div>{data.hello}</div>;
}
"""

_ACTION_PAGE = """@server
async def load_contact(request):
    return {"sent": False}

@action
async def submit(request):
    return {"sent": True}

# --- JavaScript/PSX (Client + Server) ---

import React from 'react';

export default function Contact({ data }) {
    return <div>{data.sent ? 'sent' : 'draft'}</div>;
}
"""

_NO_LOADER_PAGE = """# --- JavaScript/PSX (Client + Server) ---

import React from 'react';

export default function Static() {
    return <div>hi</div>;
}
"""


def _write_page(tmp_path: Path, name: str, body: str) -> Path:
    pages = tmp_path / "pages"
    pages.mkdir(exist_ok=True)
    source = pages / name
    source.write_text(body, encoding="utf-8")
    return source


def test_load_loader_returns_callable_loader(tmp_path: Path) -> None:
    source = _write_page(tmp_path, "index.pyxl", _LOADER_PAGE)
    load_home = load_loader(source)
    result = asyncio.run(load_home(SimpleNamespace()))
    assert result == {"hello": "world"}


def test_load_page_exposes_action(tmp_path: Path) -> None:
    source = _write_page(tmp_path, "contact.pyxl", _ACTION_PAGE)
    page = load_page(source)
    # Both the loader and the action are plain async functions on the module.
    assert page.__pyxle_metadata__.loader_name == "load_contact"
    assert asyncio.run(page.load_contact(SimpleNamespace())) == {"sent": False}
    assert asyncio.run(page.submit(SimpleNamespace())) == {"sent": True}


def test_load_loader_raises_without_loader(tmp_path: Path) -> None:
    source = _write_page(tmp_path, "static.pyxl", _NO_LOADER_PAGE)
    with pytest.raises(ValueError, match="no @server loader"):
        load_loader(source)


def test_load_page_accepts_str_path(tmp_path: Path) -> None:
    source = _write_page(tmp_path, "index.pyxl", _LOADER_PAGE)
    load_home = load_loader(str(source))
    assert asyncio.run(load_home(SimpleNamespace())) == {"hello": "world"}


def test_load_page_honors_explicit_build_root(tmp_path: Path) -> None:
    source = _write_page(tmp_path, "index.pyxl", _LOADER_PAGE)
    build_root = tmp_path / "custom-build"
    page = load_page(source, build_root=build_root)
    assert build_root.exists()
    assert asyncio.run(page.load_home(SimpleNamespace())) == {"hello": "world"}


def test_repeated_loads_do_not_collide(tmp_path: Path) -> None:
    # Loading the same page twice must yield independently importable modules.
    source = _write_page(tmp_path, "index.pyxl", _LOADER_PAGE)
    first = load_page(source)
    second = load_page(source)
    assert first is not second
    assert asyncio.run(first.load_home(SimpleNamespace())) == {"hello": "world"}
    assert asyncio.run(second.load_home(SimpleNamespace())) == {"hello": "world"}
