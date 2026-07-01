"""Tests for the AI-accessibility feature (per-page ``.md`` + ``/llms.txt``)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.testclient import TestClient

from pyxle.devserver import llms
from pyxle.devserver.starlette_app import _maybe_markdown_response, _merge_vary

pytestmark = pytest.mark.anyio("asyncio")


@pytest.fixture
def anyio_backend() -> str:  # pragma: no cover - fixture wiring
    return "asyncio"


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path,expected",
    [
        ("/about.md", "/about"),
        ("/index.md", "/"),
        ("/docs/routing.md", "/docs/routing"),
        ("/docs/a/b.md", "/docs/a/b"),
        ("/deep/index.md", "/deep/"),
        ("/plain", "/plain"),
    ],
)
def test_strip_md_suffix(path, expected):
    assert llms.strip_md_suffix(path) == expected


@pytest.mark.parametrize(
    "page_path,expected",
    [
        ("/", "/index.md"),
        ("/about", "/about.md"),
        ("/docs/{slug:path}", "/docs/{slug:path}.md"),
        ("/blog/", "/blog.md"),
    ],
)
def test_markdown_route_path(page_path, expected):
    assert llms.markdown_route_path(page_path) == expected


def test_wants_markdown():
    req_md = SimpleNamespace(headers={"accept": "text/markdown, text/html"})
    req_html = SimpleNamespace(headers={"accept": "text/html,application/xhtml+xml"})
    req_none = SimpleNamespace(headers={})
    assert llms.wants_markdown(req_md) is True
    assert llms.wants_markdown(req_html) is False
    assert llms.wants_markdown(req_none) is False


def test_is_enabled():
    assert llms.is_enabled(None) is False
    assert llms.is_enabled(SimpleNamespace(enabled=False)) is False
    assert llms.is_enabled(SimpleNamespace(enabled=True)) is True


# ---------------------------------------------------------------------------
# HTML -> markdown converter
# ---------------------------------------------------------------------------


def test_html_to_markdown_structure():
    html = (
        "<nav>menu</nav><main><h1>Title</h1>"
        "<p>Hello <strong>world</strong> and <em>friends</em>, see "
        '<a href="/x">here</a>.</p>'
        "<ul><li>one</li><li>two</li></ul>"
        "<ol><li>first</li><li>second</li></ol>"
        "<pre><code>x = 1\ny = 2</code></pre>"
        "<p>inline <code>code()</code> sample</p>"
        "<blockquote>quoted</blockquote><hr>"
        "<script>evil()</script><style>.a{}</style></main>"
    )
    md = llms.html_to_markdown(html)
    assert "# Title" in md
    assert "**world**" in md
    assert "*friends*" in md
    assert "[here](/x)" in md
    assert "- one" in md and "- two" in md
    assert "1. first" in md and "2. second" in md
    assert "x = 1\ny = 2" in md  # code preserved verbatim
    assert "`code()`" in md
    assert "---" in md
    # dropped content
    assert "evil()" not in md
    assert ".a{}" not in md


def test_html_to_markdown_collapses_blank_lines_and_trails_newline():
    md = llms.html_to_markdown("<p>a</p><p>b</p>")
    assert md.endswith("\n")
    assert "\n\n\n" not in md


def test_html_to_markdown_link_without_href():
    md = llms.html_to_markdown("<a>bare</a>")
    assert "bare" in md
    assert "](" not in md


# ---------------------------------------------------------------------------
# Resolution ladder
# ---------------------------------------------------------------------------


@pytest.fixture
def app_tree(tmp_path):
    pages = tmp_path / "pages"
    (pages / "docs").mkdir(parents=True)
    settings = SimpleNamespace(pages_dir=pages, project_root=tmp_path, debug=True, llms=SimpleNamespace(enabled=True, auto_convert=False))
    return SimpleNamespace(root=tmp_path, pages=pages, settings=settings)


def _page_in(app_tree, rel: str, path: str, **over):
    abs_src = app_tree.pages / rel
    return SimpleNamespace(
        path=path,
        source_absolute_path=abs_src,
        source_relative_path=Path(rel),
        server_module_path=abs_src.with_suffix(".py"),
        module_key=f"pages.{rel.replace('/', '.').removesuffix('.pyxl')}",
        **over,
    )


async def _resolve(app_tree, page, request_path, config=None):
    request = SimpleNamespace(url=SimpleNamespace(path=request_path), headers={})
    return await llms.resolve_page_markdown(
        request=request,
        page=page,
        settings=app_tree.settings,
        renderer=None,
        config=config or app_tree.settings.llms,
    )


async def test_colocated_md_file_wins(app_tree):
    (app_tree.pages / "about.pyxl").write_text("x")
    (app_tree.pages / "about.md").write_text("# About (file)\n")
    page = _page_in(app_tree, "about.pyxl", "/about")
    assert await _resolve(app_tree, page, "/about.md") == "# About (file)\n"


async def test_page_local_to_markdown(app_tree):
    (app_tree.pages / "p.pyxl").write_text("x")
    (app_tree.pages / "p.py").write_text(
        "async def to_markdown(ctx):\n    return f'# {ctx.path}'\n"
    )
    page = _page_in(app_tree, "p.pyxl", "/p")
    assert await _resolve(app_tree, page, "/p.md") == "# /p"


async def test_directory_llms_py_covers_subtree(app_tree):
    (app_tree.pages / "docs" / "intro.pyxl").write_text("x")
    (app_tree.pages / "docs" / "llms.py").write_text(
        "def to_markdown(ctx):\n    return 'DIR: ' + ctx.path\n"
    )
    page = _page_in(app_tree, "docs/intro.pyxl", "/docs/intro")
    assert await _resolve(app_tree, page, "/docs/intro.md") == "DIR: /docs/intro"


async def test_root_llms_py_is_app_wide(app_tree):
    (app_tree.pages / "contact.pyxl").write_text("x")
    (app_tree.pages / "llms.py").write_text(
        "def to_markdown(ctx):\n    return 'ROOT'\n"
    )
    page = _page_in(app_tree, "contact.pyxl", "/contact")
    assert await _resolve(app_tree, page, "/contact.md") == "ROOT"


async def test_nearest_directory_handler_wins_over_root(app_tree):
    (app_tree.pages / "docs" / "x.pyxl").write_text("x")
    (app_tree.pages / "llms.py").write_text("def to_markdown(ctx):\n    return 'ROOT'\n")
    (app_tree.pages / "docs" / "llms.py").write_text("def to_markdown(ctx):\n    return 'DOCS'\n")
    page = _page_in(app_tree, "docs/x.pyxl", "/docs/x")
    assert await _resolve(app_tree, page, "/docs/x.md") == "DOCS"


async def test_handler_returning_none_falls_through(app_tree):
    (app_tree.pages / "docs" / "y.pyxl").write_text("x")
    # directory handler declines -> root handler answers
    (app_tree.pages / "docs" / "llms.py").write_text("def to_markdown(ctx):\n    return None\n")
    (app_tree.pages / "llms.py").write_text("def to_markdown(ctx):\n    return 'ROOT'\n")
    page = _page_in(app_tree, "docs/y.pyxl", "/docs/y")
    assert await _resolve(app_tree, page, "/docs/y.md") == "ROOT"


async def test_auto_convert_fallback(app_tree, monkeypatch):
    import pyxle.ssr.view as view

    async def fake_render(*, request, settings, page, renderer, suppress_per_user=False):
        return "<h1>Converted</h1>", 200

    monkeypatch.setattr(view, "render_page_body_html", fake_render)
    (app_tree.pages / "z.pyxl").write_text("x")
    page = _page_in(app_tree, "z.pyxl", "/z")
    cfg = SimpleNamespace(enabled=True, auto_convert=True)
    md = await _resolve(app_tree, page, "/z.md", config=cfg)
    assert "# Converted" in md


async def test_no_source_returns_none(app_tree):
    (app_tree.pages / "empty.pyxl").write_text("x")
    page = _page_in(app_tree, "empty.pyxl", "/empty")
    # no .md, no handlers, auto_convert off -> None (caller redirects)
    assert await _resolve(app_tree, page, "/empty.md") is None


async def test_handler_must_return_str_or_none(app_tree):
    (app_tree.pages / "bad.pyxl").write_text("x")
    (app_tree.pages / "bad.py").write_text("def to_markdown(ctx):\n    return 123\n")
    page = _page_in(app_tree, "bad.pyxl", "/bad")
    with pytest.raises(TypeError):
        await _resolve(app_tree, page, "/bad.md")


# ---------------------------------------------------------------------------
# /llms.txt generation
# ---------------------------------------------------------------------------


def _routes(*paths):
    return SimpleNamespace(pages=[SimpleNamespace(path=p) for p in paths])


def test_build_llms_txt(tmp_path):
    settings = SimpleNamespace(project_root=tmp_path / "acme-docs", pages_dir=tmp_path)
    routes = _routes("/", "/about", "/docs/{slug:path}", "/benchmarks")
    txt = llms.build_llms_txt(routes=routes, settings=settings)
    assert txt.startswith("# Acme Docs")
    assert "- [Home](/index.md)" in txt
    assert "- [About](/about.md)" in txt
    assert "- [Benchmarks](/benchmarks.md)" in txt
    assert "slug" not in txt  # dynamic route omitted


# ---------------------------------------------------------------------------
# Route + middleware integration (via TestClient)
# ---------------------------------------------------------------------------


def test_markdown_route_serves_and_redirects(app_tree):
    (app_tree.pages / "about.pyxl").write_text("x")
    (app_tree.pages / "about.md").write_text("# About\n")
    (app_tree.pages / "missing.pyxl").write_text("x")

    routes = SimpleNamespace(
        pages=[
            _page_in(app_tree, "about.pyxl", "/about"),
            _page_in(app_tree, "missing.pyxl", "/missing"),
        ]
    )
    md_routes = llms.build_markdown_routes(
        routes, settings=app_tree.settings, renderer=None, config=app_tree.settings.llms
    )
    app = Starlette(routes=md_routes)
    client = TestClient(app)

    resp = client.get("/about.md")
    assert resp.status_code == 200
    assert resp.text == "# About\n"
    assert resp.headers["content-type"].startswith("text/markdown")

    # No source resolves -> 307 redirect to the canonical page.
    resp2 = client.get("/missing.md", follow_redirects=False)
    assert resp2.status_code == 307
    assert resp2.headers["location"] == "/missing"


def test_markdown_route_dynamic_slug(app_tree):
    (app_tree.pages / "docs").mkdir(exist_ok=True)
    (app_tree.pages / "docs" / "llms.py").write_text(
        "def to_markdown(ctx):\n    return 'SLUG:' + ctx.request.path_params.get('slug', '')\n"
    )
    (app_tree.pages / "docs" / "cat.pyxl").write_text("x")
    routes = SimpleNamespace(
        pages=[_page_in(app_tree, "docs/cat.pyxl", "/docs/{slug:path}")]
    )
    md_routes = llms.build_markdown_routes(
        routes, settings=app_tree.settings, renderer=None, config=app_tree.settings.llms
    )
    client = TestClient(Starlette(routes=md_routes))
    resp = client.get("/docs/getting-started.md")
    assert resp.status_code == 200
    assert resp.text == "SLUG:getting-started"


def test_llms_txt_route_default_and_hook(app_tree):
    routes = _routes("/", "/about")
    # default generated index
    default_route = llms.make_llms_txt_route(routes, settings=app_tree.settings)
    client = TestClient(Starlette(routes=[default_route]))
    resp = client.get("/llms.txt")
    assert resp.status_code == 200
    assert "## Pages" in resp.text
    assert "- [About](/about.md)" in resp.text

    # hook override in root pages/llms.py
    (app_tree.pages / "llms.py").write_text(
        "def llms_txt(ctx):\n    return '# Custom\\n' + ctx.render_default()\n"
    )
    hook_route = llms.make_llms_txt_route(routes, settings=app_tree.settings)
    client2 = TestClient(Starlette(routes=[hook_route]))
    resp2 = client2.get("/llms.txt")
    assert resp2.text.startswith("# Custom\n")
    assert "## Pages" in resp2.text


def test_discovery_middleware_adds_headers():
    async def app(scope, receive, send):
        await PlainTextResponse("ok")(scope, receive, send)

    wrapped = llms.LlmsDiscoveryMiddleware(app)
    client = TestClient(wrapped)
    resp = client.get("/anything")
    assert resp.headers["x-llms-txt"] == "/llms.txt"
    assert 'rel="llms-txt"' in resp.headers["link"]


def test_discovery_middleware_appends_to_existing_link():
    async def app(scope, receive, send):
        resp = PlainTextResponse("ok")
        resp.headers["link"] = "</style.css>; rel=preload"
        await resp(scope, receive, send)

    client = TestClient(llms.LlmsDiscoveryMiddleware(app))
    resp = client.get("/x")
    link = resp.headers["link"]
    assert "rel=preload" in link and 'rel="llms-txt"' in link


# ---------------------------------------------------------------------------
# starlette_app helpers: _merge_vary + _maybe_markdown_response (negotiation)
# ---------------------------------------------------------------------------


def test_merge_vary():
    r = PlainTextResponse("x")
    _merge_vary(r, "Accept")
    assert r.headers["vary"] == "Accept"
    _merge_vary(r, "Accept")  # idempotent
    assert r.headers["vary"] == "Accept"

    r2 = PlainTextResponse("x")
    r2.headers["vary"] = "X-Foo"
    _merge_vary(r2, "Accept")
    assert r2.headers["vary"] == "X-Foo, Accept"

    r3 = PlainTextResponse("x")
    r3.headers["vary"] = "accept"  # already present (case-insensitive)
    _merge_vary(r3, "Accept")
    assert r3.headers["vary"] == "accept"


def _req(path: str, accept: str = "text/markdown"):
    return SimpleNamespace(url=SimpleNamespace(path=path), headers={"accept": accept})


async def test_negotiation_disabled_returns_none(app_tree):
    page = _page_in(app_tree, "about.pyxl", "/about")
    resp = await _maybe_markdown_response(
        _req("/about"), page, settings=app_tree.settings,
        renderer=None, llms_cfg=SimpleNamespace(enabled=False),
    )
    assert resp is None


async def test_negotiation_not_requested_returns_none(app_tree):
    page = _page_in(app_tree, "about.pyxl", "/about")
    resp = await _maybe_markdown_response(
        _req("/about", accept="text/html"), page, settings=app_tree.settings,
        renderer=None, llms_cfg=app_tree.settings.llms,
    )
    assert resp is None


async def test_negotiation_serves_markdown(app_tree):
    (app_tree.pages / "about.pyxl").write_text("x")
    (app_tree.pages / "about.md").write_text("# About\n")
    page = _page_in(app_tree, "about.pyxl", "/about")
    resp = await _maybe_markdown_response(
        _req("/about"), page, settings=app_tree.settings,
        renderer=None, llms_cfg=app_tree.settings.llms,
    )
    assert resp is not None
    assert resp.body == b"# About\n"
    assert resp.headers["vary"] == "Accept"
    assert resp.headers["content-type"].startswith("text/markdown")


async def test_negotiation_none_when_unresolved(app_tree):
    (app_tree.pages / "solo.pyxl").write_text("x")
    page = _page_in(app_tree, "solo.pyxl", "/solo")
    resp = await _maybe_markdown_response(
        _req("/solo"), page, settings=app_tree.settings,
        renderer=None, llms_cfg=app_tree.settings.llms,
    )
    assert resp is None


async def test_negotiation_swallows_errors(app_tree, monkeypatch):
    async def boom(**_kw):
        raise RuntimeError("nope")

    monkeypatch.setattr(llms, "resolve_page_markdown", boom)
    page = _page_in(app_tree, "about.pyxl", "/about")
    resp = await _maybe_markdown_response(
        _req("/about"), page, settings=app_tree.settings,
        renderer=None, llms_cfg=app_tree.settings.llms,
    )
    assert resp is None


# ---------------------------------------------------------------------------
# Error/fallback branch coverage
# ---------------------------------------------------------------------------


def test_markdown_route_redirects_on_handler_error(app_tree):
    (app_tree.pages / "boom.pyxl").write_text("x")
    (app_tree.pages / "boom.py").write_text("def to_markdown(ctx):\n    raise RuntimeError('x')\n")
    routes = SimpleNamespace(pages=[_page_in(app_tree, "boom.pyxl", "/boom")])
    md_routes = llms.build_markdown_routes(
        routes, settings=app_tree.settings, renderer=None, config=app_tree.settings.llms
    )
    client = TestClient(Starlette(routes=md_routes))
    resp = client.get("/boom.md", follow_redirects=False)
    assert resp.status_code == 307
    assert resp.headers["location"] == "/boom"


async def test_local_handler_import_failure_falls_through(app_tree):
    (app_tree.pages / "brk.pyxl").write_text("x")
    (app_tree.pages / "brk.py").write_text("raise ValueError('boom')\n")
    (app_tree.pages / "llms.py").write_text("def to_markdown(ctx):\n    return 'ROOT'\n")
    page = _page_in(app_tree, "brk.pyxl", "/brk")
    # page-local server module fails to import -> fall through to root llms.py
    assert await _resolve(app_tree, page, "/brk.md") == "ROOT"


def test_llms_txt_hook_returning_none_falls_back(app_tree):
    (app_tree.pages / "llms.py").write_text("def llms_txt(ctx):\n    return None\n")
    route = llms.make_llms_txt_route(_routes("/", "/about"), settings=app_tree.settings)
    resp = TestClient(Starlette(routes=[route])).get("/llms.txt")
    assert resp.status_code == 200 and "## Pages" in resp.text


def test_llms_txt_hook_error_falls_back(app_tree):
    (app_tree.pages / "llms.py").write_text("def llms_txt(ctx):\n    raise RuntimeError('x')\n")
    route = llms.make_llms_txt_route(_routes("/", "/about"), settings=app_tree.settings)
    resp = TestClient(Starlette(routes=[route])).get("/llms.txt")
    assert resp.status_code == 200 and "## Pages" in resp.text


async def test_discovery_middleware_passes_non_http():
    seen = []

    async def downstream(scope, receive, send):
        seen.append(scope["type"])

    await llms.LlmsDiscoveryMiddleware(downstream)({"type": "lifespan"}, None, None)
    assert seen == ["lifespan"]


def test_html_to_markdown_blockquote_and_nested_list():
    md = llms.html_to_markdown(
        "<blockquote>quote</blockquote><ul><li>a<ul><li>b</li></ul></li></ul>"
    )
    assert "quote" in md
    assert "- a" in md and "b" in md


# ---------------------------------------------------------------------------
# wrap_markdown hook (root pages/llms.py frames every .md response)
# ---------------------------------------------------------------------------


async def test_wrap_markdown_hook_frames_output(app_tree):
    (app_tree.pages / "about.pyxl").write_text("x")
    (app_tree.pages / "about.md").write_text("# About\n")
    (app_tree.pages / "llms.py").write_text(
        "def wrap_markdown(ctx, md):\n    return f'<!-- {ctx.path} -->\\n' + md + '\\n---\\nfooter'\n"
    )
    page = _page_in(app_tree, "about.pyxl", "/about")
    out = await _resolve(app_tree, page, "/about.md")
    assert out.startswith("<!-- /about -->")
    assert "# About" in out
    assert out.rstrip().endswith("footer")


async def test_wrap_markdown_hook_none_keeps_markdown(app_tree):
    (app_tree.pages / "about.pyxl").write_text("x")
    (app_tree.pages / "about.md").write_text("# About\n")
    (app_tree.pages / "llms.py").write_text("def wrap_markdown(ctx, md):\n    return None\n")
    page = _page_in(app_tree, "about.pyxl", "/about")
    assert await _resolve(app_tree, page, "/about.md") == "# About\n"


async def test_wrap_markdown_not_applied_on_redirect(app_tree):
    # No source resolves -> None (redirect); wrap must not run / must stay None.
    (app_tree.pages / "solo.pyxl").write_text("x")
    (app_tree.pages / "llms.py").write_text("def wrap_markdown(ctx, md):\n    return 'WRAPPED'\n")
    page = _page_in(app_tree, "solo.pyxl", "/solo")
    assert await _resolve(app_tree, page, "/solo.md") is None


async def test_wrap_markdown_must_return_str_or_none(app_tree):
    (app_tree.pages / "about.pyxl").write_text("x")
    (app_tree.pages / "about.md").write_text("# About\n")
    (app_tree.pages / "llms.py").write_text("def wrap_markdown(ctx, md):\n    return 42\n")
    page = _page_in(app_tree, "about.pyxl", "/about")
    with pytest.raises(TypeError):
        await _resolve(app_tree, page, "/about.md")


# ---------------------------------------------------------------------------
# ctx.run_loader() — loader data without the SSR render
# ---------------------------------------------------------------------------


async def test_run_loader_returns_loader_data(app_tree):
    (app_tree.pages / "p.pyxl").write_text("x")
    (app_tree.pages / "p.py").write_text(
        "def load(request):\n"
        "    return {'n': 42}\n"
        "\n"
        "async def to_markdown(ctx):\n"
        "    data = await ctx.run_loader()\n"
        "    return f\"# {data['n']}\"\n"
    )
    page = _page_in(app_tree, "p.pyxl", "/p", has_loader=True, loader_name="load")
    assert await _resolve(app_tree, page, "/p.md") == "# 42"


async def test_run_loader_no_loader_returns_empty(app_tree):
    (app_tree.pages / "q.pyxl").write_text("x")
    (app_tree.pages / "q.py").write_text(
        "async def to_markdown(ctx):\n"
        "    data = await ctx.run_loader()\n"
        "    return 'EMPTY' if data == {} else 'HAS'\n"
    )
    page = _page_in(app_tree, "q.pyxl", "/q", has_loader=False, loader_name=None)
    assert await _resolve(app_tree, page, "/q.md") == "EMPTY"
