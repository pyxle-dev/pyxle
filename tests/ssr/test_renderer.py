from __future__ import annotations

import asyncio
import shutil
from pathlib import Path
from subprocess import CompletedProcess
from textwrap import dedent

import pytest

import pyxle.ssr.renderer as renderer_module
from pyxle.ssr.renderer import (
    BROWSER_ONLY_GLOBALS,
    BrowserGlobalRenderError,
    ComponentRenderer,
    ComponentRenderError,
    RenderResult,
    _derive_project_paths,
    _format_node_error,
    _parse_runtime_output,
    CjsDependencyRenderError,
    detect_browser_only_global,
    detect_dynamic_require,
)
from tests.ssr.utils import ensure_test_node_modules


@pytest.fixture
def anyio_backend() -> str:  # pragma: no cover - fixture wiring
    return "asyncio"


@pytest.mark.anyio
async def test_renderer_caches_component(tmp_path: Path) -> None:
    calls: list[Path] = []

    async def factory(path: Path):
        calls.append(path)

        async def render(props):
            return RenderResult(html=f"rendered:{props['value']}")

        return render

    renderer = ComponentRenderer(factory=factory)

    component = tmp_path / "component.jsx"
    component.write_text("export default () => null;\n", encoding="utf-8")

    first = await renderer.render(component, {"value": "a"})
    second = await renderer.render(component, {"value": "a"})

    assert first.html == "rendered:a"
    assert second.html == "rendered:a"
    assert calls == [component.resolve()]


@pytest.mark.anyio
async def test_renderer_deduplicates_concurrent_factory_invocations(tmp_path: Path) -> None:
    component = tmp_path / "race.jsx"
    component.write_text("export default () => null;\n", encoding="utf-8")

    factory_calls = 0
    factory_started = asyncio.Event()
    allow_finish = asyncio.Event()

    async def factory(path: Path):
        nonlocal factory_calls
        factory_calls += 1
        factory_started.set()

        await allow_finish.wait()

        async def render(props):
            return RenderResult(html="ok")

        return render

    renderer = ComponentRenderer(factory=factory)

    async def invoke():
        return await renderer.render(component, {})

    first = asyncio.create_task(invoke())
    await factory_started.wait()
    second = asyncio.create_task(invoke())
    await asyncio.sleep(0)
    allow_finish.set()

    assert (await first).html == "ok"
    assert (await second).html == "ok"
    assert factory_calls == 1


@pytest.mark.anyio
async def test_renderer_forwards_request_pathname_to_new_factories(tmp_path: Path) -> None:
    """Render callables declaring ``request_pathname`` receive it."""
    seen: list[str | None] = []

    async def factory(path: Path):
        async def render(props, *, request_pathname=None):
            seen.append(request_pathname)
            return RenderResult(html="ok")

        return render

    renderer = ComponentRenderer(factory=factory)
    component = tmp_path / "c.jsx"
    component.write_text("export default () => null;\n", encoding="utf-8")

    await renderer.render(component, {}, request_pathname="/dashboard")
    await renderer.render(component, {}, request_pathname="/settings")
    await renderer.render(component, {})  # no pathname given

    assert seen == ["/dashboard", "/settings", None]


@pytest.mark.anyio
async def test_renderer_forwards_csrf_token_to_factories(tmp_path: Path) -> None:
    """Render callables declaring ``csrf_token`` receive the SSR-time
    token so ``<Form>`` can embed it as a hidden input. Pathname and
    csrf flow through the same kwargs in tandem."""
    seen: list[tuple[str | None, str | None]] = []

    async def factory(path: Path):
        async def render(props, *, request_pathname=None, csrf_token=None):
            seen.append((request_pathname, csrf_token))
            return RenderResult(html="ok")

        return render

    renderer = ComponentRenderer(factory=factory)
    component = tmp_path / "c.jsx"
    component.write_text("export default () => null;\n", encoding="utf-8")

    await renderer.render(component, {}, request_pathname="/x", csrf_token="tok-1")
    await renderer.render(component, {}, csrf_token="tok-2")
    await renderer.render(component, {})  # neither

    assert seen == [("/x", "tok-1"), (None, "tok-2"), (None, None)]


@pytest.mark.anyio
async def test_renderer_passes_csrf_only_when_factory_accepts_it(tmp_path: Path) -> None:
    """Legacy factories accepting only ``request_pathname`` still work —
    we don't crash by passing an unexpected ``csrf_token`` kwarg."""
    seen: list[str | None] = []

    async def factory(path: Path):
        async def render(props, *, request_pathname=None):  # no csrf_token
            seen.append(request_pathname)
            return RenderResult(html="ok")

        return render

    renderer = ComponentRenderer(factory=factory)
    component = tmp_path / "c.jsx"
    component.write_text("export default () => null;\n", encoding="utf-8")

    await renderer.render(component, {}, request_pathname="/x", csrf_token="tok-ignored")
    assert seen == ["/x"]


@pytest.mark.anyio
async def test_renderer_omits_pathname_for_legacy_factories(tmp_path: Path) -> None:
    """Callables without ``request_pathname`` still work (back-compat)."""
    call_count = 0

    async def factory(path: Path):
        async def render(props):   # old signature, no request_pathname
            nonlocal call_count
            call_count += 1
            return RenderResult(html="legacy")

        return render

    renderer = ComponentRenderer(factory=factory)
    component = tmp_path / "legacy.jsx"
    component.write_text("export default () => null;\n", encoding="utf-8")

    result = await renderer.render(component, {}, request_pathname="/foo")
    assert result.html == "legacy"
    assert call_count == 1


@pytest.mark.anyio
async def test_renderer_supports_sync_factory(tmp_path: Path) -> None:
    component = tmp_path / "view.jsx"
    component.write_text("export default () => null;\n", encoding="utf-8")

    def factory(path: Path):
        def render(props):
            return f"sync:{props.get('value', '0')}"

        return render

    renderer = ComponentRenderer(factory=factory)
    result = await renderer.render(component, {"value": "42"})
    assert result.html == "sync:42"


@pytest.mark.anyio
@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is required for SSR rendering tests")
async def test_renderer_default_factory_produces_html(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    component = project_root / ".pyxle-build" / "client" / "pages" / "fallback.jsx"
    component.parent.mkdir(parents=True, exist_ok=True)
    stylesheet = component.parent / "styles" / "fallback.css"
    stylesheet.parent.mkdir(parents=True, exist_ok=True)
    stylesheet.write_text(
        ".hero { color: red; }\n",
        encoding="utf-8",
    )

    component.write_text(
        dedent(
            """
            import React from 'react';
            import './styles/fallback.css';

            export default function Fallback({ count }) {
                return <section data-count={count}>Count: {count}</section>;
            }
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )

    ensure_test_node_modules(project_root)

    renderer = ComponentRenderer()
    result = await renderer.render(component, {"count": 3})

    assert "<section" in result.html
    assert "data-count=\"3\"" in result.html
    assert "Count:" in result.html
    assert "3</section>" in result.html
    assert result.inline_styles
    inline_style = result.inline_styles[0]
    assert inline_style.contents.strip().startswith(".hero")
    assert inline_style.identifier.startswith("pyxle-inline-style-")
    assert inline_style.source and inline_style.source.endswith("styles/fallback.css")


@pytest.mark.anyio
@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is required for SSR rendering tests")
async def test_renderer_css_module_emits_scoped_class(tmp_path: Path) -> None:
    """A ``*.module.css`` import must render deterministic scoped class names on
    the server so they match Vite's client output — otherwise React 19 flags a
    hydration mismatch on the default (no-Tailwind) scaffold."""
    project_root = tmp_path / "project"
    component = project_root / ".pyxle-build" / "client" / "pages" / "card.jsx"
    component.parent.mkdir(parents=True, exist_ok=True)
    module_css = component.parent / "styles" / "card.module.css"
    module_css.parent.mkdir(parents=True, exist_ok=True)
    module_css.write_text(".box { color: red; }\n", encoding="utf-8")

    component.write_text(
        dedent(
            """
            import React from 'react';
            import styles from './styles/card.module.css';

            export default function Card() {
                return <div className={styles.box}>hi</div>;
            }
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )

    ensure_test_node_modules(project_root)

    renderer = ComponentRenderer()
    result = await renderer.render(component, {})

    # Scoped, not the raw local name — basename_local_<hash>.
    assert 'class="card_box_' in result.html
    assert 'class="box"' not in result.html


@pytest.mark.anyio
@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is required for SSR rendering tests")
async def test_renderer_substitutes_public_env_during_ssr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``import.meta.env.PYXLE_PUBLIC_*`` must resolve during SSR (F4).

    Vite substitutes these public env vars into the client bundle. Without a
    matching esbuild ``define`` on the server, the expression rendered as
    ``undefined`` -> a blank first paint and a hydration mismatch. The SSR
    build must now bake the same value the client will see.
    """
    monkeypatch.setenv("PYXLE_PUBLIC_API_URL", "https://api.example.com")

    project_root = tmp_path / "project"
    component = project_root / ".pyxle-build" / "client" / "pages" / "env.jsx"
    component.parent.mkdir(parents=True, exist_ok=True)
    component.write_text(
        dedent(
            """
            import React from 'react';

            export default function EnvProbe() {
                return <span data-api={import.meta.env.PYXLE_PUBLIC_API_URL}>ok</span>;
            }
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )

    ensure_test_node_modules(project_root)

    renderer = ComponentRenderer()
    result = await renderer.render(component, {})

    # The value is baked in -- not "undefined" -- so server and client agree.
    assert 'data-api="https://api.example.com"' in result.html
    assert "undefined" not in result.html


@pytest.mark.anyio
async def test_renderer_clear_resets_cache(tmp_path: Path) -> None:
    calls = 0

    def factory(path: Path):
        nonlocal calls
        calls += 1

        def render(props):
            return "ok"

        return render

    renderer = ComponentRenderer(factory=factory)

    component = tmp_path / "component.jsx"

    await renderer.render(component, {})
    await renderer.render(component, {})
    renderer.clear()
    await renderer.render(component, {})

    assert calls == 2
    await renderer.render(component, {})
    assert calls == 2


@pytest.mark.anyio
async def test_renderer_raises_on_unserializable_props(tmp_path: Path) -> None:
    renderer = ComponentRenderer()

    component = tmp_path / "component.jsx"
    component.write_text("export default () => null;\n", encoding="utf-8")

    async def stub_factory(path: Path):
        raise ComponentRenderError("boom")

    renderer._factory = lambda path: stub_factory(path)  # type: ignore[assignment]

    with pytest.raises(ComponentRenderError):
        await renderer.render(component, {"value": object()})


@pytest.mark.anyio
async def test_default_factory_invokes_runtime(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    component = tmp_path / "project" / ".pyxle-build" / "client" / "pages" / "demo.jsx"
    component.parent.mkdir(parents=True, exist_ok=True)

    class FakeRuntime:
        def __init__(self, path: Path) -> None:
            self.path = path

        def render(
            self,
            props: dict[str, object],
            *,
            request_pathname: str | None = None,
            csrf_token: str | None = None,
        ) -> RenderResult:
            return RenderResult(html=f"rendered:{props['value']}:{self.path.name}")

    monkeypatch.setattr(renderer_module, "_NodeComponentRuntime", FakeRuntime)

    render_fn = renderer_module._default_factory(component)
    result = await render_fn({"value": "ok"})
    assert result.html == "rendered:ok:demo.jsx"


def test_parse_runtime_output_invalid_json() -> None:
    with pytest.raises(ComponentRenderError):
        _parse_runtime_output("not-json")


def test_parse_runtime_output_with_console_logs() -> None:
    payload = "console log\n" '{"ok": true, "html": "<div></div>"}'
    result = _parse_runtime_output(payload)
    assert result["ok"] is True
    assert result["html"] == "<div></div>"


def test_parse_runtime_output_empty_payload_returns_empty_dict() -> None:
    assert _parse_runtime_output("   \n") == {}


def test_parse_runtime_output_invalid_snippet_raises() -> None:
    noisy = "log output {\"ok\": false"
    with pytest.raises(ComponentRenderError):
        _parse_runtime_output(noisy)


def test_parse_inline_styles_handles_non_list_payload() -> None:
    assert renderer_module._parse_inline_styles({}) == ()


def test_parse_inline_styles_filters_invalid_entries() -> None:
    payload = [
        {"identifier": "ok", "contents": "body", "source": 123},
        {"identifier": None, "contents": "missing"},
        "not-a-dict",
    ]
    fragments = renderer_module._parse_inline_styles(payload)
    assert len(fragments) == 1
    fragment = fragments[0]
    assert fragment.identifier == "ok"
    assert fragment.contents == "body"
    assert fragment.source is None


def test_format_node_error_prefers_json_message() -> None:
    process = CompletedProcess(args=["node"], returncode=1, stdout="", stderr='{"message": "boom"}')
    assert _format_node_error(process) == "boom"


def test_format_node_error_handles_json_without_message() -> None:
    process = CompletedProcess(args=["node"], returncode=1, stdout="", stderr='{"detail": "??"}')
    assert _format_node_error(process) == "SSR runtime failed to execute"


def test_format_node_error_returns_stderr_when_not_json() -> None:
    process = CompletedProcess(args=["node"], returncode=1, stdout="", stderr="plain failure")
    assert _format_node_error(process) == "plain failure"


def test_format_node_error_returns_default_when_empty() -> None:
    process = CompletedProcess(args=["node"], returncode=1, stdout="", stderr="")
    assert _format_node_error(process) == "SSR runtime failed to execute"


def test_derive_project_paths_errors_outside_client(tmp_path: Path) -> None:
    component = tmp_path / "pages" / "index.jsx"
    component.parent.mkdir(parents=True, exist_ok=True)
    component.write_text("export default () => null;\n", encoding="utf-8")

    with pytest.raises(ComponentRenderError):
        _derive_project_paths(component)


def test_node_runtime_surfaces_process_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project_root = tmp_path / "project"
    component = project_root / ".pyxle-build" / "client" / "pages" / "demo.jsx"
    component.parent.mkdir(parents=True, exist_ok=True)
    component.write_text("export default () => null;\n", encoding="utf-8")

    monkeypatch.setattr(renderer_module, "_resolve_node_executable", lambda: "node")
    monkeypatch.setattr(renderer_module, "_resolve_runtime_script", lambda: project_root / "runtime.mjs")

    class DummyProcess:
        returncode = 1
        stdout = ""
        stderr = "boom"

    monkeypatch.setattr(renderer_module.subprocess, "run", lambda *args, **kwargs: DummyProcess())

    runtime = renderer_module._NodeComponentRuntime(component)

    with pytest.raises(ComponentRenderError, match="boom"):
        runtime.render({})


def test_node_runtime_serialization_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    component = tmp_path / "project" / ".pyxle-build" / "client" / "pages" / "serialize.jsx"
    component.parent.mkdir(parents=True, exist_ok=True)
    component.write_text("export default () => null;\n", encoding="utf-8")

    monkeypatch.setattr(renderer_module, "_resolve_node_executable", lambda: "node")
    monkeypatch.setattr(renderer_module, "_resolve_runtime_script", lambda: component)

    runtime = renderer_module._NodeComponentRuntime(component)

    with pytest.raises(ComponentRenderError, match="Unable to serialize props"):
        runtime.render({"value": object()})


def test_node_runtime_success_returns_html(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    component = tmp_path / "project" / ".pyxle-build" / "client" / "pages" / "success.jsx"
    component.parent.mkdir(parents=True, exist_ok=True)
    component.write_text("export default () => null;\n", encoding="utf-8")

    monkeypatch.setattr(renderer_module, "_resolve_node_executable", lambda: "node")
    monkeypatch.setattr(renderer_module, "_resolve_runtime_script", lambda: component)

    class DummyProcess:
        returncode = 0
        stdout = '{"ok": true, "html": "<section>ok</section>"}'
        stderr = ""

    monkeypatch.setattr(renderer_module.subprocess, "run", lambda *args, **kwargs: DummyProcess())

    runtime = renderer_module._NodeComponentRuntime(component)
    result = runtime.render({})
    assert isinstance(result, RenderResult)
    assert result.html == "<section>ok</section>"
    assert result.inline_styles == ()


def test_node_runtime_payload_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    component = tmp_path / "project" / ".pyxle-build" / "client" / "pages" / "payload.jsx"
    component.parent.mkdir(parents=True, exist_ok=True)
    component.write_text("export default () => null;\n", encoding="utf-8")

    monkeypatch.setattr(renderer_module, "_resolve_node_executable", lambda: "node")
    monkeypatch.setattr(renderer_module, "_resolve_runtime_script", lambda: component)

    class DummyProcess:
        returncode = 0
        stdout = '{"ok": false, "message": "bad"}'
        stderr = ""

    monkeypatch.setattr(renderer_module.subprocess, "run", lambda *args, **kwargs: DummyProcess())

    runtime = renderer_module._NodeComponentRuntime(component)

    with pytest.raises(ComponentRenderError, match="bad"):
        runtime.render({})


def test_resolve_node_executable_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(renderer_module.shutil, "which", lambda _: None)

    with pytest.raises(ComponentRenderError):
        renderer_module._resolve_node_executable()


def test_resolve_runtime_script_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    original_exists = renderer_module.Path.exists

    def fake_exists(self: Path) -> bool:  # type: ignore[override]
        if self.name == "render_component.mjs":
            return False
        return original_exists(self)

    monkeypatch.setattr(renderer_module.Path, "exists", fake_exists, raising=True)

    with pytest.raises(ComponentRenderError):
        renderer_module._resolve_runtime_script()


def test_node_runtime_rejects_non_string_html(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    component = tmp_path / "project" / ".pyxle-build" / "client" / "pages" / "nonstring.jsx"
    component.parent.mkdir(parents=True, exist_ok=True)
    component.write_text("export default () => null;\n", encoding="utf-8")

    monkeypatch.setattr(renderer_module, "_resolve_node_executable", lambda: "node")
    monkeypatch.setattr(renderer_module, "_resolve_runtime_script", lambda: component)

    class DummyProcess:
        returncode = 0
        stdout = '{"ok": true, "html": 42}'
        stderr = ""

    monkeypatch.setattr(renderer_module.subprocess, "run", lambda *args, **kwargs: DummyProcess())

    runtime = renderer_module._NodeComponentRuntime(component)

    with pytest.raises(ComponentRenderError):
        runtime.render({})


# ---------------------------------------------------------------------------
# Browser-global ReferenceError detection
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", BROWSER_ONLY_GLOBALS)
def test_detect_browser_only_global_matches_each_global(name: str) -> None:
    """Every browser-only global's bare ReferenceError message is detected."""
    assert detect_browser_only_global(f"{name} is not defined") == name


def test_detect_browser_only_global_accepts_reference_error_prefix() -> None:
    """The full Node message shape with the ReferenceError prefix is detected."""
    assert detect_browser_only_global("ReferenceError: window is not defined") == "window"


def test_detect_browser_only_global_matches_inside_larger_message() -> None:
    """A ReferenceError embedded in surrounding runtime text is still found."""
    message = "Render failed: localStorage is not defined\n    at Page (pages/index.jsx:3)"
    assert detect_browser_only_global(message) == "localStorage"


@pytest.mark.parametrize(
    "message",
    [
        "myHelper is not defined",
        "ReferenceError: fetchData is not defined",
        "SSR runtime failed to execute",
        "window.matchMedia is not a function",
        "thewindow is not defined",
        "",
    ],
)
def test_detect_browser_only_global_ignores_unrelated_errors(message: str) -> None:
    """Unrelated errors — including ReferenceErrors on other names — return None."""
    assert detect_browser_only_global(message) is None


def test_browser_global_render_error_message_and_attributes() -> None:
    """The enriched error names the source file, the global, and the remedy."""
    error = BrowserGlobalRenderError(
        global_name="window",
        source_file="pages/dashboard.pyxl",
        original_message="window is not defined",
    )

    assert isinstance(error, ComponentRenderError)
    assert error.global_name == "window"
    assert error.source_file == "pages/dashboard.pyxl"
    assert error.original_message == "window is not defined"

    message = str(error)
    assert message.startswith("window is not defined")
    assert "pages/dashboard.pyxl" in message
    assert "browser global" in message
    assert "server-side rendering" in message
    assert "useEffect" in message
    assert "event handler" in message
    assert "<ClientOnly>" in message
    assert "docs/guides/client-components.md" in message


# --------------------------------------------------------------------------- #
# CJS dynamic-require detection (a CommonJS dep require()'d react during SSR)  #
# --------------------------------------------------------------------------- #


def test_detect_dynamic_require_matches_module_name() -> None:
    assert detect_dynamic_require('Dynamic require of "react" is not supported') == "react"


def test_detect_dynamic_require_matches_inside_larger_message() -> None:
    msg = 'ComponentRenderError: Dynamic require of "react-dom" is not supported\n at foo'
    assert detect_dynamic_require(msg) == "react-dom"


@pytest.mark.parametrize(
    "message",
    ["window is not defined", "some other failure", "require is not defined", ""],
)
def test_detect_dynamic_require_ignores_unrelated_errors(message: str) -> None:
    assert detect_dynamic_require(message) is None


def test_cjs_dependency_render_error_message_and_attributes() -> None:
    error = CjsDependencyRenderError(
        module_name="react",
        source_file="pages/index.pyxl",
        original_message='Dynamic require of "react" is not supported',
    )
    assert isinstance(error, ComponentRenderError)
    assert error.module_name == "react"
    assert error.source_file == "pages/index.pyxl"
    message = str(error)
    assert "require('react')" in message
    assert "pages/index.pyxl" in message
    assert "ES module" in message
    assert "<ClientOnly>" in message


class TestNamingTheComponentThatRaised:
    """A runtime SSR failure used to name a variable and nothing else.

    ``NoSuchThing is not defined`` carries no position, so the location remap
    that handles build errors has nothing to work on. The Node stack does know
    which component was executing — but it points into a content-hashed bundle
    under ``.pyxle-build/.ssr-tmp/`` that has no source map, so the line number
    is not recoverable. The function name is, and it survives bundling.
    """

    # A real stack, captured from a failing render rather than invented.
    REAL_STACK = (
        "ReferenceError: NoSuchThing is not defined\n"
        "    at JsxErr (file:///app/.pyxle-build/.ssr-tmp/worker-RFdpNf/"
        "a2b51051edb22a59d06b4b50df262f262699c7b2.mjs?v=e96352:68:120)\n"
        "    at Object.react_stack_bottom_frame (/app/node_modules/react-dom/cjs/"
        "react-dom-server-legacy.node.development.js:9808:18)\n"
        "    at renderWithHooks (/app/node_modules/react-dom/cjs/"
        "react-dom-server-legacy.node.development.js:5062:19)"
    )

    def test_it_names_the_authors_component(self):
        from pyxle.ssr.renderer import _authored_frame_name

        assert _authored_frame_name(self.REAL_STACK) == "JsxErr"

    def test_react_internals_are_never_named(self):
        """The failure happens *inside* React, so the first frames after the
        author's are always react-dom. Naming one of those would point the
        reader at a file in node_modules they cannot fix."""
        from pyxle.ssr.renderer import _authored_frame_name

        react_only = (
            "ReferenceError: boom\n"
            "    at renderWithHooks (/app/node_modules/react-dom/cjs/x.js:1:1)\n"
            "    at renderElement (/app/node_modules/react-dom/cjs/x.js:2:2)"
        )
        assert _authored_frame_name(react_only) is None

    def test_node_internals_are_never_named(self):
        from pyxle.ssr.renderer import _authored_frame_name

        assert _authored_frame_name("    at run (node:internal/modules/x:1:1)") is None

    def test_an_unparseable_stack_gives_no_answer(self):
        """An absent answer beats a confident wrong one."""
        from pyxle.ssr.renderer import _authored_frame_name

        assert _authored_frame_name("") is None
        assert _authored_frame_name("ReferenceError: boom") is None


def test_node_runtime_sends_props_over_stdin_not_argv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Loader output must never reach the subprocess command line.

    ``/proc/<pid>/cmdline`` is world-readable on Linux, so props in argv publish
    whatever the page's loader returned - session tokens, user records - to every
    local user for the life of the render. Asserting on the *transport* rather
    than on a rendered string is what makes this bite: a regression that moves
    props back into argv still renders perfectly.
    """
    component = tmp_path / "project" / ".pyxle-build" / "client" / "pages" / "stdin.jsx"
    component.parent.mkdir(parents=True, exist_ok=True)
    component.write_text("export default () => null;\n", encoding="utf-8")

    monkeypatch.setattr(renderer_module, "_resolve_node_executable", lambda: "node")
    monkeypatch.setattr(renderer_module, "_resolve_runtime_script", lambda: component)

    captured: dict[str, object] = {}

    class DummyProcess:
        returncode = 0
        stdout = '{"ok": true, "html": "<p>ok</p>"}'
        stderr = ""

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured["input"] = kwargs.get("input")
        return DummyProcess()

    monkeypatch.setattr(renderer_module.subprocess, "run", fake_run)

    secret = "session-token-do-not-leak"
    renderer_module._NodeComponentRuntime(component).render({"session": secret})

    command = captured["command"]
    assert isinstance(command, list)
    assert not any(secret in part for part in command), (
        f"props leaked onto the command line: {command!r}"
    )
    assert secret in (captured["input"] or ""), "props were not delivered over stdin"


@pytest.mark.anyio
@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is required for SSR rendering tests")
async def test_renderer_handles_props_larger_than_arg_max(tmp_path: Path) -> None:
    """Props above ARG_MAX used to fail the spawn with a bare ``OSError``.

    ``getconf ARG_MAX`` is 2 MiB on a typical Linux box, so a page whose loader
    returns a few megabytes - a large query result - could not render at all.
    Over stdin there is no such ceiling.
    """
    project_root = tmp_path / "project"
    component = project_root / ".pyxle-build" / "client" / "pages" / "big.jsx"
    component.parent.mkdir(parents=True, exist_ok=True)
    component.write_text(
        dedent(
            """
            import React from 'react';

            export default function Big({ blob }) {
                return <section data-size={String(blob.length)}>ok</section>;
            }
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )

    ensure_test_node_modules(project_root)

    blob = "x" * (3 * 1024 * 1024)
    result = await ComponentRenderer().render(component, {"blob": blob})

    assert f'data-size="{len(blob)}"' in result.html
