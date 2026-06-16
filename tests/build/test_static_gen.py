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
