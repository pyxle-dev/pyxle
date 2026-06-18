"""Tests for the optional OpenTelemetry tracing integration.

OpenTelemetry isn't installed in the test environment, so these cover the
default no-op behaviour and the "extra not installed" failure path. The real
span-emitting path is exercised only when the [observability-otel] extra is
present (env-gated, like the pydantic/structlog optional paths).
"""

from __future__ import annotations

import pytest

from pyxle.observability import otel
from pyxle.observability.otel import (
    OtelNotInstalledError,
    is_enabled,
    reset_otel,
    setup_otel,
    span,
)


@pytest.fixture(autouse=True)
def _reset():
    reset_otel()
    yield
    reset_otel()


def test_disabled_by_default() -> None:
    assert is_enabled() is False


def test_span_is_noop_when_disabled() -> None:
    # The no-op span is a usable context manager that swallows attributes.
    with span("ssr.render") as active:
        active.set_attribute("pyxle.page", "/")
    assert is_enabled() is False


def test_span_returns_shared_noop_singleton() -> None:
    # No allocation per call when tracing is off.
    assert span("a") is otel._NOOP_SPAN
    assert span("b") is otel._NOOP_SPAN


def test_setup_otel_raises_when_sdk_absent(monkeypatch) -> None:
    monkeypatch.setattr(otel, "_try_import_otel", lambda: None)
    with pytest.raises(OtelNotInstalledError):
        setup_otel()
    assert is_enabled() is False


def test_otel_not_installed_error_names_the_extra() -> None:
    assert "observability-otel" in str(OtelNotInstalledError())


def test_reset_otel_clears_tracer() -> None:
    # Simulate an active tracer, then reset.
    otel._TRACER = object()
    assert is_enabled() is True
    reset_otel()
    assert is_enabled() is False


def test_try_import_otel_returns_none_when_absent() -> None:
    # OpenTelemetry isn't installed in the test environment.
    assert otel._try_import_otel() is None


def test_span_uses_real_tracer_when_enabled() -> None:
    # A fake tracer exercises the active-span path (and attribute setting)
    # without needing the OpenTelemetry SDK installed.
    class _FakeSpan:
        def __init__(self) -> None:
            self.attrs: dict = {}

        def set_attribute(self, key, value) -> None:
            self.attrs[key] = value

    class _FakeCtx:
        def __init__(self, span_obj) -> None:
            self._span = span_obj

        def __enter__(self):
            return self._span

        def __exit__(self, *_exc) -> bool:
            return False

    class _FakeTracer:
        def start_as_current_span(self, _name):
            return _FakeCtx(_FakeSpan())

    otel._TRACER = _FakeTracer()
    assert is_enabled() is True
    with span("ssr.render", {"pyxle.page": "/"}) as active:
        assert active.attrs["pyxle.page"] == "/"
    # No attributes provided -> still works.
    with span("loader") as active2:
        assert active2.attrs == {}
