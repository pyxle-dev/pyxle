"""Tests for /healthz and /readyz readiness dependency checks."""

from __future__ import annotations

from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from pyxle.devserver.starlette_app import (
    _health_payload,
    _healthz_endpoint,
    _metrics_summary,
    _readiness_checks,
    _readyz_endpoint,
)
from pyxle.observability.metrics import MetricsRegistry


class _Pool:
    def __init__(self, alive: int, size: int) -> None:
        self.alive_count = alive
        self.size = size


class _State:
    pass


class _App:
    def __init__(self, **state) -> None:
        self.state = _State()
        for key, value in state.items():
            setattr(self.state, key, value)


# ---------------------------------------------------------------------------
# _readiness_checks


def test_no_pool_means_no_check() -> None:
    assert _readiness_checks(_App(pyxle_ssr_pool=None)) == {}


def test_healthy_pool_check_ok() -> None:
    checks = _readiness_checks(_App(pyxle_ssr_pool=_Pool(alive=2, size=2)))
    assert checks["ssr_pool"] == {"ok": True, "alive": 2, "size": 2}


def test_dead_pool_check_not_ok() -> None:
    checks = _readiness_checks(_App(pyxle_ssr_pool=_Pool(alive=0, size=2)))
    assert checks["ssr_pool"]["ok"] is False


# ---------------------------------------------------------------------------
# _metrics_summary


def test_metrics_summary_none_without_registry() -> None:
    assert _metrics_summary(_App()) is None


def test_metrics_summary_reports_totals() -> None:
    reg = MetricsRegistry()
    reg.observe_request(200, 5.0)
    reg.record_cache("hit")
    summary = _metrics_summary(_App(pyxle_metrics=reg))
    assert summary == {"requests_total": 1, "cache_hit_ratio": 1.0}


# ---------------------------------------------------------------------------
# _health_payload composition


def test_payload_not_ready_until_flag_set() -> None:
    payload = _health_payload(_App(pyxle_ready=False, pyxle_started_at=0.0))
    assert payload["ready"] is False
    assert payload["status"] == "ok"
    assert payload["uptime"] >= 0


def test_payload_ready_requires_flag_and_checks() -> None:
    # Flag set but a dependency is down -> not ready.
    down = _health_payload(
        _App(pyxle_ready=True, pyxle_started_at=0.0, pyxle_ssr_pool=_Pool(0, 1))
    )
    assert down["ready"] is False
    # Flag set and the dependency is healthy -> ready.
    up = _health_payload(
        _App(pyxle_ready=True, pyxle_started_at=0.0, pyxle_ssr_pool=_Pool(1, 1))
    )
    assert up["ready"] is True


def test_payload_includes_metrics_when_registry_present() -> None:
    payload = _health_payload(_App(pyxle_ready=True, pyxle_metrics=MetricsRegistry()))
    assert "metrics" in payload
    assert "checks" in payload


# ---------------------------------------------------------------------------
# endpoint status codes


def _probe_client(**state) -> TestClient:
    app = Starlette(
        routes=[
            Route("/healthz", _healthz_endpoint),
            Route("/readyz", _readyz_endpoint),
        ]
    )
    for key, value in state.items():
        setattr(app.state, key, value)
    return TestClient(app)


def test_healthz_is_always_200() -> None:
    # Liveness is 200 even when not ready (a dead pool, flag unset).
    client = _probe_client(pyxle_ready=False, pyxle_ssr_pool=_Pool(0, 1))
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json()["ready"] is False


def test_readyz_503_when_dependency_down() -> None:
    client = _probe_client(pyxle_ready=True, pyxle_ssr_pool=_Pool(0, 2))
    resp = client.get("/readyz")
    assert resp.status_code == 503
    assert resp.json()["checks"]["ssr_pool"]["ok"] is False


def test_readyz_200_when_ready_and_healthy() -> None:
    client = _probe_client(pyxle_ready=True, pyxle_ssr_pool=_Pool(2, 2))
    resp = client.get("/readyz")
    assert resp.status_code == 200
    assert resp.json()["ready"] is True
