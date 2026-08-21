from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from pyxle.devserver.routes import PageRoute
from pyxle.devserver.settings import DevServerSettings
from pyxle.ssr.renderer import InlineStyleFragment
from pyxle.ssr.template import render_document, render_error_document, render_head_markup


@pytest.fixture
def page_route(tmp_path: Path) -> PageRoute:
    return PageRoute(
        path="/",
        source_relative_path=Path("index.pyxl"),
        source_absolute_path=tmp_path / "pages" / "index.pyxl",
        server_module_path=tmp_path / "server" / "index.py",
        client_module_path=tmp_path / "client" / "index.jsx",
        metadata_path=tmp_path / "metadata" / "index.json",
        module_key="pyxle.server.pages.index",
        client_asset_path="/pages/index.jsx",
        server_asset_path="/pages/index.py",
        content_hash="abc",
        loader_name="load_home",
        loader_line=10,
        head_elements=(),
    head_is_dynamic=False,
    )


def test_render_document_injects_expected_scripts(page_route: PageRoute, tmp_path: Path) -> None:
    settings = DevServerSettings.from_project_root(tmp_path)

    html = render_document(
        settings=settings,
        page=page_route,
        body_html="<p>Hello</p>",
        props={"data": {"greeting": "</script>"}},
        script_nonce="test-nonce",
        head_elements=page_route.head_elements,
    )

    assert "<!DOCTYPE html>" in html
    assert "<div id=\"root\">" in html
    assert "<p>Hello</p>" in html
    # No page/layout title → the app's own name, never the framework's.
    assert f"<title>{tmp_path.name}</title>" in html
    assert "window.__PYXLE_PAGE_PATH__ = \"/pages/index.jsx\"" in html
    assert "@vite/client" in html
    assert "@react-refresh" in html
    assert "__vite_plugin_react_preamble_installed__" in html
    assert "client-entry.js" in html
    assert '"data":{"greeting":"<\\/script>"}' in html
    assert "<\\/script>" in html  # escaped closing tag in props payload
    assert 'nonce="test-nonce"' in html


def test_dev_document_reports_a_module_that_never_loaded(
    page_route: PageRoute, tmp_path: Path
) -> None:
    """The browser's half of the framework's most silent failure.

    A module Vite refuses is not a JavaScript error: no ``window.onerror``
    handler, no framework overlay and no Vite log line ever hears about it, and
    the page is left rendered and inert. A capturing listener does hear it, so
    the console stops being one more place that says nothing.
    """

    settings = DevServerSettings.from_project_root(tmp_path, starlette_host="0.0.0.0")

    html = render_document(
        settings=settings,
        page=page_route,
        body_html="<p>Hello</p>",
        props={},
        script_nonce="test-nonce",
        head_elements=page_route.head_elements,
        request_host="192.168.1.11",
    )

    reporter = html.split("@vite/client")[0]
    # Installed BEFORE the module scripts it is there to watch.
    assert "addEventListener('error'" in reporter
    assert "}, true);" in reporter  # capturing: load failures do not bubble
    # Watches the exact origin this page's modules come from.
    assert "http://192.168.1.11:5173" in reporter
    assert 'nonce="test-nonce"' in reporter
    assert "did not load, so this page will not become " in reporter


def test_production_document_carries_no_dev_reporter(
    page_route: PageRoute, tmp_path: Path
) -> None:
    settings = replace(
        DevServerSettings.from_project_root(tmp_path),
        debug=False,
        page_manifest={"pages/index.jsx": {"file": "assets/index.js"}},
    )

    html = render_document(
        settings=settings,
        page=page_route,
        body_html="<p>Hello</p>",
        props={},
        script_nonce="test-nonce",
        head_elements=page_route.head_elements,
    )

    assert "addEventListener('error'" not in html


def test_default_title_uses_configured_app_name(page_route: PageRoute, tmp_path: Path) -> None:
    """`name` in pyxle.config.json is the default <title> for untitled pages."""
    settings = DevServerSettings.from_project_root(tmp_path, app_name="Acme Dashboard")

    html = render_document(
        settings=settings,
        page=page_route,
        body_html="<p>Hello</p>",
        props={},
        script_nonce="n",
        head_elements=(),
    )

    assert "<title>Acme Dashboard</title>" in html
    assert "<title>Pyxle</title>" not in html


def test_default_title_falls_back_to_project_directory(tmp_path: Path) -> None:
    """With no configured name, the project directory names the app."""
    settings = DevServerSettings.from_project_root(tmp_path)

    assert settings.document_title_default == tmp_path.name


def test_default_title_escapes_markup() -> None:
    """A name is user data on its way into HTML — it must be escaped."""
    markup = render_head_markup((), '<script>alert("x")</script>')

    assert "<script>" not in markup
    assert "&lt;script&gt;" in markup


def test_page_title_suppresses_the_default() -> None:
    """A page/layout <title> wins; no default is injected alongside it."""
    markup = render_head_markup(("<title>My Page</title>",), "Acme Dashboard")

    assert "<title>My Page</title>" in markup
    assert "Acme Dashboard" not in markup


def test_render_document_embeds_nav_seed(page_route: PageRoute, tmp_path: Path) -> None:
    settings = DevServerSettings.from_project_root(tmp_path)

    html = render_document(
        settings=settings,
        page=page_route,
        body_html="<p>Hi</p>",
        props={"data": {}},
        script_nonce="seed-nonce",
        head_elements=page_route.head_elements,
        nav_cache_ttl=300,
    )

    # The seed blob lets the client cache the page it landed on (so the active
    # self-link's prefetch is a hit) and carries the per-page nav-cache TTL.
    assert 'id="__PYXLE_NAV_SEED__"' in html
    assert '"navCacheTtlSeconds":300' in html
    assert '"headMarkup":' in html


def test_render_document_nav_seed_defaults_to_null_ttl(page_route: PageRoute, tmp_path: Path) -> None:
    settings = DevServerSettings.from_project_root(tmp_path)

    html = render_document(
        settings=settings,
        page=page_route,
        body_html="<p>Hi</p>",
        props={},
        script_nonce="n",
        head_elements=page_route.head_elements,
    )

    assert 'id="__PYXLE_NAV_SEED__"' in html
    # No TTL passed → null, so the client applies its default lifetime.
    assert '"navCacheTtlSeconds":null' in html


def test_render_document_embeds_configured_nav_stale_default(
    page_route: PageRoute, tmp_path: Path
) -> None:
    from pyxle.config import NavigationConfig

    settings = DevServerSettings.from_project_root(
        tmp_path, navigation=NavigationConfig(default_prefetch_ttl=90)
    )
    html = render_document(
        settings=settings,
        page=page_route,
        body_html="<p>Hi</p>",
        props={},
        script_nonce="n",
        head_elements=page_route.head_elements,
    )

    # The configured default (seconds) is exposed to the client in milliseconds.
    assert "window.__PYXLE_NAV_STALE_MS__ = 90000" in html


def test_render_document_omits_nav_stale_when_unconfigured(
    page_route: PageRoute, tmp_path: Path
) -> None:
    settings = DevServerSettings.from_project_root(tmp_path)
    html = render_document(
        settings=settings,
        page=page_route,
        body_html="<p>Hi</p>",
        props={},
        script_nonce="n",
        head_elements=page_route.head_elements,
    )

    # No config → no override embedded; the client keeps its built-in default.
    assert "__PYXLE_NAV_STALE_MS__" not in html


def test_render_document_embeds_custom_csrf_names(
    page_route: PageRoute, tmp_path: Path
) -> None:
    """Custom ``csrf.cookieName`` / ``csrf.headerName`` must reach the client.

    The client runtime reads the CSRF cookie and sends the CSRF header by
    name; without these globals a custom-configured app gets every action
    POST rejected with 403 (the middleware uses the configured names while
    the client keeps the defaults).
    """
    from pyxle.config import CsrfConfig

    settings = DevServerSettings.from_project_root(
        tmp_path,
        csrf=CsrfConfig(cookie_name="cloud-csrf", header_name="x-cloud-csrf"),
    )

    html = render_document(
        settings=settings,
        page=page_route,
        body_html="<p>Hi</p>",
        props={},
        script_nonce="n",
        head_elements=page_route.head_elements,
    )

    assert 'window.__PYXLE_CSRF_COOKIE__ = "cloud-csrf";' in html
    assert 'window.__PYXLE_CSRF_HEADER__ = "x-cloud-csrf";' in html
    # The bootstrap script must carry the CSP nonce like every other script.
    assert '<script nonce="n">window.__PYXLE_CSRF_COOKIE__' in html


def test_render_document_embeds_custom_csrf_names_in_production(
    page_route: PageRoute, tmp_path: Path
) -> None:
    """The manifest-backed production shell carries the globals too."""
    from pyxle.config import CsrfConfig

    settings = DevServerSettings.from_project_root(
        tmp_path,
        debug=False,
        csrf=CsrfConfig(cookie_name="cloud-csrf", header_name="x-cloud-csrf"),
        page_manifest={"/": {"client": {"file": "assets/index.js", "css": []}}},
    )

    html = render_document(
        settings=settings,
        page=page_route,
        body_html="<div>Prod</div>",
        props={},
        script_nonce="secure",
        head_elements=page_route.head_elements,
    )

    assert "/client/assets/index.js" in html
    assert 'window.__PYXLE_CSRF_COOKIE__ = "cloud-csrf";' in html
    assert 'window.__PYXLE_CSRF_HEADER__ = "x-cloud-csrf";' in html


def test_render_document_injects_modulepreload_in_production(
    page_route: PageRoute, tmp_path: Path
) -> None:
    """The production shell preloads the entry module and its imported chunks."""
    settings = DevServerSettings.from_project_root(
        tmp_path,
        debug=False,
        page_manifest={
            "/": {
                "client": {
                    "file": "assets/index.js",
                    "imports": ["dist/assets/vendor.js", "dist/assets/shared.js"],
                    "css": [],
                }
            }
        },
    )

    html = render_document(
        settings=settings,
        page=page_route,
        body_html="<div>Prod</div>",
        props={},
        script_nonce="secure",
        head_elements=page_route.head_elements,
    )

    # The entry module itself is preloaded (so it fetches during head parse,
    # not when the <script> at the body end is reached)...
    assert '<link rel="modulepreload" href="/client/assets/index.js" />' in html
    # ...and so are the chunks it statically imports.
    assert '<link rel="modulepreload" href="/client/dist/assets/vendor.js" />' in html
    assert '<link rel="modulepreload" href="/client/dist/assets/shared.js" />' in html


def test_render_document_embeds_only_non_default_csrf_names(
    page_route: PageRoute, tmp_path: Path
) -> None:
    """A custom cookie name with the default header emits one global only."""
    from pyxle.config import CsrfConfig

    settings = DevServerSettings.from_project_root(
        tmp_path, csrf=CsrfConfig(cookie_name="cloud-csrf")
    )

    html = render_document(
        settings=settings,
        page=page_route,
        body_html="<p>Hi</p>",
        props={},
        script_nonce="n",
        head_elements=page_route.head_elements,
    )

    assert 'window.__PYXLE_CSRF_COOKIE__ = "cloud-csrf";' in html
    assert "__PYXLE_CSRF_HEADER__" not in html


def test_render_document_embeds_auto_port_namespaced_cookie_name(
    page_route: PageRoute, tmp_path: Path
) -> None:
    """The auto (port-namespaced) cookie name must reach the client.

    With ``csrf.cookieName`` unset, the middleware names the cookie
    ``pyxle-csrf-<bind port>`` — a name the client cannot derive itself
    (behind a reverse proxy the bind port is invisible), so the shell must
    inject it. The default header name still needs no global."""
    from pyxle.config import CsrfConfig

    settings = DevServerSettings.from_project_root(
        tmp_path, starlette_port=8103, csrf=CsrfConfig()
    )
    html = render_document(
        settings=settings,
        page=page_route,
        body_html="<p>Hi</p>",
        props={},
        script_nonce="n",
        head_elements=page_route.head_elements,
    )

    assert 'window.__PYXLE_CSRF_COOKIE__ = "pyxle-csrf-8103";' in html
    assert "__PYXLE_CSRF_HEADER__" not in html


def test_render_document_omits_csrf_names_for_client_fallbacks(
    page_route: PageRoute, tmp_path: Path
) -> None:
    """No CSRF config, or names matching the client's baked-in fallbacks,
    embed nothing — the client resolves ``pyxle-csrf`` / ``x-csrf-token``
    on its own."""
    from pyxle.config import CsrfConfig

    for csrf in (
        None,
        CsrfConfig(cookie_name="pyxle-csrf"),
        CsrfConfig(cookie_name="pyxle-csrf", header_name="X-CSRF-Token"),
    ):
        settings = DevServerSettings.from_project_root(tmp_path, csrf=csrf)
        html = render_document(
            settings=settings,
            page=page_route,
            body_html="<p>Hi</p>",
            props={},
            script_nonce="n",
            head_elements=page_route.head_elements,
        )
        assert "__PYXLE_CSRF_COOKIE__" not in html
        assert "__PYXLE_CSRF_HEADER__" not in html


def test_render_document_embeds_auth_seed(
    page_route: PageRoute, tmp_path: Path
) -> None:
    """An auth provider's scope seed becomes window.__PYXLE_AUTH__ so the
    client useAuth hook shows the signed-in user on the first frame."""
    settings = DevServerSettings.from_project_root(tmp_path)
    seed = {
        "user": {"id": "u1", "email": "a@b.c", "emailVerified": True, "plan": "free"},
        "endpoints": {"me": "/auth/me", "logout": "/auth/logout"},
    }
    html = render_document(
        settings=settings,
        page=page_route,
        body_html="<p>Hi</p>",
        props={},
        script_nonce="n",
        head_elements=page_route.head_elements,
        auth_seed=seed,
    )
    assert "window.__PYXLE_AUTH__ = " in html
    assert '"email":"a@b.c"' in html
    assert '<script nonce="n">window.__PYXLE_AUTH__' in html


def test_render_document_embeds_anonymous_auth_seed(
    page_route: PageRoute, tmp_path: Path
) -> None:
    """A seed with ``user: None`` is still emitted — the client then knows it
    is definitively logged out, no /auth/me round-trip needed."""
    settings = DevServerSettings.from_project_root(tmp_path)
    html = render_document(
        settings=settings,
        page=page_route,
        body_html="<p>Hi</p>",
        props={},
        script_nonce="n",
        head_elements=page_route.head_elements,
        auth_seed={"user": None, "endpoints": {"me": "/auth/me"}},
    )
    assert '"user":null' in html
    assert "window.__PYXLE_AUTH__ = " in html


def test_render_document_omits_auth_seed_when_absent(
    page_route: PageRoute, tmp_path: Path
) -> None:
    """No auth provider on the request → no seed script (useAuth resolves over
    the network instead)."""
    settings = DevServerSettings.from_project_root(tmp_path)
    html = render_document(
        settings=settings,
        page=page_route,
        body_html="<p>Hi</p>",
        props={},
        script_nonce="n",
        head_elements=page_route.head_elements,
    )
    assert "__PYXLE_AUTH__" not in html


def test_render_document_auth_seed_escapes_script_close(
    page_route: PageRoute, tmp_path: Path
) -> None:
    """A hostile value in the seed (here an email-shaped payload) must not be
    able to break out of the inline <script>."""
    settings = DevServerSettings.from_project_root(tmp_path)
    html = render_document(
        settings=settings,
        page=page_route,
        body_html="<p>Hi</p>",
        props={},
        script_nonce="n",
        head_elements=page_route.head_elements,
        auth_seed={"user": {"email": "</script><script>alert(1)</script>"}, "endpoints": {}},
    )
    # The raw closing tag must be escaped in the emitted seed.
    assert "</script><script>alert(1)" not in html
    assert "<\\/script>" in html


def test_render_document_inlines_global_styles(page_route: PageRoute, tmp_path: Path) -> None:
    style_path = tmp_path / "styles" / "base.css"
    style_path.parent.mkdir(parents=True, exist_ok=True)
    style_path.write_text("body { color: #444; }\n</style>", encoding="utf-8")

    settings = DevServerSettings.from_project_root(
        tmp_path,
        global_stylesheets=("styles/base.css",),
    )

    html = render_document(
        settings=settings,
        page=page_route,
        body_html="<main></main>",
        props={},
        script_nonce="nonce",
        head_elements=page_route.head_elements,
    )

    assert 'data-pyxle-style="' in html
    assert "body { color: #444; }" in html
    # Closing tags should be escaped to avoid terminating the style prematurely.
    assert "<\\/style>" in html
    assert html.index("data-pyxle-style") < html.index("data-pyxle-head-start")


def test_render_document_includes_inline_styles(page_route: PageRoute, tmp_path: Path) -> None:
    settings = DevServerSettings.from_project_root(tmp_path)

    html = render_document(
        settings=settings,
        page=page_route,
        body_html="<main></main>",
        props={},
        script_nonce="nonce",
        head_elements=page_route.head_elements,
        inline_styles=(
            InlineStyleFragment(
                identifier="style-inline",
                contents=".hero { color: red; }\n</style>",
                source="pages/components/hero.css",
            ),
            InlineStyleFragment(
                identifier="style-inline",
                contents=".ignored { color: blue; }",
                source="pages/ignored.css",
            ),
            InlineStyleFragment(
                identifier="style-secondary",
                contents="",
                source=None,
            ),
        ),
    )

    assert html.count('data-pyxle-inline-style="style-inline"') == 1
    assert 'data-pyxle-inline-source="pages/components/hero.css"' in html
    assert '.hero { color: red; }' in html
    assert '<\\/style>' in html
    assert 'data-pyxle-inline-style="style-secondary"' in html
    assert 'data-pyxle-inline-source="pages/ignored.css"' not in html
    assert html.index('data-pyxle-inline-style="style-inline"') < html.index('data-pyxle-head-start')


def test_render_document_uses_dynamic_route_asset_path(tmp_path: Path) -> None:
    settings = DevServerSettings.from_project_root(tmp_path)

    dynamic_page = PageRoute(
        path="/posts/{id}",
        source_relative_path=Path("posts/[id].pyxl"),
        source_absolute_path=tmp_path / "pages" / "posts" / "[id].pyxl",
        server_module_path=tmp_path / "server" / "posts" / "[id].py",
        client_module_path=tmp_path / "client" / "posts" / "[id].jsx",
        metadata_path=tmp_path / "metadata" / "posts" / "[id].json",
        module_key="pyxle.server.pages.posts.[id]",
        client_asset_path="/pages/posts/[id].jsx",
        server_asset_path="/pages/posts/[id].py",
        content_hash="hash",
        loader_name="load_post",
        loader_line=12,
        head_elements=(),
        head_is_dynamic=False,
    )

    html = render_document(
        settings=settings,
        page=dynamic_page,
        body_html="<article></article>",
        props={},
        script_nonce="nonce",
        head_elements=dynamic_page.head_elements,
    )

    assert 'window.__PYXLE_PAGE_PATH__ = "/pages/posts/[id].jsx"' in html


def test_render_document_includes_custom_head(page_route: PageRoute, tmp_path: Path) -> None:
    settings = DevServerSettings.from_project_root(tmp_path)

    custom_page = replace(
        page_route,
        head_elements=(
            "<title>Custom Title</title>",
            '<meta name="description" content="Demo" />',
        ),
    )

    html = render_document(
        settings=settings,
        page=custom_page,
        body_html="<p>Body</p>",
        props={},
        script_nonce="another",
        head_elements=custom_page.head_elements,
    )

    assert "<title>Custom Title</title>" in html
    assert '<meta name="description" content="Demo" />' in html
    assert "<title>Pyxle</title>" not in html
    vite_index = html.index("@vite/client")
    custom_index = html.index("<title>Custom Title</title>")
    assert custom_index > vite_index


def test_render_document_allows_empty_nonce(page_route: PageRoute, tmp_path: Path) -> None:
    settings = DevServerSettings.from_project_root(tmp_path)

    html = render_document(
        settings=settings,
        page=page_route,
        body_html="<div></div>",
        props={},
        script_nonce="",
        head_elements=page_route.head_elements,
    )

    assert "nonce=\"" not in html


def test_render_document_uses_manifest_assets_in_production(page_route: PageRoute, tmp_path: Path) -> None:
    settings = DevServerSettings.from_project_root(
        tmp_path,
        debug=False,
        page_manifest={
            "/": {
                "client": {
                    "file": "assets/index.js",
                    "imports": [],
                    "css": ["assets/index.css"],
                }
            }
        },
    )

    html = render_document(
        settings=settings,
        page=page_route,
        body_html="<div>Prod</div>",
        props={},
        script_nonce="secure",
        head_elements=page_route.head_elements,
    )

    assert "/client/assets/index.js" in html
    assert 'rel="stylesheet" href="/client/assets/index.css"' in html
    assert "@vite/client" not in html
    assert "client-entry.js" not in html


def test_render_document_missing_manifest_entry_shows_fallback(
    page_route: PageRoute, tmp_path: Path
) -> None:
    settings = DevServerSettings.from_project_root(
        tmp_path,
        debug=False,
        page_manifest={},
    )

    html = render_document(
        settings=settings,
        page=page_route,
        body_html="<div>Prod</div>",
        props={},
        script_nonce="secure",
        head_elements=page_route.head_elements,
    )

    assert "Missing Manifest Entry" in html


def test_render_document_invalid_manifest_client_shows_fallback(
    page_route: PageRoute, tmp_path: Path
) -> None:
    settings = DevServerSettings.from_project_root(
        tmp_path,
        debug=False,
        page_manifest={"/": {"client": "oops"}},
    )

    html = render_document(
        settings=settings,
        page=page_route,
        body_html="<div>Prod</div>",
        props={},
        script_nonce="secure",
        head_elements=page_route.head_elements,
    )

    assert "Missing Manifest Entry" in html


def test_render_document_missing_manifest_file_shows_fallback(
    page_route: PageRoute, tmp_path: Path
) -> None:
    settings = DevServerSettings.from_project_root(
        tmp_path,
        debug=False,
        page_manifest={"/": {"client": {"file": ""}}},
    )

    html = render_document(
        settings=settings,
        page=page_route,
        body_html="<div>Prod</div>",
        props={},
        script_nonce="secure",
        head_elements=page_route.head_elements,
    )

    assert "Missing Manifest Entry" in html


def test_render_document_ignores_non_list_css_assets(
    page_route: PageRoute, tmp_path: Path
) -> None:
    settings = DevServerSettings.from_project_root(
        tmp_path,
        debug=False,
        page_manifest={
            "/": {
                "client": {
                    "file": "assets/index.js",
                    "css": "not-a-list",
                }
            }
        },
    )

    html = render_document(
        settings=settings,
        page=page_route,
        body_html="<div>Prod</div>",
        props={},
        script_nonce="secure",
        head_elements=page_route.head_elements,
    )

    assert "/client/assets/index.js" in html
    assert 'rel="stylesheet"' not in html


def test_render_document_skips_blank_head_fragments(
    page_route: PageRoute, tmp_path: Path
) -> None:
    settings = DevServerSettings.from_project_root(tmp_path)
    custom = replace(
        page_route,
        head_elements=(
            "",
            '<meta name="description" content="Demo" />\n<link rel="icon" href="/favicon.ico" />',
        ),
    )

    html = render_document(
        settings=settings,
        page=custom,
        body_html="<div>Body</div>",
        props={},
        script_nonce="nonce",
        head_elements=custom.head_elements,
    )

    assert '<meta name="description" content="Demo" />' in html
    assert '<link rel="icon" href="/favicon.ico" />' in html
    assert html.count('<link rel="icon" href="/favicon.ico" />') == 1


# ---------------------------------------------------------------------------
# render_error_document — dev vs production behavior
# ---------------------------------------------------------------------------


class _SecretError(RuntimeError):
    """Custom exception used by the error-document tests."""


def test_render_error_document_dev_mode_includes_details(
    page_route: PageRoute, tmp_path: Path
) -> None:
    """Dev mode keeps the developer-friendly overlay: route path,
    exception type, exception message, and the Vite HMR client tag
    so the page reloads when the developer fixes the bug."""
    settings = DevServerSettings.from_project_root(tmp_path)  # debug=True
    error = _SecretError("DB row 12345 / api_key=sk_live_abc123")

    html = render_error_document(
        settings=settings, page=page_route, error=error
    )

    assert "Server Render Failed" in html
    assert "_SecretError" in html
    # The raw DB row ID is still visible (not a secret pattern), but
    # api_key=... is redacted by the sensitive-pattern filter.
    assert "DB row 12345" in html
    assert "sk_live_abc123" not in html
    assert "[REDACTED_SECRET]" in html
    assert page_route.path in html
    assert "@vite/client" in html


def test_render_error_document_production_mode_redacts_internals(
    page_route: PageRoute, tmp_path: Path
) -> None:
    """Production mode (debug=False) MUST NOT leak the exception
    type, the exception message, the route path, or the Vite HMR
    client tag. Per CLAUDE.md rule 18, production responses must
    not expose internal state."""
    settings = replace(
        DevServerSettings.from_project_root(tmp_path), debug=False
    )
    error = _SecretError("DB row 12345 / api_key=sk_live_abc123")

    html = render_error_document(
        settings=settings, page=page_route, error=error
    )

    # Generic error page is rendered.
    assert "Server Error" in html
    assert "<!DOCTYPE html>" in html
    # NONE of the internal details leak.
    assert "_SecretError" not in html
    assert "DB row 12345" not in html
    assert "api_key" not in html
    assert "sk_live_abc123" not in html
    # The dev-mode Vite client tag must be absent in production.
    assert "@vite/client" not in html
    assert "5173" not in html
    # The route path must not leak either — it can be reconstructed
    # from the request URL but the response body shouldn't echo it.
    assert page_route.path not in html or page_route.path == "/"


def test_render_error_document_production_handles_html_in_message(
    page_route: PageRoute, tmp_path: Path
) -> None:
    """Even though prod doesn't include the message, confirm that
    an exception whose ``str()`` contains HTML/script tags can't
    sneak in via any code path. The output is the same fixed
    string regardless of the input error."""
    settings = replace(
        DevServerSettings.from_project_root(tmp_path), debug=False
    )
    error = RuntimeError("<script>alert('xss')</script>")

    html = render_error_document(
        settings=settings, page=page_route, error=error
    )

    assert "<script>alert" not in html
    assert "alert(" not in html
    assert "RuntimeError" not in html


def test_render_error_document_dev_escapes_html_in_message(
    page_route: PageRoute, tmp_path: Path
) -> None:
    """Dev mode shows the message but must HTML-escape it so an
    attacker who can trigger an exception with HTML in the
    message text cannot inject script tags into the dev overlay."""
    settings = DevServerSettings.from_project_root(tmp_path)  # debug=True
    error = RuntimeError("<script>alert('xss')</script>")

    html = render_error_document(
        settings=settings, page=page_route, error=error
    )

    # Raw script tag is NOT in the output.
    assert "<script>alert" not in html
    # Escaped form IS in the output.
    assert "&lt;script&gt;alert" in html

def test_document_emits_loading_asset_when_boundary_present(
    page_route: PageRoute, tmp_path: Path
) -> None:
    settings = DevServerSettings.from_project_root(tmp_path)
    loading_route = replace(page_route, client_asset_path="/pages/dashboard/loading.jsx")
    page = replace(page_route, loading_boundary=loading_route)

    html = render_document(
        settings=settings,
        page=page,
        body_html="<main>x</main>",
        props={"data": {}},
        script_nonce="n",
        head_elements=(),
    )
    # The client reads this to wrap the page in the same loading <Suspense>.
    assert 'window.__PYXLE_LOADING_ASSET__ = "/pages/dashboard/loading.jsx"' in html


def test_document_loading_asset_is_null_without_boundary(
    page_route: PageRoute, tmp_path: Path
) -> None:
    settings = DevServerSettings.from_project_root(tmp_path)
    html = render_document(
        settings=settings,
        page=page_route,  # no loading_boundary
        body_html="<main>x</main>",
        props={"data": {}},
        script_nonce="n",
        head_elements=(),
    )
    assert "window.__PYXLE_LOADING_ASSET__ = null" in html


def test_document_emits_error_asset_when_boundary_present(
    page_route: PageRoute, tmp_path: Path
) -> None:
    settings = DevServerSettings.from_project_root(tmp_path)
    error_route = replace(page_route, client_asset_path="/pages/dashboard/error.jsx")
    page = replace(page_route, error_boundary=error_route)

    html = render_document(
        settings=settings,
        page=page,
        body_html="<main>x</main>",
        props={"data": {}},
        script_nonce="n",
        head_elements=(),
    )
    # The client reads this to wrap the page in the React error boundary whose
    # fallback is the nearest error.pyxl.
    assert 'window.__PYXLE_ERROR_ASSET__ = "/pages/dashboard/error.jsx"' in html


def test_document_error_asset_is_null_without_boundary(
    page_route: PageRoute, tmp_path: Path
) -> None:
    settings = DevServerSettings.from_project_root(tmp_path)
    html = render_document(
        settings=settings,
        page=page_route,  # no error_boundary
        body_html="<main>x</main>",
        props={"data": {}},
        script_nonce="n",
        head_elements=(),
    )
    assert "window.__PYXLE_ERROR_ASSET__ = null" in html


# ---------------------------------------------------------------------------
# render_error_document — the status decides the wording
# ---------------------------------------------------------------------------


def test_production_404_does_not_blame_the_server(
    page_route: PageRoute, tmp_path: Path
) -> None:
    """A loader raising ``LoaderError(status_code=404)`` is stating a fact
    about the request, not reporting a fault.

    Every sub-500 status used to render the 500 document, so a visitor who
    followed a stale link was told "Server Error / The server encountered an
    error while processing this request. Please try again later." — which sends
    them to complain to the wrong people, or to wait for a recovery that is
    never coming. Found on a public status page, where the visitor is a
    customer of the operator rather than the operator.
    """
    settings = DevServerSettings.from_project_root(tmp_path, debug=False)

    html = render_error_document(
        settings=settings, page=page_route,
        error=RuntimeError("No status page here."), status_code=404,
    )

    assert "<title>Not found</title>" in html
    assert "There is nothing at this address." in html
    assert "Server Error" not in html
    assert "try again later" not in html


def test_production_5xx_keeps_the_opaque_wording(
    page_route: PageRoute, tmp_path: Path
) -> None:
    settings = DevServerSettings.from_project_root(tmp_path, debug=False)

    html = render_error_document(
        settings=settings, page=page_route,
        error=RuntimeError("boom"), status_code=500,
    )

    assert "Server Error" in html
    assert "try again later" in html


def test_production_defaults_to_the_server_error_document(
    page_route: PageRoute, tmp_path: Path
) -> None:
    """No status supplied means 500, and 500 stays opaque — the server really
    is at fault and "try again later" is honest advice."""
    settings = DevServerSettings.from_project_root(tmp_path, debug=False)

    assert "Server Error" in render_error_document(
        settings=settings, page=page_route, error=RuntimeError("x")
    )


@pytest.mark.parametrize("status", [402, 405, 408, 409, 418, 422, 429, 451, 499])
def test_no_4xx_is_ever_called_a_server_error(
    page_route: PageRoute, tmp_path: Path, status: int
) -> None:
    """The wording is a rule over status classes, not a list of statuses.

    Every status here is a 4xx, and several have no wording of their own — 418
    and 499 never will. A 4xx says something about the *request*, so none of
    them may be reported as the server failing: telling a rate-limited visitor
    the server broke sends them to complain to the wrong people and to wait for
    a recovery that is not coming. Adding wording for a status is a refinement;
    it must never be the thing that stops a page lying about whose fault it is.
    """
    settings = DevServerSettings.from_project_root(tmp_path, debug=False)

    html = render_error_document(
        settings=settings, page=page_route, error=RuntimeError("x"),
        status_code=status,
    )

    assert "Server Error" not in html
    assert "try again later" not in html


@pytest.mark.parametrize("status", [500, 502, 503, 504, 599])
def test_every_5xx_keeps_the_server_error_wording(
    page_route: PageRoute, tmp_path: Path, status: int
) -> None:
    """The other half of the rule, including 5xx nobody wrote wording for."""
    settings = DevServerSettings.from_project_root(tmp_path, debug=False)

    assert "Server Error" in render_error_document(
        settings=settings, page=page_route, error=RuntimeError("x"),
        status_code=status,
    )


@pytest.mark.parametrize("status,heading", [
    (400, "Bad request"),
    (401, "Sign in required"),
    (402, "Payment required"),
    (403, "Not available"),
    (404, "Not found"),
    (405, "Not allowed here"),
    (408, "Request timed out"),
    (409, "Already changed"),
    (410, "Gone"),
    (422, "Could not process"),
    (429, "Too many requests"),
    (451, "Unavailable for legal reasons"),
])
def test_each_status_has_its_own_wording(
    page_route: PageRoute, tmp_path: Path, status: int, heading: str
) -> None:
    settings = DevServerSettings.from_project_root(tmp_path, debug=False)

    html = render_error_document(
        settings=settings, page=page_route, error=RuntimeError("x"),
        status_code=status,
    )

    assert f"<title>{heading}</title>" in html


def test_a_status_document_still_leaks_nothing(
    page_route: PageRoute, tmp_path: Path
) -> None:
    """The wording changed; the rule about internal state did not."""
    settings = DevServerSettings.from_project_root(tmp_path, debug=False)
    error = _SecretError("DB row 12345 / api_key=sk_live_abc123")

    html = render_error_document(
        settings=settings, page=page_route, error=error, status_code=404,
    )

    assert "_SecretError" not in html
    assert "sk_live_abc123" not in html
    assert "DB row 12345" not in html
    # Not asserted on `page_route.path`: it is "/", which appears in every
    # closing tag. The dev-mode overlay is where the route is disclosed, and
    # that path is covered by its own test above.
    assert "No status page here" not in html


# ---------------------------------------------------------------------------
# <Script> declarations that were never evaluated
# ---------------------------------------------------------------------------
#
# <Script> is harvested from `.pyxl` source at compile time exactly like
# <Head>, so `<Script src={analyticsUrl} />` reaches the document shell as the
# literal text `{analyticsUrl}`. It leaves by two doors — a `beforeInteractive`
# tag written straight into <head>, and the `__PYXLE_SCRIPTS__` payload the
# bootstrap loader injects from — and both produce a request for a relative URL
# that does not exist. The <Script> component loads the evaluated src itself.


def _script(**overrides) -> dict:
    script = {
        "src": "/analytics.js",
        "strategy": "afterInteractive",
        "async": False,
        "defer": False,
        "module": False,
        "noModule": False,
    }
    script.update(overrides)
    return script


def test_an_unevaluated_before_interactive_script_is_not_written_into_head(
    page_route: PageRoute, tmp_path: Path
) -> None:
    settings = DevServerSettings.from_project_root(tmp_path)
    page = replace(
        page_route,
        scripts=(_script(src="{analyticsUrl}", strategy="beforeInteractive"),),
    )

    html = render_document(
        settings=settings,
        page=page,
        body_html="<p>Hello</p>",
        props={},
        script_nonce="n",
        head_elements=(),
    )

    assert "analyticsUrl" not in html


def test_an_unevaluated_script_is_not_handed_to_the_bootstrap_loader(
    page_route: PageRoute, tmp_path: Path
) -> None:
    settings = DevServerSettings.from_project_root(tmp_path)
    page = replace(page_route, scripts=(_script(src="{analyticsUrl}"),))

    html = render_document(
        settings=settings,
        page=page,
        body_html="<p>Hello</p>",
        props={},
        script_nonce="n",
        head_elements=(),
    )

    assert "window.__PYXLE_SCRIPTS__ = [];" in html
    assert "analyticsUrl" not in html


def test_an_expression_in_any_extracted_prop_drops_the_script(
    page_route: PageRoute, tmp_path: Path
) -> None:
    """`strategy={...}` is unevaluated too — the literal text is neither
    "beforeInteractive" nor a strategy the client recognises, so the tag would
    be placed by a value that was never read."""
    settings = DevServerSettings.from_project_root(tmp_path)
    page = replace(
        page_route,
        scripts=(_script(src="/analytics.js", strategy="{chosenStrategy}"),),
    )

    html = render_document(
        settings=settings,
        page=page,
        body_html="<p>Hello</p>",
        props={},
        script_nonce="n",
        head_elements=(),
    )

    assert "chosenStrategy" not in html
    assert "window.__PYXLE_SCRIPTS__ = [];" in html


def test_an_evaluated_script_is_still_emitted_both_ways(
    page_route: PageRoute, tmp_path: Path
) -> None:
    """The filter must not eat working scripts."""
    settings = DevServerSettings.from_project_root(tmp_path)
    page = replace(
        page_route,
        scripts=(
            _script(src="/early.js", strategy="beforeInteractive"),
            _script(src="/later.js"),
        ),
    )

    html = render_document(
        settings=settings,
        page=page,
        body_html="<p>Hello</p>",
        props={},
        script_nonce="n",
        head_elements=(),
    )

    assert '<script src="/early.js"' in html
    assert '"src":"/later.js"' in html


# ---------------------------------------------------------------------------
# render_error_document — naming the file and line the author can act on
# ---------------------------------------------------------------------------


def _raise_from_file(path: Path, source: str) -> BaseException:
    """Run *source* as if it were *path*, and hand back what it raised.

    ``compile(..., str(path), "exec")`` is what makes this worth doing: the
    traceback frame carries that filename, so the helper produces the same shape
    of traceback a compiled ``.pyxl`` produces, and ``linecache`` reads the real
    file back off disk.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")
    try:
        exec(compile(source, str(path), "exec"), {"__name__": "pyxle.server.pages.x"})
    except BaseException as exc:  # noqa: BLE001 - handing the error back is the point
        return exc
    raise AssertionError("source was expected to raise")


def test_error_document_names_the_authors_file_line_and_source(
    page_route: PageRoute, tmp_path: Path
) -> None:
    """The overlay names the file, the line, and the line's text.

    This is the whole ticket: the terminal traceback always had this and the
    browser page dropped it, which hurts most for an import error, whose message
    names ``pyxle.server`` — a module the developer never typed.
    """
    page = tmp_path / "pages" / "relimp.pyxl"
    error = _raise_from_file(page, "from ._shared import GREETING\n")
    settings = DevServerSettings.from_project_root(tmp_path)  # debug=True

    html = render_error_document(settings=settings, page=page_route, error=error)

    assert "pages/relimp.pyxl, line 1" in html
    assert "from ._shared import GREETING" in html


def test_error_document_origin_is_relative_to_the_project(
    page_route: PageRoute, tmp_path: Path
) -> None:
    """The path shown is project-relative; an absolute path is noise."""
    page = tmp_path / "pages" / "deep" / "boom.pyxl"
    error = _raise_from_file(page, "value = undefined_name\n")
    settings = DevServerSettings.from_project_root(tmp_path)

    html = render_error_document(settings=settings, page=page_route, error=error)

    assert "pages/deep/boom.pyxl, line 1" in html
    assert str(tmp_path) not in html


def test_error_document_follows_the_exception_chain_to_the_authors_frame(
    page_route: PageRoute, tmp_path: Path
) -> None:
    """A wrapper exception must not hide the frame that names the mistake.

    ``LoaderCrashError`` is raised at a framework call site, so its own
    traceback holds no file the developer wrote; the frame that does hangs off
    the exception it wrapped.
    """
    page = tmp_path / "pages" / "loaderboom.pyxl"
    inner = _raise_from_file(page, "value = undefined_name_the_dev_typed\n")
    try:
        raise RuntimeError("Loader 'load' failed") from inner
    except RuntimeError as wrapper:
        error: BaseException = wrapper

    settings = DevServerSettings.from_project_root(tmp_path)
    html = render_error_document(settings=settings, page=page_route, error=error)

    assert "pages/loaderboom.pyxl, line 1" in html
    assert "undefined_name_the_dev_typed" in html


def test_error_document_ignores_generated_modules_in_the_build_directory(
    page_route: PageRoute, tmp_path: Path
) -> None:
    """A path inside the build directory names an artifact, not the author's
    file, and pointing at it sends the reader to edit a generated module."""
    settings = DevServerSettings.from_project_root(tmp_path)
    generated = settings.build_root / "server" / "pages" / "index.py"
    error = _raise_from_file(generated, "value = undefined_name\n")

    html = render_error_document(settings=settings, page=page_route, error=error)

    assert '<div class="pyxle-origin">' not in html
    assert "index.py" not in html


def test_error_document_without_any_user_frame_renders_normally(
    page_route: PageRoute, tmp_path: Path
) -> None:
    """A purely internal failure still renders — it simply has no origin to
    show, and must not gain an empty box or raise while rendering."""
    settings = DevServerSettings.from_project_root(tmp_path)
    error = RuntimeError("something internal went wrong")

    html = render_error_document(settings=settings, page=page_route, error=error)

    assert "Server Render Failed" in html
    assert '<div class="pyxle-origin">' not in html


def test_error_document_escapes_the_source_line(
    page_route: PageRoute, tmp_path: Path
) -> None:
    """The author's line is printed, so it is escaped like every other value.

    A ``.pyxl`` file legitimately contains JSX, so a line with ``<script>`` in
    it is ordinary source, not necessarily an attack — which is exactly why it
    must never be emitted raw.
    """
    page = tmp_path / "pages" / "xss.pyxl"
    error = _raise_from_file(page, "boom = '<script>alert(1)</script>' + missing\n")
    settings = DevServerSettings.from_project_root(tmp_path)

    html = render_error_document(settings=settings, page=page_route, error=error)

    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


def test_error_document_production_never_shows_the_origin(
    page_route: PageRoute, tmp_path: Path
) -> None:
    """Production must not gain a file path, a line number or a line of source.

    The overlay is a dev affordance; leaking the author's source tree to a
    visitor is precisely what CLAUDE.md rule 18 forbids.
    """
    page = tmp_path / "pages" / "relimp.pyxl"
    error = _raise_from_file(page, "from ._shared import GREETING\n")
    settings = replace(DevServerSettings.from_project_root(tmp_path), debug=False)

    html = render_error_document(settings=settings, page=page_route, error=error)

    assert "relimp.pyxl" not in html
    assert "GREETING" not in html
    assert '<div class="pyxle-origin">' not in html
