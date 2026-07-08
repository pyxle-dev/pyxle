from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

from pyxle.devserver.client_files import (
    CLIENT_ENTRY_FILENAME,
    CLIENT_HTML_FILENAME,
    TSCONFIG_FILENAME,
    VITE_CONFIG_FILENAME,
    _build_public_env_defines,
    _render_client_entry,
    _render_client_index,
    _render_client_runtime_index_types,
    _render_client_runtime_link_types,
    _render_slot_runtime,
    _render_slot_runtime_types,
    _render_tsconfig,
    _render_use_action_component,
    _render_use_action_component_types,
    _render_use_auth_component,
    _render_use_auth_component_types,
    _render_use_pathname_component,
    _render_use_pathname_component_types,
    _render_use_websocket_component,
    _render_use_websocket_component_types,
    _render_vite_config,
    write_client_bootstrap_files,
)
from pyxle.devserver.settings import DevServerSettings


def create_project(tmp_path: Path) -> DevServerSettings:
    root = tmp_path / "project"
    (root / "pages").mkdir(parents=True)
    (root / "public").mkdir()
    return DevServerSettings.from_project_root(root)


def test_write_client_bootstrap_files_generates_expected_artifacts(tmp_path: Path) -> None:
    settings = create_project(tmp_path)

    write_client_bootstrap_files(settings)

    client_root = settings.client_build_dir
    index_html = (client_root / CLIENT_HTML_FILENAME).read_text(encoding="utf-8")
    vite_config = (client_root / VITE_CONFIG_FILENAME).read_text(encoding="utf-8")
    client_entry = (client_root / CLIENT_ENTRY_FILENAME).read_text(encoding="utf-8")
    tsconfig = (client_root / TSCONFIG_FILENAME).read_text(encoding="utf-8")
    slot_runtime = (client_root / "pyxle" / "slot.jsx").read_text(encoding="utf-8")
    index_types = (client_root / "pyxle" / "index.d.ts").read_text(encoding="utf-8")
    link_types = (client_root / "pyxle" / "link.d.ts").read_text(encoding="utf-8")
    slot_types = (client_root / "pyxle" / "slot.d.ts").read_text(encoding="utf-8")

    assert index_html == _render_client_index()
    assert vite_config == _render_vite_config(settings)
    assert client_entry == _render_client_entry(settings)
    assert tsconfig == _render_tsconfig()
    assert slot_runtime == _render_slot_runtime()
    assert index_types == _render_client_runtime_index_types()
    assert link_types == _render_client_runtime_link_types()
    assert slot_types == _render_slot_runtime_types()


def test_tsconfig_avoids_typescript_7_deprecated_options() -> None:
    """The generated tsconfig must not use options TypeScript deprecates for 7.0:
    ``moduleResolution: "node10"``/``"Node"`` (TS5107) and ``baseUrl`` (TS5101),
    which make ``pyxle typecheck`` fail with deprecation errors on a current
    TypeScript. A Vite/esbuild project should use ``bundler`` resolution, with
    ``paths`` resolved relative to the tsconfig (so no ``baseUrl`` is needed)."""
    options = json.loads(_render_tsconfig())["compilerOptions"]

    assert options["moduleResolution"] == "Bundler"
    assert options["moduleResolution"].lower() not in {"node", "node10"}
    assert "baseUrl" not in options
    # paths survive the baseUrl removal by being explicitly tsconfig-relative.
    assert options["paths"]["pyxle/client"] == ["./pyxle/client"]
    assert all(
        target.startswith("./")
        for targets in options["paths"].values()
        for target in targets
    )


def test_client_entry_seeds_nav_cache_and_guards_self_prefetch(tmp_path: Path) -> None:
    settings = DevServerSettings.from_project_root(tmp_path)
    client_entry = _render_client_entry(settings)

    # Per-entry navigation-cache TTL with a 2-minute default (replacing the
    # old global 30s), resolved from the payload's edge-cache TTL.
    assert "DEFAULT_NAV_STALE_MS" in client_entry
    assert "120_000" in client_entry
    assert "navTtlFromPayload" in client_entry
    assert "navCacheTtlSeconds" in client_entry

    # The page the user landed on is seeded into the cache from the SSR blob,
    # so its own prefetch is a hit instead of a second loader run.
    assert "seedCurrentPage" in client_entry
    assert "__PYXLE_NAV_SEED__" in client_entry

    # Belt-and-suspenders: prefetch never re-fetches the current page.
    assert "Never prefetch the page we're already on" in client_entry


def test_client_entry_wraps_pages_in_error_boundary(tmp_path: Path) -> None:
    settings = DevServerSettings.from_project_root(tmp_path)
    client_entry = _render_client_entry(settings)

    # A client-side React error boundary that renders the nearest error.pyxl on
    # a render fault, keyed by pagePath so navigation clears the error state.
    assert "class PyxleErrorBoundary extends React.Component" in client_entry
    assert "getDerivedStateFromError" in client_entry
    assert "fallbackComponent" in client_entry
    # The error context is passed under the `error` prop key, matching the
    # server's props={"error": ...} so one error.pyxl reads props.error on both.
    assert "buildClientErrorContext" in client_entry
    assert "error: buildClientErrorContext(this.state.error)" in client_entry

    # The nearest error.pyxl asset is threaded through renderPage from both the
    # navigation payload and the SSR-seeded global.
    assert "errorAssetPath" in client_entry
    assert "window.__PYXLE_ERROR_ASSET__" in client_entry
    assert "payload.page?.errorAssetPath" in client_entry


def test_write_client_bootstrap_files_is_idempotent(tmp_path: Path) -> None:
    settings = create_project(tmp_path)

    write_client_bootstrap_files(settings)
    first_contents = {
        name: (settings.client_build_dir / name).read_text(encoding="utf-8")
        for name in (
            CLIENT_HTML_FILENAME,
            VITE_CONFIG_FILENAME,
            CLIENT_ENTRY_FILENAME,
            TSCONFIG_FILENAME,
            "pyxle/slot.jsx",
            "pyxle/index.d.ts",
            "pyxle/link.d.ts",
            "pyxle/slot.d.ts",
        )
    }

    write_client_bootstrap_files(settings)

    second_contents = {
        name: (settings.client_build_dir / name).read_text(encoding="utf-8")
        for name in (
            CLIENT_HTML_FILENAME,
            VITE_CONFIG_FILENAME,
            CLIENT_ENTRY_FILENAME,
            TSCONFIG_FILENAME,
            "pyxle/slot.jsx",
            "pyxle/index.d.ts",
            "pyxle/link.d.ts",
            "pyxle/slot.d.ts",
        )
    }

    assert first_contents == second_contents


def test_client_entry_includes_global_style_imports(tmp_path: Path) -> None:
    root = tmp_path / "project"
    (root / "pages").mkdir(parents=True)
    (root / "public").mkdir()
    style_path = root / "styles" / "theme.css"
    style_path.parent.mkdir(parents=True, exist_ok=True)
    style_path.write_text("body { color: hotpink; }\n", encoding="utf-8")

    settings = DevServerSettings.from_project_root(
        root,
        global_stylesheets=("styles/theme.css",),
    )

    write_client_bootstrap_files(settings)

    client_entry = (settings.client_build_dir / CLIENT_ENTRY_FILENAME).read_text(encoding="utf-8")
    import_statement = settings.global_stylesheets[0].import_specifier
    assert f"import '{import_statement}';" in client_entry


def test_client_entry_includes_global_script_imports_before_styles(tmp_path: Path) -> None:
    root = tmp_path / "project"
    (root / "pages").mkdir(parents=True)
    (root / "public").mkdir()
    script_path = root / "scripts" / "analytics.js"
    script_path.parent.mkdir(parents=True, exist_ok=True)
    script_path.write_text("console.log('analytics');\n", encoding="utf-8")
    style_path = root / "styles" / "theme.css"
    style_path.parent.mkdir(parents=True, exist_ok=True)
    style_path.write_text("body { color: rebeccapurple; }\n", encoding="utf-8")

    settings = DevServerSettings.from_project_root(
        root,
        global_scripts=("scripts/analytics.js",),
        global_stylesheets=("styles/theme.css",),
    )

    write_client_bootstrap_files(settings)

    client_entry = (settings.client_build_dir / CLIENT_ENTRY_FILENAME).read_text(encoding="utf-8")
    script_import = f"import '{settings.global_scripts[0].import_specifier}';"
    style_import = f"import '{settings.global_stylesheets[0].import_specifier}';"

    assert script_import in client_entry
    assert style_import in client_entry
    assert client_entry.index(script_import) < client_entry.index("import React from 'react';")
    assert client_entry.index(script_import) < client_entry.index(style_import)


def test_client_entry_omits_overlay_in_production(tmp_path: Path) -> None:
    root = tmp_path / "project"
    (root / "pages").mkdir(parents=True)
    (root / "public").mkdir()

    dev_settings = DevServerSettings.from_project_root(root, debug=True)
    prod_settings = DevServerSettings.from_project_root(root, debug=False)

    dev_entry = _render_client_entry(dev_settings)
    prod_entry = _render_client_entry(prod_settings)

    assert "__PYXLE_ERROR_OVERLAY__" in dev_entry
    assert "/__pyxle__/overlay" in dev_entry
    assert "__PYXLE_ERROR_OVERLAY__" not in prod_entry
    assert "/__pyxle__/overlay" not in prod_entry


def test_client_entry_forwards_server_logs_to_console_in_dev(tmp_path: Path) -> None:
    root = tmp_path / "project"
    (root / "pages").mkdir(parents=True)
    (root / "public").mkdir()

    dev_settings = DevServerSettings.from_project_root(root, debug=True)
    prod_settings = DevServerSettings.from_project_root(root, debug=False)

    dev_entry = _render_client_entry(dev_settings)
    prod_entry = _render_client_entry(prod_settings)

    # The dev overlay client consumes the "log" event, maps the level to the
    # matching console method, and prefixes it as a server log.
    assert "payload.type === 'log'" in dev_entry
    assert "[pyxle:server]" in dev_entry
    assert "console[method]" in dev_entry

    # Strictly dev-only: never present in the production bundle.
    assert "payload.type === 'log'" not in prod_entry
    assert "[pyxle:server]" not in prod_entry


def test_client_entry_includes_nav_progress_bar(tmp_path: Path) -> None:
    """Client runtime ships a navigation progress bar IIFE that
    ``markNavigating`` calls on start/finish. The bar is always
    present (dev AND prod) and integrates transparently — no user
    opt-in required."""
    settings = create_project(tmp_path)
    entry = _render_client_entry(settings)

    # Module initialised as a top-level const IIFE — keeps state
    # encapsulated so nothing leaks onto window.
    assert "const navProgress = (function initNavProgress()" in entry
    assert "return { start: start, finish: finish };" in entry

    # Stable DOM ids — users can style the bar by targeting these
    # directly, so changing them is a breaking change.
    assert "__pyxle_nav_progress__" in entry
    assert "__pyxle_nav_progress_style__" in entry

    # CSS custom properties for user overrides.
    assert "--pyxle-nav-progress-height" in entry
    assert "--pyxle-nav-progress-color" in entry
    assert "--pyxle-nav-progress-shadow" in entry

    # markNavigating is wired up to the progress bar on both
    # edges of every navigation.
    assert "navProgress.start()" in entry
    assert "navProgress.finish()" in entry


def test_client_entry_nav_progress_includes_opt_out_hooks(tmp_path: Path) -> None:
    """Two opt-out mechanisms must be present: a window global
    checked lazily (so it can be set before the runtime loads) and
    a data attribute on <html> (so SSR-side rendering can disable
    it per-page without JS)."""
    settings = create_project(tmp_path)
    entry = _render_client_entry(settings)

    assert "window.__pyxle_disable_progress__ === true" in entry
    assert "data-pyxle-progress" in entry
    assert "'off'" in entry  # the attribute value that disables the bar


def test_client_entry_nav_progress_accessibility(tmp_path: Path) -> None:
    """The progress bar element carries ARIA progressbar semantics
    so screen readers announce navigation as 'Loading page' with a
    live 0-100 value."""
    settings = create_project(tmp_path)
    entry = _render_client_entry(settings)

    assert "'role', 'progressbar'" in entry
    assert "'aria-label', 'Loading page'" in entry
    assert "'aria-valuemin', '0'" in entry
    assert "'aria-valuemax', '100'" in entry
    assert "aria-valuenow" in entry


def test_client_entry_nav_progress_respects_reduced_motion(tmp_path: Path) -> None:
    """Users with `prefers-reduced-motion: reduce` see a static bar
    (snap to 30%, no ticking decay) instead of animated progress."""
    settings = create_project(tmp_path)
    entry = _render_client_entry(settings)

    assert "prefers-reduced-motion: reduce" in entry
    assert "prefersReducedMotion" in entry


def test_client_entry_nav_progress_is_present_in_both_dev_and_prod(tmp_path: Path) -> None:
    """The progress bar is a user-experience feature, not a debug
    tool — it must ship in both dev and production builds."""
    root = tmp_path / "project"
    (root / "pages").mkdir(parents=True)
    (root / "public").mkdir()

    dev_entry = _render_client_entry(
        DevServerSettings.from_project_root(root, debug=True)
    )
    prod_entry = _render_client_entry(
        DevServerSettings.from_project_root(root, debug=False)
    )

    for entry in (dev_entry, prod_entry):
        assert "const navProgress = (function initNavProgress()" in entry
        assert "navProgress.start()" in entry
        assert "navProgress.finish()" in entry


def test_vite_config_aliases_cover_client_runtime(tmp_path: Path) -> None:
    settings = create_project(tmp_path)
    vite_config = _render_vite_config(settings)

    assert "find: /^pyxle\\/client$/" in vite_config
    assert "find: /^pyxle\\/client\\/(.+)$/" in vite_config
    assert "find: 'pyxle/client'" not in vite_config


def _write_package_json(root: Path, *, tailwind: bool) -> None:
    dev = {"vite": "^7"}
    if tailwind:
        dev["@tailwindcss/vite"] = "^4"
        dev["tailwindcss"] = "^4"
    (root / "package.json").write_text(
        json.dumps({"devDependencies": dev}), encoding="utf-8"
    )


def test_vite_config_injects_tailwind_plugin_only_when_present(tmp_path: Path) -> None:
    settings = create_project(tmp_path)
    root = settings.project_root

    # No @tailwindcss/vite in package.json -> plugin absent.
    assert "@tailwindcss/vite" not in _render_vite_config(settings)
    assert "plugins: [react()]" in _render_vite_config(settings)

    # Declaring the dependency turns the plugin on.
    _write_package_json(root, tailwind=True)
    tailwind_config = _render_vite_config(settings)
    assert "import tailwindcss from '@tailwindcss/vite';" in tailwind_config
    assert "plugins: [react(), tailwindcss()]" in tailwind_config


def test_vite_config_injects_jsconfig_import_alias(tmp_path: Path) -> None:
    settings = create_project(tmp_path)
    root = settings.project_root

    # No jsconfig -> no user alias entry.
    assert "find: '@'" not in _render_vite_config(settings)

    (root / "jsconfig.json").write_text(
        json.dumps({"compilerOptions": {"baseUrl": ".", "paths": {"@/*": ["./*"]}}}),
        encoding="utf-8",
    )
    config = _render_vite_config(settings)
    assert "{ find: '@', replacement: projectRoot }" in config


def test_vite_config_ignores_malformed_project_files(tmp_path: Path) -> None:
    """Broken package.json / jsconfig.json must degrade gracefully — no Tailwind
    plugin, no user alias, and never a crash."""
    settings = create_project(tmp_path)
    root = settings.project_root
    (root / "package.json").write_text("{ not json", encoding="utf-8")
    (root / "jsconfig.json").write_text("{ also not json", encoding="utf-8")

    config = _render_vite_config(settings)
    assert "@tailwindcss/vite" not in config
    assert "find: '@'" not in config


def test_vite_config_alias_resolves_subdirectory_target(tmp_path: Path) -> None:
    settings = create_project(tmp_path)
    (settings.project_root / "jsconfig.json").write_text(
        json.dumps({"compilerOptions": {"paths": {"~/*": ["./src/*"]}}}),
        encoding="utf-8",
    )
    config = _render_vite_config(settings)
    assert "{ find: '~', replacement: path.resolve(projectRoot, 'src') }" in config


def test_vite_config_pins_deterministic_css_module_names(tmp_path: Path) -> None:
    """A deterministic generateScopedName is required so SSR + client CSS Module
    class names match exactly (no React hydration mismatch)."""
    settings = create_project(tmp_path)
    config = _render_vite_config(settings)

    assert "function pyxleCssModuleClass(name, filename, css)" in config
    assert "generateScopedName: pyxleCssModuleClass" in config


def test_vite_config_has_explicit_build_block(tmp_path: Path) -> None:
    settings = create_project(tmp_path)
    vite_config = _render_vite_config(settings)

    assert "build: {" in vite_config
    assert "target: 'es2020'" in vite_config
    assert "cssCodeSplit: true" in vite_config
    # We do our own --analyze, so Vite's slow gzip reporting is off.
    assert "reportCompressedSize: false" in vite_config


def test_vite_config_respects_base_environment(tmp_path: Path) -> None:
    settings = create_project(tmp_path)
    vite_config = _render_vite_config(settings)

    assert "const base = process.env.PYXLE_VITE_BASE ?? '/';" in vite_config
    assert "base," in vite_config


def test_vite_config_sets_browser_origin_for_assets(tmp_path: Path) -> None:
    """Vite must emit ABSOLUTE asset URLs against its own origin.

    Regression: Pyxle serves the HTML document from its own origin, but Vite
    rewrites CSS ``url(...)`` references (web fonts, background images) to
    root-relative ``/@fs/...`` paths. Without ``server.origin`` those resolve
    against Pyxle's origin and 404 — e.g. ``@fontsource`` ``.woff2`` files never
    load in ``pyxle dev`` even though Vite serves them fine on its own port.
    """

    settings = create_project(tmp_path)
    vite_config = _render_vite_config(settings)

    # A single browser-connectable origin drives server.host/port AND origin.
    assert "const viteHost = '127.0.0.1';" in vite_config
    assert "const vitePort = Number(process.env.PYXLE_VITE_PORT ?? 5173);" in vite_config
    assert "origin: `http://${browserHost}:${vitePort}`" in vite_config
    assert "host: viteHost," in vite_config
    assert "port: vitePort," in vite_config


def test_vite_config_origin_normalises_wildcard_bind_host(tmp_path: Path) -> None:
    """A wildcard bind host (0.0.0.0 / ::) is normalised to a connectable host.

    Browsers cannot connect to ``0.0.0.0``/``::``, so the emitted asset origin
    falls back to ``localhost`` — mirroring ``ssr/template.py``'s <script>
    origin so scripts and assets always share one origin.
    """

    settings = replace(create_project(tmp_path), vite_host="0.0.0.0")
    vite_config = _render_vite_config(settings)

    assert "const viteHost = '0.0.0.0';" in vite_config
    # browserHost falls back to localhost for wildcard binds.
    assert "? 'localhost'" in vite_config
    assert "origin: `http://${browserHost}:${vitePort}`" in vite_config


def test_build_public_env_defines_empty(monkeypatch) -> None:
    """No PYXLE_PUBLIC_ vars means no define block."""

    # Clear any existing PYXLE_PUBLIC_ vars
    for key in list(k for k in __import__("os").environ if k.startswith("PYXLE_PUBLIC_")):
        monkeypatch.delenv(key, raising=False)

    result = _build_public_env_defines()
    assert result == ""


def test_build_public_env_defines_injects_vars(monkeypatch) -> None:
    """PYXLE_PUBLIC_ vars are injected as import.meta.env defines."""

    monkeypatch.setenv("PYXLE_PUBLIC_API_URL", "https://api.example.com")
    monkeypatch.setenv("PYXLE_PUBLIC_APP_NAME", "MyApp")

    result = _build_public_env_defines()
    assert "define:" in result
    assert (
        "'import.meta.env.PYXLE_PUBLIC_API_URL': JSON.stringify(\"https://api.example.com\")"
        in result
    )
    assert "'import.meta.env.PYXLE_PUBLIC_APP_NAME': JSON.stringify(\"MyApp\")" in result


def test_build_public_env_defines_wraps_values_in_json_stringify(monkeypatch) -> None:
    """Regression: define VALUES must be emitted as quoted JS string literals (via
    JSON.stringify), never spliced in raw. A bare value is invalid JS — it is
    silently dropped in `pyxle dev` and crashes `vite build` with esbuild's
    "Invalid define value" for any non-identifier value (a URL, a 0x… key)."""

    for key in list(k for k in __import__("os").environ if k.startswith("PYXLE_PUBLIC_")):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("PYXLE_PUBLIC_TURNSTILE_KEY", "0x4AAAAAADkljWFuoy5AWuHN")

    result = _build_public_env_defines()
    assert 'JSON.stringify("0x4AAAAAADkljWFuoy5AWuHN")' in result
    # never spliced in as a bare (unquoted) value — that is what esbuild rejects
    assert "': 0x4AAAAAADkljWFuoy5AWuHN" not in result


def test_vite_config_includes_public_env_defines(tmp_path: Path, monkeypatch) -> None:
    """Full Vite config includes the define block when PYXLE_PUBLIC_ vars are set."""

    monkeypatch.setenv("PYXLE_PUBLIC_SITE_NAME", "TestSite")

    settings = create_project(tmp_path)
    vite_config = _render_vite_config(settings)

    assert "define:" in vite_config
    assert "import.meta.env.PYXLE_PUBLIC_SITE_NAME" in vite_config
    assert 'JSON.stringify("TestSite")' in vite_config


def test_vite_config_no_define_block_without_public_vars(tmp_path: Path, monkeypatch) -> None:
    """Vite config omits define block when no PYXLE_PUBLIC_ vars exist."""

    for key in list(k for k in __import__("os").environ if k.startswith("PYXLE_PUBLIC_")):
        monkeypatch.delenv(key, raising=False)

    settings = create_project(tmp_path)
    vite_config = _render_vite_config(settings)

    assert "define:" not in vite_config


def test_build_public_env_defines_escapes_special_chars(monkeypatch) -> None:
    """Values with special characters are properly JSON-escaped."""

    monkeypatch.setenv("PYXLE_PUBLIC_MSG", 'Hello "World" & <Friends>')

    result = _build_public_env_defines()
    assert "define:" in result
    # JSON encoding should escape the double quotes
    assert r'\"World\"' in result


# ---------------------------------------------------------------------------
# BFCache restore handler
# ---------------------------------------------------------------------------


def test_client_entry_includes_bfcache_pageshow_handler(tmp_path: Path) -> None:
    """The client runtime registers a ``pageshow`` listener so that a
    BFCache restore triggers ``router.refresh()``. Without this a user
    who backgrounds a tab for a long time and comes back can see stale
    content (or raw JSON if the browser's HTTP cache confused the
    HTML/JSON variants for the same URL)."""
    settings = create_project(tmp_path)
    entry = _render_client_entry(settings)

    assert "addEventListener('pageshow'" in entry or 'addEventListener("pageshow"' in entry
    assert "event.persisted" in entry
    assert "router.refresh()" in entry


# ---------------------------------------------------------------------------
# usePathname hook
# ---------------------------------------------------------------------------


def test_client_entry_dispatches_route_change_event(tmp_path: Path) -> None:
    """The client runtime dispatches a ``pyxle:routechange`` custom event
    after both ``navigateTo`` and ``refreshCurrentPage`` complete.  This
    is the signal consumed by ``usePathname()``."""
    settings = create_project(tmp_path)
    entry = _render_client_entry(settings)

    assert "pyxle:routechange" in entry
    # Must appear at least twice: once in navigateTo, once in refreshCurrentPage.
    assert entry.count("pyxle:routechange") >= 2


def test_client_entry_navigation_reuses_inflight_prefetch(tmp_path: Path) -> None:
    """A hover/viewport prefetch and the click that follows it must share ONE
    network request.

    ``navigateTo()`` has to consult the in-flight ``navigationPromises`` map
    (not just the settled ``navigationCache``) before issuing its own fetch.
    Otherwise every hover-then-click that lands inside the prefetch's flight
    time fetches the page payload twice — visible as duplicate rows in the
    network tab, and dangerous because the page's ``@server`` loader (which
    may have side effects) runs twice per navigation.

    Awaiting the shared prefetch is not abortable the way the
    controller-owned fetch is, so the navigation also carries a monotonic
    sequence token and bails out when a newer navigation supersedes it
    mid-wait — a rapid second click must win, not render a stale page.
    """
    settings = create_project(tmp_path)
    entry = _render_client_entry(settings)

    nav_to = entry.split("async function navigateTo", 1)[1].split(
        "async function refreshCurrentPage", 1
    )[0]

    # Consult the in-flight prefetch BEFORE falling back to a fresh fetch.
    assert "navigationPromises.has(cacheKey)" in nav_to
    assert "await navigationPromises.get(cacheKey).catch(() => {});" in nav_to
    assert nav_to.index("navigationPromises.has(cacheKey)") < nav_to.index(
        "requestNavigationPayload(url, { useController: true })"
    )

    # Supersede guard: a newer navigation invalidates the waiting one.
    assert "let navigationSequence = 0;" in entry
    assert "const navToken = ++navigationSequence;" in nav_to
    assert "navToken !== navigationSequence" in nav_to


def test_use_pathname_component_is_ssr_safe() -> None:
    """The generated usePathname hook must guard window access for SSR."""
    source = _render_use_pathname_component()
    assert "typeof window" in source
    assert "usePathname" in source
    assert "pyxle:routechange" in source


def test_use_pathname_reads_ssr_pathname_global() -> None:
    """The hook reads globalThis.__PYXLE_CURRENT_PATHNAME__ during SSR.

    Without this the hook returns '/' on the server and hydration mismatches
    on every active-link-highlighting layout.  The SSR worker sets the global
    before rendering — the hook must consume it.
    """
    source = _render_use_pathname_component()
    # The executable expression (not just a docstring mention) must be present.
    assert "typeof globalThis.__PYXLE_CURRENT_PATHNAME__" in source
    # And the fallback to '/' is still there for tests / direct renders
    # that bypass the SSR worker.
    assert "return '/'" in source


def test_head_component_ssr_branch_registers_children() -> None:
    """SSR branch still registers children with __PYXLE_HEAD_REGISTRY__."""
    from pyxle.devserver.client_files import _render_head_component
    source = _render_head_component()
    assert "typeof window === 'undefined'" in source
    assert "__PYXLE_HEAD_REGISTRY__" in source
    assert "renderToStaticMarkup" in source


def test_image_component_exposes_loading_state_and_callbacks() -> None:
    """Image must track state, fire onLoad/onError, and expose data attr."""
    from pyxle.devserver.client_files import _render_image_component
    source = _render_image_component()

    # Loading state and the three phases.
    assert "STATE_LOADING" in source
    assert "STATE_LOADED" in source
    assert "STATE_ERROR" in source
    # Exposed to CSS / selectors for external styling & tests.
    assert "data-pyxle-image-state" in source

    # onLoad / onError hooks wired up (not just passed through).
    assert "handleLoad" in source
    assert "handleError" in source

    # Cache-hit path — images already loaded don't fire native 'load'; we
    # check .complete and synthesize the event.
    assert ".complete" in source
    assert "fromCache" in source


def test_image_component_supports_blur_placeholder_and_fallback() -> None:
    """Image supports blur-up placeholder and automatic fallback on error."""
    from pyxle.devserver.client_files import _render_image_component
    source = _render_image_component()

    # Blur placeholder with blurDataURL or solid color fallback.
    assert "placeholder" in source
    assert "blurDataURL" in source
    assert "placeholderColor" in source
    assert "filter: blurDataURL ? 'blur(20px)' : undefined" in source

    # fallbackSrc replaces src once on error before surfacing it.
    assert "fallbackSrc" in source


def test_image_component_detects_ssr_hydration_error_via_complete() -> None:
    """Image must drive the fallback path when the browser finished a failed
    SSR-initiated fetch before React hydration attached its onError listener.

    The post-mount useEffect checks `complete && naturalWidth === 0` (image
    has terminated fetching but has no pixels) and swaps in `fallbackSrc`
    just like a live error would — otherwise a broken SSR-rendered <img>
    would strand in the loading state forever.
    """
    from pyxle.devserver.client_files import _render_image_component
    source = _render_image_component()

    # Positive branch (cache hit) stays.
    assert "el.naturalWidth > 0" in source
    # Negative branch — terminal failure detected post-hydration.
    assert "fallbackSrc && currentSrc !== fallbackSrc" in source
    # The effect must react to currentSrc (re-run after fallback swap).
    assert "}, [currentSrc]);" in source


def test_image_component_types_model_new_api() -> None:
    """TypeScript definitions expose placeholder/blurDataURL/fallbackSrc/state."""
    from pyxle.devserver.client_files import _render_image_component_types
    types = _render_image_component_types()
    assert "PyxleImageState" in types
    assert "placeholder?:" in types
    assert "blurDataURL?:" in types
    assert "fallbackSrc?:" in types
    assert "onLoad?:" in types
    assert "onError?:" in types


def test_image_component_responsive_srcset_via_loader() -> None:
    """A `loader` opts into responsive srcset; without one, no srcset is emitted
    (resizing needs a real backend, so a fake srcset would just re-download the
    full image at every width)."""
    from pyxle.devserver.client_files import _render_image_component
    source = _render_image_component()

    # Loader-gated srcset generation across a responsive width ladder.
    assert "usesLoader = typeof loader === 'function'" in source
    assert "srcSet = usesLoader" in source
    assert "candidateWidths" in source
    assert "DEVICE_SIZES = [640, 750, 828, 1080, 1200, 1920, 2048, 3840]" in source
    assert "IMAGE_SIZES" in source
    # Fixed width -> 1x + 2x (retina); responsive -> full device ladder.
    assert "atLeast(w)" in source and "atLeast(w * 2)" in source


def test_image_component_fill_priority_and_sizes() -> None:
    """Fill mode, priority (fetchpriority=high), and the sizes attribute."""
    from pyxle.devserver.client_files import _render_image_component
    source = _render_image_component()

    assert "fill = false" in source
    # Fill positions the image to cover a positioned ancestor.
    assert "position: 'absolute'" in source
    assert "objectFit: objectFit || 'cover'" in source
    # LCP priority maps to the lowercase `fetchpriority` HTML attribute. React
    # 18.3.1 does not recognise the camelCase `fetchPriority` prop and warns on
    # it, so the component spreads the lowercase attribute only when priority is
    # set (passed straight through to the DOM, no warning).
    assert "fetchPriority" not in source
    assert "{...(priority ? { fetchpriority: 'high' } : {})}" in source
    # sizes defaults to 100vw under fill.
    assert "resolvedSizes = sizes || (fill ? '100vw' : undefined)" in source
    # Data/blob srcs bypass the loader.
    assert "isPassthroughSrc" in source


def test_image_component_types_model_responsive_api() -> None:
    """The .d.ts exposes the Next.js-parity props and loader types."""
    from pyxle.devserver.client_files import _render_image_component_types
    types = _render_image_component_types()
    for token in ("fill?:", "sizes?:", "quality?:", "loader?:", "objectFit?:"):
        assert token in types
    assert "ImageLoaderProps" in types
    assert "export type ImageLoader" in types


def test_resolve_action_url_reads_ssr_pathname_global() -> None:
    """useAction and Form must resolve the action URL against the real
    request path during SSR — otherwise the form emits a server URL
    rooted at /api/__actions/index/... while the client computes
    /api/__actions/<page>/..., causing a hydration mismatch warning."""
    from pyxle.devserver.client_files import (
        _render_use_action_component,
        _render_form_component,
    )
    for source in (_render_use_action_component(), _render_form_component()):
        assert "__PYXLE_CURRENT_PATHNAME__" in source, (
            "resolveActionUrl must read the framework's SSR pathname global"
        )
        # The window-branch still comes first — we only hit the SSR branch
        # when there's no window (true SSR path).
        assert "typeof window !== 'undefined'" in source


def test_use_action_surfaces_validation_field_errors() -> None:
    """useAction must expose the server's per-field validation errors so a
    form can render messages next to each input. The dispatcher returns a
    top-level ``fields`` map (field path -> messages) on a 422; the hook
    mirrors it into ``execute.fields`` and the resolved result object, and
    clears it at the start of every new request."""
    source = _render_use_action_component()

    # Dedicated state for field errors, cleared on each new submit.
    assert "const [fields, setFields] = useState(null);" in source
    assert "setFields(null);" in source
    # The error branch reads the server's ``fields`` key and surfaces it.
    assert "json.fields ?? null" in source
    assert "setFields(fieldErrors);" in source
    # ``fields`` is attached to the callable and present in the result object.
    assert "execute.fields = fields;" in source
    assert "fields: fieldErrors" in source
    # The framework's reserved key must not leak into the success ``data``.
    assert "fields: _fields" in source


def test_use_action_types_declare_fields() -> None:
    """The .d.ts for useAction must declare the ``fields`` surface so editors
    and typecheck see it."""
    types = _render_use_action_component_types()
    assert "fields" in types
    assert "ActionFieldErrors" in types
    assert "ActionInvoker" in types


def test_csrf_runtime_honours_configured_names() -> None:
    """useAction and Form must resolve the CSRF cookie/header names from the
    document-shell globals (``csrf.cookieName`` / ``csrf.headerName`` in
    pyxle.config.json) instead of hardwiring the defaults.

    Regression: the runtime used to match a literal ``pyxle-csrf`` cookie and
    always send ``x-csrf-token``, so any app with a custom cookie name had
    every action POST rejected with 403 — "CSRF token missing" in a clean
    browser, or "CSRF token mismatch" when a stale default-named cookie from
    another localhost app was still around (cookies ignore ports).
    """
    from pyxle.devserver.client_files import (
        _render_form_component,
        _render_use_action_component,
    )

    for source in (_render_use_action_component(), _render_form_component()):
        # Configured names come from the document-shell globals…
        assert "globalThis.__PYXLE_CSRF_COOKIE__" in source
        assert "globalThis.__PYXLE_CSRF_HEADER__" in source
        # …with the framework defaults as fallback.
        assert "return 'pyxle-csrf';" in source
        assert "return 'x-csrf-token';" in source
        # The fetch header key is resolved, never hardwired.
        assert "headers[csrfHeaderName()] = csrfToken" in source
        assert "headers['x-csrf-token']" not in source
        # No hardcoded cookie-name lookup remains.
        assert "pyxle-csrf=" not in source


def test_script_component_is_real_runtime_not_stub() -> None:
    """Script must actually load scripts — not just return null."""
    from pyxle.devserver.client_files import _render_script_component
    source = _render_script_component()

    # No longer a stub — the component should have real implementation.
    assert "ensureScriptLoaded" in source
    assert "document.head.appendChild" in source

    # All three strategies must be handled explicitly.
    assert "lazyOnload" in source
    assert "afterInteractive" in source
    assert "beforeInteractive" in source

    # Lazy strategy must prefer requestIdleCallback, fall back to setTimeout.
    assert "requestIdleCallback" in source
    assert "setTimeout" in source

    # Dedup + load-state tracking.
    assert "scriptPromises" in source
    assert "data-pyxle-script-loaded" in source

    # onLoad / onError must both be hooked up.
    assert "onLoad" in source
    assert "onError" in source


def test_script_component_inline_children_supported() -> None:
    """<Script>inline code</Script> without src must insert an inline tag."""
    from pyxle.devserver.client_files import _render_script_component
    source = _render_script_component()
    # The inline branch appears when src is falsy.
    assert "if (!src)" in source
    assert "textContent = children" in source


def test_script_component_types_include_optional_src_and_children() -> None:
    """TypeScript definitions match the new runtime capability."""
    from pyxle.devserver.client_files import _render_script_component_types
    types = _render_script_component_types()
    # src must be optional so inline-only usage type-checks.
    assert "src?: string" in types
    # children is accepted for inline script content.
    assert "children?: string" in types
    # Standard integrity / security props are modelled.
    assert "integrity" in types
    assert "crossOrigin" in types


def test_head_component_client_branch_applies_and_cleans_up() -> None:
    """Client branch must update DOM on mount/update AND clean up on unmount.

    Previously the client useEffect was a stub, so state-driven head changes
    never reached the DOM. This test pins the new behaviour in place.
    """
    from pyxle.devserver.client_files import _render_head_component
    source = _render_head_component()

    # The helper that actually applies markup must exist and handle both
    # <title> (document.title) and other elements (document.head).
    assert "applyHeadMarkup" in source
    assert "document.title" in source
    assert "document.head" in source

    # Cleanup function (return-value of useEffect) must remove what was
    # inserted and restore the previous title — no leaks across renders.
    assert "parentNode.removeChild" in source
    assert "previousTitle" in source

    # Adoption of SSR-rendered nodes is what keeps hydration duplicate-free.
    assert "findEquivalentHeadElement" in source
    assert "data-pyxle-head-client" in source


def test_use_pathname_component_types() -> None:
    """Type definition declares usePathname returning a string."""
    types = _render_use_pathname_component_types()
    assert "usePathname" in types
    assert "string" in types


def test_write_client_bootstrap_files_generates_use_pathname(tmp_path: Path) -> None:
    """Bootstrap writes both the JSX hook and its type declaration."""
    settings = create_project(tmp_path)
    write_client_bootstrap_files(settings)

    hook = (settings.client_build_dir / "pyxle" / "use-pathname.jsx").read_text(encoding="utf-8")
    assert "usePathname" in hook
    assert "pyxle:routechange" in hook

    types = (settings.client_build_dir / "pyxle" / "use-pathname.d.ts").read_text(encoding="utf-8")
    assert "usePathname" in types


# ---------------------------------------------------------------------------
# useAuth hook


def test_use_auth_component_exposes_full_surface() -> None:
    """The hook exposes user state plus login/signup/logout/refresh."""
    source = _render_use_auth_component()
    assert "export function useAuth()" in source
    for member in ("user", "isAuthenticated", "loading", "error", "login", "signup", "logout", "refresh"):
        assert member in source
    # Shared store via useSyncExternalStore so consumers stay in sync.
    assert "useSyncExternalStore" in source


def test_use_auth_component_is_ssr_safe() -> None:
    """The hook must guard window access and use a stable server snapshot so
    hydration never mismatches."""
    source = _render_use_auth_component()
    assert "typeof window === 'undefined'" in source
    # A constant server snapshot keeps the hydration render identical on both
    # sides; the client swaps to the seeded value after hydration.
    assert "getServerSnapshot" in source
    assert "SERVER_SNAPSHOT" in source


def test_use_auth_component_seeds_from_window_global() -> None:
    """The hook seeds from window.__PYXLE_AUTH__ so a signed-in user appears
    on the first client frame with no round-trip."""
    source = _render_use_auth_component()
    assert "window.__PYXLE_AUTH__" in source
    # Endpoints come from the seed with conventional /auth/* fallbacks.
    assert "/auth/me" in source
    assert "/auth/login" in source
    assert "/auth/logout" in source


def test_use_auth_component_sends_csrf_on_mutations() -> None:
    """login/signup/logout POSTs must carry the CSRF token like useAction."""
    source = _render_use_auth_component()
    assert "csrfHeaderName()" in source
    assert "getCsrfToken()" in source
    assert "credentials: 'same-origin'" in source


def test_use_auth_component_types() -> None:
    """Type declaration exposes the user shape and the hook result."""
    types = _render_use_auth_component_types()
    assert "useAuth" in types
    assert "PyxleUser" in types
    assert "isAuthenticated" in types


def test_write_client_bootstrap_files_generates_use_auth(tmp_path: Path) -> None:
    """Bootstrap writes the useAuth hook, its types, and the barrel export."""
    settings = create_project(tmp_path)
    write_client_bootstrap_files(settings)

    hook = (settings.client_build_dir / "pyxle" / "use-auth.jsx").read_text(encoding="utf-8")
    assert "export function useAuth()" in hook

    types = (settings.client_build_dir / "pyxle" / "use-auth.d.ts").read_text(encoding="utf-8")
    assert "useAuth" in types

    barrel = (settings.client_build_dir / "pyxle" / "client.js").read_text(encoding="utf-8")
    assert "useAuth" in barrel


# ---------------------------------------------------------------------------
# useWebSocket hook


def test_use_websocket_component_exposes_contract() -> None:
    source = _render_use_websocket_component()
    assert "export function useWebSocket(path, options" in source
    for member in ("status", "send", "lastMessage", "error"):
        assert member in source


def test_use_websocket_component_is_ssr_safe() -> None:
    """The hook must never open a socket during SSR — all socket code is gated
    behind a typeof-window check inside useEffect."""
    source = _render_use_websocket_component()
    assert "typeof window === 'undefined'" in source
    assert "useEffect" in source
    # The window guard precedes any `new WebSocket(` construction.
    assert source.index("typeof window === 'undefined'") < source.index("new WebSocket(")


def test_use_websocket_component_reconnects_on_protocol_change() -> None:
    """`protocols` must enter the effect deps (via a stable JSON key) so a
    changed subprotocol reconnects, without an inline array literal causing a
    reconnect on every render."""
    source = _render_use_websocket_component()
    assert "JSON.stringify(protocols" in source
    assert "protocolsKey" in source
    # The deps array includes the stable key.
    assert "[path, reconnect, maxRetries, protocolsKey]" in source


def test_use_websocket_component_uses_exponential_backoff() -> None:
    """Reconnect must back off exponentially with jitter and a cap — not the
    fixed-delay loop the dev overlay uses (which would thundering-herd a
    restarting server)."""
    source = _render_use_websocket_component()
    assert "Math.pow(2, retries)" in source
    assert "30000" in source  # 30s cap
    assert "Math.random()" in source  # jitter


def test_use_websocket_component_parses_json_safely() -> None:
    source = _render_use_websocket_component()
    assert "JSON.parse(data)" in source
    # JSON.parse is wrapped so a non-JSON frame keeps the raw string.
    assert "try {" in source


def test_use_websocket_component_resolves_same_origin_url() -> None:
    source = _render_use_websocket_component()
    assert "wss:" in source and "ws:" in source
    assert "window.location.protocol === 'https:'" in source


def test_use_websocket_component_types() -> None:
    types = _render_use_websocket_component_types()
    assert "useWebSocket" in types
    assert "WebSocketStatus" in types
    assert "UseWebSocketResult" in types


def test_write_client_bootstrap_files_generates_use_websocket(tmp_path: Path) -> None:
    settings = create_project(tmp_path)
    write_client_bootstrap_files(settings)

    hook = (settings.client_build_dir / "pyxle" / "use-websocket.jsx").read_text(encoding="utf-8")
    assert "export function useWebSocket" in hook

    types = (settings.client_build_dir / "pyxle" / "use-websocket.d.ts").read_text(encoding="utf-8")
    assert "useWebSocket" in types

    barrel = (settings.client_build_dir / "pyxle" / "client.js").read_text(encoding="utf-8")
    assert "useWebSocket" in barrel


def test_navigation_scrolls_to_hash_after_cross_page_commit(tmp_path: Path) -> None:
    """Navigating to /page#anchor must scroll to the anchor once the next
    page's DOM commits — previously only same-page hash links scrolled and
    cross-page navigations were pinned to the top."""
    settings = create_project(tmp_path)
    entry = _render_client_entry(settings)

    # The bounded animation-frame poller exists…
    assert "function scrollToHashWhenReady" in entry
    assert "requestAnimationFrame(() => scrollToHashWhenReady" in entry
    # …and the navigation commit invokes it for hash URLs, after the
    # native-like jump to top, respecting scroll: 'preserve'.
    assert "if (url.hash) {" in entry
    assert "scrollToHashWhenReady(url.hash);" in entry
    commit = entry.index("window.scrollTo(0, 0);")
    assert entry.index("scrollToHashWhenReady(url.hash);", commit) > commit
