"""Tests for build-time static pre-rendering (``pyxle build --static``).

``build_page_response`` (the real SSR render) is monkeypatched so the render
loop is tested without Node; the pool-booting glue (``generate_static_site``)
is exercised end-to-end by the production serve path.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from starlette.responses import Response, StreamingResponse

from pyxle.build import static_gen
from pyxle.cache import PageCache
from pyxle.cache.backends import FileCacheBackend


@pytest.fixture
def anyio_backend() -> str:  # pragma: no cover - fixture wiring
    return "asyncio"


def _page(path: str) -> SimpleNamespace:
    return SimpleNamespace(path=path, has_loader=False)


@pytest.mark.anyio
async def test_prerender_pages_writes_cache_entries(monkeypatch, tmp_path: Path) -> None:
    async def _fake(*, request, settings, page, renderer):
        return Response(
            content=f"<html>{request.url.path}</html>".encode(),
            status_code=200,
            media_type="text/html",
        )

    monkeypatch.setattr(static_gen, "build_page_response", _fake)
    prerender_dir = tmp_path / "prerendered"

    rendered = await static_gen.prerender_pages(
        settings=object(),
        pages=[_page("/"), _page("/about")],
        renderer=object(),
        prerender_dir=prerender_dir,
    )

    assert sorted(rendered) == ["/", "/about"]
    entry = await FileCacheBackend(prerender_dir).get(PageCache.make_key("/about"))
    assert entry is not None
    assert entry.body == b"<html>/about</html>"
    assert entry.revalidate is None  # pre-rendered entries never auto-expire
    assert entry.etag == PageCache.make_etag(entry.body)


@pytest.mark.anyio
async def test_prerender_skips_non_200_responses(monkeypatch, tmp_path: Path) -> None:
    async def _fake(*, request, settings, page, renderer):
        return Response(status_code=404)

    monkeypatch.setattr(static_gen, "build_page_response", _fake)

    rendered = await static_gen.prerender_pages(
        settings=object(),
        pages=[_page("/missing")],
        renderer=object(),
        prerender_dir=tmp_path / "prerendered",
    )

    assert rendered == []


@pytest.mark.anyio
async def test_prerender_materializes_streaming_response(monkeypatch, tmp_path: Path) -> None:
    async def _gen():
        yield b"<ht"
        yield b"ml>"

    async def _fake(*, request, settings, page, renderer):
        return StreamingResponse(_gen(), status_code=200, media_type="text/html")

    monkeypatch.setattr(static_gen, "build_page_response", _fake)
    prerender_dir = tmp_path / "prerendered"

    rendered = await static_gen.prerender_pages(
        settings=object(),
        pages=[_page("/s")],
        renderer=object(),
        prerender_dir=prerender_dir,
    )

    assert rendered == ["/s"]
    entry = await FileCacheBackend(prerender_dir).get(PageCache.make_key("/s"))
    assert entry is not None and entry.body == b"<html>"


@pytest.mark.anyio
async def test_prerender_skips_missing_manifest_placeholder(monkeypatch, tmp_path: Path) -> None:
    # A page missing from the manifest renders the framework's 200 placeholder;
    # it must never be persisted as a static page.
    async def _fake(*, request, settings, page, renderer):
        return Response(
            content=b"<title>Pyxle - Missing Manifest Entry</title><h1>...</h1>",
            status_code=200,
            media_type="text/html",
        )

    monkeypatch.setattr(static_gen, "build_page_response", _fake)
    prerender_dir = tmp_path / "prerendered"

    rendered = await static_gen.prerender_pages(
        settings=object(),
        pages=[_page("/x")],
        renderer=object(),
        prerender_dir=prerender_dir,
    )

    assert rendered == []
    assert await FileCacheBackend(prerender_dir).get(PageCache.make_key("/x")) is None


@pytest.mark.anyio
async def test_prerender_ambient_activates_plugin_context(monkeypatch) -> None:
    """The build-time prerender environment must expose the active plugin
    context so a page/layout loader that uses a plugin service (e.g. pyxle-db's
    ``get_database()``) resolves at build time instead of raising
    ``PluginServiceError`` (regression for F36 — ``--static`` pre-rendered 0
    pages because no plugin context was active)."""
    import pyxle.plugins as plugins_mod
    from pyxle.plugins import (
        PluginContext,
        PluginServiceError,
        PyxlePlugin,
        plugin,
    )

    class _DbStub(PyxlePlugin):
        name = "db"
        version = "0.0.1"

        def __init__(self) -> None:
            self.started = False
            self.shut = False

        async def on_startup(self, ctx: PluginContext) -> None:
            self.started = True
            ctx.register("db.database", "<connection>")

        async def on_shutdown(self, ctx: PluginContext) -> None:
            self.shut = True

    stub = _DbStub()
    # Bypass module import: the ambient manager builds specs from settings then
    # calls load_plugins — return our stub so no real plugin module is needed.
    monkeypatch.setattr(plugins_mod, "load_plugins", lambda specs: (stub,))
    settings = SimpleNamespace(project_root="/tmp/app", plugins=["pyxle-db"])

    async with static_gen._prerender_ambient(settings):
        # Startup ran and the service resolves through the *active* context,
        # exactly as a loader's get_database() would at request time.
        assert stub.started is True
        assert plugin("db.database") == "<connection>"

    # Torn down: plugins shut down and the active context is cleared.
    assert stub.shut is True
    with pytest.raises(PluginServiceError):
        plugin("db.database")
