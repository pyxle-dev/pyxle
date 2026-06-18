"""Request observability for Pyxle: correlation IDs and request timing.

This package is the foundation the heavier observability features build on
(metrics, structured logging, OpenTelemetry). It is import-light and has **no**
third-party dependencies, and it depends on nothing else in ``pyxle`` — the
devserver imports *it*, never the reverse, so there is no import cycle.

The default tier (request-id + timing) is pure-stdlib and cheap enough to run
on every request. Exporter-style features that cost real money per request
(structured access logs, the metrics endpoint, OpenTelemetry spans) live behind
their own opt-in config and optional extras.
"""

from __future__ import annotations

from pyxle.observability.middleware import (
    RequestIdMiddleware,
    get_request_id,
    request_timing_ms,
)

__all__ = [
    "RequestIdMiddleware",
    "get_request_id",
    "request_timing_ms",
]
