"""Tests for @action routing, dispatch, and the ActionRoute descriptor."""

from __future__ import annotations

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


def test_import_module_reimports_when_debug(tmp_path: Path) -> None:
    """In dev mode (debug=True), _import_module always re-executes the module."""
    from pyxle.devserver.starlette_app import _import_module

    mod_path = tmp_path / "test_debug_reload.py"
    mod_path.write_text("COUNTER = 0\n", encoding="utf-8")
    key = "pyxle._test_debug_reload"

    first = _import_module(key, mod_path, debug=True)
    assert first.COUNTER == 0

    # Mutate state that would persist if the module were cached
    first.COUNTER = 42

    # In debug mode, the module must be re-imported (fresh exec), resetting state
    second = _import_module(key, mod_path, debug=True)
    assert second is not first
    assert second.COUNTER == 0  # Reset — module was re-executed

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
