"""Tests for structured access logging."""

from __future__ import annotations

import io
import json
import logging

import pytest

from pyxle.observability import logging as obs_logging
from pyxle.observability.logging import (
    ACCESS_LOGGER_NAME,
    bind_request_id,
    configure_logging,
    current_request_id,
    log_access,
)


@pytest.fixture(autouse=True)
def _reset_request_id():
    bind_request_id(None)
    yield
    bind_request_id(None)


def _capture(log_format: str) -> io.StringIO:
    configure_logging(log_format=log_format)
    logger = logging.getLogger(ACCESS_LOGGER_NAME)
    buffer = io.StringIO()
    logger.handlers[0].stream = buffer  # redirect the configured StreamHandler
    return buffer


# ---------------------------------------------------------------------------
# request-id contextvar


def test_bind_and_read_request_id() -> None:
    assert current_request_id() is None
    bind_request_id("abc123")
    assert current_request_id() == "abc123"


# ---------------------------------------------------------------------------
# configure_logging


def test_configure_logging_is_idempotent() -> None:
    configure_logging()
    configure_logging()
    logger = logging.getLogger(ACCESS_LOGGER_NAME)
    # Repeated calls must not stack handlers.
    assert len(logger.handlers) == 1
    assert logger.propagate is False


def test_configure_logging_sets_level() -> None:
    configure_logging(log_level="DEBUG")
    assert logging.getLogger(ACCESS_LOGGER_NAME).level == logging.DEBUG


# ---------------------------------------------------------------------------
# JSON output


def test_access_log_json_includes_fields_and_request_id() -> None:
    buffer = _capture("json")
    bind_request_id("req-1")
    log_access(method="GET", path="/x", status=200, duration_ms=12.345)
    record = json.loads(buffer.getvalue().strip())
    assert record["message"] == "http_request"
    assert record["level"] == "info"
    assert record["request_id"] == "req-1"
    assert record["method"] == "GET"
    assert record["path"] == "/x"
    assert record["status"] == 200
    assert record["duration_ms"] == 12.345


def test_access_log_json_omits_request_id_when_unbound() -> None:
    buffer = _capture("json")
    log_access(method="POST", path="/y", status=500, duration_ms=1.0)
    record = json.loads(buffer.getvalue().strip())
    assert "request_id" not in record
    assert record["status"] == 500


# ---------------------------------------------------------------------------
# console output


def test_access_log_console_includes_request_id_and_fields() -> None:
    buffer = _capture("console")
    bind_request_id("req-2")
    log_access(method="GET", path="/z", status=204, duration_ms=2.0)
    line = buffer.getvalue().strip()
    assert "http_request" in line
    assert "request_id=req-2" in line
    assert "status=204" in line


# ---------------------------------------------------------------------------
# formatter edge cases


def test_json_formatter_includes_exception() -> None:
    import sys

    formatter = obs_logging._JsonFormatter()
    try:
        raise ValueError("boom")
    except ValueError:
        record = logging.LogRecord(
            "n", logging.ERROR, __file__, 1, "failed", None, sys.exc_info()
        )
    payload = json.loads(formatter.format(record))
    assert payload["level"] == "error"
    assert "exc_info" in payload


def test_console_formatter_without_fields_is_plain() -> None:
    formatter = obs_logging._ConsoleFormatter()
    record = logging.LogRecord("n", logging.INFO, __file__, 1, "plain", None, None)
    line = formatter.format(record)
    assert "plain" in line
    assert "[" not in line  # no request-id / fields block
