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


@pytest.mark.parametrize(
    "accept,expected",
    [
        # -- explicit opt-in ------------------------------------------------
        ("text/markdown", True),
        ("text/markdown, text/html", True),  # equal q -> markdown wins ties
        ("text/html, text/markdown", True),
        ("TEXT/MARKDOWN", True),  # type/subtype match is case-insensitive
        ("text/markdown;q=0.5", True),  # html absent -> not acceptable
        ("text/markdown;q=0.9, text/html;q=0.8", True),
        ("text/markdown, */*;q=0.1", True),  # html only via low-q wildcard
        ("text/html;q=0, text/markdown;q=0.001", True),  # html excluded
        ("text/markdown;level=1;q=0.3, text/html;q=0.2", True),  # extra params
        ('text/markdown;note="a,b"', True),  # quoted comma inside a param
        ("text/markdown;q=2", True),  # q clamped to 1.0
        ("text/markdown;q=abc", True),  # malformed q -> default 1.0
        ("text/markdown;q=nan", True),  # non-finite q -> default 1.0
        ("text/markdown;q=0.5;q=0", True),  # first q ends media params (accept-ext)
        # -- browsers / no explicit markdown --------------------------------
        ("", False),
        ("text/html", False),
        ("application/json", False),
        ("text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8", False),
        ("*/*", False),  # wildcards never select markdown
        ("text/*", False),
        # -- substring must NOT match (the original bug) ---------------------
        ("text/markdownish", False),
        ("application/text/markdown", False),
        # -- q-value semantics (RFC 9110 §12.5.1) ----------------------------
        ("text/html, text/markdown;q=0", False),  # q=0 means NOT acceptable
        ("text/html;q=0.9, text/markdown;q=0.8", False),  # HTML preferred
        ("text/markdown;q=0.5, */*;q=0.9", False),  # html effective 0.9 via wildcard
        ("text/markdown;q=-1", False),  # clamped to 0 -> excluded
        ('text/markdown;note="a,b";q=0', False),  # q=0 after quoted comma
        # -- malformed headers never raise, fall back to HTML ----------------
        ("garbage", False),
        ("text/", False),
        ("/markdown", False),
        ("text/a/b", False),
        (";;;,,,", False),
        (",", False),
    ],
)
def test_markdown_is_acceptable(accept, expected):
    assert llms.markdown_is_acceptable(accept) is expected


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
    assert "[here](/x.md)" in md  # internal links are rewritten by default
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
# Internal-link rewriting (autoConvert / html_to_markdown default)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "href,expected",
    [
        # rewritten: extensionless page paths get .md, query/fragment preserved
        ("/about", "/about.md"),
        ("/about?x=1", "/about.md?x=1"),
        ("/about#y", "/about.md#y"),
        ("/about?x=1#y", "/about.md?x=1#y"),
        ("/", "/index.md"),
        ("/docs/", "/docs.md"),  # trailing slash -> canonical page path
        ("/docs/v1.2/intro", "/docs/v1.2/intro.md"),  # dot in a non-final segment
        ("docs/intro", "docs/intro.md"),  # relative links too
        ("../routing", "../routing.md"),
        ("/apinot", "/apinot.md"),  # /api prefix must match on segment boundary
        # untouched: external / protocol links
        ("https://example.com/about", "https://example.com/about"),
        ("http://example.com/", "http://example.com/"),
        ("//cdn.example.com/lib.js", "//cdn.example.com/lib.js"),
        ("mailto:hi@example.com", "mailto:hi@example.com"),
        ("tel:+15550100", "tel:+15550100"),
        # untouched: API routes
        ("/api", "/api"),
        ("/api/search?q=x", "/api/search?q=x"),
        # untouched: assets and existing .md links
        ("/logo.png", "/logo.png"),
        ("/styles/site.css", "/styles/site.css"),
        ("/about.md", "/about.md"),
        # untouched: same-page and empty links
        ("#section", "#section"),
        ("?q=1", "?q=1"),
        ("", ""),
        # untouched: unparseable URL never raises
        ("https://[bad", "https://[bad"),
    ],
)
def test_rewrite_internal_href(href, expected):
    assert llms._rewrite_internal_href(href) == expected


def test_html_to_markdown_rewrites_links_by_default():
    html = (
        '<p><a href="/about?x=1#y">About</a> '
        '<a href="https://ext.example/z">Ext</a> '
        '<a href="/api/search">API</a> '
        '<a href="/logo.png">Logo</a></p>'
    )
    md = llms.html_to_markdown(html)
    assert "[About](/about.md?x=1#y)" in md
    assert "[Ext](https://ext.example/z)" in md
    assert "[API](/api/search)" in md
    assert "[Logo](/logo.png)" in md


def test_html_to_markdown_rewrite_links_opt_out():
    md = llms.html_to_markdown('<a href="/about">About</a>', rewrite_links=False)
    assert "[About](/about)" in md


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
        return '<h1>Converted</h1><a href="/about">About</a>', 200

    monkeypatch.setattr(view, "render_page_body_html", fake_render)
    (app_tree.pages / "z.pyxl").write_text("x")
    page = _page_in(app_tree, "z.pyxl", "/z")
    cfg = SimpleNamespace(enabled=True, auto_convert=True)
    md = await _resolve(app_tree, page, "/z.md", config=cfg)
    assert "# Converted" in md
    # autoConvert output keeps agents on the markdown channel
    assert "[About](/about.md)" in md


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


def _routes_in(app_tree, *specs):
    """Route table of full page descriptors; each spec is ``(rel_source, path)``."""
    return SimpleNamespace(pages=[_page_in(app_tree, rel, path) for rel, path in specs])


def test_build_llms_txt_links_md_only_when_it_resolves(app_tree):
    # about has a co-located .md -> .md link; bare has no source -> HTML link
    (app_tree.pages / "about.pyxl").write_text("x")
    (app_tree.pages / "about.md").write_text("# About\n")
    (app_tree.pages / "bare.pyxl").write_text("x")
    routes = _routes_in(
        app_tree,
        ("index.pyxl", "/"),
        ("about.pyxl", "/about"),
        ("bare.pyxl", "/bare"),
        ("docs/[[...slug]].pyxl", "/docs/{slug:path}"),
    )
    txt = llms.build_llms_txt(
        routes=routes,
        settings=app_tree.settings,
        config=app_tree.settings.llms,
        base_url="https://example.com",
    )
    assert txt.startswith("# ")
    assert "## Pages" in txt
    assert "- [About](https://example.com/about.md)" in txt
    assert "- [Bare](https://example.com/bare)" in txt  # never a dead-end .md link
    assert "- [Home](https://example.com/)" in txt  # no source -> HTML URL
    assert "slug" not in txt  # dynamic route omitted


def test_build_llms_txt_handlers_count_as_markdown(app_tree):
    # a page-local to_markdown and a directory llms.py both make .md real
    (app_tree.pages / "p.pyxl").write_text("x")
    (app_tree.pages / "p.py").write_text("def to_markdown(ctx):\n    return '# P'\n")
    (app_tree.pages / "docs" / "intro.pyxl").write_text("x")
    (app_tree.pages / "docs" / "llms.py").write_text(
        "def to_markdown(ctx):\n    return '# D'\n"
    )
    routes = _routes_in(app_tree, ("p.pyxl", "/p"), ("docs/intro.pyxl", "/docs/intro"))
    txt = llms.build_llms_txt(
        routes=routes,
        settings=app_tree.settings,
        config=app_tree.settings.llms,
        base_url="https://example.com",
    )
    assert "- [P](https://example.com/p.md)" in txt
    assert "- [Intro](https://example.com/docs/intro.md)" in txt


def test_build_llms_txt_auto_convert_links_every_page(app_tree):
    (app_tree.pages / "bare.pyxl").write_text("x")
    routes = _routes_in(app_tree, ("bare.pyxl", "/bare"))
    cfg = SimpleNamespace(enabled=True, auto_convert=True)
    txt = llms.build_llms_txt(
        routes=routes, settings=app_tree.settings, config=cfg, base_url="https://example.com"
    )
    assert "- [Bare](https://example.com/bare.md)" in txt


def test_build_llms_txt_empty_base_url_is_relative(app_tree):
    (app_tree.pages / "about.pyxl").write_text("x")
    (app_tree.pages / "about.md").write_text("# About\n")
    routes = _routes_in(app_tree, ("about.pyxl", "/about"))
    txt = llms.build_llms_txt(
        routes=routes, settings=app_tree.settings, config=app_tree.settings.llms
    )
    assert "- [About](/about.md)" in txt


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
    (app_tree.pages / "about.pyxl").write_text("x")
    (app_tree.pages / "about.md").write_text("# About\n")
    (app_tree.pages / "bare.pyxl").write_text("x")
    routes = _routes_in(app_tree, ("about.pyxl", "/about"), ("bare.pyxl", "/bare"))
    # default generated index: absolute URLs from the request's scheme + host,
    # .md only for pages whose markdown actually resolves
    default_route = llms.make_llms_txt_route(routes, settings=app_tree.settings)
    client = TestClient(Starlette(routes=[default_route]))
    resp = client.get("/llms.txt")
    assert resp.status_code == 200
    assert "## Pages" in resp.text
    assert "- [About](http://testserver/about.md)" in resp.text
    assert "- [Bare](http://testserver/bare)" in resp.text

    # hook override in root pages/llms.py; render_default() keeps the same links
    (app_tree.pages / "llms.py").write_text(
        "def llms_txt(ctx):\n    return '# Custom\\n' + ctx.render_default()\n"
    )
    hook_route = llms.make_llms_txt_route(routes, settings=app_tree.settings)
    client2 = TestClient(Starlette(routes=[hook_route]))
    resp2 = client2.get("/llms.txt")
    assert resp2.text.startswith("# Custom\n")
    assert "## Pages" in resp2.text
    assert "- [About](http://testserver/about.md)" in resp2.text


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
    (app_tree.pages / "about.pyxl").write_text("x")
    (app_tree.pages / "llms.py").write_text("def llms_txt(ctx):\n    return None\n")
    route = llms.make_llms_txt_route(
        _routes_in(app_tree, ("about.pyxl", "/about")), settings=app_tree.settings
    )
    resp = TestClient(Starlette(routes=[route])).get("/llms.txt")
    assert resp.status_code == 200 and "## Pages" in resp.text


def test_llms_txt_hook_error_falls_back(app_tree):
    (app_tree.pages / "about.pyxl").write_text("x")
    (app_tree.pages / "llms.py").write_text("def llms_txt(ctx):\n    raise RuntimeError('x')\n")
    route = llms.make_llms_txt_route(
        _routes_in(app_tree, ("about.pyxl", "/about")), settings=app_tree.settings
    )
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


def test_markdown_route_auto_convert_rewrites_links_and_wrap_sees_them(
    app_tree, monkeypatch
):
    import pyxle.ssr.view as view

    async def fake_render(*, request, settings, page, renderer, suppress_per_user=False):
        html = (
            '<p>See <a href="/about?x=1#y">about</a>, '
            '<a href="https://ext.example/z">ext</a> and '
            '<a href="/api/go">api</a>.</p>'
        )
        return html, 200

    monkeypatch.setattr(view, "render_page_body_html", fake_render)
    (app_tree.pages / "z.pyxl").write_text("x")
    (app_tree.pages / "llms.py").write_text(
        "def wrap_markdown(ctx, md):\n    return 'HDR\\n' + md\n"
    )
    routes = SimpleNamespace(pages=[_page_in(app_tree, "z.pyxl", "/z")])
    cfg = SimpleNamespace(enabled=True, auto_convert=True)
    md_routes = llms.build_markdown_routes(
        routes, settings=app_tree.settings, renderer=None, config=cfg
    )
    resp = TestClient(Starlette(routes=md_routes)).get("/z.md")
    assert resp.status_code == 200
    # wrap_markdown received the already-rewritten markdown
    assert resp.text.startswith("HDR\n")
    assert "[about](/about.md?x=1#y)" in resp.text
    assert "[ext](https://ext.example/z)" in resp.text
    assert "[api](/api/go)" in resp.text


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


def test_llms_txt_sync_hook_runs_off_event_loop(app_tree):
    """A sync ``llms_txt`` hook runs in a worker thread (no running event
    loop), so a hook that calls ``ctx.render_default()`` — filesystem checks
    plus module imports — can never block the loop."""
    (app_tree.pages / "about.pyxl").write_text("x")
    (app_tree.pages / "about.md").write_text("# About\n")
    routes = _routes_in(app_tree, ("about.pyxl", "/about"))
    (app_tree.pages / "llms.py").write_text(
        "import asyncio\n"
        "def llms_txt(ctx):\n"
        "    try:\n"
        "        asyncio.get_running_loop()\n"
        "        marker = 'ON_LOOP'\n"
        "    except RuntimeError:\n"
        "        marker = 'OFF_LOOP'\n"
        "    return f'# {marker}\\n' + ctx.render_default()\n"
    )
    route = llms.make_llms_txt_route(routes, settings=app_tree.settings)
    client = TestClient(Starlette(routes=[route]))
    resp = client.get("/llms.txt")
    assert resp.status_code == 200
    assert resp.text.startswith("# OFF_LOOP\n")
    assert "- [About](http://testserver/about.md)" in resp.text
