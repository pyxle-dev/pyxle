from __future__ import annotations

from pathlib import Path

from starlette.testclient import TestClient

from pyxle.devserver.builder import build_once
from pyxle.devserver.registry import load_metadata_registry
from pyxle.devserver.routes import build_route_table
from pyxle.devserver.settings import DevServerSettings
from pyxle.devserver.starlette_app import create_starlette_app
from tests.ssr.utils import ensure_test_node_modules


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_ssr_integration_renders_pages_with_loader(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    (project_root / "public").mkdir(parents=True)
    settings = DevServerSettings.from_project_root(project_root)
    ensure_test_node_modules(project_root)

    _write(
        settings.pages_dir / "index.pyxl",
        """\n\nHEAD = [\n    \"<title>SSR Integration</title>\",\n    '<meta name=\"description\" content=\"SSR integration test\" />',\n]\n\ndef server(fn):\n    return fn\n\n@server\nasync def load_home(request):\n    return ({\n        \"message\": \"Hello from SSR\",\n        \"query\": request.query_params.get(\"hello\", \"world\"),\n    }, 201)\n\n# --- JavaScript/PSX (Client + Server) ---\n\nimport React from 'react';\n\nexport default function Home({ data }) {\n    return (\n        <main data-query={data.query}>\n            <h1>{data.message}</h1>\n        </main>\n    );\n}\n""",
    )

    _write(
        settings.pages_dir / "posts/[id].pyxl",
        """import React from 'react';\n\nexport default function Post({ data }) {\n    return <section data-route=\"post\">Static post</section>;\n}\n""",
    )

    build_once(settings)
    registry = load_metadata_registry(settings)
    routes = build_route_table(registry)

    app = create_starlette_app(settings, routes)

    with TestClient(app) as client:
        response = client.get("/", params={"hello": "pytest"})
        assert response.status_code == 201

    html = response.text
    assert "<title>SSR Integration</title>" in html
    assert '<meta name="description" content="SSR integration test" />' in html
    assert '<main data-query="pytest">' in html
    assert '<h1>Hello from SSR</h1>' in html
    assert 'window.__PYXLE_PAGE_PATH__ = "/pages/index.jsx";' in html

    dynamic = client.get("/posts/42")
    assert dynamic.status_code == 200
    assert '<section data-route="post">Static post</section>' in dynamic.text
    assert 'window.__PYXLE_PAGE_PATH__ = "/pages/posts/[id].jsx";' in dynamic.text


def test_ssr_integration_renders_astral_characters(tmp_path: Path) -> None:
    """Emoji / astral characters must survive the Python<->Node SSR transport —
    in component markup AND in loader-provided props — without an encoding crash.
    The transport pins UTF-8 so this holds regardless of the system locale."""
    project_root = tmp_path / "project"
    (project_root / "public").mkdir(parents=True)
    settings = DevServerSettings.from_project_root(project_root)
    ensure_test_node_modules(project_root)

    wave = "\U0001f44b"    # waving hand — supplied by the loader as a prop
    rocket = "\U0001f680"  # rocket — embedded directly in component markup

    _write(
        settings.pages_dir / "index.pyxl",
        "def server(fn):\n"
        "    return fn\n\n"
        "@server\n"
        "async def load(request):\n"
        f"    return {{'wave': '{wave}'}}\n\n"
        "import React from 'react';\n\n"
        "export default function Home({ data }) {\n"
        f"    return <main><h1>Static {rocket}</h1><p data-wave={{data.wave}}>{{data.wave}}</p></main>;\n"
        "}\n",
    )

    build_once(settings)
    registry = load_metadata_registry(settings)
    routes = build_route_table(registry)
    app = create_starlette_app(settings, routes)

    with TestClient(app) as client:
        response = client.get("/")

    assert response.status_code == 200
    assert rocket in response.text  # astral char in static markup survives
    assert wave in response.text  # astral char from loader props survives


def _refresh_live_routes(app, settings) -> None:
    """Drive the live route-table refresh through the same module-level helpers
    ``DevServer._handle_rebuild`` uses, so the test exercises the real code."""
    from pyxle.devserver import _apply_refreshed_routes, _rebuild_app_routes

    new_routes, error_boundaries = _rebuild_app_routes(app, settings)
    _apply_refreshed_routes(app, new_routes, error_boundaries)


def _page_with_loader(loader: str) -> str:
    return (
        "def server(fn):\n    return fn\n\n"
        "@server\n"
        f"async def {loader}(request):\n"
        f"    return {{'msg': 'from {loader}'}}\n\n"
        "import React from 'react';\n\n"
        "export default function Home({ data }) { return <h1>{data.msg}</h1>; }\n"
    )


def test_dev_route_refresh_reconciles_loader_rename(tmp_path: Path) -> None:
    """Renaming a @server loader reconciles live via the route-table refresh —
    without it the frozen route errors on the old name (the reported bug)."""
    project_root = tmp_path / "project"
    (project_root / "public").mkdir(parents=True)
    settings = DevServerSettings.from_project_root(project_root)
    ensure_test_node_modules(project_root)

    index = settings.pages_dir / "index.pyxl"
    _write(index, _page_with_loader("load_home"))
    build_once(settings)
    app = create_starlette_app(settings, build_route_table(load_metadata_registry(settings)))

    with TestClient(app) as client:
        assert "from load_home" in client.get("/").text

        # Rename the loader + rebuild. BEFORE the refresh, the frozen route
        # still calls ``load_home`` → the page errors.
        _write(index, _page_with_loader("load"))
        build_once(settings)
        assert client.get("/").status_code >= 500

        # The live route refresh reconciles it with no restart.
        _refresh_live_routes(app, settings)
        reconciled = client.get("/")
        assert reconciled.status_code == 200
        assert "from load" in reconciled.text


def test_dev_route_refresh_picks_up_new_page(tmp_path: Path) -> None:
    """A newly created page becomes routable live after the route refresh."""
    project_root = tmp_path / "project"
    (project_root / "public").mkdir(parents=True)
    settings = DevServerSettings.from_project_root(project_root)
    ensure_test_node_modules(project_root)

    _write(settings.pages_dir / "index.pyxl", _page_with_loader("load"))
    build_once(settings)
    app = create_starlette_app(settings, build_route_table(load_metadata_registry(settings)))

    with TestClient(app) as client:
        assert client.get("/about").status_code == 404  # no route yet

        _write(
            settings.pages_dir / "about.pyxl",
            "import React from 'react';\n\nexport default function About() { return <h1>About page</h1>; }\n",
        )
        build_once(settings)
        _refresh_live_routes(app, settings)

        resp = client.get("/about")
        assert resp.status_code == 200
        assert "About page" in resp.text


def test_ssr_integration_composes_layouts_and_templates(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    (project_root / "public").mkdir(parents=True)
    settings = DevServerSettings.from_project_root(project_root)
    ensure_test_node_modules(project_root)

    _write(
        settings.pages_dir / "layout.pyxl",
        """import React from 'react';\nimport { Slot } from 'pyxle/client';\n\nexport default function SiteLayout({ children, data }) {\n    return (\n        <div className=\"site-layout\">\n            <header data-layout=\"root\">\n                <Slot name=\"hero\" props={{ data }} fallback={<p>layout fallback</p>} />\n            </header>\n            <main>{children}</main>\n        </div>\n    );\n}\n\nexport const slots = {\n    hero: ({ data }) => <h1 data-slot=\"layout\">{data.page?.title}</h1>,\n};\n""",
    )

    _write(
        settings.pages_dir / "blog/template.pyxl",
        """import React from 'react';\nimport { Slot } from 'pyxle/client';\n\nexport default function BlogTemplate({ children, data }) {\n    return (\n        <section className=\"blog-template\">\n            <div data-wrapper=\"template\">\n                <Slot name=\"hero\" props={{ data }} fallback={<p>template fallback</p>} />\n            </div>\n            <article>{children}</article>\n        </section>\n    );\n}\n\nexport const slots = {\n    hero: ({ data }) => <h2 data-slot=\"template\">{data.page?.intro}</h2>,\n};\n""",
    )

    _write(
        settings.pages_dir / "blog/index.pyxl",
        """from pyxle.runtime import server\n\n@server\nasync def load_blog(request):\n    return {\n        \"page\": {\n            \"title\": \"Nested Layouts\",\n            \"intro\": \"Preview template fallbacks in action\",\n        },\n        \"post\": {\n            \"title\": \"Nested slots\",\n        },\n    }\n\nimport React from 'react';\n\nexport const slots = {\n    hero: ({ data }) => <p data-slot=\"page\">{data.post.title}</p>,\n};\n\nexport default function BlogPage({ data }) {\n    return (\n        <div data-leaf=\"blog\">\n            <strong>{data.post.title}</strong>\n        </div>\n    );\n}\n""",
    )

    build_once(settings)
    registry = load_metadata_registry(settings)
    routes = build_route_table(registry)

    app = create_starlette_app(settings, routes)

    with TestClient(app) as client:
        response = client.get("/blog")
        assert response.status_code == 200

        html = response.text
        assert 'class="site-layout"' in html
        assert 'data-wrapper="template"' in html
        assert 'data-slot="layout">Nested Layouts<' in html
        assert 'data-slot="template">Preview template fallbacks in action<' in html
        assert 'data-slot="page">Nested slots<' in html
        assert 'data-leaf="blog"' in html
        assert 'window.__PYXLE_PAGE_PATH__ = "/routes/blog/index.jsx";' in html

        nav = client.get("/blog", headers={"x-pyxle-navigation": "1"})
        assert nav.status_code == 200
        payload = nav.json()
        assert payload["page"]["clientAssetPath"] == "/routes/blog/index.jsx"
        assert payload["props"]["data"]["post"]["title"] == "Nested slots"
