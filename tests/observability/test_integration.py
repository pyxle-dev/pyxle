"""End-to-end: the request-id middleware is wired into the real app."""

from __future__ import annotations

import re
from pathlib import Path

from starlette.testclient import TestClient

from pyxle.config import ObservabilityConfig
from pyxle.devserver.builder import build_once
from pyxle.devserver.registry import load_metadata_registry
from pyxle.devserver.routes import build_route_table
from pyxle.devserver.settings import DevServerSettings
from pyxle.devserver.starlette_app import create_starlette_app

_HEX32 = re.compile(r"\A[0-9a-f]{32}\Z")

# An API endpoint that echoes the correlation id it received, proving the id
# reaches handlers via request.state without any change to the handler API.
_ECHO_API = """from starlette.responses import JSONResponse
from pyxle.observability import get_request_id

async def endpoint(request):
    return JSONResponse({
        "state": getattr(request.state, "request_id", None),
        "helper": get_request_id(request),
    })
"""


def _app(tmp_path: Path, *, observability=None) -> TestClient:
    root = tmp_path / "project"
    (root / "pages" / "api").mkdir(parents=True)
    (root / "public").mkdir()
    (root / "pages" / "api" / "whoami.py").write_text(_ECHO_API, encoding="utf-8")
    settings = DevServerSettings.from_project_root(root, observability=observability)
    build_once(settings)
    registry = load_metadata_registry(settings)
    table = build_route_table(registry)
    return TestClient(create_starlette_app(settings, table))


def test_request_id_header_on_real_app(tmp_path: Path) -> None:
    resp = _app(tmp_path).get("/api/whoami")
    assert resp.status_code == 200
    header = resp.headers["x-request-id"]
    assert _HEX32.match(header)
    # The id the handler saw matches the response header, via both surfaces.
    assert resp.json() == {"state": header, "helper": header}


def test_disabled_via_config_omits_header(tmp_path: Path) -> None:
    client = _app(
        tmp_path,
        observability=ObservabilityConfig(request_id=False, timing=False),
    )
    resp = client.get("/api/whoami")
    assert resp.status_code == 200
    assert "x-request-id" not in resp.headers
    assert resp.json()["state"] is None


def test_request_is_recorded_into_metrics_registry(tmp_path: Path) -> None:
    client = _app(tmp_path)
    client.get("/api/whoami")
    client.get("/api/whoami")
    snap = client.app.state.pyxle_metrics.snapshot()
    assert snap["requests_total"] >= 2
    assert snap["requests_by_status"].get("2xx", 0) >= 2
    assert snap["request_duration"]["count"] >= 2


def test_metrics_registry_present_even_when_request_id_disabled(tmp_path: Path) -> None:
    # The registry is always created; only the request-id/header is gated.
    client = _app(
        tmp_path,
        observability=ObservabilityConfig(request_id=False, timing=False),
    )
    client.get("/api/whoami")
    assert client.app.state.pyxle_metrics is not None


def test_metrics_endpoint_off_by_default(tmp_path: Path) -> None:
    resp = _app(tmp_path).get("/api/__pyxle/metrics")
    # No route registered -> the catch-all/404 handles it (not a 200 metrics body).
    assert resp.status_code != 200


def test_metrics_endpoint_serves_prometheus_when_enabled(tmp_path: Path) -> None:
    client = _app(tmp_path, observability=ObservabilityConfig(metrics_endpoint=True))
    client.get("/api/whoami")  # generate a request to record
    resp = client.get("/api/__pyxle/metrics")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/plain")
    assert "pyxle_requests_total" in resp.text


def test_metrics_endpoint_custom_path(tmp_path: Path) -> None:
    client = _app(
        tmp_path,
        observability=ObservabilityConfig(
            metrics_endpoint=True, metrics_endpoint_path="/internal/m"
        ),
    )
    assert client.get("/internal/m").status_code == 200


def test_metrics_endpoint_bearer_token(tmp_path: Path) -> None:
    client = _app(
        tmp_path,
        observability=ObservabilityConfig(
            metrics_endpoint=True, metrics_endpoint_token="s3cret"
        ),
    )
    assert client.get("/api/__pyxle/metrics").status_code == 401
    ok = client.get(
        "/api/__pyxle/metrics", headers={"Authorization": "Bearer s3cret"}
    )
    assert ok.status_code == 200
    bad = client.get(
        "/api/__pyxle/metrics", headers={"Authorization": "Bearer wrong"}
    )
    assert bad.status_code == 401
