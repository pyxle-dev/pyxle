"""A ``STANDALONE`` layout stops the chain — wrappers, loaders and head blocks.

All three walks have to agree. Stopping the wrapper but not the loader would
mean an outer layout's query running on every request to a section that does not
render it; stopping the loader but not the head would mean a page that opted out
of the app shell still inheriting its analytics snippet.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pyxle.devserver.registry import find_layout_head_contributions, find_layout_loaders
from pyxle.devserver.settings import DevServerSettings


def head_variable(contribution):
    """The layout chain's ``HEAD`` elements, resolved the way a render resolves
    them. These fixtures declare literal heads, so no module is imported."""
    from pyxle.ssr.view import _resolve_layout_head_elements

    return _resolve_layout_head_elements(contribution.head_sources, {})


@pytest.fixture
def project(tmp_path):
    """A build tree with a root layout and a standalone one beneath it."""
    settings = DevServerSettings.from_project_root(tmp_path)

    def metadata(relative: str, **fields):
        path = settings.metadata_build_dir / "pages" / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "route_path": "/" + relative.replace(".json", ""),
            "alternate_route_paths": [],
            "client_path": "/pages/x.jsx",
            "server_path": "/pages/x.py",
            "loader_name": None,
            "loader_line": None,
            "head": [],
            "head_dynamic": False,
            "scripts": [],
            "images": [],
            "head_jsx_blocks": [],
            "actions": [],
            "websocket_name": None,
            "websocket_line": None,
            "standalone": False,
        }
        payload.update(fields)
        path.write_text(json.dumps(payload))
        server = settings.server_build_dir / "pages" / relative.replace(".json", ".py")
        server.parent.mkdir(parents=True, exist_ok=True)
        server.write_text("")

    metadata(
        "layout.json",
        loader_name="load",
        head_jsx_blocks=['<script src="/analytics.js" />'],
        head=['<style>.app{color:red}</style>'],
    )
    metadata(
        "public/layout.json",
        loader_name="load",
        standalone=True,
        head_jsx_blocks=['<meta name="robots" content="noindex" />'],
        head=['<style>.public{color:blue}</style>'],
    )
    metadata("public/status.json")
    metadata("admin/monitors.json")
    # A standalone **template**, not layout. Templates carry the same directive
    # and are walked by the same three passes, so they belong in the same
    # fixture -- the wrapper walk read only `layout.json` and honoured the head
    # and the loader here while still wrapping the page in the root layout.
    metadata(
        "checkout/template.json",
        loader_name="load",
        standalone=True,
        head_jsx_blocks=['<meta name="checkout" content="yes" />'],
        head=['<style>.checkout{color:green}</style>'],
    )
    metadata("checkout/pay.json")
    return settings


class TestTheLoaderChain:
    def test_a_page_under_a_standalone_layout_runs_only_its_loader(self, project):
        """Otherwise the outer layout's query runs on every request to a
        section that never renders it."""
        found = find_layout_loaders(project, Path("public/status.pyxl"))
        paths = [str(info.relative_path) for info in found]

        assert any("public" in p for p in paths)
        assert not any(p in ("layout.pyxl", "template.pyxl") for p in paths), (
            "the root layout's loader still runs for a standalone section"
        )

    def test_an_ordinary_page_still_gets_the_root_layout(self, project):
        """The change must be invisible to every page that did not ask for it."""
        found = find_layout_loaders(project, Path("admin/monitors.pyxl"))
        assert [str(i.relative_path) for i in found] == ["layout.pyxl"]


class TestTheHeadChain:
    def test_a_standalone_section_does_not_inherit_the_app_head(self, project):
        contribution = find_layout_head_contributions(project, Path("public/status.pyxl"))
        assert any("robots" in b for b in contribution.jsx_blocks)
        assert not any("analytics" in b for b in contribution.jsx_blocks), (
            "a page that opted out of the app shell inherited its analytics tag"
        )

    def test_the_head_variable_channel_stops_there_too(self, project):
        """Both channels walk the same ancestors, so both must stop at the same
        place — otherwise a standalone section still inherits the app shell's
        critical CSS."""
        contribution = find_layout_head_contributions(project, Path("public/status.pyxl"))
        assert any(".public" in e for e in head_variable(contribution))
        assert not any(".app" in e for e in head_variable(contribution))

    def test_an_ordinary_page_still_inherits_it(self, project):
        contribution = find_layout_head_contributions(project, Path("admin/monitors.pyxl"))
        assert any("analytics" in b for b in contribution.jsx_blocks)
        assert any(".app" in e for e in head_variable(contribution))


class TestTheWrapperChain:
    def test_discovery_stops_at_a_standalone_layout(self, project, tmp_path):
        """The client composer walks the same ancestors and must stop at the
        same place, or the page renders inside a shell whose loader never ran.
        """
        from pyxle.devserver.layouts import _discover_wrappers

        pages_root = project.client_build_dir / "pages"
        for relative in ("layout.jsx", "public/layout.jsx"):
            path = pages_root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("export default function L({children}){return children}")

        wrappers = _discover_wrappers(Path("public"), project)
        relatives = [str(w.relative_path) for w in wrappers]

        assert any("public" in r for r in relatives)
        assert "layout.jsx" not in relatives, (
            "the root layout still wraps a standalone section"
        )

    def test_discovery_stops_at_a_standalone_template_too(self, project):
        """`template.pyxl` carries `STANDALONE` exactly as `layout.pyxl` does.

        This walk used to read only `layout.json`, so a standalone template got
        its head dropped and its ancestors' loaders skipped while their markup
        still wrapped the page -- it rendered inside a layout whose loader never
        ran, so any component of that layout reading its own data found nothing.
        """
        from pyxle.devserver.layouts import _discover_wrappers

        pages_root = project.client_build_dir / "pages"
        for relative in ("layout.jsx", "checkout/template.jsx"):
            path = pages_root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("export default function L({children}){return children}")

        wrappers = _discover_wrappers(Path("checkout"), project)
        relatives = [str(w.relative_path) for w in wrappers]

        assert any("checkout" in r for r in relatives), (
            "the standalone template stopped wrapping its own pages"
        )
        assert "layout.jsx" not in relatives, (
            "the root layout still wraps a section behind a standalone template"
        )

    def test_an_ordinary_directory_still_gets_every_ancestor(self, project):
        from pyxle.devserver.layouts import _discover_wrappers

        pages_root = project.client_build_dir / "pages"
        path = pages_root / "layout.jsx"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("export default function L({children}){return children}")

        wrappers = _discover_wrappers(Path("admin"), project)
        assert [str(w.relative_path) for w in wrappers] == ["layout.jsx"]


class TestAllThreeWalksAgreeForATemplate:
    """The module docstring claims all three walks agree. Until the wrapper walk
    learned about templates that was true for `layout.pyxl` only, and untested
    for `template.pyxl` -- which is exactly where it was false."""

    def test_the_loader_walk_stops_at_a_standalone_template(self, project):
        found = find_layout_loaders(project, Path("checkout/pay.pyxl"))
        paths = [str(info.relative_path) for info in found]
        assert any("checkout" in p for p in paths)
        assert "layout.pyxl" not in paths

    def test_the_head_walk_stops_at_a_standalone_template(self, project):
        contribution = find_layout_head_contributions(project, Path("checkout/pay.pyxl"))
        assert any("checkout" in b for b in contribution.jsx_blocks)
        assert not any("analytics" in b for b in contribution.jsx_blocks)
        assert any(".checkout" in e for e in head_variable(contribution))
        assert not any(".app" in e for e in head_variable(contribution))
