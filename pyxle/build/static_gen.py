"""Static pre-rendering for ``pyxle build --static`` (SSG).

Renders pages that carry no per-request data -- no ``@server`` loader and no
dynamic route parameters -- to HTML at build time and stores each as a
page-cache entry under ``dist/prerendered/``. At serve time the page cache is
warmed from that directory (:func:`pyxle.cache.warm_page_cache`), so the very
first request for a static page is a cache hit with no cold SSR render.

The render loop (:func:`prerender_pages`) takes an injected renderer so it is
unit-testable without Node; :func:`generate_static_site` is the build-time glue
that boots a short-lived SSR worker pool around it.
"""

from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator, Callable, Iterable

from starlette.requests import Request
from starlette.responses import Response

from pyxle.cache import PageCache
from pyxle.cache.backends import CacheEntry, FileCacheBackend
from pyxle.cli.logger import ConsoleLogger
from pyxle.devserver.routes import PageRoute, select_static_pages
from pyxle.devserver.settings import DevServerSettings
from pyxle.ssr import build_page_response
from pyxle.ssr.renderer import ComponentRenderer, pool_render_factory

#: Sub-directory of ``dist`` holding pre-rendered page-cache entries.
PRERENDER_DIRNAME = "prerendered"


@asynccontextmanager
async def _prerender_ambient(settings: DevServerSettings) -> AsyncIterator[None]:
    """Stand up the plugin context that page/layout loaders see at request time.

    Static pre-rendering runs loaders at build time (Next's ``getStaticProps``
    hits its data source at build, too). A loader that calls a plugin service --
    e.g. ``get_database()`` from a ``pyxle-db`` app, or a layout loader that does
    -- resolves it through the **active plugin context** (``pyxle.plugins.plugin``).
    Without this, ``--static`` raises ``PluginServiceError: No active plugin
    context`` and pre-renders nothing. This mirrors the serve lifespan
    (``starlette_app`` lifespan): load the configured plugins, run their startup
    so services register (DB connections open, migrations apply), set the active
    context, and tear it all down afterwards.
    """
    from pyxle.plugins import (
        PluginContext,
        PluginSpec,
        load_plugins,
        run_shutdown,
        run_startup,
        set_active_context,
    )

    specs = tuple(
        PluginSpec.from_config_entry(entry, source=str(settings.project_root))
        for entry in settings.plugins
    )
    plugins = load_plugins(specs)
    ctx = PluginContext(settings=settings)
    # run_startup is INSIDE the try so that if one plugin's on_startup fails, the
    # plugins that already started (e.g. opened a DB connection) are still torn
    # down by run_shutdown in the finally, rather than leaked.
    try:
        await run_startup(plugins, ctx)
        set_active_context(ctx)
        yield
    finally:
        set_active_context(None)
        await run_shutdown(plugins, ctx)


def _request_for(path: str) -> Request:
    async def _receive() -> dict:
        return {"type": "http.request", "body": b"", "more_body": False}

    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "GET",
            "path": path,
            "root_path": "",
            "query_string": b"",
            "headers": [],
        },
        _receive,
    )


async def _materialize(response: Response) -> bytes:
    body = getattr(response, "body", None)
    if body is not None:
        return bytes(body)
    chunks: list[bytes] = []
    async for chunk in response.body_iterator:
        chunks.append(
            chunk if isinstance(chunk, (bytes, bytearray)) else str(chunk).encode("utf-8")
        )
    return b"".join(chunks)


async def prerender_pages(
    *,
    settings: DevServerSettings,
    pages: Iterable[PageRoute],
    renderer: ComponentRenderer,
    prerender_dir: Path,
    clock: Callable[[], float] = time.time,
) -> list[str]:
    """Render each page and store it as a page-cache entry under ``prerender_dir``.

    Returns the route paths successfully pre-rendered. A page whose render does
    not return ``200`` is skipped -- it simply falls back to live SSR (and the
    runtime cache) at serve time. Entries are stored with no expiry; they live
    until the deploy is replaced or the route is invalidated.
    """

    backend = FileCacheBackend(prerender_dir)
    rendered: list[str] = []
    for page in pages:
        response = await build_page_response(
            request=_request_for(page.path),
            settings=settings,
            page=page,
            renderer=renderer,
        )
        if response.status_code != 200:
            continue
        body = await _materialize(response)
        # A page missing from page-manifest.json renders the framework's
        # "Missing Manifest Entry" placeholder at status 200. Never persist that
        # as a static page — it would be served to every visitor forever; skip it
        # so the route falls back to live SSR (and surfaces the build problem).
        if b"Missing Manifest Entry" in body:
            continue
        entry = CacheEntry(
            body=body,
            status_code=200,
            etag=PageCache.make_etag(body),
            stored_at=clock(),
            revalidate=None,
        )
        await backend.set(PageCache.make_key(page.path), entry)
        rendered.append(page.path)
    return rendered


def generate_static_site(
    settings: DevServerSettings, dist_dir: Path, *, logger: ConsoleLogger | None = None
) -> list[str]:  # pragma: no cover - boots the Node SSR pool; core is tested via prerender_pages
    """Pre-render every statically-renderable page into ``dist/prerendered``.

    Boots a short-lived SSR worker pool, renders each loader-less, non-dynamic
    page, and writes the results. Assumes ``pyxle build`` already produced
    ``dist``. Returns the pre-rendered route paths.
    """

    from dataclasses import replace

    from pyxle.build.manifest import load_manifest
    from pyxle.build.production import _resolve_pool_size
    from pyxle.devserver.registry import build_metadata_registry
    from pyxle.devserver.routes import build_route_table
    from pyxle.ssr.worker_pool import SsrWorkerPool

    log = logger or ConsoleLogger()
    manifest = load_manifest(dist_dir / "page-manifest.json")
    dist_settings = replace(settings, debug=False, page_manifest=manifest)
    routes = build_route_table(build_metadata_registry(dist_settings))
    static_pages = select_static_pages(routes.pages)
    if not static_pages:
        log.info("No statically-renderable pages found; skipping --static prerender.")
        return []

    async def _run() -> list[str]:
        from pyxle.ssr.template import vite_owns_stylesheets  # noqa: PLC0415

        pool = SsrWorkerPool(
            size=max(1, _resolve_pool_size(dist_settings.ssr_workers)),
            project_root=dist_settings.project_root,
            client_root=dist_settings.client_build_dir,
            pages_root=dist_settings.pages_dir,
            vite_owns_css=vite_owns_stylesheets(dist_settings),
        )
        await pool.start()
        try:
            # Establish the same plugin context a request would see, so static
            # pages whose page/layout loaders use a plugin service (e.g. the
            # pyxle-db `get_database()`) pre-render instead of raising.
            async with _prerender_ambient(dist_settings):
                renderer = ComponentRenderer(factory=pool_render_factory(pool))
                return await prerender_pages(
                    settings=dist_settings,
                    pages=static_pages,
                    renderer=renderer,
                    prerender_dir=dist_dir / PRERENDER_DIRNAME,
                )
        finally:
            await pool.stop()

    rendered = asyncio.run(_run())
    log.success(
        f"Pre-rendered {len(rendered)} static page(s) into {dist_dir / PRERENDER_DIRNAME}"
    )
    return rendered
