from __future__ import annotations

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
    _render_use_pathname_component,
    _render_use_pathname_component_types,
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


def test_vite_config_respects_base_environment(tmp_path: Path) -> None:
    settings = create_project(tmp_path)
    vite_config = _render_vite_config(settings)

    assert "const base = process.env.PYXLE_VITE_BASE ?? '/';" in vite_config
    assert "base," in vite_config


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
    assert "'import.meta.env.PYXLE_PUBLIC_API_URL': \"https://api.example.com\"" in result
    assert "'import.meta.env.PYXLE_PUBLIC_APP_NAME': \"MyApp\"" in result


def test_vite_config_includes_public_env_defines(tmp_path: Path, monkeypatch) -> None:
    """Full Vite config includes the define block when PYXLE_PUBLIC_ vars are set."""

    monkeypatch.setenv("PYXLE_PUBLIC_SITE_NAME", "TestSite")

    settings = create_project(tmp_path)
    vite_config = _render_vite_config(settings)

    assert "define:" in vite_config
    assert "import.meta.env.PYXLE_PUBLIC_SITE_NAME" in vite_config
    assert '"TestSite"' in vite_config


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
