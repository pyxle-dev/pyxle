from __future__ import annotations

from pathlib import Path

import pytest

from pyxle.devserver.builder import build_once
from pyxle.devserver.path_utils import (
    route_path_from_relative,
    route_path_variants_from_relative,
)
from pyxle.devserver.registry import load_metadata_registry
from pyxle.devserver.routes import build_route_table
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
        settings.pages_dir / "blog/index.pyxl",
        """import React from 'react';\n\nexport default function BlogIndex() {\n    return <section>Blog</section>;\n}\n""",
    )

    write_file(
        settings.pages_dir / "posts/[id].pyxl",
        """import React from 'react';\n\nexport default function Post({ data }) {\n    return <article>{data.title}</article>;\n}\n""",
    )

    write_file(
        settings.pages_dir / "docs/[[...slug]].pyxl",
        """import React from 'react';\n\nexport default function Docs() {\n    return <article>Docs</article>;\n}\n""",
    )

    write_file(
        settings.pages_dir / "(marketing)/about.pyxl",
        """import React from 'react';\n\nexport default function About() {\n    return <section>About</section>;\n}\n""",
    )

    write_file(
        settings.pages_dir / "api/greet.py",
        """async def endpoint(request):\n    return {\"message\": \"hello\"}\n""",
    )

    write_file(
        settings.pages_dir / "api/posts/[id].py",
        """async def endpoint(request):\n    return {\"id\": request.path_params.get(\"id\")}\n""",
    )

    write_file(
        settings.pages_dir / "api/files/[[...path]].py",
        """async def endpoint(request):\n    return {\"path\": request.path_params.get(\"path\")}\n""",
    )

    return settings


def write_file(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_route_path_from_relative_converts_dynamic_segments() -> None:
    assert route_path_from_relative(Path("index.pyxl")) == "/"
    assert route_path_from_relative(Path("posts/[id].pyxl")) == "/posts/{id}"
    assert route_path_from_relative(Path("blog/index.pyxl")) == "/blog"
    assert route_path_from_relative(Path("api/posts/[id].py")) == "/api/posts/{id}"
    assert route_path_from_relative(Path("(marketing)/about.pyxl")) == "/about"
    assert route_path_from_relative(Path("docs/[...slug].pyxl")) == "/docs/{slug:path}"
    assert route_path_from_relative(Path("[[...slug]].pyxl")) == "/"


def test_route_path_variants_include_optional_catchall() -> None:
    spec = route_path_variants_from_relative(Path("docs/[[...slug]].pyxl"))
    assert spec.primary == "/docs"
    assert spec.aliases == ("/docs/{slug:path}",)

    root_spec = route_path_variants_from_relative(Path("[[...segments]].pyxl"))
    assert root_spec.primary == "/"
    assert root_spec.aliases == ("/{segments:path}",)


def test_build_route_table_generates_expected_descriptors(project: DevServerSettings) -> None:
    build_once(project)
    registry = load_metadata_registry(project)

    table = build_route_table(registry)

    page_paths = {route.path for route in table.pages}
    api_paths = {route.path for route in table.apis}

    assert page_paths == {
        "/",
        "/blog",
        "/posts/{id}",
        "/docs",
        "/docs/{slug:path}",
        "/about",
    }
    assert api_paths == {"/api/greet", "/api/posts/{id}", "/api/files", "/api/files/{path:path}"}

    home_route = table.find_page("/")
    assert home_route is not None
    assert home_route.has_loader is True
    assert home_route.loader_name == "load_home"
    assert home_route.module_key == "pyxle.server.pages.index"
    assert home_route.client_asset_path == "/pages/index.jsx"
    assert home_route.head_elements == ("<title>Home</title>",)

    blog_route = table.find_page("/blog")
    assert blog_route is not None
    assert blog_route.path == "/blog"
    assert blog_route.server_module_path.as_posix().endswith("server/pages/blog/index.py")
    assert blog_route.head_elements == ()

    dynamic_route = table.find_page("/posts/{id}")
    assert dynamic_route is not None
    assert dynamic_route.has_loader is False
    assert dynamic_route.module_key == "pyxle.server.pages.posts.id"
    assert dynamic_route.head_elements == ()

    optional_base = table.find_page("/docs")
    optional_alias = table.find_page("/docs/{slug:path}")
    assert optional_base is not None
    assert optional_alias is not None
    assert optional_base.client_module_path == optional_alias.client_module_path

    grouped_route = table.find_page("/about")
    assert grouped_route is not None
    assert grouped_route.source_relative_path.as_posix() == "(marketing)/about.pyxl"

    api_route = table.find_api("/api/posts/{id}")
    assert api_route is not None
    assert api_route.module_key == "pyxle.server.api.posts.id"
    assert api_route.server_module_path.as_posix().endswith("server/api/posts/[id].py")

    optional_api_base = table.find_api("/api/files")
    optional_api_alias = table.find_api("/api/files/{path:path}")
    assert optional_api_base is not None
    assert optional_api_alias is not None
    assert optional_api_base.source_relative_path == optional_api_alias.source_relative_path

    # Ensure dynamic conversion always uses braces regardless of metadata source.
    for route in table.pages + table.apis:
        assert "[" not in route.path and "]" not in route.path


def test_loading_pages_are_excluded_from_routing_and_collected(tmp_path: Path) -> None:
    root = tmp_path / "project"
    (root / "pages").mkdir(parents=True)
    (root / "public").mkdir()
    settings = DevServerSettings.from_project_root(root)

    write_file(
        settings.pages_dir / "index.pyxl",
        "import React from 'react';\n\n"
        "export default function Home() {\n  return <div>Home</div>;\n}\n",
    )
    write_file(
        settings.pages_dir / "loading.pyxl",
        "import React from 'react';\n\n"
        "export default function Loading() {\n  return <p>Loading…</p>;\n}\n",
    )
    write_file(
        settings.pages_dir / "dashboard/index.pyxl",
        "import React from 'react';\n\n"
        "export default function Dashboard() {\n  return <main>Dash</main>;\n}\n",
    )
    write_file(
        settings.pages_dir / "dashboard/loading.pyxl",
        "import React from 'react';\n\n"
        "export default function DashLoading() {\n  return <p>Loading dash…</p>;\n}\n",
    )

    build_once(settings)
    table = build_route_table(load_metadata_registry(settings))

    # loading.pyxl is compiled but never served as a normal page.
    page_paths = {route.path for route in table.pages}
    assert "/loading" not in page_paths
    assert "/dashboard/loading" not in page_paths
    assert "/" in page_paths and "/dashboard" in page_paths

    # ...and it is collected into the loading-boundary set instead.
    loading_sources = {
        route.source_relative_path.as_posix() for route in table.loading_boundary_pages
    }
    assert loading_sources == {"loading.pyxl", "dashboard/loading.pyxl"}


def test_loading_boundary_is_stamped_on_nearest_pages(tmp_path: Path) -> None:
    root = tmp_path / "project"
    (root / "pages").mkdir(parents=True)
    (root / "public").mkdir()
    settings = DevServerSettings.from_project_root(root)

    page = "import React from 'react';\n\nexport default function P() {{ return <main>{0}</main>; }}\n"
    loading = "import React from 'react';\n\nexport default function L() {{ return <p>loading {0}</p>; }}\n"
    write_file(settings.pages_dir / "index.pyxl", page.format("home"))
    write_file(settings.pages_dir / "loading.pyxl", loading.format("root"))
    write_file(settings.pages_dir / "dashboard/index.pyxl", page.format("dash"))
    write_file(settings.pages_dir / "dashboard/settings.pyxl", page.format("settings"))
    write_file(settings.pages_dir / "dashboard/loading.pyxl", loading.format("dash"))

    build_once(settings)
    table = build_route_table(load_metadata_registry(settings))
    by_path = {route.path: route for route in table.pages}

    # Root loading.pyxl applies to "/"; the nearer dashboard/loading.pyxl wins
    # for "/dashboard" and "/dashboard/settings".
    assert by_path["/"].loading_boundary is not None
    assert by_path["/"].loading_boundary.source_relative_path.as_posix() == "loading.pyxl"
    assert (
        by_path["/dashboard"].loading_boundary.source_relative_path.as_posix()
        == "dashboard/loading.pyxl"
    )
    assert (
        by_path["/dashboard/settings"].loading_boundary.source_relative_path.as_posix()
        == "dashboard/loading.pyxl"
    )


def test_no_loading_boundary_leaves_routes_unstamped(tmp_path: Path) -> None:
    root = tmp_path / "project"
    (root / "pages").mkdir(parents=True)
    (root / "public").mkdir()
    settings = DevServerSettings.from_project_root(root)
    write_file(
        settings.pages_dir / "index.pyxl",
        "import React from 'react';\n\nexport default function P() { return <main>home</main>; }\n",
    )

    build_once(settings)
    table = build_route_table(load_metadata_registry(settings))
    assert all(route.loading_boundary is None for route in table.pages)


def test_error_boundary_is_stamped_on_nearest_pages(tmp_path: Path) -> None:
    root = tmp_path / "project"
    (root / "pages").mkdir(parents=True)
    (root / "public").mkdir()
    settings = DevServerSettings.from_project_root(root)

    page = "import React from 'react';\n\nexport default function P() {{ return <main>{0}</main>; }}\n"
    error = "import React from 'react';\n\nexport default function E() {{ return <p>error {0}</p>; }}\n"
    write_file(settings.pages_dir / "index.pyxl", page.format("home"))
    write_file(settings.pages_dir / "error.pyxl", error.format("root"))
    write_file(settings.pages_dir / "dashboard/index.pyxl", page.format("dash"))
    write_file(settings.pages_dir / "dashboard/settings.pyxl", page.format("settings"))
    write_file(settings.pages_dir / "dashboard/error.pyxl", error.format("dash"))

    build_once(settings)
    table = build_route_table(load_metadata_registry(settings))
    by_path = {route.path: route for route in table.pages}

    # error.pyxl is collected, not routed as a normal page.
    page_paths = {route.path for route in table.pages}
    assert "/error" not in page_paths and "/dashboard/error" not in page_paths
    error_sources = {
        route.source_relative_path.as_posix() for route in table.error_boundary_pages
    }
    assert error_sources == {"error.pyxl", "dashboard/error.pyxl"}

    # Root error.pyxl applies to "/"; the nearer dashboard/error.pyxl wins for
    # "/dashboard" and "/dashboard/settings" (closest-ancestor walk-up).
    assert by_path["/"].error_boundary is not None
    assert by_path["/"].error_boundary.source_relative_path.as_posix() == "error.pyxl"
    assert (
        by_path["/dashboard"].error_boundary.source_relative_path.as_posix()
        == "dashboard/error.pyxl"
    )
    assert (
        by_path["/dashboard/settings"].error_boundary.source_relative_path.as_posix()
        == "dashboard/error.pyxl"
    )


def test_no_error_boundary_leaves_routes_unstamped(tmp_path: Path) -> None:
    root = tmp_path / "project"
    (root / "pages").mkdir(parents=True)
    (root / "public").mkdir()
    settings = DevServerSettings.from_project_root(root)
    write_file(
        settings.pages_dir / "index.pyxl",
        "import React from 'react';\n\nexport default function P() { return <main>home</main>; }\n",
    )

    build_once(settings)
    table = build_route_table(load_metadata_registry(settings))
    assert all(route.error_boundary is None for route in table.pages)


def test_build_route_table_falls_back_to_inferred_path(project: DevServerSettings) -> None:
    build_once(project)

    metadata_path = project.metadata_build_dir / "pages" / "index.json"
    payload = metadata_path.read_text(encoding="utf-8")
    metadata_path.write_text(payload.replace("\"route_path\": \"/\"", "\"route_path\": \"/home-custom\""), encoding="utf-8")

    registry = load_metadata_registry(project)
    table = build_route_table(registry)

    route = table.find_page("/")
    assert route is not None
    assert route.path == "/"

    missing = table.find_api("/does-not-exist")
    assert missing is None


def test_select_static_pages_filters_loaders_and_dynamic_routes() -> None:
    from types import SimpleNamespace

    from pyxle.devserver.routes import select_static_pages

    pages = [
        SimpleNamespace(path="/", has_loader=False),  # static
        SimpleNamespace(path="/about", has_loader=False),  # static
        SimpleNamespace(path="/feed", has_loader=True),  # has a loader -> skip
        SimpleNamespace(path="/posts/{slug}", has_loader=False),  # dynamic -> skip
    ]

    assert [page.path for page in select_static_pages(pages)] == ["/", "/about"]

