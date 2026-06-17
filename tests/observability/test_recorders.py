"""The thin ``_record_*`` helpers that feed the metrics registry."""

from __future__ import annotations

from pyxle.devserver.starlette_app import _record_action_metric, _record_cache_metric
from pyxle.observability.metrics import MetricsRegistry
from pyxle.ssr.view import _record_render_metric


def _request_with(registry):
    class _State:
        pyxle_metrics = registry

    class _App:
        state = _State()

    class _Req:
        app = _App()

    return _Req()


def _request_without_registry():
    class _Req:
        pass

    return _Req()


def test_record_cache_metric() -> None:
    reg = MetricsRegistry()
    _record_cache_metric(_request_with(reg), "hit")
    assert reg.cache.hits == 1


def test_record_action_metric() -> None:
    reg = MetricsRegistry()
    _record_action_metric(_request_with(reg), 7.0)
    assert reg.action_duration.total == 1


def test_record_render_metric_render_and_loader() -> None:
    reg = MetricsRegistry()
    _record_render_metric(_request_with(reg), "render", 40.0)
    _record_render_metric(_request_with(reg), "loader", 5.0)
    assert reg.render_duration.total == 1
    assert reg.loader_duration.total == 1


def test_recorders_no_op_without_registry() -> None:
    # No registry bound -> the helpers must be silent no-ops.
    req = _request_without_registry()
    _record_cache_metric(req, "miss")
    _record_action_metric(req, 1.0)
    _record_render_metric(req, "render", 1.0)
