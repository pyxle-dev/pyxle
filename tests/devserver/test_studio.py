"""Tests for Pyxle Studio — the dev-only dashboard at ``/__pyxle/studio``."""

from __future__ import annotations

import asyncio
import inspect
import json
import threading
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from starlette.requests import Request
from starlette.responses import HTMLResponse
from starlette.testclient import TestClient

import pyxle
from pyxle.cli.logger import ConsoleLogger
from pyxle.config import CacheConfig, CsrfConfig, StudioConfig
from pyxle.devserver import DevServer, _notify_studio_rebuild, _rebuild_app_routes
from pyxle.devserver.builder import BuildSummary, build_once
from pyxle.devserver.registry import load_metadata_registry
from pyxle.devserver.routes import build_route_table
from pyxle.devserver.settings import DevServerSettings
from pyxle.devserver.starlette_app import create_starlette_app
from pyxle.devserver import studio as studio_pkg
from pyxle.devserver.studio import (
    REQUEST_LOG_LIMIT,
    STUDIO_PATH,
    StudioManager,
    is_enabled,
)
from pyxle.devserver.studio import api as studio_api
from pyxle.devserver.watcher import WatcherStatistics

pytestmark = pytest.mark.anyio("asyncio")

API = f"{STUDIO_PATH}/api"


@pytest.fixture
def anyio_backend() -> str:  # pragma: no cover - fixture wiring
    return "asyncio"


def write_file(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


@pytest.fixture
def project(tmp_path: Path) -> DevServerSettings:
    """A built project exercising every field Studio reports on."""
    root = tmp_path / "project"
    (root / "pages").mkdir(parents=True)
    (root / "public").mkdir()
    settings = DevServerSettings.from_project_root(root)

    write_file(
        settings.pages_dir / "index.pyxl",
        """
@server
async def load_home(request):
    return {"message": "hi", "q": request.query_params.get("q")}

import React from 'react';

export default function Home({ data }) {
    return <div>{data.message}</div>;
}
""",
    )

    write_file(
        settings.pages_dir / "layout.pyxl",
        """
@server
async def load_layout(request):
    return {"nav": []}

import React from 'react';

export default function Layout({ children }) {
    return <div>{children}</div>;
}
""",
    )

    write_file(
        settings.pages_dir / "posts/[slug].pyxl",
        """
@server
async def load_post(request):
    return {"slug": request.path_params["slug"]}

import React from 'react';

export default function Post({ data }) {
    return <article>{data.slug}</article>;
}
""",
    )

    write_file(
        settings.pages_dir / "plain.pyxl",
        """import React from 'react';

export default function Plain() {
    return <div>plain</div>;
}
""",
    )

    write_file(
        settings.pages_dir / "fail.pyxl",
        """from pyxle.runtime import LoaderError

@server
async def load_fail(request):
    raise LoaderError("teapot refused", status_code=418)

import React from 'react';

export default function Fail() {
    return <div />;
}
""",
    )

    write_file(
        settings.pages_dir / "boom.pyxl",
        """
@server
async def load_boom(request):
    raise RuntimeError("password=hunter2 exploded")

import React from 'react';

export default function Boom() {
    return <div />;
}
""",
    )

    write_file(
        settings.pages_dir / "unser.pyxl",
        """
@server
async def load_unser(request):
    return {"bad": {1, 2, 3}}

import React from 'react';

export default function Unser() {
    return <div />;
}
""",
    )

    write_file(
        settings.pages_dir / "nonfinite.pyxl",
        """
@server
async def load_nonfinite(request):
    return {"ratio": float("inf")}

import React from 'react';

export default function NonFinite() {
    return <div />;
}
""",
    )

    write_file(
        settings.pages_dir / "slow.pyxl",
        """import asyncio

@server
async def load_slow(request):
    await asyncio.sleep(30)
    return {}

import React from 'react';

export default function Slow() {
    return <div />;
}
""",
    )

    write_file(
        settings.pages_dir / "signup.pyxl",
        """from pyxle.runtime import action
from pydantic import BaseModel

class SignupBody(BaseModel):
    email: str

@action
async def register(request, body: SignupBody):
    return {"ok": True}

import React from 'react';

export default function SignupPage() {
    return <div />;
}
""",
    )

    write_file(
        settings.pages_dir / "noschema.pyxl",
        """from pyxle.runtime import action

@action
async def ping(request):
    return {"pong": True}

import React from 'react';

export default function NoSchema() {
    return <div />;
}
""",
    )

    write_file(
        settings.pages_dir / "ws.pyxl",
        """async def websocket(ws):
    await ws.accept()
    await ws.close()

import React from 'react';

export default function Ws() {
    return <div />;
}
""",
    )

    write_file(
        settings.pages_dir / "notes/[[...slug]].pyxl",
        """from pyxle.runtime import action

@action
async def save(request):
    return {"saved": True}

import React from 'react';

export default function Notes() {
    return <div />;
}
""",
    )

    write_file(
        settings.pages_dir / "docs/guide.pyxl",
        """import React from 'react';

export default function Guide() {
    return <div>guide</div>;
}
""",
    )

    write_file(
        settings.pages_dir / "docs/loading.pyxl",
        """import React from 'react';

export default function Loading() {
    return <div>loading</div>;
}
""",
    )

    write_file(
        settings.pages_dir / "docs/error.pyxl",
        """import React from 'react';

export default function DocsError() {
    return <div>error</div>;
}
""",
    )

    write_file(
        settings.pages_dir / "api/pulse.py",
        """from starlette.responses import JSONResponse

async def endpoint(request):
    return JSONResponse({"ok": True})
""",
    )

    build_once(settings)
    return settings


@pytest.fixture
def table(project: DevServerSettings):
    registry = load_metadata_registry(project)
    return build_route_table(registry)


def _make_app(settings: DevServerSettings, table, monkeypatch):
    monkeypatch.setattr("pyxle.devserver.starlette_app.ComponentRenderer", object)

    async def fake_build_page_response(
        *, request, settings, page, renderer, overlay=None, **_kw
    ):
        return HTMLResponse(f"<div>{page.path}</div>")

    monkeypatch.setattr(
        "pyxle.devserver.starlette_app.build_page_response",
        fake_build_page_response,
    )
    return create_starlette_app(settings, table)


@pytest.fixture
def app(project: DevServerSettings, table, monkeypatch):
    return _make_app(project, table, monkeypatch)


@pytest.fixture
def client(app) -> TestClient:
    # A loopback base URL AND a loopback client peer so both guard layers (peer
    # trust + Host header) accept every request. TestClient otherwise reports a
    # non-loopback ``testclient`` peer, which the peer layer would refuse.
    return TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 5555))


def _stub_logger() -> ConsoleLogger:
    return ConsoleLogger(secho=lambda *_args, **_kwargs: None)


# ---------------------------------------------------------------------------
# Presence and absence: debug gating + config gating
# ---------------------------------------------------------------------------


def test_production_app_has_no_studio(project, table, monkeypatch) -> None:
    prod_settings = replace(project, debug=False, page_manifest={})
    app = _make_app(prod_settings, table, monkeypatch)
    client = TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 5555))

    assert app.state.pyxle_studio is None
    assert client.get(STUDIO_PATH).status_code == 404
    assert client.get(f"{API}/bootstrap").status_code == 404


def test_config_disabled_removes_studio(project, table, monkeypatch) -> None:
    settings = replace(project, studio=StudioConfig(enabled=False))
    app = _make_app(settings, table, monkeypatch)
    client = TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 5555))

    assert app.state.pyxle_studio is None
    assert client.get(STUDIO_PATH).status_code == 404


def test_dev_app_serves_studio_index(app, client) -> None:
    assert isinstance(app.state.pyxle_studio, StudioManager)

    for path in (STUDIO_PATH, f"{STUDIO_PATH}/"):
        response = client.get(path)
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/html")
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["x-frame-options"] == "DENY"
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["referrer-policy"] == "no-referrer"


def test_assets_serve_with_correct_content_types(client) -> None:
    css = client.get(f"{STUDIO_PATH}/assets/studio.css")
    assert css.status_code == 200
    assert css.headers["content-type"] == "text/css; charset=utf-8"
    assert css.headers["cache-control"] == "no-store"

    js = client.get(f"{STUDIO_PATH}/assets/studio.js")
    assert js.status_code == 200
    assert js.headers["content-type"] == "text/javascript; charset=utf-8"
    assert js.headers["x-content-type-options"] == "nosniff"

    unknown = client.get(f"{STUDIO_PATH}/assets/evil.js")
    assert unknown.status_code == 404
    assert unknown.json() == {"ok": False, "error": "Unknown asset."}


def test_studio_forces_request_id_middleware_only_when_enabled(
    project, table, monkeypatch
) -> None:
    """Studio's live feed rides the RequestIdMiddleware observer, so an enabled
    Studio must pull the middleware in even with observability fully off — and
    a disabled Studio must not."""
    from pyxle.config import ObservabilityConfig
    from pyxle.observability import RequestIdMiddleware

    all_off = ObservabilityConfig(request_id=False, timing=False)

    with_studio = _make_app(replace(project, observability=all_off), table, monkeypatch)
    assert any(m.cls is RequestIdMiddleware for m in with_studio.user_middleware)

    without_studio = _make_app(
        replace(project, observability=all_off, studio=StudioConfig(enabled=False)),
        table,
        monkeypatch,
    )
    assert all(m.cls is not RequestIdMiddleware for m in without_studio.user_middleware)


def test_is_enabled_helper() -> None:
    assert is_enabled(None) is True
    assert is_enabled(StudioConfig()) is True
    assert is_enabled(SimpleNamespace(enabled=False)) is False
    assert is_enabled(SimpleNamespace()) is False


# ---------------------------------------------------------------------------
# Host-header guard (DNS-rebinding defence)
# ---------------------------------------------------------------------------


def test_foreign_host_header_is_rejected(app) -> None:
    # A loopback peer with a foreign Host exercises the DNS-rebinding layer:
    # the browser is on the box, but the attacker pointed a name at 127.0.0.1.
    evil = TestClient(app, base_url="http://evil.example", client=("127.0.0.1", 5555))
    response = evil.get(f"{API}/bootstrap")
    assert response.status_code == 403
    payload = response.json()
    assert payload["ok"] is False
    assert "evil.example" in payload["error"]
    assert "allowedHosts" in payload["error"]

    # The SSE endpoint sits behind the same guard.
    assert evil.get(f"{STUDIO_PATH}/events").status_code == 403


def test_config_allowed_hosts_extend_the_allowlist(project, table, monkeypatch) -> None:
    settings = replace(project, studio=StudioConfig(allowed_hosts=("mybox.local",)))
    app = _make_app(settings, table, monkeypatch)

    allowed = TestClient(app, base_url="http://mybox.local", client=("127.0.0.1", 5555))
    assert allowed.get(f"{API}/bootstrap").status_code == 200

    still_denied = TestClient(
        app, base_url="http://otherbox.local", client=("127.0.0.1", 5555)
    )
    assert still_denied.get(f"{API}/bootstrap").status_code == 403


def test_remote_peer_with_spoofed_loopback_host_is_rejected(app) -> None:
    # A non-loopback client peer presenting a loopback-looking Host must be
    # refused: binding to 0.0.0.0 never silently exposes Studio to the LAN.
    remote = TestClient(app, base_url="http://127.0.0.1", client=("10.0.0.9", 40000))
    assert remote.get(f"{API}/bootstrap").status_code == 403


def test_remote_peer_with_explicitly_allowed_host_is_served(
    project, table, monkeypatch
) -> None:
    # A remote peer is served only when its Host was explicitly opted in via
    # ``studio.allowedHosts`` — the deliberate "reach it from another device" path.
    settings = replace(project, studio=StudioConfig(allowed_hosts=("mybox.local",)))
    app = _make_app(settings, table, monkeypatch)

    remote = TestClient(app, base_url="http://mybox.local", client=("10.0.0.9", 40000))
    assert remote.get(f"{API}/bootstrap").status_code == 200

    # …but the implicit loopback names still don't work from a remote peer.
    spoofed = TestClient(app, base_url="http://127.0.0.1", client=("10.0.0.9", 40000))
    assert spoofed.get(f"{API}/bootstrap").status_code == 403


def _request_with_host(host: str | None) -> Request:
    headers = [] if host is None else [(b"host", host.encode("latin-1"))]
    return Request({"type": "http", "method": "GET", "path": "/", "headers": headers})


def _request_with_peer(host: str | None, client: tuple | None) -> Request:
    scope: dict = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "headers": [] if host is None else [(b"host", host.encode("latin-1"))],
    }
    if client is not None:
        scope["client"] = client
    return Request(scope)


def test_peer_is_loopback_classifies_client_addresses() -> None:
    # No client peer (test ASGI, unix socket) is fully trusted.
    assert studio_api._peer_is_loopback(_request_with_peer(None, None)) is True
    assert studio_api._peer_is_loopback(_request_with_peer(None, ("127.0.0.1", 1))) is True
    assert studio_api._peer_is_loopback(_request_with_peer(None, ("::1", 1))) is True
    assert studio_api._peer_is_loopback(_request_with_peer(None, ("localhost", 1))) is True
    assert studio_api._peer_is_loopback(_request_with_peer(None, ("10.0.0.9", 1))) is False


def test_host_allowed_direct_cases() -> None:
    allowed = frozenset({"localhost", "127.0.0.1", "::1"})
    explicit: frozenset = frozenset()
    # No client peer → treated as loopback; a browser cannot omit Host anyway.
    assert studio_api._host_allowed(_request_with_host(None), allowed, explicit) is True
    assert (
        studio_api._host_allowed(_request_with_host("localhost:8000"), allowed, explicit)
        is True
    )
    assert (
        studio_api._host_allowed(_request_with_host("[::1]:8000"), allowed, explicit)
        is True
    )
    assert (
        studio_api._host_allowed(
            _request_with_host("attacker.example"), allowed, explicit
        )
        is False
    )


def test_host_allowed_peer_layer_direct() -> None:
    allowed = frozenset({"localhost", "127.0.0.1", "::1"})
    explicit = frozenset({"mybox.local"})
    # Remote peer: only an explicitly-allowlisted Host passes — never the
    # implicit loopback names, and an absent Host is refused outright.
    remote_ok = _request_with_peer("mybox.local", ("10.0.0.9", 40000))
    remote_loopback_host = _request_with_peer("127.0.0.1", ("10.0.0.9", 40000))
    remote_no_host = _request_with_peer(None, ("10.0.0.9", 40000))
    assert studio_api._host_allowed(remote_ok, allowed, explicit) is True
    assert studio_api._host_allowed(remote_loopback_host, allowed, explicit) is False
    assert studio_api._host_allowed(remote_no_host, allowed, explicit) is False
    # Loopback peer with an absent Host is allowed (non-browser local client).
    loopback_no_host = _request_with_peer(None, ("127.0.0.1", 5555))
    assert studio_api._host_allowed(loopback_no_host, allowed, explicit) is True


def test_explicit_hostnames_extracts_allowed_hosts() -> None:
    # No config / empty allowedHosts → no explicit names.
    assert studio_api._explicit_hostnames(None) == frozenset()
    assert studio_api._explicit_hostnames(SimpleNamespace(allowed_hosts=())) == frozenset()
    # Ports stripped, hostnames lowercased.
    config = SimpleNamespace(allowed_hosts=("MyBox.Local:3000", "127.0.0.1"))
    assert studio_api._explicit_hostnames(config) == frozenset(
        {"mybox.local", "127.0.0.1"}
    )


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("localhost", "localhost"),
        ("LOCALHOST:8000", "localhost"),
        ("127.0.0.1:9000", "127.0.0.1"),
        ("[::1]:8000", "::1"),
        ("[::1]", "::1"),
        ("[::1", "::1"),  # malformed bracket never raises
        ("  Example.COM  ", "example.com"),
    ],
)
def test_host_without_port(raw: str, expected: str) -> None:
    assert studio_api._host_without_port(raw) == expected


def test_allowed_hostnames_composition() -> None:
    # Bind-all hosts are never valid incoming Host headers.
    bind_all = SimpleNamespace(starlette_host="0.0.0.0")
    assert studio_api._allowed_hostnames(bind_all, None) == frozenset(
        {"localhost", "127.0.0.1", "::1"}
    )

    named = SimpleNamespace(starlette_host="Dev.Box")
    config = SimpleNamespace(allowed_hosts=("LAN.example:3000",))
    hostnames = studio_api._allowed_hostnames(named, config)
    assert "dev.box" in hostnames
    assert "lan.example" in hostnames  # port stripped, lowercased
    assert "localhost" in hostnames


# ---------------------------------------------------------------------------
# Bootstrap payload
# ---------------------------------------------------------------------------


def test_bootstrap_payload_defaults(project, client) -> None:
    payload = client.get(f"{API}/bootstrap").json()
    assert payload["ok"] is True
    assert payload["version"] == pyxle.__version__
    assert payload["studioPath"] == STUDIO_PATH
    assert payload["project"] == "project"
    assert payload["host"] == project.starlette_host
    assert payload["port"] == project.starlette_port
    assert payload["vitePort"] == project.vite_port
    # No CSRF config: disabled, with no names to send.
    assert payload["csrf"] == {"enabled": False, "cookieName": None, "headerName": None}


def test_bootstrap_payload_csrf_default_names_derive_from_port(project) -> None:
    settings = replace(project, csrf=CsrfConfig(enabled=True))
    payload = studio_api._bootstrap_payload(settings)
    assert payload["csrf"]["enabled"] is True
    assert payload["csrf"]["cookieName"] == f"pyxle-csrf-{settings.starlette_port}"
    assert payload["csrf"]["headerName"] == "x-csrf-token"


def test_bootstrap_payload_csrf_custom_names_honoured(project) -> None:
    settings = replace(
        project,
        csrf=CsrfConfig(enabled=True, cookie_name="my-csrf", header_name="X-My-Token"),
    )
    payload = studio_api._bootstrap_payload(settings)
    assert payload["csrf"]["cookieName"] == "my-csrf"
    assert payload["csrf"]["headerName"] == "X-My-Token"


def test_bootstrap_payload_csrf_disabled_config(project) -> None:
    settings = replace(project, csrf=CsrfConfig(enabled=False, cookie_name="x"))
    payload = studio_api._bootstrap_payload(settings)
    assert payload["csrf"] == {"enabled": False, "cookieName": None, "headerName": None}


# ---------------------------------------------------------------------------
# Routes payload
# ---------------------------------------------------------------------------


def test_routes_payload_reports_every_page_facet(client) -> None:
    payload = client.get(f"{API}/routes").json()
    assert payload["ok"] is True
    pages = {page["path"]: page for page in payload["pages"]}

    home = pages["/"]
    assert home["loader"]["name"] == "load_home"
    assert isinstance(home["loader"]["line"], int)
    assert home["source"] == "index.pyxl"
    assert home["sourceAbsolute"].endswith("index.pyxl")
    assert home["cache"] == {"revalidate": None, "edgeMaxAge": None}
    assert home["usesSuspense"] is False
    assert home["headDynamic"] is False
    assert home["websocket"] is None
    # The root layout's loader wraps the home page.
    assert home["layouts"] == [
        {"source": "layout.pyxl", "loaderName": "load_layout"}
    ]
    assert home["boundaries"] == {"loading": None, "error": None}

    assert pages["/plain"]["loader"] is None

    signup = pages["/signup"]
    assert signup["actions"] == [
        {
            "name": "register",
            "line": signup["actions"][0]["line"],
            "url": "/api/__actions/signup/register",
        }
    ]
    assert isinstance(signup["actions"][0]["line"], int)

    ws = pages["/ws"]
    assert ws["websocket"]["name"] == "websocket"
    assert isinstance(ws["websocket"]["line"], int)

    guide = pages["/docs/guide"]
    assert guide["boundaries"] == {
        "loading": "docs/loading.pyxl",
        "error": "docs/error.pyxl",
    }

    apis = {api["path"]: api for api in payload["apis"]}
    assert apis["/api/pulse"]["source"] == "api/pulse.py"
    assert apis["/api/pulse"]["sourceAbsolute"].endswith("pulse.py")


def test_routes_payload_alias_rows_share_the_concrete_action_url(client, table) -> None:
    # An optional-catch-all page surfaces as several rows (/notes AND
    # /notes/{slug:path}) sharing one module, plus an is_catchall helper action
    # route. Every alias row must expose the SAME concrete, runnable URL — keyed
    # by module, not page path — and never the is_catchall helper (whose path
    # carries a `_pyxle_action_path` capture the tester can't POST to).
    assert any(action.is_catchall for action in table.actions)

    payload = client.get(f"{API}/routes").json()
    pages = {page["path"]: page for page in payload["pages"]}

    assert pages["/notes"]["actions"][0]["url"] == "/api/__actions/notes/save"
    # The alias row shares the same runnable URL (previously None — the bug that
    # made the tester report "No action" on the parameterised path).
    alias = pages["/notes/{slug:path}"]
    assert alias["actions"][0]["url"] == "/api/__actions/notes/save"

    for page in payload["pages"]:
        for action in page["actions"]:
            assert action["url"] is None or "_pyxle_action_path" not in action["url"]


def test_action_schema_resolves_action_on_alias_path(client, table) -> None:
    # Regression: selecting an action under the parameterised alias row
    # (/notes/{slug:path}) resolves to the same concrete action as the primary
    # path — it used to fail with "No action '…' on page '/notes/{slug:path}'".
    assert any(action.is_catchall for action in table.actions)

    primary = client.get(f"{API}/action-schema", params={"path": "/notes", "name": "save"}).json()
    alias = client.get(
        f"{API}/action-schema", params={"path": "/notes/{slug:path}", "name": "save"}
    ).json()

    assert primary["ok"] is True
    assert alias["ok"] is True
    assert alias["url"] == primary["url"] == "/api/__actions/notes/save"


def test_routes_payload_reports_edge_cache_max_age(project, table) -> None:
    settings = replace(project, cache=CacheConfig(routes=(("/docs/*", 60),)))
    payload = studio_api._routes_payload(settings, table)
    pages = {page["path"]: page for page in payload["pages"]}
    assert pages["/docs/guide"]["cache"]["edgeMaxAge"] == 60
    assert pages["/"]["cache"]["edgeMaxAge"] is None


def test_edge_max_age_guard_branches(project) -> None:
    assert studio_api._edge_max_age(project, "/") is None  # no cache config
    disabled = replace(project, cache=SimpleNamespace(enabled=False))
    assert studio_api._edge_max_age(disabled, "/") is None
    # An enabled cache object without the policy hook degrades to None.
    hookless = replace(project, cache=SimpleNamespace(enabled=True))
    assert studio_api._edge_max_age(hookless, "/") is None


def test_routes_payload_broken_layout_never_hides_the_route(
    project, table, monkeypatch
) -> None:
    def boom(settings, page_relative_path):
        raise RuntimeError("layout metadata unreadable")

    monkeypatch.setattr("pyxle.devserver.registry.find_layout_loaders", boom)
    payload = studio_api._routes_payload(project, table)
    pages = {page["path"]: page for page in payload["pages"]}
    assert pages["/"]["layouts"] == []
    assert pages["/"]["loader"]["name"] == "load_home"


# ---------------------------------------------------------------------------
# Action schema
# ---------------------------------------------------------------------------


def test_action_schema_returns_pydantic_json_schema(client) -> None:
    response = client.get(
        f"{API}/action-schema", params={"path": "/signup", "name": "register"}
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["url"] == "/api/__actions/signup/register"
    assert payload["note"] is None
    assert "email" in payload["schema"]["properties"]


def test_action_schema_unknown_action_404s(client) -> None:
    response = client.get(
        f"{API}/action-schema", params={"path": "/signup", "name": "missing"}
    )
    assert response.status_code == 404
    payload = response.json()
    assert payload["ok"] is False
    assert "missing" in payload["error"]


def test_action_schema_action_without_model_has_null_schema(client) -> None:
    payload = client.get(
        f"{API}/action-schema", params={"path": "/noschema", "name": "ping"}
    ).json()
    assert payload["ok"] is True
    assert payload["schema"] is None
    assert payload["url"] == "/api/__actions/noschema/ping"


def test_action_schema_stale_metadata_reports_not_exported(
    project, table, monkeypatch
) -> None:
    monkeypatch.setattr(
        "pyxle.devserver.starlette_app._import_module",
        lambda *args, **kwargs: SimpleNamespace(),
    )
    payload = studio_api._action_schema_payload(project, table, "/signup", "register")
    assert payload["ok"] is False
    assert "not exported" in payload["error"]


def test_action_schema_import_error_is_redacted(project, table, monkeypatch) -> None:
    def boom(*args, **kwargs):
        raise RuntimeError("password=hunter2 in module scope")

    monkeypatch.setattr("pyxle.devserver.starlette_app._import_module", boom)
    payload = studio_api._action_schema_payload(project, table, "/signup", "register")
    assert payload["ok"] is False
    assert payload["error"].startswith("RuntimeError")
    assert "hunter2" not in payload["error"]


def test_action_schema_pydantic_missing_becomes_a_note(
    project, table, monkeypatch
) -> None:
    from pyxle.devserver.validation import PydanticNotInstalledError

    def raise_missing(action_fn):
        raise PydanticNotInstalledError()

    monkeypatch.setattr(
        "pyxle.devserver.validation.resolve_body_model", raise_missing
    )
    payload = studio_api._action_schema_payload(project, table, "/signup", "register")
    assert payload["ok"] is True
    assert payload["schema"] is None
    assert "Pydantic" in payload["note"]


# ---------------------------------------------------------------------------
# Run loader
# ---------------------------------------------------------------------------


def _run_loader(client: TestClient, payload) -> object:
    return client.post(f"{API}/run-loader", json=payload)


def test_run_loader_returns_data_and_duration(client) -> None:
    response = _run_loader(client, {"path": "/"})
    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["data"] == {"message": "hi", "q": None}
    assert isinstance(payload["durationMs"], (int, float))


def test_run_loader_passes_query_params(client) -> None:
    payload = _run_loader(client, {"path": "/", "query": {"q": "zed"}}).json()
    assert payload["data"]["q"] == "zed"


def test_run_loader_substitutes_path_params(client) -> None:
    payload = _run_loader(
        client, {"path": "/posts/{slug}", "params": {"slug": "abc"}}
    ).json()
    assert payload["ok"] is True
    assert payload["data"] == {"slug": "abc"}


def test_run_loader_reports_loader_error_with_status(client) -> None:
    response = _run_loader(client, {"path": "/fail"})
    assert response.status_code == 200  # tester outcome, not transport failure
    payload = response.json()
    assert payload["ok"] is False
    assert payload["kind"] == "loader_error"
    assert payload["status"] == 418
    assert "teapot" in payload["error"]
    assert isinstance(payload["durationMs"], (int, float))


def test_run_loader_redacts_exception_messages(client) -> None:
    payload = _run_loader(client, {"path": "/boom"}).json()
    assert payload["ok"] is False
    assert payload["kind"] == "exception"
    assert payload["error"].startswith("RuntimeError")
    assert "hunter2" not in payload["error"]


def test_run_loader_page_without_loader_notes_it(client) -> None:
    payload = _run_loader(client, {"path": "/plain"}).json()
    assert payload == {
        "ok": True,
        "data": None,
        "note": "This page has no @server loader.",
    }


def test_run_loader_rejects_wrong_content_type(client) -> None:
    response = client.post(
        f"{API}/run-loader", content=b"path=/", headers={"content-type": "text/plain"}
    )
    assert response.status_code == 415
    assert "application/json" in response.json()["error"]


def test_run_loader_rejects_invalid_json(client) -> None:
    response = client.post(
        f"{API}/run-loader",
        content=b"{nope",
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 400
    assert "not valid JSON" in response.json()["error"]


def test_run_loader_rejects_non_object_body(client) -> None:
    response = client.post(
        f"{API}/run-loader",
        content=b"[1, 2]",
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 400
    assert "JSON object" in response.json()["error"]


def test_run_loader_empty_body_means_missing_path(client) -> None:
    response = client.post(
        f"{API}/run-loader", headers={"content-type": "application/json"}
    )
    assert response.status_code == 400
    assert "'path'" in response.json()["error"]


def test_run_loader_rejects_relative_path(client) -> None:
    assert _run_loader(client, {"path": "posts"}).status_code == 400


def test_run_loader_rejects_non_object_params(client) -> None:
    response = _run_loader(client, {"path": "/", "params": ["nope"]})
    assert response.status_code == 400
    assert "'params'" in response.json()["error"]


def test_run_loader_unknown_path_404s(client) -> None:
    response = _run_loader(client, {"path": "/nope"})
    assert response.status_code == 404
    assert "/nope" in response.json()["error"]


def test_run_loader_non_serialisable_data_is_an_exception_outcome(client) -> None:
    payload = _run_loader(client, {"path": "/unser"}).json()
    assert payload["ok"] is False
    assert payload["kind"] == "exception"
    assert "not JSON-serialisable" in payload["error"]


def test_run_loader_non_finite_float_is_an_exception_outcome(client) -> None:
    # ``allow_nan=False`` in the serialisability guard means a loader returning
    # inf/nan takes the graceful exception path (HTTP 200) instead of slipping
    # through to a 500 at JSONResponse render time.
    response = _run_loader(client, {"path": "/nonfinite"})
    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is False
    assert payload["kind"] == "exception"
    assert "not JSON-serialisable" in payload["error"]


def test_run_loader_times_out_wedged_loaders(client, monkeypatch) -> None:
    monkeypatch.setattr(studio_api, "_LOADER_TIMEOUT_S", 0.05)
    payload = _run_loader(client, {"path": "/slow"}).json()
    assert payload["ok"] is False
    assert payload["kind"] == "timeout"
    assert "did not finish" in payload["error"]


async def test_synthesized_loader_request_is_minimal_and_bodyless(tmp_path) -> None:
    settings = DevServerSettings.from_project_root(tmp_path)
    app_sentinel = object()
    outer = Request(
        {
            "type": "http",
            "method": "POST",
            "path": f"{API}/run-loader",
            "headers": [(b"cookie", b"session=secret"), (b"authorization", b"Bearer x")],
            "app": app_sentinel,
        }
    )

    synthetic = studio_api._synthesize_loader_request(
        outer,
        settings=settings,
        path="/posts/abc",
        params={"slug": "abc"},
        query={"q": 1},
    )

    assert synthetic.method == "GET"
    assert synthetic.url.path == "/posts/abc"
    assert synthetic.query_params["q"] == "1"
    assert synthetic.path_params == {"slug": "abc"}
    assert synthetic.scope["app"] is app_sentinel
    assert synthetic.scope["pyxle"] == {"studio": True}
    # Non-derived: the caller's cookies and authorization never leak through.
    assert "cookie" not in synthetic.headers
    assert "authorization" not in synthetic.headers
    assert await synthetic.body() == b""


def test_substitute_path_params_helper() -> None:
    substitute = studio_api._substitute_path_params
    assert substitute("/posts/{slug}", {"slug": "abc"}) == "/posts/abc"
    assert substitute("/docs/{rest:path}", {"rest": "a/b"}) == "/docs/a/b"
    # Missing params keep the placeholder rather than crashing.
    assert substitute("/posts/{slug}", {}) == "/posts/{slug}"
    assert substitute("/static", {"slug": "x"}) == "/static"


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


def test_metrics_payload_is_json_safe_and_excludes_studio_traffic(app, client) -> None:
    registry = app.state.pyxle_metrics
    registry.observe_request(200, 12.0)
    registry.observe_loader(3.0)
    registry.observe_request(500, 20000.0)  # beyond the last bucket bound

    payload = client.get(f"{API}/metrics").json()
    assert payload["ok"] is True
    # Studio's own polling is excluded: only the seeded observations count.
    assert payload["snapshot"]["requests_total"] == 2
    assert payload["snapshot"]["requests_by_status"] == {"2xx": 1, "5xx": 1}
    assert payload["uptimeSeconds"] >= 0.0

    buckets = payload["buckets"]
    assert set(buckets) == {"request", "render", "loader", "action"}
    # The +inf bound becomes null (JSON cannot carry Infinity).
    *finite, last = buckets["request"]
    assert last == [None, 2]
    assert all(isinstance(bound, (int, float)) for bound, _count in finite)
    assert buckets["loader"][-1] == [None, 1]
    assert buckets["render"][-1] == [None, 0]


def test_metrics_payload_without_registry() -> None:
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace()))
    payload = studio_api._metrics_payload(request)
    assert payload == {"ok": False, "error": "Metrics registry unavailable."}


def test_metrics_payload_skips_uptime_when_start_time_unknown() -> None:
    from pyxle.observability.metrics import MetricsRegistry

    state = SimpleNamespace(pyxle_metrics=MetricsRegistry(), pyxle_started_at="nope")
    payload = studio_api._metrics_payload(SimpleNamespace(app=SimpleNamespace(state=state)))
    assert payload["ok"] is True
    assert "uptimeSeconds" not in payload
    assert "buckets" in payload


# ---------------------------------------------------------------------------
# Config view (secret redaction)
# ---------------------------------------------------------------------------


def test_config_endpoint_serialises_settings_and_blocks(project, client) -> None:
    payload = client.get(f"{API}/config").json()
    assert payload["ok"] is True
    assert payload["settings"]["project_root"] == str(project.project_root)
    assert payload["settings"]["debug"] is True
    assert set(payload["blocks"]) == {
        "cors",
        "csrf",
        "cache",
        "navigation",
        "rate_limit",
        "observability",
        "llms",
        "studio",
    }
    assert payload["plugins"] == []


def test_config_payload_redacts_secret_shaped_values(project) -> None:
    settings = replace(
        project,
        csrf=CsrfConfig(enabled=True, cookie_name="my-cookie"),
        plugins=(
            {
                "name": "pyxle-auth",
                "apiKey": "sk-live-abc123",
                "tokenAuth": True,
                "sessionSecret": None,
                "options": {"database_dsn": "postgres://u:p@h/db", "pool": 5},
            },
            "pyxle-db",
        ),
    )
    payload = studio_api._config_payload(settings)

    plugin = payload["plugins"][0]
    assert plugin["name"] == "pyxle-auth"  # non-secret keys pass through
    assert plugin["apiKey"] == "••••••"
    assert plugin["options"]["database_dsn"] == "••••••"
    assert plugin["options"]["pool"] == 5
    # Booleans and Nones are never masked — they reveal nothing.
    assert plugin["tokenAuth"] is True
    assert plugin["sessionSecret"] is None
    assert payload["plugins"][1] == "pyxle-db"
    assert payload["blocks"]["csrf"]["cookie_name"] == "my-cookie"


def test_config_payload_redacts_secret_shaped_values_under_innocent_keys(project) -> None:
    # A DSN stored under a perfectly innocent key (``url``) is masked by the
    # value scan even though key-name matching alone would leak it.
    settings = replace(
        project,
        plugins=(
            {
                "name": "db",
                "config": {"url": "postgresql://admin:S3cret@h/db", "pool": 5},
            },
        ),
    )
    payload = studio_api._config_payload(settings)

    plugin = payload["plugins"][0]
    assert plugin["name"] == "db"  # non-secret string passes through unchanged
    masked = plugin["config"]["url"]
    assert masked != "postgresql://admin:S3cret@h/db"
    assert "S3cret" not in masked
    assert "postgresql://" not in masked
    # Non-credential values are never touched.
    assert plugin["config"]["pool"] == 5
    # The secret never appears anywhere in the serialised payload.
    assert "S3cret" not in json.dumps(payload)


def test_redact_value_masks_dsn_by_value_only() -> None:
    # A DSN under a non-secret key is masked purely by the value heuristic.
    redacted = studio_api._redact_value("url", "postgres://u:p@host/db")
    assert redacted == "[REDACTED_DSN]"
    # A plain, non-secret string is returned verbatim.
    assert studio_api._redact_value("host", "example.internal") == "example.internal"
    assert studio_api._redact_value("kind", "pages") == "pages"


def test_redact_value_lists_under_secret_keys() -> None:
    redacted = studio_api._redact_value("api_tokens", ["a", "b"])
    assert redacted == ["••••••", "••••••"]
    assert studio_api._redact_value(None, "plain") == "plain"
    assert studio_api._redact_value("password", True) is True


def test_block_dict_variants() -> None:
    assert studio_api._block_dict(None) is None
    as_dict = studio_api._block_dict(StudioConfig(allowed_hosts=("a",)))
    assert as_dict["enabled"] is True
    assert list(as_dict["allowed_hosts"]) == ["a"]
    assert studio_api._block_dict("raw-value") == "raw-value"


# ---------------------------------------------------------------------------
# Check (pyxle check parity)
# ---------------------------------------------------------------------------


def _check_settings(tmp_path: Path) -> SimpleNamespace:
    root = tmp_path / "checkproj"
    (root / "pages").mkdir(parents=True)
    return SimpleNamespace(pages_dir=root / "pages", project_root=root)


def test_check_payload_reports_diagnostics_without_node(tmp_path, monkeypatch) -> None:
    settings = _check_settings(tmp_path)
    write_file(
        settings.pages_dir / "good.pyxl",
        """
@server
async def fine(request):
    return {"ok": True}

import React from 'react';

export default function Good() {
    return <div />;
}
""",
    )
    write_file(
        settings.pages_dir / "bad.pyxl",
        """
@server
async def broken(request):
    data = (1 + )
""",
    )
    monkeypatch.setattr("shutil.which", lambda name: None)

    payload = studio_api._check_payload(settings)
    assert payload["ok"] is True
    assert payload["filesChecked"] == 2
    assert payload["jsxValidated"] is False
    assert isinstance(payload["durationMs"], (int, float))
    assert payload["diagnostics"], "the broken page must produce diagnostics"
    for diagnostic in payload["diagnostics"]:
        assert diagnostic["file"] == "pages/bad.pyxl"
        assert diagnostic["fileAbsolute"].endswith("bad.pyxl")
    error = payload["diagnostics"][0]
    assert error["section"] == "python"
    assert error["severity"] == "error"
    assert isinstance(error["line"], int)


def test_check_payload_survives_a_parser_crash(tmp_path, monkeypatch) -> None:
    settings = _check_settings(tmp_path)
    write_file(settings.pages_dir / "page.pyxl", "export default function P() {}\n")
    monkeypatch.setattr("shutil.which", lambda name: "/fake/node")

    def crash(self, *args, **kwargs):
        raise RuntimeError("password=zzz internal state")

    monkeypatch.setattr("pyxle.compiler.parser.PyxParser.parse", crash)

    payload = studio_api._check_payload(settings)
    assert payload["jsxValidated"] is True
    diagnostic = payload["diagnostics"][0]
    assert "parser crashed" in diagnostic["message"]
    assert "zzz" not in diagnostic["message"]  # redacted
    assert diagnostic["severity"] == "error"
    assert diagnostic["line"] is None


def test_check_payload_with_missing_pages_dir(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("shutil.which", lambda name: None)
    settings = SimpleNamespace(pages_dir=tmp_path / "absent", project_root=tmp_path)
    payload = studio_api._check_payload(settings)
    assert payload["ok"] is True
    assert payload["filesChecked"] == 0
    assert payload["diagnostics"] == []


def test_check_endpoint_runs_over_the_live_project(project, client, monkeypatch) -> None:
    monkeypatch.setattr("shutil.which", lambda name: None)
    payload = client.post(f"{API}/check").json()
    assert payload["ok"] is True
    assert payload["jsxValidated"] is False
    assert payload["filesChecked"] == len(list(project.pages_dir.rglob("*.pyxl")))
    errors = [d for d in payload["diagnostics"] if d["severity"] == "error"]
    assert errors == []


# ---------------------------------------------------------------------------
# Request feed: ring buffer, observer wiring, /api/requests
# ---------------------------------------------------------------------------


def _feed_event(path: str = "/x", **over) -> dict:
    event = {
        "method": "GET",
        "path": path,
        "status": 200,
        "duration_ms": 1.5,
        "request_id": "abc123",
        "route": {"target": "page", "path": path},
    }
    event.update(over)
    return event


def test_request_log_is_bounded_with_monotonic_sequence() -> None:
    manager = StudioManager(settings=SimpleNamespace())
    for index in range(REQUEST_LOG_LIMIT + 50):
        manager.record_request(_feed_event(path=f"/p{index}"))

    entries = manager.recent_requests()
    assert len(entries) == REQUEST_LOG_LIMIT
    sequences = [entry["seq"] for entry in entries]
    assert sequences == sorted(sequences)
    assert sequences[0] == 51  # the oldest 50 fell off the far end
    assert sequences[-1] == REQUEST_LOG_LIMIT + 50
    assert entries[0]["path"] == "/p50"


def test_request_entry_serialises_camel_case() -> None:
    manager = StudioManager(settings=SimpleNamespace())
    manager.record_request(_feed_event(duration_ms=1.23456))
    entry = manager.recent_requests()[0]
    assert entry["method"] == "GET"
    assert entry["path"] == "/x"
    assert entry["status"] == 200
    assert entry["durationMs"] == 1.23
    assert entry["requestId"] == "abc123"
    assert entry["routeTarget"] == "page"
    assert entry["routePath"] == "/x"
    assert isinstance(entry["timestamp"], float)


def test_request_entry_tolerates_missing_and_odd_fields() -> None:
    manager = StudioManager(settings=SimpleNamespace())
    manager.record_request({})
    manager.record_request(_feed_event(route="not-a-dict"))
    empty, odd_route = manager.recent_requests()
    assert empty["method"] == ""
    assert empty["status"] == 0
    assert empty["requestId"] is None
    assert empty["routeTarget"] is None
    assert odd_route["routeTarget"] is None
    assert odd_route["routePath"] is None


async def test_subscriber_overflow_drops_oldest_event(monkeypatch) -> None:
    monkeypatch.setattr(studio_pkg, "_SUBSCRIBER_QUEUE_LIMIT", 2)
    manager = StudioManager(settings=SimpleNamespace())
    queue = manager.subscribe()
    assert manager.subscriber_count == 1

    for index in range(3):
        manager.record_request(_feed_event(path=f"/p{index}"))

    assert queue.qsize() == 2
    assert queue.get_nowait()["payload"]["path"] == "/p1"  # /p0 was dropped
    assert queue.get_nowait()["payload"]["path"] == "/p2"

    manager.unsubscribe(queue)
    assert manager.subscriber_count == 0
    manager.unsubscribe(queue)  # idempotent


async def test_notify_rebuild_success_payload() -> None:
    manager = StudioManager(settings=SimpleNamespace())
    queue = manager.subscribe()
    stats = WatcherStatistics(
        elapsed_seconds=1.23456,
        summary=BuildSummary(compiled_pages=["pages/a.pyxl"], removed=["pages/b.pyxl"]),
        error=None,
        changed_paths=[Path("pages/a.pyxl")],
    )
    await manager.notify_rebuild(stats)
    event = queue.get_nowait()
    assert event["type"] == "rebuild"
    assert event["payload"] == {
        "ok": True,
        "elapsedSeconds": 1.235,
        "changedPaths": ["pages/a.pyxl"],
        "compiledPages": ["pages/a.pyxl"],
        "removed": ["pages/b.pyxl"],
    }


async def test_notify_rebuild_failure_payload() -> None:
    manager = StudioManager(settings=SimpleNamespace())
    queue = manager.subscribe()
    stats = WatcherStatistics(
        elapsed_seconds=0.5,
        summary=None,
        error=RuntimeError("compile exploded"),
        changed_paths=[Path("pages/a.pyxl")],
    )
    await manager.notify_rebuild(stats)
    payload = queue.get_nowait()["payload"]
    assert payload["ok"] is False
    assert payload["error"] == "compile exploded"
    assert "compiledPages" not in payload


def test_requests_endpoint_feeds_from_live_traffic(client) -> None:
    client.get("/")  # a real page request, observed by the middleware
    client.get(f"{API}/bootstrap")  # studio traffic, excluded from the feed

    payload = client.get(f"{API}/requests").json()
    assert payload["ok"] is True
    paths = [entry["path"] for entry in payload["requests"]]
    assert "/" in paths
    assert not any(path.startswith(STUDIO_PATH) for path in paths)

    entry = next(e for e in payload["requests"] if e["path"] == "/")
    assert entry["method"] == "GET"
    assert entry["status"] == 200
    assert entry["requestId"]
    assert entry["routeTarget"] == "page"
    assert entry["routePath"] == "/"


# ---------------------------------------------------------------------------
# SSE stream
# ---------------------------------------------------------------------------


async def test_sse_stream_emits_retry_then_events_and_unsubscribes() -> None:
    manager = StudioManager(settings=SimpleNamespace())
    response = studio_api._sse_response(SimpleNamespace(), manager)
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-accel-buffering"] == "no"
    assert manager.subscriber_count == 1

    stream = response.body_iterator
    assert await stream.__anext__() == "retry: 3000\n\n"

    manager.record_request(_feed_event(path="/live"))
    frame = await stream.__anext__()
    assert frame.startswith("data: ")
    event = json.loads(frame[len("data: "):])
    assert event["type"] == "request"
    assert event["payload"]["path"] == "/live"

    await stream.aclose()
    assert manager.subscriber_count == 0


async def test_events_endpoint_opens_a_stream_for_allowed_hosts(tmp_path) -> None:
    from pyxle.devserver.routes import RouteTable

    settings = DevServerSettings.from_project_root(tmp_path)
    manager = StudioManager(settings=settings)
    routes = studio_api.build_studio_routes(
        settings=settings, routes=RouteTable(pages=[], apis=[]), manager=manager
    )
    events_route = next(
        route for route in routes if route.path == f"{STUDIO_PATH}/events"
    )

    response = await events_route.endpoint(_request_with_host("127.0.0.1:8000"))
    assert response.headers["content-type"].startswith("text/event-stream")
    assert manager.subscriber_count == 1
    stream = response.body_iterator
    assert await stream.__anext__() == "retry: 3000\n\n"
    await stream.aclose()
    assert manager.subscriber_count == 0


async def test_sse_stream_pings_idle_connections(monkeypatch) -> None:
    monkeypatch.setattr(studio_api, "_SSE_PING_INTERVAL_S", 0.01)
    manager = StudioManager(settings=SimpleNamespace())
    stream = studio_api._sse_response(SimpleNamespace(), manager).body_iterator

    assert await stream.__anext__() == "retry: 3000\n\n"
    assert await stream.__anext__() == ": ping\n\n"
    # The stream keeps serving real events after a ping.
    manager.record_request(_feed_event(path="/after-ping"))
    frame = await stream.__anext__()
    assert "/after-ping" in frame
    await stream.aclose()
    assert manager.subscriber_count == 0


# ---------------------------------------------------------------------------
# Dev-server wiring: rebuild notifications, hot refresh, browser opening
# ---------------------------------------------------------------------------


def _stats() -> WatcherStatistics:
    return WatcherStatistics(
        elapsed_seconds=0.1,
        summary=BuildSummary(compiled_pages=["pages/index.pyxl"]),
        error=None,
        changed_paths=[],
    )


async def test_notify_studio_rebuild_dispatches_to_manager(monkeypatch) -> None:
    received: list[WatcherStatistics] = []

    class FakeStudio:
        async def notify_rebuild(self, stats: WatcherStatistics) -> None:
            received.append(stats)

    loop = asyncio.get_running_loop()
    monkeypatch.setattr(
        "pyxle.devserver.asyncio.run_coroutine_threadsafe",
        lambda coro, loop: loop.create_task(coro),
    )
    stats = _stats()
    _notify_studio_rebuild(FakeStudio(), loop, stats)
    await asyncio.sleep(0)
    assert received == [stats]


def test_notify_studio_rebuild_without_studio_is_a_noop() -> None:
    _notify_studio_rebuild(None, None, _stats())


async def test_notify_studio_rebuild_closes_coroutine_on_dead_loop(monkeypatch) -> None:
    coroutines: list = []

    class FakeStudio:
        def notify_rebuild(self, stats: WatcherStatistics):
            coroutine = self._notify(stats)
            coroutines.append(coroutine)
            return coroutine

        async def _notify(self, stats: WatcherStatistics) -> None:  # pragma: no cover - never scheduled
            raise AssertionError("must not run")

    def raise_runtime(coro, loop):
        raise RuntimeError("Event loop is closed")

    monkeypatch.setattr("pyxle.devserver.asyncio.run_coroutine_threadsafe", raise_runtime)
    _notify_studio_rebuild(FakeStudio(), asyncio.get_running_loop(), _stats())
    assert inspect.getcoroutinestate(coroutines[0]) == "CORO_CLOSED"


def test_hot_route_refresh_threads_the_studio_manager(
    app, project, monkeypatch
) -> None:
    manager = app.state.pyxle_studio
    assert isinstance(manager, StudioManager)

    captured: dict = {}

    def spy_build_app_routes(**kwargs):
        captured.update(kwargs)
        return ([], object())

    monkeypatch.setattr(
        "pyxle.devserver.starlette_app._build_app_routes", spy_build_app_routes
    )
    _rebuild_app_routes(app, project)
    assert captured["studio"] is manager


def test_maybe_open_browser_rewrites_bind_all_host(monkeypatch, tmp_path) -> None:
    settings = DevServerSettings.from_project_root(
        tmp_path, starlette_host="0.0.0.0", starlette_port=8123
    )
    opened: list[str] = []
    done = threading.Event()

    def fake_open(url: str) -> None:
        opened.append(url)
        done.set()

    monkeypatch.setattr("webbrowser.open", fake_open)
    server = DevServer(settings, logger=_stub_logger(), open_browser_path=STUDIO_PATH)
    server._maybe_open_browser(settings)
    assert done.wait(timeout=5)
    assert opened == [f"http://127.0.0.1:8123{STUDIO_PATH}"]


def test_maybe_open_browser_uses_configured_host(monkeypatch, tmp_path) -> None:
    settings = DevServerSettings.from_project_root(
        tmp_path, starlette_host="localhost", starlette_port=9001
    )
    opened: list[str] = []
    done = threading.Event()

    def fake_open(url: str) -> None:
        opened.append(url)
        done.set()

    monkeypatch.setattr("webbrowser.open", fake_open)
    server = DevServer(settings, logger=_stub_logger(), open_browser_path=STUDIO_PATH)
    server._maybe_open_browser(settings)
    assert done.wait(timeout=5)
    assert opened == [f"http://localhost:9001{STUDIO_PATH}"]


def test_maybe_open_browser_disabled_when_no_path(monkeypatch, tmp_path) -> None:
    settings = DevServerSettings.from_project_root(tmp_path)
    opened: list[str] = []
    monkeypatch.setattr("webbrowser.open", opened.append)
    server = DevServer(settings, logger=_stub_logger(), open_browser_path=None)
    server._maybe_open_browser(settings)
    assert opened == []


def test_maybe_open_browser_swallows_webbrowser_errors(monkeypatch, tmp_path) -> None:
    settings = DevServerSettings.from_project_root(tmp_path)
    done = threading.Event()

    def fake_open(url: str) -> None:
        try:
            raise RuntimeError("headless box")
        finally:
            done.set()

    monkeypatch.setattr("webbrowser.open", fake_open)
    server = DevServer(settings, logger=_stub_logger(), open_browser_path=STUDIO_PATH)
    server._maybe_open_browser(settings)  # must not raise, in thread or out
    assert done.wait(timeout=5)
