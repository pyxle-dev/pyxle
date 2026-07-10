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


@pytest.fixture(autouse=True)
def _reset_layout_metadata_cache():
    """Start every test with an empty layout-metadata cache.

    The cache is module-level and keyed by absolute path; resetting keeps a
    test that reuses a path from ever seeing a prior test's cached parse.
    """
    from pyxle.devserver.registry import invalidate_metadata_cache

    invalidate_metadata_cache()
    yield
    invalidate_metadata_cache()


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
    assert dynamic_page.module_key == "pyxle.server.pages.posts.id"
    assert dynamic_page.head_elements == ()
    assert metadata.sources["posts/[id].pyxl"].content_hash == dynamic_page.content_hash

    api_entry = registry.find_api("/api/greet")
    assert api_entry is not None
    assert api_entry.module_key == "pyxle.server.api.greet"
    assert metadata.sources["api/greet.py"].content_hash == api_entry.content_hash

    dynamic_api = registry.find_api("/api/posts/{id}")
    assert dynamic_api is not None
    assert dynamic_api.module_key == "pyxle.server.api.posts.id"
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

    assert key == "pyxle.server.api._1_2._123_slug_lives._.file_name"


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


def test_find_layout_head_jsx_blocks_no_layout(project: DevServerSettings) -> None:
    """Test that empty tuple is returned when no layout exists."""
    from pyxle.devserver.registry import find_layout_head_jsx_blocks

    build_once(project)

    # A page at root with no layout.pyxl
    blocks = find_layout_head_jsx_blocks(project, Path("index.pyxl"))
    assert blocks == ()


def test_find_layout_head_jsx_blocks_root_layout(project: DevServerSettings) -> None:
    """Test finding head blocks from root layout.pyxl."""
    from pyxle.devserver.registry import find_layout_head_jsx_blocks

    # Write a layout.pyxl at the root
    write_file(
        project.pages_dir / "layout.pyxl",
        """\n\nHEAD = "<meta name='viewport' content='width=device-width'/>"\n\nimport React from 'react';\n\nexport default function Layout({ children }) {\n    return <div>{children}</div>;\n}\n<Head>\n<title>Layout Title</title>\n</Head>\n""",
    )

    build_once(project)
    blocks = find_layout_head_jsx_blocks(project, Path("index.pyxl"))
    assert len(blocks) > 0
    assert any("<title>Layout Title</title>" in block for block in blocks)


def test_find_layout_head_jsx_blocks_nested_layout(project: DevServerSettings) -> None:
    """Test finding head blocks from nested layout.pyxl."""
    from pyxle.devserver.registry import find_layout_head_jsx_blocks

    # Write a nested layout
    write_file(
        project.pages_dir / "posts" / "layout.pyxl",
        """\n\nimport React from 'react';\n\nexport default function PostsLayout({ children }) {\n    return <div>{children}</div>;\n}\n<Head>\n<meta name='posts-section' content='true'/>\n</Head>\n""",
    )

    build_once(project)
    
    # Page in the posts directory should find the posts layout
    blocks = find_layout_head_jsx_blocks(project, Path("posts/[id].pyxl"))
    assert len(blocks) > 0
    assert any("posts-section" in block for block in blocks)


def test_find_layout_head_jsx_blocks_layout_hierarchy(project: DevServerSettings) -> None:
    """Test that layout hierarchy is respected (parent and nested layouts)."""
    from pyxle.devserver.registry import find_layout_head_jsx_blocks

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
    blocks = find_layout_head_jsx_blocks(project, Path("posts/[id].pyxl"))
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


# ---------------------------------------------------------------------------
# Layout-metadata cache (SSR hot-path optimization)
# ---------------------------------------------------------------------------


def _write_layout_metadata(path: Path, head_block: str) -> None:
    """Write a minimal, valid layout metadata JSON file at *path*."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "route_path": "/layout",
                "client_path": "/pages/layout.jsx",
                "server_path": "/pages/layout.py",
                "head_jsx_blocks": [head_block],
            }
        ),
        encoding="utf-8",
    )


def test_load_page_metadata_cached_avoids_reread(tmp_path: Path, monkeypatch) -> None:
    """A warm cache serves the parsed metadata without touching disk again."""
    import pyxle.devserver.registry as registry_module

    path = tmp_path / "layout.json"
    _write_layout_metadata(path, "<title>V1</title>")

    reads: list[Path] = []
    original = registry_module._load_page_metadata

    def counting_load(p: Path):
        reads.append(p)
        return original(p)

    monkeypatch.setattr(registry_module, "_load_page_metadata", counting_load)

    first = registry_module._load_page_metadata_cached(path)
    second = registry_module._load_page_metadata_cached(path)

    assert first is not None
    assert first.head_jsx_blocks == ("<title>V1</title>",)
    # Same parsed object returned from cache; the disk reader ran exactly once.
    assert second is first
    assert reads == [path]


def test_metadata_cache_invalidation_picks_up_edits(tmp_path: Path) -> None:
    """An on-disk edit is only observed after the cache is invalidated."""
    from pyxle.devserver.registry import (
        _load_page_metadata_cached,
        invalidate_metadata_cache,
    )

    path = tmp_path / "layout.json"
    _write_layout_metadata(path, "<title>V1</title>")

    assert _load_page_metadata_cached(path).head_jsx_blocks == ("<title>V1</title>",)

    # Edit the file: the cache must keep serving the old parse until invalidated.
    _write_layout_metadata(path, "<title>V2</title>")
    assert _load_page_metadata_cached(path).head_jsx_blocks == ("<title>V1</title>",)

    invalidate_metadata_cache()
    assert _load_page_metadata_cached(path).head_jsx_blocks == ("<title>V2</title>",)


def test_metadata_cache_skips_store_on_racing_invalidation(
    tmp_path: Path, monkeypatch
) -> None:
    """A read whose generation was bumped mid-parse is returned but not cached.

    Models the dev watcher thread invalidating the cache while a request is
    parsing a layout file: the in-flight result is handed back to its caller,
    but caching it would pin a value the rebuild superseded, so it is dropped
    and the next caller re-reads.
    """
    import pyxle.devserver.registry as registry_module

    path = tmp_path / "layout.json"
    _write_layout_metadata(path, "<title>V1</title>")

    original = registry_module._load_page_metadata

    def load_then_invalidate(p: Path):
        result = original(p)
        # Simulate a concurrent rebuild landing while this parse is in flight.
        registry_module.invalidate_metadata_cache()
        return result

    monkeypatch.setattr(registry_module, "_load_page_metadata", load_then_invalidate)

    value = registry_module._load_page_metadata_cached(path)
    assert value.head_jsx_blocks == ("<title>V1</title>",)
    # The racing invalidation means nothing was cached under the stale key.
    with registry_module._metadata_cache_lock:
        assert path not in registry_module._metadata_cache


def test_find_layout_walks_share_metadata_cache(tmp_path: Path, monkeypatch) -> None:
    """The head-block and loader walks read each layout file from one cache."""
    import pyxle.devserver.registry as registry_module
    from pyxle.devserver.registry import (
        find_layout_head_jsx_blocks,
        find_layout_loaders,
    )

    settings = DevServerSettings.from_project_root(tmp_path / "project")
    layout_json = (
        settings.metadata_build_dir / "pages" / "layout.json"
    )
    layout_json.parent.mkdir(parents=True, exist_ok=True)
    layout_json.write_text(
        json.dumps(
            {
                "route_path": "/layout",
                "client_path": "/pages/layout.jsx",
                "server_path": "/pages/layout.py",
                "head_jsx_blocks": ["<title>Shared</title>"],
                "loader_name": "load_layout",
            }
        ),
        encoding="utf-8",
    )

    reads: list[Path] = []
    original = registry_module._load_page_metadata

    def counting_load(p: Path):
        reads.append(p)
        return original(p)

    monkeypatch.setattr(registry_module, "_load_page_metadata", counting_load)

    blocks = find_layout_head_jsx_blocks(settings, Path("index.pyxl"))
    loaders = find_layout_loaders(settings, Path("index.pyxl"))

    assert any("<title>Shared</title>" in block for block in blocks)
    assert len(loaders) == 1 and loaders[0].loader_name == "load_layout"
    # Both walks visited layout.json but the shared cache read it from disk once.
    assert reads.count(layout_json) == 1


def test_find_layout_head_jsx_blocks_hot_reload(project: DevServerSettings) -> None:
    """Rebuilding the registry re-reads an edited layout (dev hot-reload).

    Proves the cache invalidation contract end-to-end: after a layout's
    compiled metadata changes on disk, ``build_metadata_registry`` (which the
    dev route-table refresh runs on every rebuild) drops the stale parse so the
    next render sees the new ``<Head>`` blocks.
    """
    from pyxle.devserver.registry import find_layout_head_jsx_blocks

    write_file(
        project.pages_dir / "layout.pyxl",
        """\n\nimport React from 'react';\n\nexport default function Layout({ children }) {\n    return <div>{children}</div>;\n}\n<Head>\n<meta name='layout-version' content='v1'/>\n</Head>\n""",
    )
    build_once(project)
    build_metadata_registry(project)  # warms the cache for this generation

    blocks = find_layout_head_jsx_blocks(project, Path("index.pyxl"))
    assert any("v1" in block for block in blocks)

    # Edit the layout and rebuild, exactly as the dev watcher does.
    write_file(
        project.pages_dir / "layout.pyxl",
        """\n\nimport React from 'react';\n\nexport default function Layout({ children }) {\n    return <div>{children}</div>;\n}\n<Head>\n<meta name='layout-version' content='v2'/>\n</Head>\n""",
    )
    build_once(project)
    build_metadata_registry(project)  # bumps the generation -> drops stale parse

    refreshed = find_layout_head_jsx_blocks(project, Path("index.pyxl"))
    assert any("v2" in block for block in refreshed)
    assert not any("v1" in block for block in refreshed)

