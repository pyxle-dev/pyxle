"""Structural contract for ``_build_page_manifest``.

``page-manifest.json`` is the bridge between the build pipeline and the
production server: it tells the SSR runtime which compiled client/server
assets, loader, and head metadata belong to each route. The CSS / hashed
asset-path half of that contract is pinned in ``test_pipeline_css.py``; this
file pins the rest -- the server block, loader and head propagation, route
aliasing, the no-Vite-manifest fallback, and API entries.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pyxle.build.pipeline import _build_page_manifest
from pyxle.devserver.registry import (
    ApiRegistryEntry,
    MetadataRegistry,
    PageRegistryEntry,
)
from pyxle.devserver.settings import DevServerSettings


def _project(tmp_path: Path) -> tuple[DevServerSettings, Path]:
    project = tmp_path / "project"
    project.mkdir()
    return DevServerSettings.from_project_root(project), project


def _make_page(
    route_path: str,
    *,
    project: Path,
    alternate_route_paths: tuple[str, ...] = (),
    loader_name: str | None = None,
    loader_line: int | None = None,
    head_elements: tuple[str, ...] = (),
    head_jsx_blocks: tuple[str, ...] = (),
) -> PageRegistryEntry:
    """Build a minimal ``PageRegistryEntry`` for ``_build_page_manifest`` input."""

    return PageRegistryEntry(
        route_path=route_path,
        alternate_route_paths=alternate_route_paths,
        source_relative_path=Path("pages/index.pyxl"),
        source_absolute_path=project / "pages" / "index.pyxl",
        server_module_path=project / ".pyxle-build" / "server" / "pages" / "index.py",
        client_module_path=project / ".pyxle-build" / "client" / "pages" / "index.jsx",
        metadata_path=project / ".pyxle-build" / "metadata" / "pages" / "index.json",
        client_asset_path="/pages/index.jsx",
        server_asset_path="server/pages/index.py",
        module_key="pyxle.server.pages.index",
        content_hash="hash123",
        loader_name=loader_name,
        loader_line=loader_line,
        head_elements=head_elements,
        head_is_dynamic=False,
        head_jsx_blocks=head_jsx_blocks,
    )


def test_manifest_falls_back_to_raw_client_asset_without_vite(tmp_path: Path) -> None:
    """Without a Vite manifest the client file stays the raw asset path (no
    ``dist/`` prefix) and the server block carries the module key."""

    settings, project = _project(tmp_path)
    registry = MetadataRegistry(pages=[_make_page("/", project=project)], apis=[])

    manifest = _build_page_manifest(settings, registry, vite_manifest=None)

    entry = manifest["/"]
    assert entry["client"] == {
        "file": "/pages/index.jsx",
        "imports": [],
        "css": [],
    }
    assert entry["server"] == {
        "file": "server/pages/index.py",
        "module_key": "pyxle.server.pages.index",
    }
    assert "loader" not in entry
    assert "head" not in entry
    assert "head_jsx_blocks" not in entry


def test_manifest_propagates_loader_metadata(tmp_path: Path) -> None:
    """A page with a loader exposes its name and source line for the server."""

    settings, project = _project(tmp_path)
    page = _make_page("/", project=project, loader_name="load_home", loader_line=12)
    registry = MetadataRegistry(pages=[page], apis=[])

    manifest = _build_page_manifest(settings, registry)

    assert manifest["/"]["loader"] == {"name": "load_home", "line": 12}


def test_manifest_propagates_head_elements_and_jsx_blocks(tmp_path: Path) -> None:
    """Static head elements and dynamic ``<Head>`` JSX blocks flow through."""

    settings, project = _project(tmp_path)
    page = _make_page(
        "/",
        project=project,
        head_elements=("<title>Home</title>",),
        head_jsx_blocks=("<Head><meta name=\"x\" /></Head>",),
    )
    registry = MetadataRegistry(pages=[page], apis=[])

    manifest = _build_page_manifest(settings, registry)

    entry = manifest["/"]
    assert entry["head"] == ["<title>Home</title>"]
    assert entry["head_jsx_blocks"] == ['<Head><meta name="x" /></Head>']


def test_manifest_aliases_alternate_routes_to_the_same_entry(tmp_path: Path) -> None:
    """Alternate route paths reuse the page's entry verbatim."""

    settings, project = _project(tmp_path)
    page = _make_page(
        "/", project=project, alternate_route_paths=("/index", "/home")
    )
    registry = MetadataRegistry(pages=[page], apis=[])

    manifest = _build_page_manifest(settings, registry)

    assert manifest["/"] == manifest["/index"] == manifest["/home"]


def test_manifest_emits_api_entries(tmp_path: Path) -> None:
    """API endpoints are emitted with a ``type: api`` marker and server block."""

    settings, project = _project(tmp_path)
    api = ApiRegistryEntry(
        route_path="/api/health",
        alternate_route_paths=(),
        source_relative_path=Path("pages/api/health.py"),
        source_absolute_path=project / "pages" / "api" / "health.py",
        server_module_path=project
        / ".pyxle-build"
        / "server"
        / "pages"
        / "api"
        / "health.py",
        module_key="pyxle.server.pages.api.health",
        content_hash="apihash",
    )
    registry = MetadataRegistry(pages=[], apis=[api])

    manifest = _build_page_manifest(settings, registry)

    assert manifest["/api/health"] == {
        "type": "api",
        "server": {
            "file": "pages/api/health.py",
            "module_key": "pyxle.server.pages.api.health",
        },
    }


def test_build_time_check_and_load_manifest_share_one_rule(tmp_path: Path) -> None:
    """``_require_servable_manifest`` must apply *exactly* the rule
    ``load_manifest`` applies, so "the build succeeded" and "the build is
    servable" cannot drift apart.

    The leading-slash fallback above is the shape a bundle-less build produces;
    it is rejected at build time and at serve time, by the same validator.
    """
    import json as _json

    from pyxle.build.manifest import load_manifest
    from pyxle.build.pipeline import ClientBuildError, _require_servable_manifest

    settings, project = _project(tmp_path)
    registry = MetadataRegistry(pages=[_make_page("/", project=project)], apis=[])
    unservable = _build_page_manifest(settings, registry, vite_manifest=None)

    manifest_path = tmp_path / "page-manifest.json"

    # Build time: refuses, naming the offending entry.
    with pytest.raises(ClientBuildError) as excinfo:
        _require_servable_manifest(unservable, manifest_path)
    assert "/pages/index.jsx" in str(excinfo.value)

    # Serve time: the same payload is rejected by load_manifest.
    manifest_path.write_text(_json.dumps(unservable), encoding="utf-8")
    with pytest.raises(ValueError, match="unsafe path"):
        load_manifest(manifest_path)

    # And a real (Vite-built) manifest passes both gates.
    servable = _build_page_manifest(
        settings,
        registry,
        vite_manifest={"pages/index.jsx": {"file": "assets/index-DEADBEEF.js"}},
    )
    _require_servable_manifest(servable, manifest_path)
    manifest_path.write_text(_json.dumps(servable), encoding="utf-8")
    assert load_manifest(manifest_path)["/"]["client"]["file"] == (
        "dist/assets/index-DEADBEEF.js"
    )
