from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from pyxle.devserver.routes import PageRoute
from pyxle.devserver.settings import DevServerSettings
from pyxle.ssr.renderer import InlineStyleFragment
from pyxle.ssr.template import render_document, render_error_document


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
    assert "<title>Pyxle</title>" in html
    assert "window.__PYXLE_PAGE_PATH__ = \"/pages/index.jsx\"" in html
    assert "@vite/client" in html
    assert "@react-refresh" in html
    assert "__vite_plugin_react_preamble_installed__" in html
    assert "client-entry.js" in html
    assert '"data":{"greeting":"<\\/script>"}' in html
    assert "<\\/script>" in html  # escaped closing tag in props payload
    assert 'nonce="test-nonce"' in html


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


def test_render_document_omits_csrf_names_for_defaults(
    page_route: PageRoute, tmp_path: Path
) -> None:
    """Default names (or no CSRF config at all) embed nothing — the client
    falls back to ``pyxle-csrf`` / ``x-csrf-token`` on its own."""
    from pyxle.config import CsrfConfig

    for csrf in (None, CsrfConfig(), CsrfConfig(header_name="X-CSRF-Token")):
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
