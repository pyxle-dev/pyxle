from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from starlette.requests import Request
from starlette.responses import PlainTextResponse

from pyxle.devserver.route_hooks import (
    DEFAULT_API_POLICIES,
    DEFAULT_PAGE_POLICIES,
    RouteContext,
    RouteHook,
    RouteHookError,
    attach_route_metadata,
    enforce_allowed_methods,
    load_route_hooks,
    wrap_with_route_hooks,
)


def _make_request():
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "headers": [],
    }

    async def _receive():  # pragma: no cover - helper used in async tests
        return {"type": "http.request", "body": b"", "more_body": False}

    return Request(scope, receive=_receive)


def _make_request_with(method: str = "GET", path: str = "/", *, body: bytes = b""):
    scope = {
        "type": "http",
        "method": method,
        "path": path,
        "headers": [],
    }

    async def _receive():
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(scope, receive=_receive)


_SENTINEL_RESPONSE = PlainTextResponse("downstream-sentinel")


async def _sentinel_call_next(request):
    """Shared downstream that returns a recognisable sentinel response.

    Exercised on policy pass-through paths and supplied (but never reached) on
    short-circuit paths, so tests can assert on response identity.
    """
    return _SENTINEL_RESPONSE


def _make_context(*, target: str = "page", allowed_methods=("GET",)) -> RouteContext:
    return RouteContext(
        target=target,  # type: ignore[arg-type]
        path="/widgets",
        source_relative_path=Path("widgets.pyxl"),
        source_absolute_path=Path("/tmp/widgets.pyxl"),
        module_key="pyxle.server.pages.widgets",
        content_hash="hash123",
        has_loader=True,
        head_elements=("<title>Widgets</title>",),
        allowed_methods=allowed_methods,
    )


class CallableHookInstance:
    """Object whose async ``__call__`` is the route hook itself.

    Has no lifecycle methods, so it exercises the ``_get_async_call_method``
    resolution branch (object with an awaitable ``__call__``).
    """

    async def __call__(self, context, request, call_next):
        request.state.callable_hook_path = context.path
        return await call_next(request)


class PreOnlyLifecycleHook(RouteHook):
    """Lifecycle hook that only overrides ``on_pre_call``."""

    async def on_pre_call(self, request, context) -> None:
        request.state.pre_only_seen = context.path


class ErrorRecordingLifecycleHook(RouteHook):
    """Lifecycle hook that records errors raised by the wrapped handler."""

    async def on_error(self, request, context, exc) -> None:
        request.state.recorded_error = repr(exc)


class _OnErrorOnlyDuckHook:
    """Plain object whose only *callable* lifecycle attribute is ``on_error``.

    ``on_pre_call`` and ``on_post_call`` are present but non-callable, so the
    lifecycle wrapper takes the false side of both the ``callable(on_pre)`` and
    ``callable(on_post)`` guards. ``on_error`` is callable, satisfying the
    "has at least one lifecycle hook" requirement and recording the error.
    """

    on_pre_call = "not-callable"
    on_post_call = "not-callable"

    async def on_error(self, request, context, exc) -> None:
        request.state.duck_error = repr(exc)


class _OnPostOnlyDuckHook:
    """Plain object whose only *callable* lifecycle attribute is ``on_post_call``.

    Used on the error path: ``on_error`` is non-callable, so when the wrapped
    handler raises the ``callable(on_error)`` guard is false and the exception
    propagates untouched. The callable ``on_post_call`` registers the object as
    a lifecycle hook but is never reached because the handler raised first.
    """

    on_pre_call = "not-callable"
    on_error = "not-callable"

    async def on_post_call(self, request, response, context) -> None:
        request.state.duck_post = context.path


def test_load_route_hooks_accepts_async_callable():
    hooks = load_route_hooks(["tests.devserver.sample_middlewares:record_route_hook"])
    assert len(hooks) == 1


def test_load_route_hooks_supports_factory():
    hooks = load_route_hooks(["tests.devserver.sample_middlewares:build_target_hook"])
    assert len(hooks) == 1


def test_load_route_hooks_rejects_bad_spec():
    with pytest.raises(RouteHookError):
        load_route_hooks(["invalid-spec"])


def test_load_route_hooks_require_async_callables():
    with pytest.raises(RouteHookError):
        load_route_hooks(["tests.devserver.sample_middlewares:invalid_route_hook_factory"])


def test_wrap_with_route_hooks_runs_chain_in_order():
    order: list[str] = []

    async def first(context, request, call_next):
        order.append(f"first:{context.path}")
        response = await call_next(request)
        response.headers["x-first"] = "1"
        return response

    async def second(context, request, call_next):
        order.append(f"second:{context.target}")
        return await call_next(request)

    async def handler(request):
        return PlainTextResponse("ok")

    context = RouteContext(
        target="page",
        path="/",
        source_relative_path=Path("index.pyxl"),
        source_absolute_path=Path("/tmp/index.pyxl"),
        module_key="pyxle.server.pages.index",
        content_hash="abc",
    )

    async def _run():
        wrapped = wrap_with_route_hooks(handler, hooks=[first, second], context=context)
        response = await wrapped(_make_request())
        assert response.headers["x-first"] == "1"

    asyncio.run(_run())
    assert order == ["first:/", "second:page"]


def test_load_route_hooks_supports_lifecycle_classes():
    hooks = load_route_hooks(
        ["tests.devserver.sample_middlewares:LifecycleRecordingHook"]
    )

    assert len(hooks) == 1

    async def handler(request):
        return PlainTextResponse("ok")

    context = RouteContext(
        target="page",
        path="/",
        source_relative_path=Path("index.pyxl"),
        source_absolute_path=Path("/tmp/index.pyxl"),
        module_key="pyxle.server.pages.index",
        content_hash="abc",
    )

    async def _run():
        wrapped = wrap_with_route_hooks(handler, hooks=hooks, context=context)
        request = _make_request()
        response = await wrapped(request)
        assert response.status_code == 200
        assert getattr(request.state, "hook_markers", []) == ["pre", "post"]

    asyncio.run(_run())


def test_route_hook_base_methods_return_none():
    hook = RouteHook()
    request = _make_request()
    context = _make_context()

    async def _run():
        response = PlainTextResponse("ok")
        assert await hook.on_pre_call(request, context) is None
        assert await hook.on_post_call(request, response, context) is None
        assert await hook.on_error(request, context, ValueError("x")) is None

    asyncio.run(_run())


def test_route_context_as_dict_serializes_metadata():
    context = _make_context(target="api", allowed_methods=("GET", "POST"))

    data = context.as_dict()

    assert data == {
        "target": "api",
        "path": "/widgets",
        "source": "widgets.pyxl",
        "module": "pyxle.server.pages.widgets",
        "contentHash": "hash123",
        "hasLoader": True,
        "head": ["<title>Widgets</title>"],
        "allowedMethods": ["GET", "POST"],
    }


def test_load_route_hooks_rejects_invalid_module_path():
    # The spec has the required ``module:attribute`` form, but the module part
    # is not a valid dotted Python path (contains a hyphen).
    with pytest.raises(RouteHookError) as excinfo:
        load_route_hooks(["bad-module:hook"])

    message = str(excinfo.value)
    assert "Invalid module path 'bad-module'" in message
    assert "dotted Python import path" in message


def test_load_route_hooks_rejects_missing_attribute():
    with pytest.raises(RouteHookError) as excinfo:
        load_route_hooks(["tests.devserver.sample_middlewares:does_not_exist"])

    message = str(excinfo.value)
    assert "tests.devserver.sample_middlewares" in message
    assert "does_not_exist" in message


def test_load_route_hooks_resolves_object_with_async_call():
    hooks = load_route_hooks(
        ["tests.devserver.test_route_hooks:CallableHookInstance"]
    )
    assert len(hooks) == 1

    async def handler(request):
        return PlainTextResponse("ok")

    context = _make_context()

    async def _run():
        wrapped = wrap_with_route_hooks(handler, hooks=hooks, context=context)
        request = _make_request()
        response = await wrapped(request)
        assert response.status_code == 200
        # The instance's async __call__ ran as the hook body.
        assert getattr(request.state, "callable_hook_path", None) == "/widgets"

    asyncio.run(_run())


def test_lifecycle_hook_without_pre_or_post_still_runs_handler():
    # Only on_error is overridden; on_pre_call/on_post_call inherit the base
    # no-op implementations, so both the pre and post branches are skipped at
    # call time for the happy path.
    hooks = load_route_hooks(
        ["tests.devserver.test_route_hooks:ErrorRecordingLifecycleHook"]
    )
    assert len(hooks) == 1

    async def handler(request):
        return PlainTextResponse("ok")

    context = _make_context()

    async def _run():
        wrapped = wrap_with_route_hooks(handler, hooks=hooks, context=context)
        request = _make_request()
        response = await wrapped(request)
        assert response.status_code == 200
        # No error occurred, so on_error never recorded anything.
        assert getattr(request.state, "recorded_error", None) is None

    asyncio.run(_run())


def test_lifecycle_hook_on_error_runs_and_reraises():
    hooks = load_route_hooks(
        ["tests.devserver.test_route_hooks:ErrorRecordingLifecycleHook"]
    )

    boom = RuntimeError("handler exploded")

    async def handler(request):
        raise boom

    context = _make_context()

    async def _run():
        wrapped = wrap_with_route_hooks(handler, hooks=hooks, context=context)
        request = _make_request()
        with pytest.raises(RuntimeError) as excinfo:
            await wrapped(request)
        # on_error observed the original exception, which was then re-raised.
        assert excinfo.value is boom
        assert getattr(request.state, "recorded_error", None) == repr(boom)

    asyncio.run(_run())


def test_lifecycle_hook_pre_only_skips_post_branch():
    # PreOnlyLifecycleHook overrides on_pre_call only; on_post_call inherits the
    # base no-op. The pre branch runs, the post branch (callable but no-op) is
    # taken without raising.
    hooks = load_route_hooks(
        ["tests.devserver.test_route_hooks:PreOnlyLifecycleHook"]
    )

    async def handler(request):
        return PlainTextResponse("ok")

    context = _make_context()

    async def _run():
        wrapped = wrap_with_route_hooks(handler, hooks=hooks, context=context)
        request = _make_request()
        response = await wrapped(request)
        assert response.status_code == 200
        assert getattr(request.state, "pre_only_seen", None) == "/widgets"

    asyncio.run(_run())


def test_lifecycle_skips_non_callable_pre_and_post_guards():
    # _OnErrorOnlyDuckHook has non-callable on_pre_call/on_post_call, so on a
    # successful request the wrapper takes the false side of both the
    # callable(on_pre) and callable(on_post) guards and the handler still runs.
    hooks = load_route_hooks(
        ["tests.devserver.test_route_hooks:_OnErrorOnlyDuckHook"]
    )
    assert len(hooks) == 1

    async def handler(request):
        request.state.handler_ran = True
        return PlainTextResponse("ok")

    context = _make_context()

    async def _run():
        wrapped = wrap_with_route_hooks(handler, hooks=hooks, context=context)
        request = _make_request()
        response = await wrapped(request)
        assert response.status_code == 200
        # The handler executed despite both lifecycle guards being skipped.
        assert getattr(request.state, "handler_ran", False) is True
        # No error, so the callable on_error was not invoked.
        assert getattr(request.state, "duck_error", None) is None

    asyncio.run(_run())


def test_lifecycle_invokes_callable_on_error_with_non_callable_pre_post():
    hooks = load_route_hooks(
        ["tests.devserver.test_route_hooks:_OnErrorOnlyDuckHook"]
    )

    boom = ValueError("duck boom")

    async def handler(request):
        raise boom

    context = _make_context()

    async def _run():
        wrapped = wrap_with_route_hooks(handler, hooks=hooks, context=context)
        request = _make_request()
        with pytest.raises(ValueError) as excinfo:
            await wrapped(request)
        assert excinfo.value is boom
        # The callable on_error observed the exception before re-raise.
        assert getattr(request.state, "duck_error", None) == repr(boom)

    asyncio.run(_run())


def test_lifecycle_reraises_when_on_error_not_callable():
    # _OnPostOnlyDuckHook has a non-callable on_error. When the handler raises,
    # the callable(on_error) guard is false and the original exception
    # propagates unchanged.
    hooks = load_route_hooks(
        ["tests.devserver.test_route_hooks:_OnPostOnlyDuckHook"]
    )

    boom = RuntimeError("no handler for this")

    async def handler(request):
        raise boom

    context = _make_context()

    async def _run():
        wrapped = wrap_with_route_hooks(handler, hooks=hooks, context=context)
        request = _make_request()
        with pytest.raises(RuntimeError) as excinfo:
            await wrapped(request)
        assert excinfo.value is boom
        # on_post_call was never reached because the handler raised first.
        assert not hasattr(request.state, "duck_post")

    asyncio.run(_run())


def test_lifecycle_runs_callable_on_post_on_success():
    # Happy-path companion that exercises _OnPostOnlyDuckHook.on_post_call so
    # the post branch (callable side) runs and the handler response is returned.
    hooks = load_route_hooks(
        ["tests.devserver.test_route_hooks:_OnPostOnlyDuckHook"]
    )

    async def handler(request):
        return PlainTextResponse("ok")

    context = _make_context()

    async def _run():
        wrapped = wrap_with_route_hooks(handler, hooks=hooks, context=context)
        request = _make_request()
        response = await wrapped(request)
        assert response.status_code == 200
        assert getattr(request.state, "duck_post", None) == "/widgets"

    asyncio.run(_run())


def test_attach_route_metadata_writes_into_scope():
    context = _make_context(target="api", allowed_methods=("GET", "POST"))

    captured: dict[str, object] = {}

    async def call_next(request):
        # Reading the body confirms the request is fully wired and exercises
        # the receive channel before metadata is asserted.
        captured["body"] = await request.body()
        captured["pyxle"] = request.scope["pyxle"]
        return PlainTextResponse("ok")

    async def _run():
        request = _make_request_with(method="POST", path="/widgets", body=b"payload")
        response = await attach_route_metadata(context, request, call_next)
        assert response.status_code == 200

    asyncio.run(_run())

    assert captured["body"] == b"payload"
    assert captured["pyxle"]["route"] == context.as_dict()  # type: ignore[index]


def test_enforce_allowed_methods_blocks_disallowed_verb():
    context = _make_context(target="api", allowed_methods=("GET",))

    async def _run():
        request = _make_request_with(method="POST", path="/widgets")
        return await enforce_allowed_methods(context, request, _sentinel_call_next)

    response = asyncio.run(_run())

    # The 405 short-circuits before call_next, so the downstream sentinel is
    # never returned.
    assert response.status_code == 405
    assert response is not _SENTINEL_RESPONSE
    body = json.loads(bytes(response.body))
    assert body == {
        "error": "method_not_allowed",
        "allowed": ["GET"],
        "path": "/widgets",
    }


def test_enforce_allowed_methods_allows_permitted_verb():
    context = _make_context(target="api", allowed_methods=("GET", "POST"))

    async def _run():
        request = _make_request_with(method="POST", path="/widgets")
        return await enforce_allowed_methods(context, request, _sentinel_call_next)

    response = asyncio.run(_run())

    # The verb is permitted, so the policy delegates to the downstream handler.
    assert response is _SENTINEL_RESPONSE
    assert response.status_code == 200


def test_enforce_allowed_methods_ignores_non_api_targets():
    # A page route is never subject to the 405 policy regardless of method.
    context = _make_context(target="page", allowed_methods=("GET",))

    async def _run():
        request = _make_request_with(method="DELETE", path="/widgets")
        return await enforce_allowed_methods(context, request, _sentinel_call_next)

    response = asyncio.run(_run())

    assert response is _SENTINEL_RESPONSE
    assert response.status_code == 200


def test_enforce_allowed_methods_falls_back_to_get_when_empty():
    # An empty allowed_methods tuple falls back to ("GET",): GET passes through,
    # anything else is rejected.
    context = _make_context(target="api", allowed_methods=())

    async def _run_get():
        request = _make_request_with(method="GET", path="/widgets")
        return await enforce_allowed_methods(context, request, _sentinel_call_next)

    async def _run_post():
        request = _make_request_with(method="POST", path="/widgets")
        return await enforce_allowed_methods(context, request, _sentinel_call_next)

    get_response = asyncio.run(_run_get())
    post_response = asyncio.run(_run_post())

    assert get_response is _SENTINEL_RESPONSE
    assert get_response.status_code == 200
    assert post_response.status_code == 405
    assert json.loads(bytes(post_response.body))["allowed"] == ["GET"]


def test_wrap_with_route_hooks_returns_handler_when_no_hooks():
    async def handler(request):
        return PlainTextResponse("ok")

    # With an empty hook chain the original handler is returned unchanged
    # (same object, no wrapping) and still behaves like the bare handler.
    wrapped = wrap_with_route_hooks(handler, hooks=[], context=_make_context())
    assert wrapped is handler

    response = asyncio.run(wrapped(_make_request()))
    assert response.status_code == 200
    assert bytes(response.body) == b"ok"


def test_default_policy_tuples_expose_expected_hooks():
    # The page policy only wires metadata; the API policy additionally enforces
    # allowed methods.
    assert DEFAULT_PAGE_POLICIES == (attach_route_metadata,)
    assert DEFAULT_API_POLICIES == (attach_route_metadata, enforce_allowed_methods)


# ---------------------------------------------------------------------------
# Sync handler support — is_async_callable / ensure_async_handler
# ---------------------------------------------------------------------------


def test_is_async_callable_detects_handler_shapes():
    import functools

    from pyxle.devserver.route_hooks import is_async_callable

    async def async_fn(request):
        return PlainTextResponse("ok")

    def sync_fn(request):
        return PlainTextResponse("ok")

    class AsyncCallable:
        async def __call__(self, request):
            return PlainTextResponse("ok")

    class SyncCallable:
        def __call__(self, request):
            return PlainTextResponse("ok")

    assert is_async_callable(async_fn) is True
    assert is_async_callable(sync_fn) is False
    assert is_async_callable(functools.partial(async_fn)) is True
    assert is_async_callable(functools.partial(sync_fn)) is False
    assert is_async_callable(AsyncCallable()) is True
    assert is_async_callable(SyncCallable()) is False


def test_ensure_async_handler_returns_async_handler_unchanged():
    from pyxle.devserver.route_hooks import ensure_async_handler

    async def handler(request):
        return PlainTextResponse("ok")

    assert ensure_async_handler(handler) is handler


def test_wrap_with_route_hooks_threadpools_sync_handler():
    """A sync handler behind a hook chain runs in a worker thread, not on
    the event loop — the response still flows back through the chain."""

    seen: dict[str, object] = {}

    def handler(request):
        try:
            asyncio.get_running_loop()
            seen["on_loop"] = True
        except RuntimeError:
            seen["on_loop"] = False
        return PlainTextResponse("sync-ok")

    wrapped = wrap_with_route_hooks(
        handler,
        hooks=[attach_route_metadata],
        context=_make_context(target="api", allowed_methods=("GET",)),
    )

    response = asyncio.run(wrapped(_make_request()))

    assert response.status_code == 200
    assert bytes(response.body) == b"sync-ok"
    assert seen["on_loop"] is False
    assert wrapped.__name__ == "handler"


def test_wrap_with_route_hooks_accepts_partial_without_name():
    """functools.partial handlers (no __name__) wrap cleanly and fall back
    to a generic chain name instead of raising AttributeError."""

    import functools

    def handler(request, *, suffix):
        return PlainTextResponse(f"partial-{suffix}")

    wrapped = wrap_with_route_hooks(
        functools.partial(handler, suffix="ok"),
        hooks=[attach_route_metadata],
        context=_make_context(target="api", allowed_methods=("GET",)),
    )

    response = asyncio.run(wrapped(_make_request()))

    assert response.status_code == 200
    assert bytes(response.body) == b"partial-ok"
    assert wrapped.__name__ == "endpoint"


def test_head_is_allowed_wherever_get_is():
    """RFC 9110 defines HEAD as identical to GET without a body, Starlette
    routes it to the GET handler, and every link checker, health prober and
    `curl -I` assumes it. Refusing it made the framework advertise a GET route
    and answer 405 to half the clients that would use it.
    """
    context = _make_context(target="api", allowed_methods=("GET", "POST"))

    async def _run():
        request = _make_request_with(method="HEAD", path="/widgets")
        return await enforce_allowed_methods(context, request, _sentinel_call_next)

    assert asyncio.run(_run()) is _SENTINEL_RESPONSE


def test_head_is_still_refused_where_get_is_not_allowed():
    """A POST-only endpoint has no GET semantics for HEAD to mirror."""
    context = _make_context(target="api", allowed_methods=("POST",))

    async def _run():
        request = _make_request_with(method="HEAD", path="/widgets")
        return await enforce_allowed_methods(context, request, _sentinel_call_next)

    response = asyncio.run(_run())

    assert response.status_code == 405
