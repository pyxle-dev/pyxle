from __future__ import annotations

import json
from pathlib import Path

import pytest

from pyxle.devserver.build import load_build_metadata
from pyxle.devserver.builder import build_once
from pyxle.devserver.registry import build_metadata_registry, load_metadata_registry
from pyxle.devserver.settings import DevServerSettings


@pytest.fixture
def project(tmp_path: Path) -> DevServerSettings:
    root = tmp_path / "project"
    (root / "pages").mkdir(parents=True)
    (root / "public").mkdir()
    settings = DevServerSettings.from_project_root(root)

    write_file(
        settings.pages_dir / "index.pyxl",
        """\n\nHEAD = \"<title>Home</title>\"\n\n@server\nasync def load_home(request):\n    return {\"message\": \"hi\"}\n\n# --- JavaScript/PSX (Client + Server) ---\n\nimport React from 'react';\n\nexport default function Home({ data }) {\n    return <div>{data.message}</div>;\n}\n""",
    )

    write_file(
        settings.pages_dir / "posts/[id].pyxl",
        """import React from 'react';\n\nexport default function Post({ data }) {\n    return <article>{data.title}</article>;\n}\n""",
    )

    write_file(
        settings.pages_dir / "api/greet.py",
        """async def endpoint(request):\n    return {\"message\": \"hello\"}\n""",
    )

    write_file(
        settings.pages_dir / "api/posts/[id].py",
        """async def endpoint(request):\n    return {\"id\": request.path_params.get(\"id\")}\n""",
    )

    return settings


def write_file(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_metadata_registry_includes_pages_and_apis(project: DevServerSettings) -> None:
    build_once(project)

    registry = load_metadata_registry(project)
    metadata = load_build_metadata(project.build_root)

    assert {entry.route_path for entry in registry.pages} == {"/", "/posts/{id}"}
    assert {entry.route_path for entry in registry.apis} == {"/api/greet", "/api/posts/{id}"}
    assert all(entry.alternate_route_paths == tuple() for entry in registry.pages)
    assert all(entry.alternate_route_paths == tuple() for entry in registry.apis)

    home = registry.find_page("/")
    assert home is not None
    assert home.has_loader is True
    assert home.loader_name == "load_home"
    assert isinstance(home.loader_line, int)
    assert home.client_asset_path == "/pages/index.jsx"
    assert home.server_asset_path == "/pages/index.py"
    assert home.module_key == "pyxle.server.pages.index"
    assert home.head_elements == ("<title>Home</title>",)
    assert metadata.sources["index.pyxl"].content_hash == home.content_hash

    dynamic_page = registry.find_page("/posts/{id}")
    assert dynamic_page is not None
    assert dynamic_page.loader_name is None
    # Readable stem plus a digest of "[id]" — see test_module_keys_are_unique_per_source_file.
    assert dynamic_page.module_key.startswith("pyxle.server.pages.posts.id")
    assert dynamic_page.head_elements == ()
    assert metadata.sources["posts/[id].pyxl"].content_hash == dynamic_page.content_hash

    api_entry = registry.find_api("/api/greet")
    assert api_entry is not None
    assert api_entry.module_key == "pyxle.server.api.greet"
    assert metadata.sources["api/greet.py"].content_hash == api_entry.content_hash

    dynamic_api = registry.find_api("/api/posts/{id}")
    assert dynamic_api is not None
    assert dynamic_api.module_key.startswith("pyxle.server.api.posts.id")
    assert metadata.sources["api/posts/[id].py"].content_hash == dynamic_api.content_hash

    assert registry.find_page("/missing") is None
    assert registry.find_api("/missing") is None

    serialized = registry.to_dict()
    assert {page["route_path"] for page in serialized["pages"]} == {"/", "/posts/{id}"}
    assert all(not page.get("alternate_route_paths") for page in serialized["pages"])
    assert {api["route_path"] for api in serialized["apis"]} == {"/api/greet", "/api/posts/{id}"}
    assert all(not api.get("alternate_route_paths") for api in serialized["apis"])


def test_registry_skips_missing_artifacts(project: DevServerSettings) -> None:
    build_once(project)
    metadata = load_build_metadata(project.build_root)

    # Remove metadata JSON and server artifact for specific entries to simulate partial builds.
    (project.metadata_build_dir / "pages" / "index.json").unlink(missing_ok=True)
    (project.server_build_dir / "api" / "greet.py").unlink(missing_ok=True)

    registry = build_metadata_registry(project, metadata)

    page_routes = {entry.route_path for entry in registry.pages}
    api_routes = {entry.route_path for entry in registry.apis}

    assert page_routes == {"/posts/{id}"}
    assert api_routes == {"/api/posts/{id}"}


def test_registry_recovers_from_invalid_loader_metadata(project: DevServerSettings) -> None:
    build_once(project)
    metadata_path = project.metadata_build_dir / "pages" / "index.json"
    payload = json.loads(metadata_path.read_text(encoding="utf-8"))
    payload["loader_line"] = "not-an-int"
    payload["loader_name"] = ["not-a-string"]
    metadata_path.write_text(json.dumps(payload), encoding="utf-8")

    metadata = load_build_metadata(project.build_root)
    registry = build_metadata_registry(project, metadata)

    entry = registry.find_page("/")
    assert entry is not None
    assert entry.loader_line is None
    assert entry.loader_name is None


def test_registry_carries_cache_revalidate_from_metadata(project: DevServerSettings) -> None:
    build_once(project)
    metadata_path = project.metadata_build_dir / "pages" / "index.json"
    payload = json.loads(metadata_path.read_text(encoding="utf-8"))
    payload["cache_revalidate"] = 60
    metadata_path.write_text(json.dumps(payload), encoding="utf-8")

    metadata = load_build_metadata(project.build_root)
    registry = build_metadata_registry(project, metadata)

    entry = registry.find_page("/")
    assert entry is not None
    assert entry.cache_revalidate == 60.0


def test_module_key_sanitizes_segments() -> None:
    from pyxle.devserver import registry as registry_module

    key = registry_module._module_key(
        Path("api/[1-2]/123 slug-lives/[]/file.name.py"),
        prefix="pyxle.server.api",
        drop_leading="api",
    )

    prefix = "pyxle.server.api."
    assert key.startswith(prefix)
    segments = key[len(prefix) :].split(".")
    # Each segment keeps its readable, importable stem. A segment the cleaning
    # altered also carries a digest of the original text, so it cannot collide
    # with a differently-spelled sibling that cleans to the same name — see
    # test_module_keys_are_unique_per_source_file.
    for segment, stem in zip(
        segments, ["_1_2", "_123_slug_lives", "_", "file_name"], strict=True
    ):
        assert segment == stem or segment.startswith(stem + "_"), (segment, stem)


def test_module_keys_are_unique_per_source_file() -> None:
    """Two source files must never share a ``sys.modules`` key.

    Compiled modules are cached under this key and reused without re-checking
    which file they came from, so a shared key means one page silently serves
    the other's loader and component at a different URL, with a 200 and nothing
    in the logs. Every pair below is two legitimate pages that coexist in one
    project and cleaned to the same name before this was fixed.
    """
    from pyxle.devserver import registry as registry_module

    pairs = [
        ("docs/[slug].pyxl", "docs/[[...slug]].pyxl"),
        ("(marketing)/pricing.pyxl", "marketing/pricing.pyxl"),
        ("my-page.pyxl", "my_page.pyxl"),
        ("users/[id].pyxl", "users/id.pyxl"),
        ("blog/[...rest].pyxl", "blog/rest.pyxl"),
        ("api/embed.js.py", "api/embed_js.py"),
    ]
    for left, right in pairs:
        left_key = registry_module._module_key(
            Path(left), prefix="pyxle.server.pages"
        )
        right_key = registry_module._module_key(
            Path(right), prefix="pyxle.server.pages"
        )
        assert left_key != right_key, f"{left} and {right} share {left_key}"

    # Ordinary names are untouched, so the readable keys stay readable.
    assert (
        registry_module._module_key(Path("blog/post.pyxl"), prefix="pyxle.server.pages")
        == "pyxle.server.pages.blog.post"
    )


def test_load_page_metadata_handles_non_dict_payload(tmp_path: Path) -> None:
    from pyxle.devserver import registry as registry_module

    path = tmp_path / "meta.json"
    path.write_text("[]", encoding="utf-8")

    assert registry_module._load_page_metadata(path) is None


def test_load_page_metadata_handles_decode_errors(tmp_path: Path) -> None:
    from pyxle.devserver import registry as registry_module

    path = tmp_path / "broken.json"
    path.write_text("{invalid", encoding="utf-8")

    assert registry_module._load_page_metadata(path) is None


def test_load_page_metadata_rejects_non_string_fields(tmp_path: Path) -> None:
    from pyxle.devserver import registry as registry_module

    path = tmp_path / "meta.json"
    payload = {
        "route_path": 123,
        "client_path": "/client",
        "server_path": "/server",
    }
    path.write_text(json.dumps(payload), encoding="utf-8")

    assert registry_module._load_page_metadata(path) is None


def test_load_page_metadata_rejects_invalid_head(tmp_path: Path) -> None:
    from pyxle.devserver import registry as registry_module

    path = tmp_path / "meta.json"
    payload = {
        "route_path": "/",
        "client_path": "/pages/index.jsx",
        "server_path": "/pages/index.py",
        "head": ["<title>Home</title>", 123],
    }
    path.write_text(json.dumps(payload), encoding="utf-8")

    assert registry_module._load_page_metadata(path) is None


def test_load_page_metadata_defaults_head_when_missing(tmp_path: Path) -> None:
    from pyxle.devserver import registry as registry_module

    path = tmp_path / "meta.json"
    payload = {
        "route_path": "/",
        "client_path": "/pages/index.jsx",
        "server_path": "/pages/index.py",
        "loader_name": "load_home",
        "loader_line": 10,
    }
    path.write_text(json.dumps(payload), encoding="utf-8")

    metadata = registry_module._load_page_metadata(path)

    assert metadata is not None
    assert metadata.head_elements == ()


def test_load_page_metadata_reads_websocket(tmp_path: Path) -> None:
    """A metadata JSON with a websocket handler loads with has_websocket True —
    the disk round-trip the production server depends on."""
    from pyxle.devserver import registry as registry_module

    path = tmp_path / "meta.json"
    payload = {
        "route_path": "/chat/{room}",
        "client_path": "/pages/chat/[room].jsx",
        "server_path": "/pages/chat/[room].py",
        "websocket_name": "websocket",
        "websocket_line": 3,
    }
    path.write_text(json.dumps(payload), encoding="utf-8")

    metadata = registry_module._load_page_metadata(path)
    assert metadata is not None
    assert metadata.has_websocket is True
    assert metadata.websocket_name == "websocket"
    assert metadata.websocket_line == 3


def test_load_page_metadata_defaults_websocket_for_old_builds(tmp_path: Path) -> None:
    """A pre-2.5 build's metadata JSON (no websocket key) loads with
    has_websocket False — never crashes ``pyxle serve --skip-build``."""
    from pyxle.devserver import registry as registry_module

    path = tmp_path / "meta.json"
    payload = {
        "route_path": "/",
        "client_path": "/pages/index.jsx",
        "server_path": "/pages/index.py",
        "loader_name": "load_home",
        "loader_line": 10,
    }
    path.write_text(json.dumps(payload), encoding="utf-8")

    metadata = registry_module._load_page_metadata(path)
    assert metadata is not None
    assert metadata.has_websocket is False
    assert metadata.websocket_name is None


def test_find_layout_head_contributions_no_layout(project: DevServerSettings) -> None:
    """Test that both channels are empty when no layout exists."""
    from pyxle.devserver.registry import find_layout_head_contributions

    build_once(project)

    # A page at root with no layout.pyxl
    contribution = find_layout_head_contributions(project, Path("index.pyxl"))
    assert contribution.jsx_blocks == ()
    assert contribution.head_sources == ()


def test_find_layout_head_contributions_root_layout(project: DevServerSettings) -> None:
    """A layout's ``<Head>`` JSX and its Python ``HEAD`` variable come back in
    separate channels.

    They are different kinds of thing — unevaluated JSX source versus finished
    HTML — and only the first may be filtered for unevaluated expressions
    downstream. Returning them in one list is what deleted a layout's JSON-LD
    from every page under it.
    """
    from pyxle.devserver.registry import find_layout_head_contributions

    # Write a layout.pyxl at the root
    write_file(
        project.pages_dir / "layout.pyxl",
        """\n\nHEAD = "<meta name='viewport' content='width=device-width'/>"\n\nimport React from 'react';\n\nexport default function Layout({ children }) {\n    return <div>{children}</div>;\n}\n<Head>\n<title>Layout Title</title>\n</Head>\n""",
    )

    build_once(project)
    contribution = find_layout_head_contributions(project, Path("index.pyxl"))

    head_elements = [
        element
        for source in contribution.head_sources
        for element in source.static_elements
    ]

    assert any("<title>Layout Title</title>" in block for block in contribution.jsx_blocks)
    assert any("width=device-width" in element for element in head_elements)
    # ...and neither channel has swallowed the other.
    assert not any("width=device-width" in block for block in contribution.jsx_blocks)
    assert not any("Layout Title" in element for element in head_elements)


def test_find_layout_head_contributions_nested_layout(project: DevServerSettings) -> None:
    """Test finding head blocks from nested layout.pyxl."""
    from pyxle.devserver.registry import find_layout_head_contributions

    # Write a nested layout
    write_file(
        project.pages_dir / "posts" / "layout.pyxl",
        """\n\nimport React from 'react';\n\nexport default function PostsLayout({ children }) {\n    return <div>{children}</div>;\n}\n<Head>\n<meta name='posts-section' content='true'/>\n</Head>\n""",
    )

    build_once(project)

    # Page in the posts directory should find the posts layout
    blocks = find_layout_head_contributions(project, Path("posts/[id].pyxl")).jsx_blocks
    assert len(blocks) > 0
    assert any("posts-section" in block for block in blocks)


def test_find_layout_head_contributions_layout_hierarchy(project: DevServerSettings) -> None:
    """Test that layout hierarchy is respected (parent and nested layouts)."""
    from pyxle.devserver.registry import find_layout_head_contributions

    # Write root layout
    write_file(
        project.pages_dir / "layout.pyxl",
        """\n\nimport React from 'react';\n\nexport default function RootLayout({ children }) {\n    return <html>{children}</html>;\n}\n<Head>\n<meta name='root' content='true'/>\n</Head>\n""",
    )

    # Write nested layout
    write_file(
        project.pages_dir / "posts" / "layout.pyxl",
        """\n\nimport React from 'react';\n\nexport default function PostsLayout({ children }) {\n    return <section>{children}</section>;\n}\n<Head>\n<meta name='posts' content='true'/>\n</Head>\n""",
    )

    build_once(project)

    # Page in posts directory should find both layouts
    blocks = find_layout_head_contributions(project, Path("posts/[id].pyxl")).jsx_blocks
    assert len(blocks) >= 2
    # Should have both meta tags
    all_blocks = " ".join(blocks)
    assert "root" in all_blocks
    assert "posts" in all_blocks


# ---------------------------------------------------------------------------
# find_layout_loaders
# ---------------------------------------------------------------------------


def test_find_layout_loaders_no_layout(project: DevServerSettings) -> None:
    """Returns empty tuple when no layout file exists."""
    from pyxle.devserver.registry import find_layout_loaders

    build_once(project)
    loaders = find_layout_loaders(project, Path("index.pyxl"))
    assert loaders == ()


def test_find_layout_loaders_layout_without_loader(project: DevServerSettings) -> None:
    """Layout file with no @server decorator yields no loader info."""
    from pyxle.devserver.registry import find_layout_loaders

    write_file(
        project.pages_dir / "layout.pyxl",
        "import React from 'react';\n\nexport default function Layout({ children }) {\n    return <div>{children}</div>;\n}\n",
    )

    build_once(project)
    loaders = find_layout_loaders(project, Path("index.pyxl"))
    assert loaders == ()


def test_find_layout_loaders_layout_with_loader(project: DevServerSettings) -> None:
    """Layout file with @server decorator is discovered."""
    from pyxle.devserver.registry import find_layout_loaders

    write_file(
        project.pages_dir / "layout.pyxl",
        "@server\nasync def load_layout(request):\n    return {'app': 'test'}\n\nimport React from 'react';\n\nexport default function Layout({ children }) {\n    return <div>{children}</div>;\n}\n",
    )

    build_once(project)
    loaders = find_layout_loaders(project, Path("index.pyxl"))
    assert len(loaders) == 1
    assert loaders[0].loader_name == "load_layout"
    assert loaders[0].server_module_path.exists()


def test_find_layout_loaders_nested_hierarchy(project: DevServerSettings) -> None:
    """Both root and nested layout loaders are discovered in order."""
    from pyxle.devserver.registry import find_layout_loaders

    write_file(
        project.pages_dir / "layout.pyxl",
        "@server\nasync def load_root(request):\n    return {'level': 'root'}\n\nimport React from 'react';\n\nexport default function RootLayout({ children }) {\n    return <html>{children}</html>;\n}\n",
    )
    write_file(
        project.pages_dir / "posts" / "layout.pyxl",
        "@server\nasync def load_posts(request):\n    return {'level': 'posts'}\n\nimport React from 'react';\n\nexport default function PostsLayout({ children }) {\n    return <section>{children}</section>;\n}\n",
    )

    build_once(project)
    loaders = find_layout_loaders(project, Path("posts/[id].pyxl"))
    assert len(loaders) == 2
    # Closest layout first
    assert loaders[0].loader_name == "load_posts"
    assert loaders[1].loader_name == "load_root"



def test_api_route_nested_under_a_dynamic_section(project: DevServerSettings) -> None:
    """An endpoint beneath a section carries the whole path, dynamic segments
    and file extension included.

    This is what a per-tenant compatibility API needs — the Statuspage-shaped
    ``/s/{slug}/api/v2/summary.json`` — and it was unreachable while only a
    top-level ``pages/api/`` counted.
    """
    write_file(
        project.pages_dir / "s/[slug]/api/v2/summary.json.py",
        "async def endpoint(request):\n    return None\n",
    )

    build_once(project)
    registry = load_metadata_registry(project)

    entry = registry.find_api("/s/{slug}/api/v2/summary.json")
    assert entry is not None, [api.route_path for api in registry.apis]
    # Bracket and dot segments have to survive into an importable module name.
    assert entry.module_key.startswith("pyxle.server.api.s.slug")
    assert entry.module_key.split(".")[-1].startswith("summary_json")
    assert entry.server_module_path.exists()
