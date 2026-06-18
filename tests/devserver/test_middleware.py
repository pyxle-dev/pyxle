from __future__ import annotations

import pytest
from starlette.middleware import Middleware

from pyxle.devserver.middleware import (
    MiddlewareHookError,
    find_base_http_middlewares,
    load_custom_middlewares,
)
from tests.devserver.sample_middlewares import SimpleAsgiMiddleware


def test_load_custom_middlewares_supports_classes_and_factories() -> None:
    middlewares = load_custom_middlewares(
        [
            "tests.devserver.sample_middlewares:HeaderCaptureMiddleware",
            "tests.devserver.sample_middlewares:create_rate_limit_middleware",
            "tests.devserver.sample_middlewares:tuple_middleware_factory",
        ]
    )

    assert len(middlewares) == 3
    assert all(isinstance(item, Middleware) for item in middlewares)
    assert middlewares[-1].cls.__name__ == "ConfigurableSuffixMiddleware"


def test_load_custom_middlewares_raises_for_invalid_spec() -> None:
    with pytest.raises(MiddlewareHookError):
        load_custom_middlewares(["tests.devserver.sample_middlewares:missing"])

    with pytest.raises(MiddlewareHookError):
        load_custom_middlewares(["tests.devserver.sample_middlewares:invalid_factory"])

    with pytest.raises(MiddlewareHookError):
        load_custom_middlewares(["not-a-valid-spec"])


def test_load_custom_middlewares_accepts_asgi_classes() -> None:
    middlewares = load_custom_middlewares(
        ["tests.devserver.sample_middlewares:SimpleAsgiMiddleware"]
    )

    assert len(middlewares) == 1
    assert middlewares[0].cls is SimpleAsgiMiddleware


def test_find_base_http_middlewares_detects_subclasses() -> None:
    middlewares = load_custom_middlewares(
        [
            "tests.devserver.sample_middlewares:HeaderCaptureMiddleware",
            "tests.devserver.sample_middlewares:SimpleAsgiMiddleware",
        ]
    )

    offenders = find_base_http_middlewares(middlewares)
    # Only the BaseHTTPMiddleware subclass is flagged; the pure-ASGI one is not.
    assert offenders == ["HeaderCaptureMiddleware"]


def test_find_base_http_middlewares_empty_for_pure_asgi() -> None:
    middlewares = load_custom_middlewares(
        ["tests.devserver.sample_middlewares:SimpleAsgiMiddleware"]
    )

    assert find_base_http_middlewares(middlewares) == []
