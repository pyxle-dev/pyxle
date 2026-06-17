"""Structured logging for the observability access log.

Stdlib-primary: JSON and console structured logging work with **no** third-party
dependency, and the active request id (set by :class:`RequestIdMiddleware`) is
bound into every record through a context variable. When the optional
``[observability]`` extra (structlog) is installed it renders the records;
otherwise a stdlib formatter produces the same shape. Either way structured
logging works.
"""

from __future__ import annotations

import json
import logging
from contextvars import ContextVar
from typing import Any

ACCESS_LOGGER_NAME = "pyxle.access"

# Bound by the request-id middleware; read by the log filter below so any record
# emitted while handling a request carries its correlation id.
_request_id_var: ContextVar[str | None] = ContextVar("pyxle_request_id", default=None)


def bind_request_id(request_id: str | None) -> None:
    """Bind the current request's correlation id for log records to pick up."""
    _request_id_var.set(request_id)


def current_request_id() -> str | None:
    return _request_id_var.get()


def _try_import_structlog() -> Any | None:
    try:
        import structlog
    except ImportError:
        return None
    return structlog


class _RequestIdFilter(logging.Filter):
    """Attach the bound request id to each record."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = current_request_id()
        return True


def _record_fields(record: logging.LogRecord) -> dict[str, Any]:
    fields = dict(getattr(record, "pyxle_fields", {}))
    request_id = getattr(record, "request_id", None)
    if request_id:
        fields = {"request_id": request_id, **fields}
    return fields


class _JsonFormatter(logging.Formatter):
    """Render a record as a single JSON line (stdlib fallback)."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "level": record.levelname.lower(),
            "logger": record.name,
            "message": record.getMessage(),
            **_record_fields(record),
        }
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


class _ConsoleFormatter(logging.Formatter):
    """Human-readable single line with the request id and extra fields."""

    def format(self, record: logging.LogRecord) -> str:
        base = f"{record.levelname:<7} {record.name}: {record.getMessage()}"
        fields = _record_fields(record)
        if fields:
            rendered = " ".join(f"{key}={value}" for key, value in fields.items())
            return f"{base} [{rendered}]"
        return base


def _structlog_formatter(structlog: Any, log_format: str) -> logging.Formatter:
    """A structlog ``ProcessorFormatter`` for the stdlib handler (extra path)."""
    renderer = (
        structlog.processors.JSONRenderer()
        if log_format == "json"
        else structlog.dev.ConsoleRenderer(colors=False)
    )
    return structlog.stdlib.ProcessorFormatter(
        processors=[structlog.stdlib.ProcessorFormatter.remove_processors_meta, renderer],
    )


def configure_logging(*, log_format: str = "console", log_level: str = "INFO") -> None:
    """Configure the Pyxle access logger.

    Idempotent: each call replaces the access logger's handler, so a dev-server
    reload doesn't duplicate output. ``log_format`` is ``"console"`` or
    ``"json"``.
    """
    level = getattr(logging, log_level.upper(), logging.INFO)
    logger = logging.getLogger(ACCESS_LOGGER_NAME)
    logger.setLevel(level)
    logger.propagate = False
    for handler in list(logger.handlers):
        logger.removeHandler(handler)

    stream = logging.StreamHandler()
    stream.addFilter(_RequestIdFilter())
    structlog = _try_import_structlog()
    if structlog is not None:
        stream.setFormatter(_structlog_formatter(structlog, log_format))
    elif log_format == "json":
        stream.setFormatter(_JsonFormatter())
    else:
        stream.setFormatter(_ConsoleFormatter())
    logger.addHandler(stream)


def log_access(*, method: str, path: str, status: int, duration_ms: float) -> None:
    """Emit one structured access-log line for a completed request."""
    logging.getLogger(ACCESS_LOGGER_NAME).info(
        "http_request",
        extra={
            "pyxle_fields": {
                "method": method,
                "path": path,
                "status": status,
                "duration_ms": round(duration_ms, 3),
            }
        },
    )


__all__ = [
    "ACCESS_LOGGER_NAME",
    "bind_request_id",
    "configure_logging",
    "current_request_id",
    "log_access",
]
