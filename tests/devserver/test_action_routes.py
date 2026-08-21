"""Tests for @action routing, dispatch, and the ActionRoute descriptor."""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from textwrap import dedent

from starlette.testclient import TestClient

from pyxle.devserver.routes import ActionRoute, RouteTable, _action_routes
from pyxle.devserver.registry import PageRegistryEntry
from pyxle.devserver.starlette_app import build_action_router


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_page_entry(
    route_path: str,
    server_module_path: Path,
    actions: tuple[dict, ...] = (),
) -> PageRegistryEntry:
    """Build a minimal PageRegistryEntry for testing."""
    stub = Path("/stub/file.py")
    return PageRegistryEntry(
        route_path=route_path,
        alternate_route_paths=(),
        source_relative_path=Path("pages/index.pyxl"),
        source_absolute_path=stub,
        server_module_path=server_module_path,
        client_module_path=stub,
        metadata_path=stub,
        client_asset_path="/pages/index.jsx",
        server_asset_path="/pages/index.py",
        module_key="pyxle.server.pages.index",
        content_hash="abc123",
        loader_name=None,
        loader_line=None,
        head_elements=(),
        head_is_dynamic=False,
        actions=actions,
    )


def _write_module(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dedent(content).strip() + "\n", encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# _action_routes helper
# ---------------------------------------------------------------------------


def test_action_routes_empty_when_no_actions(tmp_path: Path) -> None:
    entry = _make_page_entry("/", tmp_path / "index.py")
    routes = _action_routes(entry)
    assert routes == []


def test_action_routes_single_action(tmp_path: Path) -> None:
    entry = _make_page_entry(
        "/settings",
        tmp_path / "settings.py",
        actions=({"name": "save_name", "line": 5},),
    )
    routes = _action_routes(entry)
    assert len(routes) == 1
    r = routes[0]
    assert isinstance(r, ActionRoute)
    assert r.path == "/api/__actions/settings/save_name"
    assert r.action_name == "save_name"
    assert r.page_path == "/settings"


def test_action_routes_root_page(tmp_path: Path) -> None:
    entry = _make_page_entry(
        "/",
        tmp_path / "index.py",
        actions=({"name": "submit", "line": 3},),
    )
    routes = _action_routes(entry)
    assert routes[0].path == "/api/__actions/index/submit"


def test_action_routes_multiple(tmp_path: Path) -> None:
    entry = _make_page_entry(
        "/dashboard",
        tmp_path / "dashboard.py",
        actions=(
            {"name": "update_profile", "line": 10},
            {"name": "delete_account", "line": 20},
        ),
    )
    routes = _action_routes(entry)
    paths = [r.path for r in routes]
    assert "/api/__actions/dashboard/update_profile" in paths
    assert "/api/__actions/dashboard/delete_account" in paths


def test_action_routes_skips_invalid_entries(tmp_path: Path) -> None:
    entry = _make_page_entry(
        "/test",
        tmp_path / "test.py",
        actions=(
            {"line": 5},            # missing name
            {"name": "", "line": 6}, # empty name
            "not-a-dict",           # type: ignore[arg-type]
        ),
    )
    routes = _action_routes(entry)
    assert routes == []


# ---------------------------------------------------------------------------
# RouteTable.find_action
# ---------------------------------------------------------------------------


def test_route_table_find_action(tmp_path: Path) -> None:
    action_route = ActionRoute(
        path="/api/__actions/index/save",
        page_path="/",
        action_name="save",
        server_module_path=tmp_path / "index.py",
        module_key="pyxle.server.pages.index",
    )
    table = RouteTable(pages=[], apis=[], actions=[action_route])
    assert table.find_action("/api/__actions/index/save") is action_route
    assert table.find_action("/api/__actions/index/other") is None


def test_page_route_has_actions_true(tmp_path: Path) -> None:
    from pyxle.devserver.routes import PageRoute

    route = PageRoute(
        path="/settings",
        source_relative_path=Path("pages/settings.pyxl"),
        source_absolute_path=tmp_path / "pages/settings.pyxl",
        server_module_path=tmp_path / "server.py",
        client_module_path=tmp_path / "client.jsx",
        metadata_path=tmp_path / "meta.json",
        module_key="pyxle.server.pages.settings",
        client_asset_path="/pages/settings.jsx",
        server_asset_path="/pages/settings.py",
        content_hash="abc",
        loader_name=None,
        loader_line=None,
        head_elements=(),
        head_is_dynamic=False,
        actions=({"name": "save", "line": 5},),
    )
    assert route.has_actions is True


def test_page_route_has_actions_false(tmp_path: Path) -> None:
    from pyxle.devserver.routes import PageRoute

    route = PageRoute(
        path="/about",
        source_relative_path=Path("pages/about.pyxl"),
        source_absolute_path=tmp_path / "pages/about.pyxl",
        server_module_path=tmp_path / "server.py",
        client_module_path=tmp_path / "client.jsx",
        metadata_path=tmp_path / "meta.json",
        module_key="pyxle.server.pages.about",
        client_asset_path="/pages/about.jsx",
        server_asset_path="/pages/about.py",
        content_hash="def",
        loader_name=None,
        loader_line=None,
        head_elements=(),
        head_is_dynamic=False,
    )
    assert route.has_actions is False


def test_page_registry_entry_has_actions(tmp_path: Path) -> None:
    entry = _make_page_entry(
        "/dashboard",
        tmp_path / "dashboard.py",
        actions=({"name": "save", "line": 3},),
    )
    assert entry.has_actions is True
    entry_no_actions = _make_page_entry("/home", tmp_path / "home.py")
    assert entry_no_actions.has_actions is False


# ---------------------------------------------------------------------------
# Action dispatch — HTTP-level tests
# ---------------------------------------------------------------------------


def test_action_dispatch_success(tmp_path: Path) -> None:
    module_path = _write_module(
        tmp_path / "server" / "pages" / "settings.py",
        """
        from pyxle.runtime import action

        @action
        async def save_name(request):
            body = await request.json()
            return {"saved": True, "name": body.get("name")}
        """,
    )

    route = ActionRoute(
        path="/api/__actions/settings/save_name",
        page_path="/settings",
        action_name="save_name",
        server_module_path=module_path,
        module_key="pyxle.server.pages.settings",
    )
    router = build_action_router([route])

    from starlette.applications import Starlette

    app = Starlette()
    app.router.routes.extend(router.routes)

    client = TestClient(app, raise_server_exceptions=False)
    response = client.post(
        "/api/__actions/settings/save_name",
        json={"name": "Alice"},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["ok"] is True
    assert data["saved"] is True
    assert data["name"] == "Alice"


def test_action_dispatch_action_error(tmp_path: Path) -> None:
    module_path = _write_module(
        tmp_path / "server" / "pages" / "form.py",
        """
        from pyxle.runtime import action, ActionError

        @action
        async def submit(request):
            raise ActionError("validation failed", status_code=422, data={"field": "email"})
        """,
    )

    route = ActionRoute(
        path="/api/__actions/form/submit",
        page_path="/form",
        action_name="submit",
        server_module_path=module_path,
        module_key="pyxle.server.pages.form",
    )
    router = build_action_router([route])

    from starlette.applications import Starlette

    app = Starlette()
    app.router.routes.extend(router.routes)

    client = TestClient(app, raise_server_exceptions=False)
    response = client.post("/api/__actions/form/submit", json={})
    assert response.status_code == 422
    data = response.json()
    assert data["ok"] is False
    assert data["error"] == "validation failed"
    assert data["data"] == {"field": "email"}


def test_action_dispatch_missing_action(tmp_path: Path) -> None:
    module_path = _write_module(
        tmp_path / "server" / "pages" / "empty.py",
        """
        # No actions defined here
        """,
    )

    route = ActionRoute(
        path="/api/__actions/empty/missing",
        page_path="/empty",
        action_name="missing",
        server_module_path=module_path,
        module_key="pyxle.server.pages.empty",
    )
    router = build_action_router([route])

    from starlette.applications import Starlette

    app = Starlette()
    app.router.routes.extend(router.routes)

    client = TestClient(app, raise_server_exceptions=False)
    response = client.post("/api/__actions/empty/missing", json={})
    assert response.status_code == 404
    assert response.json()["ok"] is False


def test_action_dispatch_untagged_function_rejected(tmp_path: Path) -> None:
    """A function without @action must not be callable as an action.

    Returns 404 (not 400) to prevent attribute-existence enumeration
    that could leak information about the module's internals (M-5).
    """
    module_path = _write_module(
        tmp_path / "server" / "pages" / "untagged.py",
        """
        async def save(request):
            return {"sneaky": True}
        """,
    )

    route = ActionRoute(
        path="/api/__actions/untagged/save",
        page_path="/untagged",
        action_name="save",
        server_module_path=module_path,
        module_key="pyxle.server.pages.untagged",
    )
    router = build_action_router([route])

    from starlette.applications import Starlette

    app = Starlette()
    app.router.routes.extend(router.routes)

    client = TestClient(app, raise_server_exceptions=False)
    response = client.post("/api/__actions/untagged/save", json={})
    assert response.status_code == 404
    assert response.json()["ok"] is False


def test_action_dispatch_non_dict_return(tmp_path: Path) -> None:
    module_path = _write_module(
        tmp_path / "server" / "pages" / "bad.py",
        """
        from pyxle.runtime import action

        @action
        async def bad_return(request):
            return "not a dict"
        """,
    )

    route = ActionRoute(
        path="/api/__actions/bad/bad_return",
        page_path="/bad",
        action_name="bad_return",
        server_module_path=module_path,
        module_key="pyxle.server.pages.bad",
    )
    router = build_action_router([route])

    from starlette.applications import Starlette

    app = Starlette()
    app.router.routes.extend(router.routes)

    client = TestClient(app, raise_server_exceptions=False)
    response = client.post("/api/__actions/bad/bad_return", json={})
    assert response.status_code == 500
    assert response.json()["ok"] is False


def test_action_dispatch_action_error_no_data(tmp_path: Path) -> None:
    """ActionError with no data must not include 'data' key in response."""
    module_path = _write_module(
        tmp_path / "server" / "pages" / "nodataerr.py",
        """
        from pyxle.runtime import action, ActionError

        @action
        async def fail(request):
            raise ActionError("just an error")
        """,
    )

    route = ActionRoute(
        path="/api/__actions/nodataerr/fail",
        page_path="/nodataerr",
        action_name="fail",
        server_module_path=module_path,
        module_key="pyxle.server.pages.nodataerr",
    )
    router = build_action_router([route])

    from starlette.applications import Starlette

    app = Starlette()
    app.router.routes.extend(router.routes)

    client = TestClient(app, raise_server_exceptions=False)
    response = client.post("/api/__actions/nodataerr/fail", json={})
    assert response.status_code == 400
    data = response.json()
    assert data["ok"] is False
    assert data["error"] == "just an error"
    assert "data" not in data


def test_action_dispatch_module_load_failure(tmp_path: Path) -> None:
    """When the server module cannot be loaded, return 500."""
    route = ActionRoute(
        path="/api/__actions/broken/save",
        page_path="/broken",
        action_name="save",
        server_module_path=tmp_path / "nonexistent.py",
        module_key="pyxle.server.pages.broken",
    )
    router = build_action_router([route])

    from starlette.applications import Starlette

    app = Starlette()
    app.router.routes.extend(router.routes)

    client = TestClient(app, raise_server_exceptions=False)
    response = client.post("/api/__actions/broken/save", json={})
    assert response.status_code == 500
    assert response.json()["ok"] is False


def test_import_module_does_not_cache_failed_imports(tmp_path: Path) -> None:
    """A module that fails to import must not be left in ``sys.modules``.

    Otherwise a later ``debug=False`` import of the same key returns the
    broken, half-initialised partial silently instead of re-raising — which
    in production would serve an empty module for every subsequent request.
    """
    from pyxle.devserver.starlette_app import ApiRouteError, _import_module

    module_key = "pyxle.server.pages.__import_failure_probe__"
    source = tmp_path / "probe.py"
    source.write_text("import a_module_that_does_not_exist\n", encoding="utf-8")
    assert module_key not in sys.modules

    for _ in range(2):
        # Each call must re-raise (not return a cached partial) and clean up.
        try:
            _import_module(module_key, source, debug=False)
        except ApiRouteError:
            pass
        else:  # pragma: no cover - defensive
            raise AssertionError("expected ApiRouteError")
        assert module_key not in sys.modules


def test_action_only_accepts_post(tmp_path: Path) -> None:
    module_path = _write_module(
        tmp_path / "server" / "pages" / "data.py",
        """
        from pyxle.runtime import action

        @action
        async def fetch(request):
            return {"ok": True}
        """,
    )

    route = ActionRoute(
        path="/api/__actions/data/fetch",
        page_path="/data",
        action_name="fetch",
        server_module_path=module_path,
        module_key="pyxle.server.pages.data",
    )
    router = build_action_router([route])

    from starlette.applications import Starlette

    app = Starlette()
    app.router.routes.extend(router.routes)

    client = TestClient(app, raise_server_exceptions=False)
    # GET should return 405
    response = client.get("/api/__actions/data/fetch")
    assert response.status_code == 405


# ---------------------------------------------------------------------------
# Catch-all action routes for pages with dynamic/catch-all parameters
# ---------------------------------------------------------------------------


def _make_page_entry_with_alternates(
    route_path: str,
    alternate_route_paths: tuple[str, ...],
    server_module_path: Path,
    actions: tuple[dict, ...] = (),
) -> PageRegistryEntry:
    """Build a PageRegistryEntry with alternate route paths for testing."""
    stub = Path("/stub/file.py")
    return PageRegistryEntry(
        route_path=route_path,
        alternate_route_paths=alternate_route_paths,
        source_relative_path=Path("pages/docs/[[...slug]].pyxl"),
        source_absolute_path=stub,
        server_module_path=server_module_path,
        client_module_path=stub,
        metadata_path=stub,
        client_asset_path="/pages/docs/[[...slug]].jsx",
        server_asset_path="/pages/docs/[[...slug]].py",
        module_key="pyxle.server.pages.docs.__slug__",
        content_hash="abc123",
        loader_name=None,
        loader_line=None,
        head_elements=(),
        head_is_dynamic=False,
        actions=actions,
    )


def test_action_routes_catchall_generated_for_dynamic_pages(
    tmp_path: Path,
) -> None:
    """Pages with parameterised alternate paths should generate a catch-all."""
    entry = _make_page_entry_with_alternates(
        route_path="/docs",
        alternate_route_paths=("/docs/{slug:path}",),
        server_module_path=tmp_path / "docs.py",
        actions=({"name": "search", "line": 5},),
    )
    routes = _action_routes(entry)
    assert len(routes) == 2

    specific = routes[0]
    assert specific.path == "/api/__actions/docs/search"
    assert specific.is_catchall is False
    assert specific.action_name == "search"

    catchall = routes[1]
    assert "{_pyxle_action_path:path}" in catchall.path
    assert catchall.is_catchall is True


def test_action_routes_no_catchall_for_static_pages(tmp_path: Path) -> None:
    """Pages without parameterised alternate paths should not get a catch-all."""
    entry = _make_page_entry(
        "/settings",
        tmp_path / "settings.py",
        actions=({"name": "save", "line": 5},),
    )
    routes = _action_routes(entry)
    assert len(routes) == 1
    assert routes[0].is_catchall is False


def test_catchall_action_dispatch_success(tmp_path: Path) -> None:
    """The catch-all handler must extract the action name from the last segment."""
    module_path = _write_module(
        tmp_path / "server" / "pages" / "docs.py",
        """
        from pyxle.runtime import action

        @action
        async def search_docs(request):
            body = await request.json()
            return {"results": [body.get("query")]}
        """,
    )

    routes = [
        ActionRoute(
            path="/api/__actions/docs/search_docs",
            page_path="/docs",
            action_name="search_docs",
            server_module_path=module_path,
            module_key="pyxle.server.pages.docs",
        ),
        ActionRoute(
            path="/api/__actions/docs/{_pyxle_action_path:path}",
            page_path="/docs",
            action_name="",
            server_module_path=module_path,
            module_key="pyxle.server.pages.docs",
            is_catchall=True,
        ),
    ]
    router = build_action_router(routes)

    from starlette.applications import Starlette

    app = Starlette()
    app.router.routes.extend(router.routes)

    client = TestClient(app, raise_server_exceptions=False)

    # Direct route still works.
    resp = client.post("/api/__actions/docs/search_docs", json={"query": "test"})
    assert resp.status_code == 200
    assert resp.json()["ok"] is True

    # Catch-all route — simulates client sending from /docs/getting-started.
    resp = client.post(
        "/api/__actions/docs/getting-started/installation/search_docs",
        json={"query": "routing"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert data["results"] == ["routing"]


def test_catchall_action_dispatch_missing_action(tmp_path: Path) -> None:
    """The catch-all handler must return 404 for non-existent actions."""
    module_path = _write_module(
        tmp_path / "server" / "pages" / "docs2.py",
        """
        from pyxle.runtime import action

        @action
        async def real_action(request):
            return {"ok": True}
        """,
    )

    route = ActionRoute(
        path="/api/__actions/docs/{_pyxle_action_path:path}",
        page_path="/docs",
        action_name="",
        server_module_path=module_path,
        module_key="pyxle.server.pages.docs2",
        is_catchall=True,
    )
    router = build_action_router([route])

    from starlette.applications import Starlette

    app = Starlette()
    app.router.routes.extend(router.routes)

    client = TestClient(app, raise_server_exceptions=False)
    resp = client.post(
        "/api/__actions/docs/some/path/nonexistent_action", json={},
    )
    assert resp.status_code == 404
    assert resp.json()["ok"] is False


def test_catchall_action_dispatch_untagged_rejected(tmp_path: Path) -> None:
    """The catch-all handler must reject functions without @action (M-5: 404)."""
    module_path = _write_module(
        tmp_path / "server" / "pages" / "docs3.py",
        """
        async def not_an_action(request):
            return {"sneaky": True}
        """,
    )

    route = ActionRoute(
        path="/api/__actions/docs/{_pyxle_action_path:path}",
        page_path="/docs",
        action_name="",
        server_module_path=module_path,
        module_key="pyxle.server.pages.docs3",
        is_catchall=True,
    )
    router = build_action_router([route])

    from starlette.applications import Starlette

    app = Starlette()
    app.router.routes.extend(router.routes)

    client = TestClient(app, raise_server_exceptions=False)
    resp = client.post(
        "/api/__actions/docs/slug/not_an_action", json={},
    )
    assert resp.status_code == 404
    assert resp.json()["ok"] is False


# ---------------------------------------------------------------------------
# Module caching: production vs debug
# ---------------------------------------------------------------------------


def test_import_module_caches_when_not_debug(tmp_path: Path) -> None:
    """In production (debug=False), _import_module returns the cached module."""
    from pyxle.devserver.starlette_app import _import_module

    mod_path = tmp_path / "test_prod_cache.py"
    mod_path.write_text("COUNTER = 1\n", encoding="utf-8")
    key = "pyxle._test_prod_cache"

    first = _import_module(key, mod_path, debug=False)
    assert first.COUNTER == 1
    first.COUNTER = 99

    second = _import_module(key, mod_path, debug=False)
    assert second is first
    assert second.COUNTER == 99  # State preserved

    sys.modules.pop(key, None)


def test_import_module_persists_globals_until_rebuild(tmp_path: Path) -> None:
    """In dev (debug=True), _import_module reuses the module across calls so
    module-level globals persist across requests exactly like production, and
    re-executes only after a rebuild advances the reload generation."""
    from pyxle.devserver.starlette_app import _import_module
    from pyxle.ssr import module_cache

    mod_path = tmp_path / "test_debug_reload.py"
    mod_path.write_text("COUNTER = 0\n", encoding="utf-8")
    key = "pyxle._test_debug_reload"

    try:
        first = _import_module(key, mod_path, debug=True)
        assert first.COUNTER == 0
        first.COUNTER = 42

        # No rebuild between requests: same module, mutated global persists.
        second = _import_module(key, mod_path, debug=True)
        assert second is first
        assert second.COUNTER == 42

        # A rebuild advances the generation → the module re-executes, resetting.
        module_cache.mark_rebuild()
        third = _import_module(key, mod_path, debug=True)
        assert third is not first
        assert third.COUNTER == 0
    finally:
        sys.modules.pop(key, None)


# ---------------------------------------------------------------------------
# Progressive enhancement: action dispatch must accept form-encoded bodies
# from no-JS ``<Form>`` POSTs and expose them transparently to user code
# that does ``await request.json()``.
# ---------------------------------------------------------------------------


def _build_greet_app(tmp_path: Path) -> "TestClient":
    """Helper: spin up an app with a single greet @action."""
    module_path = _write_module(
        tmp_path / "server" / "pages" / "actions.py",
        """
        from pyxle.runtime import action, ActionError

        @action
        async def greet(request):
            body = await request.json()
            name = (body.get("name") or "").strip()
            if not name:
                raise ActionError("Please enter your name.", status_code=400)
            return {"greeting": f"Hello, {name}!"}
        """,
    )
    route = ActionRoute(
        path="/api/__actions/actions/greet",
        page_path="/actions",
        action_name="greet",
        server_module_path=module_path,
        module_key="pyxle.server.pages.actions",
    )
    router = build_action_router([route])

    from starlette.applications import Starlette

    app = Starlette()
    app.router.routes.extend(router.routes)
    return TestClient(app, raise_server_exceptions=False)


def test_action_accepts_form_encoded_body(tmp_path: Path) -> None:
    """No-JS ``<Form>`` POST → form-urlencoded body. The dispatch shim
    makes ``request.json()`` return the parsed fields as a dict."""
    client = _build_greet_app(tmp_path)
    response = client.post(
        "/api/__actions/actions/greet",
        data={"name": "Shivam"},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["greeting"] == "Hello, Shivam!"


def test_action_form_body_strips_synthetic_csrf_field(tmp_path: Path) -> None:
    """The ``_csrf_token`` form field exists for the middleware. It must
    not leak into ``request.json()`` so user code's ``body['name']``
    stays clean."""
    module_path = _write_module(
        tmp_path / "server" / "pages" / "echo.py",
        """
        from pyxle.runtime import action

        @action
        async def echo(request):
            body = await request.json()
            return {"keys": sorted(body.keys()), "echo": body}
        """,
    )
    route = ActionRoute(
        path="/api/__actions/echo/echo",
        page_path="/echo",
        action_name="echo",
        server_module_path=module_path,
        module_key="pyxle.server.pages.echo",
    )
    router = build_action_router([route])

    from starlette.applications import Starlette

    app = Starlette()
    app.router.routes.extend(router.routes)
    client = TestClient(app, raise_server_exceptions=False)

    response = client.post(
        "/api/__actions/echo/echo",
        data={"_csrf_token": "ignored-by-shim", "name": "Shivam", "tier": "pro"},
    )
    assert response.status_code == 200
    payload = response.json()
    # No _csrf_token leakage: user's body is exactly what they sent.
    assert payload["keys"] == ["name", "tier"]
    assert payload["echo"] == {"name": "Shivam", "tier": "pro"}


def test_action_form_body_empty_field_surfaces_action_error(tmp_path: Path) -> None:
    """An empty form submission still flows through the @action so the
    server-side ``ActionError`` reaches the client — the kit demo's
    "Submit empty to see the server validation error" pattern."""
    client = _build_greet_app(tmp_path)
    response = client.post("/api/__actions/actions/greet", data={"name": ""})
    assert response.status_code == 400
    payload = response.json()
    assert payload["ok"] is False
    assert payload["error"] == "Please enter your name."


def test_action_json_body_path_unchanged(tmp_path: Path) -> None:
    """JSON callers (``useAction`` / fetch) must not be touched by the
    shim — same content type, same parsing path as before."""
    client = _build_greet_app(tmp_path)
    response = client.post(
        "/api/__actions/actions/greet",
        json={"name": "Shivam"},
    )
    assert response.status_code == 200
    assert response.json()["greeting"] == "Hello, Shivam!"


def test_action_multipart_body(tmp_path: Path) -> None:
    """Multipart form bodies (file inputs) also get the JSON shim."""
    client = _build_greet_app(tmp_path)
    response = client.post(
        "/api/__actions/actions/greet",
        files={
            "name": (None, "Shivam"),
        },
    )
    assert response.status_code == 200
    assert response.json()["greeting"] == "Hello, Shivam!"


def test_action_form_body_collapses_repeated_fields(tmp_path: Path) -> None:
    """Repeated field names (checkbox groups, multi-select) come through
    as a list — single-value fields stay as plain strings, matching what
    JSON callers would naturally send."""
    module_path = _write_module(
        tmp_path / "server" / "pages" / "tags.py",
        """
        from pyxle.runtime import action

        @action
        async def add(request):
            body = await request.json()
            return {"name": body["name"], "tags": body.get("tag")}
        """,
    )
    route = ActionRoute(
        path="/api/__actions/tags/add",
        page_path="/tags",
        action_name="add",
        server_module_path=module_path,
        module_key="pyxle.server.pages.tags",
    )
    router = build_action_router([route])

    from starlette.applications import Starlette

    app = Starlette()
    app.router.routes.extend(router.routes)
    client = TestClient(app, raise_server_exceptions=False)

    response = client.post(
        "/api/__actions/tags/add",
        content="name=Shivam&tag=python&tag=react",
        headers={"content-type": "application/x-www-form-urlencoded"},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["name"] == "Shivam"
    assert payload["tags"] == ["python", "react"]


# ---------------------------------------------------------------------------
# Pydantic body validation (Phase 2.6)
# ---------------------------------------------------------------------------


def _action_client(tmp_path: Path, page: str, action_name: str, src: str):
    """Compile a server module and return (TestClient, action_url)."""
    from starlette.applications import Starlette

    module_path = _write_module(tmp_path / "server" / "pages" / f"{page}.py", src)
    route = ActionRoute(
        path=f"/api/__actions/{page}/{action_name}",
        page_path=f"/{page}",
        action_name=action_name,
        server_module_path=module_path,
        module_key=f"pyxle.server.pages.{page}",
    )
    router = build_action_router([route])
    app = Starlette()
    app.router.routes.extend(router.routes)
    return TestClient(app, raise_server_exceptions=False), route.path


_SIGNUP_MODULE = """
from pyxle.runtime import action
from pydantic import BaseModel, Field

class Address(BaseModel):
    zip: str

class Signup(BaseModel):
    email: str
    age: int = Field(gt=0)
    address: Address
    tags: list[str]

@action
async def register(request, body: Signup):
    return {"email": body.email, "age": body.age, "zip": body.address.zip}
"""


def test_action_body_validation_success(tmp_path: Path) -> None:
    client, url = _action_client(tmp_path, "signup_ok", "register", _SIGNUP_MODULE)
    response = client.post(
        url,
        json={"email": "a@b.c", "age": 30, "address": {"zip": "12345"}, "tags": ["x"]},
    )
    assert response.status_code == 200
    data = response.json()
    assert data == {"ok": True, "email": "a@b.c", "age": 30, "zip": "12345"}


def test_action_body_validation_422_with_field_paths(tmp_path: Path) -> None:
    client, url = _action_client(tmp_path, "signup_bad", "register", _SIGNUP_MODULE)
    response = client.post(
        url, json={"age": 0, "address": {}, "tags": [123]}
    )  # missing email, age<=0, missing zip, tag not a string
    assert response.status_code == 422
    data = response.json()
    assert data["ok"] is False
    assert data["error"] == "Validation failed"
    fields = data["fields"]
    assert "email" in fields  # top-level missing
    assert "address.zip" in fields  # nested path
    assert "tags.0" in fields  # list index path
    assert any("greater than 0" in m for m in fields["age"])


def test_action_body_validation_form_encoded(tmp_path: Path) -> None:
    # A no-JS <Form> POST (form-encoded) validates transparently via the shim.
    client, url = _action_client(
        tmp_path,
        "form_signup",
        "save",
        """
        from pyxle.runtime import action
        from pydantic import BaseModel

        class NameBody(BaseModel):
            name: str

        @action
        async def save(request, body: NameBody):
            return {"name": body.name}
        """,
    )
    response = client.post(url, data={"name": "Bob"})
    assert response.status_code == 200
    assert response.json()["name"] == "Bob"


def test_action_optional_body_accepts_empty(tmp_path: Path) -> None:
    client, url = _action_client(
        tmp_path,
        "opt",
        "save",
        """
        from pyxle.runtime import action
        from pydantic import BaseModel
        from typing import Optional

        class Filter(BaseModel):
            q: str = ""

        @action
        async def save(request, body: Optional[Filter] = None):
            return {"q": body.q if body else "none"}
        """,
    )
    # Empty object validates into a default Filter.
    assert client.post(url, json={}).json()["q"] == ""


def test_action_non_json_body_is_422(tmp_path: Path) -> None:
    client, url = _action_client(tmp_path, "njson", "register", _SIGNUP_MODULE)
    response = client.post(
        url, content=b"not json", headers={"content-type": "application/json"}
    )
    assert response.status_code == 422
    assert "__root__" in response.json()["fields"]


def test_action_legacy_no_body_param_unaffected(tmp_path: Path) -> None:
    # Regression: an action without a body parameter is dispatched with just
    # `request`, exactly as before.
    client, url = _action_client(
        tmp_path,
        "legacy",
        "save",
        """
        from pyxle.runtime import action

        @action
        async def save(request):
            body = await request.json()
            return {"echo": body.get("v")}
        """,
    )
    assert client.post(url, json={"v": 42}).json()["echo"] == 42


def test_action_user_raised_validation_error_emits_fields(tmp_path: Path) -> None:
    client, url = _action_client(
        tmp_path,
        "manual",
        "save",
        """
        from pyxle.runtime import action, ValidationActionError

        @action
        async def save(request):
            raise ValidationActionError(fields={"email": ["already taken"]})
        """,
    )
    response = client.post(url, json={})
    assert response.status_code == 422
    data = response.json()
    assert data["error"] == "Validation failed"
    assert data["fields"] == {"email": ["already taken"]}


def test_action_pydantic_not_installed_is_500(tmp_path: Path, monkeypatch) -> None:
    # If a validated action is dispatched but pydantic can't resolve the model,
    # the server returns 500 (with install guidance in debug) — never crashes.
    from pyxle.devserver import validation

    monkeypatch.setattr(validation, "_try_import_pydantic", lambda: None)
    validation._RESOLVE_CACHE.clear()
    client, url = _action_client(tmp_path, "nopyd", "register", _SIGNUP_MODULE)
    # The module itself imports pydantic at top, so import would fail first in a
    # truly pydantic-less env; here we simulate resolution-time absence. Either
    # way the response is a clean 500, not a crash.
    response = client.post(url, json={})
    assert response.status_code == 500
    assert response.json()["ok"] is False


# ---------------------------------------------------------------------------
# Background tasks (request.state.background + {"background": [...]} shorthand)


def test_action_state_background_runs_after_response(tmp_path: Path) -> None:
    marker = tmp_path / "bg_state.txt"
    src = (
        "from pyxle.runtime import action\n\n"
        "def _write():\n"
        f"    open({str(marker)!r}, 'w').write('ran')\n\n"
        "@action\n"
        "async def go(request):\n"
        "    request.state.background.add_task(_write)\n"
        "    return {'ok': True}\n"
    )
    client, url = _action_client(tmp_path, "bgstate", "go", src)
    assert not marker.exists()
    response = client.post(url, json={})
    assert response.status_code == 200
    # The TestClient runs the response's background tasks before returning.
    assert marker.read_text() == "ran"


def test_action_background_survives_an_action_error(tmp_path: Path) -> None:
    """Work scheduled *before* the action refused still runs.

    ``add_task`` is a statement that executed; a later ``raise ActionError`` no
    more undoes it than it rolls back a database write on the line above. This
    is the audit-record case — "record the attempt whether or not it succeeds" —
    and dropping it would lose it silently, with a 4xx and nothing in the log.
    Cookies already behave this way on the same path.
    """
    marker = tmp_path / "bg_on_error.txt"
    src = (
        "from pyxle.runtime import action, ActionError\n\n"
        "def _write():\n"
        f"    open({str(marker)!r}, 'w').write('audited')\n\n"
        "@action\n"
        "async def go(request):\n"
        "    request.state.background.add_task(_write)\n"
        "    raise ActionError('Insufficient funds', status_code=400)\n"
    )
    client, url = _action_client(tmp_path, "bgerror", "go", src)
    assert not marker.exists()
    response = client.post(url, json={})

    assert response.status_code == 400
    assert response.json()["ok"] is False
    assert marker.read_text() == "audited"


def test_action_background_not_scheduled_before_the_error_does_not_run(
    tmp_path: Path,
) -> None:
    """The other half of the rule: ordering is the author's control.

    Work scheduled *after* the check never gets scheduled at all — this is the
    welcome-email case, and it is why the rule needs no opt-out flag.
    """
    marker = tmp_path / "bg_after_raise.txt"
    src = (
        "from pyxle.runtime import action, ActionError\n\n"
        "def _write():\n"
        f"    open({str(marker)!r}, 'w').write('sent')\n\n"
        "@action\n"
        "async def go(request):\n"
        "    raise ActionError('Payment declined', status_code=402)\n"
        "    request.state.background.add_task(_write)\n"
    )
    client, url = _action_client(tmp_path, "bgafter", "go", src)
    response = client.post(url, json={})

    assert response.status_code == 402
    assert not marker.exists()


def test_action_background_dropped_when_the_action_crashes(tmp_path: Path) -> None:
    """An unhandled exception is not a refusal — it is a bug, and the action's
    intent is unknown, so nothing it staged is applied. This matches how cookies
    already behave on the 500 path and keeps the two consistent."""
    marker = tmp_path / "bg_on_crash.txt"
    src = (
        "from pyxle.runtime import action\n\n"
        "def _write():\n"
        f"    open({str(marker)!r}, 'w').write('ran')\n\n"
        "@action\n"
        "async def go(request):\n"
        "    request.state.background.add_task(_write)\n"
        "    raise RuntimeError('boom')\n"
    )
    client, url = _action_client(tmp_path, "bgcrash", "go", src)
    response = client.post(url, json={})

    assert response.status_code == 500
    assert not marker.exists()


def test_action_background_shorthand_runs_and_is_stripped(tmp_path: Path) -> None:
    marker = tmp_path / "bg_short.txt"
    src = (
        "from pyxle.runtime import action\n\n"
        "def _write(content):\n"
        f"    open({str(marker)!r}, 'w').write(content)\n\n"
        "@action\n"
        "async def go(request):\n"
        "    return {'ok': True, 'background': [_write, 'shorthand']}\n"
    )
    client, url = _action_client(tmp_path, "bgshort", "go", src)
    response = client.post(url, json={})
    assert response.status_code == 200
    # The 'background' key is stripped from the response body.
    assert "background" not in response.json()
    assert marker.read_text() == "shorthand"


def test_action_background_malformed_returns_500(tmp_path: Path) -> None:
    src = (
        "from pyxle.runtime import action\n\n"
        "@action\n"
        "async def go(request):\n"
        "    return {'ok': True, 'background': 'not-a-list'}\n"
    )
    client, url = _action_client(tmp_path, "bgmal", "go", src)
    response = client.post(url, json={})
    assert response.status_code == 500
    assert response.json()["ok"] is False


def test_action_background_non_callable_first_returns_500(tmp_path: Path) -> None:
    src = (
        "from pyxle.runtime import action\n\n"
        "@action\n"
        "async def go(request):\n"
        "    return {'ok': True, 'background': ['not-callable', 1]}\n"
    )
    client, url = _action_client(tmp_path, "bgnc", "go", src)
    response = client.post(url, json={})
    assert response.status_code == 500


# ---------------------------------------------------------------------------
# Route policies / hooks on @action calls (auth hooks must fire for actions)


def test_action_route_hook_can_deny_the_call(tmp_path: Path) -> None:
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse

    module_path = _write_module(
        tmp_path / "server" / "pages" / "guarded.py",
        "from pyxle.runtime import action\n\n"
        "@action\nasync def go(request):\n    return {'ok': True}\n",
    )
    route = ActionRoute(
        path="/api/__actions/guarded/go",
        page_path="/guarded",
        action_name="go",
        server_module_path=module_path,
        module_key="pyxle.server.pages.guarded",
    )

    async def deny_hook(context, request, call_next):
        return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)

    router = build_action_router([route], route_hooks=[deny_hook])
    app = Starlette()
    app.router.routes.extend(router.routes)
    client = TestClient(app, raise_server_exceptions=False)

    response = client.post("/api/__actions/guarded/go", json={})
    # The hook short-circuited the action — the @action never ran.
    assert response.status_code == 401
    assert response.json()["error"] == "unauthorized"


def test_action_route_hook_receives_action_context(tmp_path: Path) -> None:
    from starlette.applications import Starlette

    module_path = _write_module(
        tmp_path / "server" / "pages" / "ctx.py",
        "from pyxle.runtime import action\n\n"
        "@action\nasync def go(request):\n    return {'ok': True}\n",
    )
    route = ActionRoute(
        path="/api/__actions/ctx/go",
        page_path="/ctx",
        action_name="go",
        server_module_path=module_path,
        module_key="pyxle.server.pages.ctx",
    )
    captured: dict = {}

    async def capture_hook(context, request, call_next):
        captured["target"] = context.target
        captured["path"] = context.path
        return await call_next(request)

    router = build_action_router([route], route_hooks=[capture_hook])
    app = Starlette()
    app.router.routes.extend(router.routes)
    client = TestClient(app, raise_server_exceptions=False)

    response = client.post("/api/__actions/ctx/go", json={})
    assert response.status_code == 200
    assert captured["target"] == "action"
    assert captured["path"] == "/api/__actions/ctx/go"


def test_action_router_without_hooks_still_dispatches(tmp_path: Path) -> None:
    # The no-hooks path (default) must keep working unchanged.
    from starlette.applications import Starlette

    module_path = _write_module(
        tmp_path / "server" / "pages" / "nohooks_dispatch.py",
        "from pyxle.runtime import action\n\n"
        "@action\nasync def go(request):\n    return {'value': 1}\n",
    )
    route = ActionRoute(
        path="/api/__actions/nohooks_dispatch/go",
        page_path="/nohooks_dispatch",
        action_name="go",
        server_module_path=module_path,
        module_key="pyxle.server.pages.nohooks_dispatch",
    )
    router = build_action_router([route])
    app = Starlette()
    app.router.routes.extend(router.routes)
    client = TestClient(app, raise_server_exceptions=False)

    response = client.post("/api/__actions/nohooks_dispatch/go", json={})
    assert response.status_code == 200
    assert response.json() == {"ok": True, "value": 1}


# ---------------------------------------------------------------------------
# Missing request.state attribute guidance
# ---------------------------------------------------------------------------


def _state_action_app(module_path: Path, *, debug: bool) -> TestClient:
    route = ActionRoute(
        path="/api/__actions/state/save",
        page_path="/state",
        action_name="save",
        server_module_path=module_path,
        module_key=f"pyxle.server.pages.state_{'dev' if debug else 'prod'}",
    )
    router = build_action_router([route], debug=debug)

    from starlette.applications import Starlette

    app = Starlette()
    app.router.routes.extend(router.routes)
    return TestClient(app, raise_server_exceptions=False)


def test_action_dispatch_missing_state_guidance_in_debug(tmp_path: Path) -> None:
    """request.state.db without the plugin → actionable guidance, not a bare
    "'State' object has no attribute 'db'"."""
    module_path = _write_module(
        tmp_path / "server" / "pages" / "state_dev.py",
        """
        from pyxle.runtime import action

        @action
        async def save(request):
            request.state.db.execute("SELECT 1")
            return {}
        """,
    )

    client = _state_action_app(module_path, debug=True)
    response = client.post("/api/__actions/state/save", json={})

    assert response.status_code == 500
    data = response.json()
    assert data["ok"] is False
    assert "request.state.db is not set" in data["error"]
    assert "pyxle-db" in data["error"]


def test_action_dispatch_missing_state_generic_in_production(tmp_path: Path) -> None:
    """Production responses never leak the guidance (or any internals)."""
    module_path = _write_module(
        tmp_path / "server" / "pages" / "state_prod.py",
        """
        from pyxle.runtime import action

        @action
        async def save(request):
            request.state.db.execute("SELECT 1")
            return {}
        """,
    )

    client = _state_action_app(module_path, debug=False)
    response = client.post("/api/__actions/state/save", json={})

    assert response.status_code == 500
    data = response.json()
    assert data["ok"] is False
    assert data["error"] == "An unexpected error occurred."


def test_action_dispatch_other_attribute_error_flows_through(tmp_path: Path) -> None:
    """A non-State AttributeError keeps the existing generic error path."""
    module_path = _write_module(
        tmp_path / "server" / "pages" / "attr_boom.py",
        """
        from pyxle.runtime import action

        @action
        async def save(request):
            raise AttributeError("boom")
        """,
    )

    route = ActionRoute(
        path="/api/__actions/attr-boom/save",
        page_path="/attr-boom",
        action_name="save",
        server_module_path=module_path,
        module_key="pyxle.server.pages.attr_boom",
    )
    router = build_action_router([route], debug=True)

    from starlette.applications import Starlette

    app = Starlette()
    app.router.routes.extend(router.routes)
    client = TestClient(app, raise_server_exceptions=False)

    response = client.post("/api/__actions/attr-boom/save", json={})

    assert response.status_code == 500
    data = response.json()
    assert data["ok"] is False
    assert data["error"] == "boom"


class TestAnActionCanSetACookie:
    """An action returns a dict and never sees the response it becomes.

    Anything that had to set a cookie — a session, a preference, a consent
    record — therefore had to be rewritten as an API route, splitting a page's
    own mutations across two places for the sake of one header. The dispatcher
    hands the action a recorder instead.
    """

    @staticmethod
    def _app(tmp_path: Path, body: str, *, page: str, name: str = "choose"):
        from starlette.applications import Starlette

        # A distinct module per test: imported modules are cached by key, so a
        # shared one would quietly serve the first test's code to all of them.
        module_path = _write_module(tmp_path / "server" / "pages" / f"{page}.py", body)
        route = ActionRoute(
            path=f"/api/__actions/{page}/{name}",
            page_path=f"/{page}",
            action_name=name,
            server_module_path=module_path,
            module_key=f"pyxle.server.pages.{page}",
        )
        app = Starlette()
        app.router.routes.extend(build_action_router([route]).routes)
        return TestClient(app, raise_server_exceptions=False)

    def test_it_lands_on_the_response(self, tmp_path: Path) -> None:
        client = self._app(tmp_path, page="prefs_set", body="""
        from pyxle.runtime import action

        @action
        async def choose(request):
            request.state.cookies.set(
                "theme", "dark", max_age=31536000, httponly=True, samesite="lax"
            )
            return {"theme": "dark"}
        """)

        response = client.post("/api/__actions/prefs_set/choose", json={})

        assert response.json() == {"ok": True, "theme": "dark"}
        header = response.headers["set-cookie"]
        assert "theme=dark" in header
        assert "Max-Age=31536000" in header
        assert "HttpOnly" in header
        assert "SameSite=lax" in header

    def test_deleting_one_works_the_same_way(self, tmp_path: Path) -> None:
        client = self._app(tmp_path, page="prefs_delete", body="""
        from pyxle.runtime import action

        @action
        async def choose(request):
            request.state.cookies.delete("session")
            return {"signedOut": True}
        """)

        response = client.post("/api/__actions/prefs_delete/choose", json={})

        assert 'session=""' in response.headers["set-cookie"]

    def test_a_refusal_still_carries_it(self, tmp_path: Path) -> None:
        """A failed attempt is the action's own answer, and may need to record
        something on the way out — a counter, or a cleared session."""
        client = self._app(tmp_path, page="prefs_refuse", body="""
        from pyxle.runtime import action, ActionError

        @action
        async def choose(request):
            request.state.cookies.set("attempts", "3")
            raise ActionError("Wrong password.", status_code=403)
        """)

        response = client.post("/api/__actions/prefs_refuse/choose", json={})

        assert response.status_code == 403
        assert "attempts=3" in response.headers["set-cookie"]

    def test_an_action_that_sets_none_gets_no_header(self, tmp_path: Path) -> None:
        client = self._app(tmp_path, page="prefs_none", body="""
        from pyxle.runtime import action

        @action
        async def choose(request):
            return {"ok": True}
        """)

        response = client.post("/api/__actions/prefs_none/choose", json={})

        assert "set-cookie" not in response.headers

    def test_two_cookies_both_arrive(self, tmp_path: Path) -> None:
        client = self._app(tmp_path, page="prefs_two", body="""
        from pyxle.runtime import action

        @action
        async def choose(request):
            request.state.cookies.set("a", "1")
            request.state.cookies.set("b", "2")
            return {}
        """)

        response = client.post("/api/__actions/prefs_two/choose", json={})

        headers = response.headers.get_list("set-cookie")
        assert [h.split(";")[0] for h in headers] == ["a=1", "b=2"]


# ---------------------------------------------------------------------------
# A failed action is written to the server log
# ---------------------------------------------------------------------------


class TestAFailedActionIsLogged:
    """In production the caller is told nothing, so the log is the only record.

    Each test asserts the record exists **and** that there is exactly one of
    it: the dispatcher wraps some exceptions before answering, and logging on
    both sides of that wrap would report a single crash twice.
    """

    ACTION_LOGGER = "pyxle.devserver.starlette_app"

    def _app(self, tmp_path: Path, *, page: str, body: str, debug: bool = False) -> TestClient:
        module_path = _write_module(tmp_path / "server" / "pages" / f"{page}.py", body)
        route = ActionRoute(
            path=f"/api/__actions/{page}/run",
            page_path=f"/{page}",
            action_name="run",
            server_module_path=module_path,
            module_key=f"pyxle.server.pages.{page}",
        )
        router = build_action_router([route], debug=debug)

        from starlette.applications import Starlette

        app = Starlette()
        app.router.routes.extend(router.routes)
        return TestClient(app, raise_server_exceptions=False)

    @staticmethod
    def _failures(caplog) -> list:
        return [r for r in caplog.records if r.levelno >= logging.ERROR]

    def test_a_crashing_action_writes_one_traceback(self, tmp_path: Path, caplog) -> None:
        """The regression P0-13 was filed for: the response is sanitized and
        the log was empty, leaving the crash with no record anywhere."""
        client = self._app(tmp_path, page="log_crash", body="""
        from pyxle.runtime import action

        @action
        async def run(request):
            raise ValueError("boom from action")
        """)

        with caplog.at_level(logging.ERROR, logger=self.ACTION_LOGGER):
            response = client.post("/api/__actions/log_crash/run", json={})

        assert response.status_code == 500
        assert response.json()["error"] == "An unexpected error occurred."
        assert "boom from action" not in response.text

        failures = self._failures(caplog)
        assert len(failures) == 1
        record = failures[0]
        assert record.exc_info is not None
        assert isinstance(record.exc_info[1], ValueError)
        # The traceback names the action and the module, so a developer
        # reading only the log knows which one of their actions crashed.
        assert "run" in record.getMessage()
        assert "log_crash" in record.getMessage()

    def test_a_wrapped_failure_is_still_logged_only_once(
        self, tmp_path: Path, caplog
    ) -> None:
        """An unset ``request.state`` attribute is re-raised as guidance. The
        wrap chains the original, so one record carries both -- and the branch
        that builds the guidance must not log on top of the shared path."""
        client = self._app(tmp_path, page="log_state", body="""
        from pyxle.runtime import action

        @action
        async def run(request):
            request.state.db.execute("SELECT 1")
            return {}
        """)

        with caplog.at_level(logging.ERROR, logger=self.ACTION_LOGGER):
            response = client.post("/api/__actions/log_state/run", json={})

        assert response.status_code == 500
        failures = self._failures(caplog)
        assert len(failures) == 1
        logged = failures[0].exc_info[1]
        assert "db" in str(logged)
        # The bare AttributeError survives as the cause, so the traceback
        # still points at the line that read the attribute.
        assert isinstance(logged.__cause__, AttributeError)

    def test_an_action_returning_a_non_dict_names_itself(
        self, tmp_path: Path, caplog
    ) -> None:
        """The reply is the same sentence for every action that breaks the
        contract, so only the log says which one did."""
        client = self._app(tmp_path, page="log_nondict", body="""
        from pyxle.runtime import action

        @action
        async def run(request):
            return ["not", "a", "dict"]
        """)

        with caplog.at_level(logging.ERROR, logger=self.ACTION_LOGGER):
            response = client.post("/api/__actions/log_nondict/run", json={})

        assert response.status_code == 500
        failures = self._failures(caplog)
        assert len(failures) == 1
        message = failures[0].getMessage()
        assert "log_nondict" in message
        assert "list" in message

    def test_an_action_error_is_not_a_server_fault(self, tmp_path: Path, caplog) -> None:
        """A deliberate refusal is the action's own answer to its caller. It
        must not fill the error log -- that is what makes a real crash findable."""
        client = self._app(tmp_path, page="log_refusal", body="""
        from pyxle.runtime import action, ActionError

        @action
        async def run(request):
            raise ActionError("Not authorised", status_code=403)
        """)

        with caplog.at_level(logging.ERROR, logger=self.ACTION_LOGGER):
            response = client.post("/api/__actions/log_refusal/run", json={})

        assert response.status_code == 403
        assert self._failures(caplog) == []

    def test_development_logs_the_crash_it_also_shows(self, tmp_path: Path, caplog) -> None:
        """``pyxle dev`` returns the real message; the log is written all the
        same, so the two environments differ only in what the caller sees."""
        client = self._app(tmp_path, page="log_dev", body="""
        from pyxle.runtime import action

        @action
        async def run(request):
            raise ValueError("boom in dev")
        """, debug=True)

        with caplog.at_level(logging.ERROR, logger=self.ACTION_LOGGER):
            response = client.post("/api/__actions/log_dev/run", json={})

        assert response.status_code == 500
        assert response.json()["error"] == "boom in dev"
        assert len(self._failures(caplog)) == 1


def test_action_and_page_share_one_production_wording() -> None:
    """A crashing action and a crashing loader are the same class of failure,
    so a developer designing for one message is designing for both. These two
    constants drifting apart is what made the action wording undocumented."""
    from pyxle.devserver.starlette_app import _PRODUCTION_ACTION_ERROR

    boundary_message = (
        Path(__file__).resolve().parents[2] / "pyxle" / "ssr" / "view.py"
    ).read_text(encoding="utf-8")

    assert _PRODUCTION_ACTION_ERROR == "An unexpected error occurred."
    assert f'"message"] = "{_PRODUCTION_ACTION_ERROR}"' in boundary_message
