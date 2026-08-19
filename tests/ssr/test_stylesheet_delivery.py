"""A stylesheet is delivered once, by whichever mechanism actually applies it.

Production links every stylesheet Vite compiled for the page, from the build
manifest, with plain render-blocking ``<link rel="stylesheet">`` tags. The SSR
worker was *also* reading those same files raw and dumping them into a
``<style>`` block, because its "should I inline?" test asked whether the project
had configured PostCSS or Tailwind — a proxy for "does Vite own CSS here?" that
is false for the plain-CSS scaffold and true for the question that matters.

Measured on the plain scaffold before the fix: 1,858 bytes inline beside 1,510
bytes of linked CSS, every selector present in both, and no ``media``/``onload``
swap on the links — so the inline copy could not even buy a faster first paint.
It was payload shipped twice.

Dev is the opposite case and must not change: there is no manifest, Vite injects
CSS through the client bundle after hydration, and the inline block is the only
thing that styles the server-rendered paint.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from pyxle.devserver.settings import DevServerSettings
from pyxle.ssr.template import vite_owns_stylesheets
from pyxle.ssr.worker_pool import SsrWorkerPool


@pytest.fixture
def anyio_backend() -> str:  # pragma: no cover - fixture wiring
    return "asyncio"


def _settings(tmp_path: Path, *, debug: bool, manifest: dict | None) -> DevServerSettings:
    from dataclasses import replace

    project = tmp_path / "project"
    (project / "pages").mkdir(parents=True, exist_ok=True)
    base = DevServerSettings.from_project_root(project)
    return replace(base, debug=debug, page_manifest=manifest)


class TestWhoOwnsStylesheetDelivery:
    """The one fact both halves read. They must never disagree again."""

    def test_a_manifest_backed_render_owns_delivery(self, tmp_path: Path) -> None:
        settings = _settings(tmp_path, debug=False, manifest={"/": {}})
        assert vite_owns_stylesheets(settings) is True

    def test_dev_does_not(self, tmp_path: Path) -> None:
        """No manifest, no links — the inline copy is the only styling the
        server-rendered paint gets."""
        settings = _settings(tmp_path, debug=True, manifest=None)
        assert vite_owns_stylesheets(settings) is False

    def test_a_non_debug_run_without_a_manifest_does_not(self, tmp_path: Path) -> None:
        """Nothing emits links without a manifest, so nothing may suppress the
        inline copy on that basis — this is the direction that would strip a
        page's CSS entirely."""
        settings = _settings(tmp_path, debug=False, manifest=None)
        assert vite_owns_stylesheets(settings) is False

    def test_debug_with_a_manifest_does_not(self, tmp_path: Path) -> None:
        settings = _settings(tmp_path, debug=True, manifest={"/": {}})
        assert vite_owns_stylesheets(settings) is False


def _pool(tmp_path: Path, *, vite_owns_css: bool) -> SsrWorkerPool:
    project_root = tmp_path / "project"
    client_root = project_root / ".pyxle-build" / "client"
    client_root.mkdir(parents=True, exist_ok=True)
    return SsrWorkerPool(
        size=1,
        project_root=project_root,
        client_root=client_root,
        vite_owns_css=vite_owns_css,
    )


async def _capture_payload(tmp_path: Path, *, vite_owns_css: bool) -> dict:
    pool = _pool(tmp_path, vite_owns_css=vite_owns_css)
    captured: dict = {}

    async def fake_send(payload: dict) -> dict:
        captured.update(payload)
        return {"id": payload["id"], "ok": True, "html": "<x/>"}

    worker = MagicMock()
    worker.alive = True
    worker.in_flight = 0
    worker.send = AsyncMock(side_effect=fake_send)
    pool._started = True
    pool._workers = [worker]

    component = tmp_path / "project" / ".pyxle-build" / "client" / "p.jsx"
    component.parent.mkdir(parents=True, exist_ok=True)
    component.touch()
    await pool.render(component, {})
    return captured


class TestTheWorkerIsToldWhoOwnsDelivery:
    """The worker must not have to guess. It used to, and guessed wrong."""

    @pytest.mark.anyio
    async def test_the_buffered_payload_carries_the_answer(self, tmp_path: Path) -> None:
        assert (await _capture_payload(tmp_path, vite_owns_css=True))["viteOwnsCss"] is True

    @pytest.mark.anyio
    async def test_dev_tells_the_worker_to_keep_inlining(self, tmp_path: Path) -> None:
        assert (await _capture_payload(tmp_path, vite_owns_css=False))["viteOwnsCss"] is False

    @pytest.mark.anyio
    async def test_the_streaming_payload_carries_it_too(self, tmp_path: Path) -> None:
        """A streamed page ships the same document; it must not ship the CSS
        twice just because it took the other code path."""
        pool = _pool(tmp_path, vite_owns_css=True)
        captured: dict = {}

        async def fake_send_stream(payload: dict, *, frame_timeout: float):
            captured.update(payload)
            yield {"id": payload["id"], "type": "end"}

        worker = MagicMock()
        worker.alive = True
        worker.in_flight = 0
        worker.send_stream = fake_send_stream
        pool._started = True
        pool._workers = [worker]

        component = tmp_path / "project" / ".pyxle-build" / "client" / "p.jsx"
        component.parent.mkdir(parents=True, exist_ok=True)
        component.touch()

        async for _ in pool.render_stream(component, {}):
            pass

        assert captured["viteOwnsCss"] is True

    def test_the_flag_defaults_to_keeping_the_inline_copy(self, tmp_path: Path) -> None:
        """An omitted flag must mean "inline", never "skip": the failure mode of
        wrongly skipping is a page with no styles at all."""
        project_root = tmp_path / "project"
        client_root = project_root / ".pyxle-build" / "client"
        client_root.mkdir(parents=True, exist_ok=True)
        pool = SsrWorkerPool(
            size=1, project_root=project_root, client_root=client_root
        )
        assert pool._vite_owns_css is False
