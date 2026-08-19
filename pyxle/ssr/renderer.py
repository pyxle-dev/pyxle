"""Component rendering helpers for server-side HTML generation."""

from __future__ import annotations

import asyncio
import inspect
import json
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, Tuple, TypeVar

from pyxle.ssr.paths import resolve_component_path
from pyxle.ssr.source_locations import remap_generated_locations


class ComponentRenderError(RuntimeError):
    """Raised when a component cannot be rendered server-side."""


#: Browser-only globals that do not exist in the Node SSR environment. A
#: ``ReferenceError`` on one of these names during a server render means the
#: component evaluated a browser API at render scope — the exact mistake
#: :class:`BrowserGlobalRenderError` explains and points at the fix for.
BROWSER_ONLY_GLOBALS: tuple[str, ...] = (
    "window",
    "document",
    "navigator",
    "localStorage",
    "sessionStorage",
    "location",
    "matchMedia",
    "requestAnimationFrame",
)

#: Matches Node's ``ReferenceError`` message shape — ``"<name> is not
#: defined"``, optionally prefixed with ``"ReferenceError:"`` — and captures
#: the offending identifier.
_REFERENCE_ERROR_RE = re.compile(
    r"(?:\bReferenceError:\s*)?\b([A-Za-z_$][A-Za-z0-9_$]*) is not defined\b"
)


#: Matches esbuild's ESM-output shim error for a CommonJS ``require()`` of a
#: module that isn't bundled — ``Dynamic require of "<name>" is not
#: supported`` — and captures the required module name.
_DYNAMIC_REQUIRE_RE = re.compile(r'Dynamic require of "([^"]+)" is not supported')


def detect_dynamic_require(message: str) -> str | None:
    """Return the module named in a "Dynamic require of X" SSR error, if any.

    A CommonJS dependency that reaches the SSR bundle and calls
    ``require("react")`` (or another runtime-provided/external module) cannot be
    linked into Pyxle's ES-module SSR output: esbuild emits a shim that throws
    ``Dynamic require of "react" is not supported`` at render time. This helper
    recognizes that shape and returns the required module name; any other
    message returns ``None`` so unrelated errors flow through unchanged.
    """
    match = _DYNAMIC_REQUIRE_RE.search(message)
    return match.group(1) if match else None


def detect_browser_only_global(message: str) -> str | None:
    """Return the browser-only global named in a Node ``ReferenceError`` message.

    Component render bodies also run in Node during SSR, where browser APIs do
    not exist — evaluating e.g. ``window.location.pathname`` at render scope
    fails with ``ReferenceError: window is not defined``. This helper
    recognizes that message shape and returns the offending name when it is
    one of :data:`BROWSER_ONLY_GLOBALS`. Any other message — including a
    ``ReferenceError`` on an unrelated identifier — returns ``None`` so those
    errors flow through unchanged.
    """
    for match in _REFERENCE_ERROR_RE.finditer(message):
        name = match.group(1)
        if name in BROWSER_ONLY_GLOBALS:
            return name
    return None


class BrowserGlobalRenderError(ComponentRenderError):
    """A render failed because a browser-only global was evaluated during SSR.

    Raised in place of a bare :class:`ComponentRenderError` when the Node
    render error is a ``ReferenceError`` on one of
    :data:`BROWSER_ONLY_GLOBALS`. The message explains why the global is
    missing on the server, names the page's ``.pyxl`` source file, and points
    at the remedy. It is surfaced in development only (error overlay, dev
    error page, server log); production HTTP responses stay generic and the
    rich message reaches the server log alone.
    """

    def __init__(self, *, global_name: str, source_file: str, original_message: str) -> None:
        """Build the enriched message from the failing global and source file.

        ``global_name`` is the browser global the component evaluated,
        ``source_file`` identifies the page's ``.pyxl`` source (e.g.
        ``pages/dashboard.pyxl``), and ``original_message`` is the Node
        runtime's raw error text, preserved verbatim at the front of the
        enriched message.
        """
        self.global_name = global_name
        self.source_file = source_file
        self.original_message = original_message
        super().__init__(
            f"{original_message}. '{global_name}' is a browser global, and browser "
            f"globals do not exist during server-side rendering: the component in "
            f"{source_file} runs in Node on the server first, where there is no "
            f"'{global_name}'. Move the browser-only code into a useEffect hook or "
            "an event handler (neither runs during the server render), or render "
            "the subtree client-only with <ClientOnly>. See the Client Components "
            "guide (docs/guides/client-components.md)."
        )


class CjsDependencyRenderError(ComponentRenderError):
    """A render failed because a CommonJS dependency did a dynamic ``require``.

    Raised in place of a bare :class:`ComponentRenderError` when the Node
    render error is esbuild's ``Dynamic require of "<module>" is not
    supported``. That happens when a dependency resolves to a CommonJS build
    that calls ``require()`` for a module Pyxle provides externally (React, or
    another runtime module) — which can't be linked into the ES-module SSR
    bundle. Pyxle prefers packages' ES-module entry points, so this almost
    always means the offending package ships *only* CommonJS. Surfaced in
    development only; production responses stay generic.
    """

    def __init__(self, *, module_name: str, source_file: str, original_message: str) -> None:
        self.module_name = module_name
        self.source_file = source_file
        self.original_message = original_message
        super().__init__(
            f"{original_message}. A CommonJS dependency imported by {source_file} "
            f"called require('{module_name}') during server-side rendering, which "
            "cannot be linked into Pyxle's ES-module SSR bundle (React and other "
            "runtime modules are provided externally, not bundled). Pyxle already "
            "prefers a package's ES-module build, so this usually means the "
            "package ships CommonJS only. Use a version/package that provides an "
            "ES module, or render the subtree client-only with <ClientOnly> so it "
            "never runs during the server render. See the Client Components guide "
            "(docs/guides/client-components.md)."
        )


@dataclass(frozen=True)
class InlineStyleFragment:
    """Inline CSS artifact emitted by the SSR runtime."""

    identifier: str
    contents: str
    source: str | None = None


@dataclass(frozen=True)
class RenderResult:
    """Normalized payload returned by component renderers."""

    html: str
    inline_styles: tuple[InlineStyleFragment, ...] = ()
    head_elements: tuple[str, ...] = ()



RenderOutput = RenderResult | str
# Render callables receive the serialized props dict and may also accept
# a ``request_pathname`` keyword argument — SSR forwards it so hooks such
# as ``usePathname`` return the request's real path instead of a fallback.
# Callables that only accept ``props`` are still supported for backward
# compatibility (see ``_invoke_render``).
_RenderCallable = Callable[..., Awaitable[RenderOutput] | RenderOutput]
_FactoryReturn = Awaitable[_RenderCallable] | _RenderCallable
_RenderFactory = Callable[[Path], _FactoryReturn]

_T = TypeVar("_T")


def _ensure_awaitable(value: Awaitable[_T] | _T) -> Awaitable[_T]:
    if asyncio.iscoroutine(value) or isinstance(value, Awaitable):
        return value  # type: ignore[return-value]

    async def _wrapper() -> _T:
        return value  # type: ignore[misc]

    return _wrapper()


class ComponentRenderer:
    """Cache-aware wrapper around the internal component rendering runtime."""

    def __init__(self, *, factory: _RenderFactory | None = None) -> None:
        self._factory: _RenderFactory = factory or _default_factory
        self._cache: Dict[Path, Tuple[float, _RenderCallable]] = {}
        self._lock = asyncio.Lock()
        self._generation = 0

    async def render(
        self,
        component_path: Path,
        props: Dict[str, Any],
        *,
        request_pathname: str | None = None,
        csrf_token: str | None = None,
    ) -> RenderResult:
        """Render ``component_path`` with the provided props.

        ``request_pathname`` is forwarded to the SSR runtime and exposed
        to component code via ``globalThis.__PYXLE_CURRENT_PATHNAME__``
        during rendering, so hooks like ``usePathname`` return the
        request's actual path and hydrate without mismatches.

        ``csrf_token`` is exposed via ``globalThis.__PYXLE_CSRF_TOKEN__``
        for the same render. ``<Form>`` reads it at SSR time so the
        rendered HTML carries a hidden ``_csrf_token`` field — that's
        what makes a no-JS form POST satisfy the CSRF middleware.
        """

        # Memoised: this runs on every render, and a raw ``resolve()`` here is
        # ~18.7us of on-CPU event-loop stall per call. See pyxle.ssr.paths.
        resolved = resolve_component_path(component_path)
        cached = self._cache.get(resolved)

        if cached is None or cached[0] != self._generation:
            async with self._lock:
                cached = self._cache.get(resolved)
                if cached is None or cached[0] != self._generation:
                    render_fn = await _ensure_awaitable(self._factory(resolved))
                    cached = (self._generation, render_fn)
                    self._cache[resolved] = cached

        _, render_fn = cached
        result = _invoke_render(
            render_fn,
            props,
            request_pathname=request_pathname,
            csrf_token=csrf_token,
        )
        resolved_result = await _ensure_awaitable(result)
        return _normalize_render_output(resolved_result)

    def clear(self) -> None:
        """Drop all cached component renderers."""

        self._generation += 1
        self._cache.clear()


def _invoke_render(
    render_fn: _RenderCallable,
    props: Dict[str, Any],
    *,
    request_pathname: str | None,
    csrf_token: str | None = None,
) -> Any:
    """Call *render_fn* with the right signature for its parameters.

    Render callables returned by the built-in factories accept optional
    ``request_pathname`` and ``csrf_token`` keyword arguments. Third-party
    callables (and tests written before these parameters existed) may
    accept only ``props``. Introspection lets us preserve both — we check
    the signature once per call and pass each keyword only when accepted.
    """
    try:
        sig = inspect.signature(render_fn)
    except (TypeError, ValueError):
        # Builtins / C-extension callables — just pass props positionally.
        return render_fn(props)

    accepted_kwargs: dict[str, str | None] = {}
    has_var_keyword = False
    accepts: dict[str, bool] = {"request_pathname": False, "csrf_token": False}

    for param in sig.parameters.values():
        if param.kind == inspect.Parameter.VAR_KEYWORD:
            has_var_keyword = True
            continue
        if param.name in accepts and param.kind in (
            inspect.Parameter.KEYWORD_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
        ):
            accepts[param.name] = True

    if has_var_keyword or accepts["request_pathname"]:
        accepted_kwargs["request_pathname"] = request_pathname
    if has_var_keyword or accepts["csrf_token"]:
        accepted_kwargs["csrf_token"] = csrf_token

    if accepted_kwargs:
        return render_fn(props, **accepted_kwargs)
    return render_fn(props)


def _default_factory(component_path: Path) -> _RenderCallable:
    runtime = _NodeComponentRuntime(component_path)

    async def _render(
        props: Dict[str, Any],
        *,
        request_pathname: str | None = None,
        csrf_token: str | None = None,
    ) -> RenderResult:
        return await asyncio.to_thread(
            runtime.render,
            props,
            request_pathname=request_pathname,
            csrf_token=csrf_token,
        )

    return _render


class _NodeComponentRuntime:
    def __init__(self, component_path: Path) -> None:
        self._component_path = resolve_component_path(component_path)
        self._client_root, self._project_root = _derive_project_paths(self._component_path)
        self._node_executable = _resolve_node_executable()
        self._runtime_script = _resolve_runtime_script()

    def render(
        self,
        props: Dict[str, Any],
        *,
        request_pathname: str | None = None,
        csrf_token: str | None = None,
    ) -> RenderResult:
        try:
            serialized_props = json.dumps(props, ensure_ascii=False, separators=(",", ":"))
        except (TypeError, ValueError) as exc:
            raise ComponentRenderError(
                f"Unable to serialize props for component '{self._component_path.name}'"
            ) from exc

        command = [
            self._node_executable,
            str(self._runtime_script),
            str(self._component_path),
            serialized_props,
            str(self._client_root),
            str(self._project_root),
        ]

        from pyxle.ssr.worker_pool import _build_node_env

        env = _build_node_env(self._project_root)
        # The Node runtime reads the pathname / csrf token from these env
        # vars and sets ``globalThis.__PYXLE_CURRENT_PATHNAME__`` /
        # ``globalThis.__PYXLE_CSRF_TOKEN__`` before invoking the page
        # component. Using env vars (rather than extra argv slots) keeps
        # the subprocess command signature stable.
        if request_pathname is not None:
            env["PYXLE_REQUEST_PATHNAME"] = request_pathname
        if csrf_token is not None:
            env["PYXLE_CSRF_TOKEN"] = csrf_token

        try:
            process = subprocess.run(  # noqa: S603 - controlled command invocation
                command,
                cwd=str(self._project_root),
                capture_output=True,
                text=True,
                # Pin UTF-8 so the SSR transport never depends on the system
                # locale: under a non-UTF-8 locale (LANG=C, common on minimal
                # Linux/CI) the default codec is ASCII and any astral character
                # (an emoji in a component or prop) crashes decode/encode.
                encoding="utf-8",
                check=False,
                env=env,
                timeout=30,
            )
        except subprocess.TimeoutExpired as exc:
            raise ComponentRenderError(
                f"SSR render timed out after 30s for '{self._component_path.name}'"
            ) from exc

        if process.returncode not in (0, None):
            raise ComponentRenderError(_format_node_error(process))

        payload = _parse_runtime_output(process.stdout)
        if not payload.get("ok"):
            message = payload.get("message") or "SSR runtime reported a failure"
            raise ComponentRenderError(message)

        html = payload.get("html")
        if not isinstance(html, str):
            raise ComponentRenderError("SSR runtime returned malformed HTML payload")

        inline_styles = _parse_inline_styles(payload.get("styles"))
        head_elements = _parse_head_elements(payload.get("headElements"))

        return RenderResult(html=html, inline_styles=inline_styles, head_elements=head_elements)


def _parse_runtime_output(raw: str) -> dict[str, Any]:
    try:
        payload = (raw or "{}").strip()
        if not payload:
            return {}
        return json.loads(payload)
    except json.JSONDecodeError as exc:
        last_brace = payload.rfind("{")
        if last_brace > 0:
            snippet = payload[last_brace:]
            try:
                return json.loads(snippet)
            except json.JSONDecodeError:
                pass
        raise ComponentRenderError("Unable to parse SSR runtime response") from exc


def _format_node_error(process: subprocess.CompletedProcess[str]) -> str:
    stderr = (process.stderr or "").strip()
    if stderr:
        try:
            payload = json.loads(stderr)
            message = payload.get("message")
            if message:
                return message
        except json.JSONDecodeError:
            return stderr
    return "SSR runtime failed to execute"


def _derive_project_paths(component_path: Path) -> Tuple[Path, Path]:
    for ancestor in component_path.parents:
        if ancestor.name == "client" and ancestor.parent.name == ".pyxle-build":
            client_root = ancestor
            project_root = ancestor.parent.parent
            return client_root, project_root
    raise ComponentRenderError(
        f"Component '{component_path}' is not inside a '.pyxle-build/client' directory"
    )


def _resolve_node_executable() -> str:
    node_exec = shutil.which("node")
    if not node_exec:
        raise ComponentRenderError(
            "Node.js executable not found. Install Node to enable server-side rendering."
        )
    return node_exec


def _resolve_runtime_script() -> Path:
    script_path = Path(__file__).with_name("render_component.mjs")
    if not script_path.exists():
        raise ComponentRenderError("SSR runtime script is missing from the installation")
    return script_path

def _normalize_render_output(value: RenderOutput) -> RenderResult:
    if isinstance(value, RenderResult):
        return value
    if isinstance(value, str):
        return RenderResult(html=value)
    raise ComponentRenderError("Renderer returned unsupported payload type")


def _parse_inline_styles(raw: Any) -> tuple[InlineStyleFragment, ...]:
    if not isinstance(raw, list):
        return ()

    fragments: list[InlineStyleFragment] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        identifier = entry.get("identifier")
        contents = entry.get("contents")
        source = entry.get("source")
        if not isinstance(identifier, str) or not isinstance(contents, str):
            continue
        if source is not None and not isinstance(source, str):
            source = None
        fragments.append(
            InlineStyleFragment(
                identifier=identifier,
                contents=contents,
                source=source,
            )
        )
    return tuple(fragments)


def _parse_head_elements(raw: Any) -> tuple[str, ...]:
    """Parse head elements extracted from React components during SSR."""
    if not isinstance(raw, list):
        return ()
    
    elements: list[str] = []
    for entry in raw:
        if isinstance(entry, str) and entry.strip():
            elements.append(entry.strip())
    return tuple(elements)


def pool_render_factory(pool: Any) -> _RenderFactory:
    """Return a render factory backed by a persistent :class:`~pyxle.ssr.worker_pool.SsrWorkerPool`.

    Pass the returned factory to :class:`ComponentRenderer` to use the worker
    pool instead of spawning a new Node.js process per request::

        from pyxle.ssr.worker_pool import SsrWorkerPool
        pool = SsrWorkerPool(size=2, project_root=root, client_root=client)
        renderer = ComponentRenderer(factory=pool_render_factory(pool))
        await pool.start()
    """
    from pyxle.ssr.worker_pool import WorkerPoolError

    def factory(component_path: Path) -> _RenderCallable:
        async def _render(
            props: Dict[str, Any],
            *,
            request_pathname: str | None = None,
            csrf_token: str | None = None,
        ) -> RenderResult:
            try:
                # Validate JSON-serializability without a redundant round-trip.
                json.dumps(props, ensure_ascii=False, separators=(",", ":"))
            except (TypeError, ValueError) as exc:
                raise ComponentRenderError(
                    f"Unable to serialize props for component '{component_path.name}'"
                ) from exc

            try:
                result = await pool.render(
                    component_path,
                    props,
                    request_pathname=request_pathname,
                    csrf_token=csrf_token,
                )
            except WorkerPoolError as exc:
                raise ComponentRenderError(
                    remap_generated_locations(str(exc), pool.client_root)
                ) from exc

            if not result.get("ok"):
                message = result.get("message") or "SSR worker reported a failure"
                # esbuild/Babel name the generated ``.jsx`` module, at a line
                # number that belongs to it and not to the author's file. Report
                # the ``.pyxl`` they actually edit.
                raise ComponentRenderError(
                    remap_generated_locations(message, pool.client_root)
                )

            html = result.get("html")
            if not isinstance(html, str):
                raise ComponentRenderError("SSR worker returned malformed HTML payload")

            return RenderResult(
                html=html,
                inline_styles=_parse_inline_styles(result.get("styles")),
                head_elements=_parse_head_elements(result.get("headElements")),
            )

        return _render

    return factory


__all__ = [
    "BROWSER_ONLY_GLOBALS",
    "BrowserGlobalRenderError",
    "ComponentRenderError",
    "ComponentRenderer",
    "InlineStyleFragment",
    "RenderResult",
    "detect_browser_only_global",
    "detect_dynamic_require",
    "CjsDependencyRenderError",
    "pool_render_factory",
]
