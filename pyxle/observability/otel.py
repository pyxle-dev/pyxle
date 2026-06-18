"""Optional OpenTelemetry tracing — fully off, and dependency-free, by default.

OpenTelemetry is the heaviest of the observability integrations, so it is a
**separate** optional extra (``[observability-otel]``) and is never imported at
module load. When the SDK isn't installed, or tracing isn't enabled in config,
:func:`span` returns a shared no-op context manager — the per-request cost of an
un-enabled span is a single boolean check and entering an empty ``with`` block.

``setup_otel`` is called once from the app lifespan; the exporter endpoint is
read from the standard ``OTEL_EXPORTER_OTLP_ENDPOINT`` environment variable
(don't reinvent it). Sampling defaults to a low ratio so tracing can't swamp a
busy production server.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Iterator

# Resolved once by setup_otel(): the active tracer, or None when tracing is off.
_TRACER: Any = None


def _try_import_otel() -> Any | None:
    try:
        from opentelemetry import trace
    except ImportError:
        return None
    return trace


def is_enabled() -> bool:
    """Whether a real tracer is active (the SDK is installed and configured)."""
    return _TRACER is not None


class OtelNotInstalledError(RuntimeError):
    """OpenTelemetry tracing was requested but the SDK isn't installed."""

    def __init__(self) -> None:
        super().__init__(
            "observability.otel is enabled but OpenTelemetry isn't installed. "
            "Install it with: pip install 'pyxle-framework[observability-otel]'."
        )


def setup_otel(
    *,
    service_name: str = "pyxle-app",
    sample_ratio: float = 0.05,
) -> bool:
    """Configure a global tracer provider. Returns ``True`` if tracing is active.

    Raises :class:`OtelNotInstalledError` when the SDK is unavailable, so a
    misconfiguration fails loudly at startup rather than silently dropping
    traces. The OTLP exporter endpoint comes from
    ``OTEL_EXPORTER_OTLP_ENDPOINT``.
    """
    global _TRACER
    trace = _try_import_otel()
    if trace is None:
        raise OtelNotInstalledError()

    from opentelemetry.sdk.resources import Resource  # noqa: PLC0415
    from opentelemetry.sdk.trace import TracerProvider  # noqa: PLC0415
    from opentelemetry.sdk.trace.export import (  # noqa: PLC0415
        BatchSpanProcessor,
    )
    from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased  # noqa: PLC0415

    provider = TracerProvider(
        resource=Resource.create({"service.name": service_name}),
        sampler=ParentBased(TraceIdRatioBased(sample_ratio)),
    )
    try:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (  # noqa: PLC0415
            OTLPSpanExporter,
        )

        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    except ImportError:
        # The API/SDK is present but no exporter — still trace (e.g. tests or a
        # console exporter the host wires up); just don't add the OTLP one.
        pass

    trace.set_tracer_provider(provider)
    _TRACER = trace.get_tracer("pyxle")
    return True


def reset_otel() -> None:
    """Drop the active tracer (used between tests and on shutdown)."""
    global _TRACER
    _TRACER = None


class _NoopSpan:
    """A shared no-op span/context-manager used when tracing is off.

    Returned directly (not via a generator), so an un-enabled ``with span(...)``
    allocates nothing on the hot path — just enters and exits this singleton.
    """

    def set_attribute(self, *_args, **_kwargs) -> None:
        pass

    def __enter__(self) -> "_NoopSpan":
        return self

    def __exit__(self, *_exc) -> bool:
        return False


_NOOP_SPAN = _NoopSpan()


@contextmanager
def _real_span(name: str, attributes: dict[str, Any] | None) -> Iterator[Any]:
    with _TRACER.start_as_current_span(name) as active:
        if attributes:
            for key, value in attributes.items():
                active.set_attribute(key, value)
        yield active


def span(name: str, attributes: dict[str, Any] | None = None):
    """A context manager spanning ``name`` when tracing is active, else a no-op.

    When tracing is off (the default) this returns a shared no-op context
    manager — no allocation, just a boolean check — so it's safe to wrap the SSR
    render, loaders, and actions unconditionally.
    """
    if _TRACER is None:
        return _NOOP_SPAN
    return _real_span(name, attributes)


__all__ = [
    "OtelNotInstalledError",
    "is_enabled",
    "reset_otel",
    "setup_otel",
    "span",
]
