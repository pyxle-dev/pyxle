"""End-to-end streaming SSR.

Builds a real project containing a ``<Suspense>`` page, serves it through the
worker pool, and asserts the document streams the static head + shell first and
the resolved boundary after — while a plain page served by the same app keeps
the buffered path.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from pyxle.devserver.builder import build_once
from pyxle.devserver.registry import load_metadata_registry
from pyxle.devserver.routes import build_route_table
from pyxle.devserver.settings import DevServerSettings
from pyxle.devserver.starlette_app import create_starlette_app
from pyxle.ssr.worker_pool import SsrWorkerPool
from tests.ssr.utils import ensure_test_node_modules


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


_SUSPENSE_PAGE = """
HEAD = ["<title>Streamed</title>"]

# --- JavaScript/PSX (Client + Server) ---

import React, { Suspense } from 'react';

let _done = false;
let _promise;
function suspendOnce() {
  if (_done) return;
  if (!_promise) {
    _promise = new Promise((resolve) => {
      setTimeout(() => { _done = true; resolve(); }, 15);
    });
  }
  throw _promise;
}

function Slow() {
  suspendOnce();
  return <p data-testid="slow">streamed-boundary</p>;
}

export default function StreamPage() {
  return (
    <main data-testid="shell">
      <h1>Shell Heading</h1>
      <Suspense fallback={<p data-testid="fallback">loading-fallback</p>}>
        <Slow />
      </Suspense>
    </main>
  );
}
"""

_PLAIN_PAGE = """
import React from 'react';

export default function Plain() {
  return <main data-testid="plain">Plain buffered page</main>;
}
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js required for streaming SSR")
def test_suspense_page_streams_while_plain_page_buffers(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    (project_root / "public").mkdir(parents=True)
    settings = DevServerSettings.from_project_root(project_root)
    ensure_test_node_modules(project_root)

    _write(settings.pages_dir / "stream.pyxl", _SUSPENSE_PAGE)
    _write(settings.pages_dir / "plain.pyxl", _PLAIN_PAGE)

    build_once(settings)
    registry = load_metadata_registry(settings)
    routes = build_route_table(registry)

    # The compiler flagged only the Suspense page.
    stream_route = next(r for r in routes.pages if r.path == "/stream")
    plain_route = next(r for r in routes.pages if r.path == "/plain")
    assert stream_route.uses_suspense is True
    assert plain_route.uses_suspense is False

    pool = SsrWorkerPool(
        size=1,
        project_root=settings.project_root,
        client_root=settings.client_build_dir,
    )
    app = create_starlette_app(settings, routes, pool=pool)

    with TestClient(app) as client:
        streamed = client.get("/stream")
        plain = client.get("/plain")

    # --- The Suspense page streamed end to end ---
    assert streamed.status_code == 200
    body = streamed.text
    # Static HEAD flushed in the prefix.
    assert "<title>Streamed</title>" in body
    # Shell + the resolved boundary both made it into the streamed body.
    assert "Shell Heading" in body
    assert "loading-fallback" in body  # the fallback streamed in the shell
    assert "streamed-boundary" in body  # the boundary resolved and streamed in
    # Hydration scaffolding came last, in the suffix.
    assert "__PYXLE_PROPS__" in body
    assert "window.__PYXLE_PAGE_PATH__" in body
    # A streamed render is per-user and never publicly cached.
    assert streamed.headers.get("cache-control") == "private, no-cache"

    # --- The plain page still renders correctly via the buffered path ---
    assert plain.status_code == 200
    assert 'data-testid="plain"' in plain.text
    assert "Plain buffered page" in plain.text
