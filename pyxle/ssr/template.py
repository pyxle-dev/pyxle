"""HTML document template helpers for SSR responses."""

from __future__ import annotations

import json
from dataclasses import dataclass
from html import escape
from typing import Any, Final

from pyxle.config import default_csrf_cookie_name
from pyxle.devserver.dev_origins import browser_vite_host
from pyxle.devserver.routes import PageRoute
from pyxle.devserver.settings import DevServerSettings
from pyxle.devserver.styles import load_inline_stylesheets

from .jsx_expressions import is_expression_value
from .renderer import InlineStyleFragment


def vite_owns_stylesheets(settings: DevServerSettings) -> bool:
    """Whether the document links Vite's compiled stylesheets itself.

    A manifest-backed render emits a ``<link rel="stylesheet">`` for every CSS
    asset Vite compiled for the page, so the SSR worker inlining the same
    stylesheets into a ``<style>`` block ships every byte twice — and the links
    are render-blocking, so the inline copy cannot even buy a faster first
    paint. Dev has no page manifest (Vite injects CSS through the client bundle
    after hydration), so there the inline copy is the *only* thing that styles
    the server-rendered paint and must stay.

    One definition, read both by the code that emits the links and by the code
    that decides whether to inline. Two consumers disagreeing about one fact is
    how the duplicate shipped in the first place.
    """
    return not settings.debug and settings.page_manifest is not None


def _browser_vite_origin(
    settings: DevServerSettings, request_host: str | None = None
) -> str:
    """Return a browser-connectable origin for the Vite dev server.

    Bind addresses like ``0.0.0.0`` or ``::`` are valid for *listening* but
    browsers cannot connect to them, so a ``<script src="http://0.0.0.0:5173/…">``
    never loads. ``request_host`` is the hostname the browser used to reach this
    document: when Vite binds every interface it answers under that name too, so
    a page served at ``http://192.168.1.11:3000`` correctly loads its scripts
    from ``http://192.168.1.11:5173`` instead of a ``localhost`` that means the
    visitor's own machine. See :mod:`pyxle.devserver.dev_origins`.
    """
    host = browser_vite_host(
        vite_host=settings.vite_host,
        starlette_host=settings.starlette_host,
        request_host=request_host,
    )
    return f"http://{host}:{settings.vite_port}"


@dataclass(frozen=True)
class DocumentShell:
  """Represents the static prefix/suffix for an HTML document."""

  prefix: str
  suffix: str


# Sentinel for "no auth seed on this request" — distinct from a seed whose
# ``user`` is ``None`` (an authenticated-middleware request for an anonymous
# visitor, which we DO emit so the client knows it is definitively logged out).
_AUTH_SEED_ABSENT: Any = object()


class ManifestLookupError(RuntimeError):
  """Raised when manifest-backed assets cannot be resolved."""


def render_document(
  *,
  settings: DevServerSettings,
  page: PageRoute,
  body_html: str,
  props: dict[str, Any],
  script_nonce: str,
  head_elements: tuple[str, ...],
  inline_styles: tuple[InlineStyleFragment, ...] = tuple(),
  nav_cache_ttl: int | None = None,
  auth_seed: Any = _AUTH_SEED_ABSENT,
  request_host: str | None = None,
) -> str:
  """Compose the HTML document for a rendered page."""
  try:
    shell = build_document_shell(
      settings=settings,
      page=page,
      props=props,
      script_nonce=script_nonce,
      head_elements=head_elements,
      inline_styles=inline_styles,
      nav_cache_ttl=nav_cache_ttl,
      auth_seed=auth_seed,
      request_host=request_host,
    )
  except ManifestLookupError:
    return _render_manifest_error(page)
  return f"{shell.prefix}{body_html}{shell.suffix}"


def build_document_shell(
  *,
  settings: DevServerSettings,
  page: PageRoute,
  props: dict[str, Any],
  script_nonce: str,
  head_elements: tuple[str, ...],
  inline_styles: tuple[InlineStyleFragment, ...] = tuple(),
  nav_cache_ttl: int | None = None,
  auth_seed: Any = _AUTH_SEED_ABSENT,
  request_host: str | None = None,
) -> DocumentShell:
  props_payload = _serialize_props(props)
  page_path_literal = json.dumps(page.client_asset_path)
  # The nearest loading.pyxl's client asset (or null). The client hydration
  # entry reads this to wrap the page in the SAME <Suspense fallback={<Loading/>}>
  # the streaming server emitted — one descriptor drives both sides, so the
  # boundary structure can never diverge.
  loading_boundary = getattr(page, "loading_boundary", None)
  loading_asset_literal = (
    json.dumps(loading_boundary.client_asset_path)
    if loading_boundary is not None
    else "null"
  )
  # The nearest error.pyxl's client asset (or null). The client hydration entry
  # reads this to wrap the page in a React error boundary whose fallback is that
  # error.pyxl — client-side render faults then render the same page the server
  # already renders on a loader/SSR failure.
  error_boundary = getattr(page, "error_boundary", None)
  error_asset_literal = (
    json.dumps(error_boundary.client_asset_path)
    if error_boundary is not None
    else "null"
  )
  head_injections = render_head_markup(head_elements, settings.document_title_default)
  # Seed payload for the client navigation cache. Lets the page the user
  # landed on satisfy its own prefetch (the active self-link) from cache
  # instead of re-running the loader, and powers instant back/forward nav.
  # ``navCacheTtlSeconds`` mirrors the page's edge-cache TTL (``None`` →
  # client default lifetime).
  nav_seed_payload = _serialize_props(
    {"headMarkup": head_injections, "navCacheTtlSeconds": nav_cache_ttl}
  )
  head_block = (
    "\n  <meta data-pyxle-head-start=\"1\" />"
    + head_injections
    + "\n  <meta data-pyxle-head-end=\"1\" />"
  )
  nonce_attr = _format_nonce_attr(script_nonce)
  global_styles = _render_global_styles_markup(settings)
  inline_styles_markup = _render_inline_styles_markup(inline_styles)
  # When the app configures a default nav-cache lifetime
  # (``navigation.defaultPrefetchTtl``), expose it to the client as
  # ``__PYXLE_NAV_STALE_MS__`` (ms). Absent → the client's 2-minute default.
  nav_stale_script = _render_nav_stale_script(settings, nonce_attr)
  # When the app customises ``csrf.cookieName`` / ``csrf.headerName``, expose
  # the names to the client runtime. Absent → the framework defaults.
  csrf_names_script = _render_csrf_names_script(settings, nonce_attr)
  # When an auth provider published a seed on the request scope, expose the
  # signed-in user + endpoint map to the client ``useAuth`` hook. Absent → no
  # script (``useAuth`` resolves over the network).
  auth_seed_script = _render_auth_seed_script(auth_seed, nonce_attr)

  if vite_owns_stylesheets(settings):
    manifest_entry = settings.page_manifest.get(page.path)
    if not isinstance(manifest_entry, dict):
      raise ManifestLookupError
    client_info = manifest_entry.get("client")
    if not isinstance(client_info, dict):
      raise ManifestLookupError
    js_file = client_info.get("file")
    if not isinstance(js_file, str) or not js_file:
      raise ManifestLookupError
    css_assets = client_info.get("css", [])
    css_links: list[str] = []
    if isinstance(css_assets, list):
      for asset in css_assets:
        if isinstance(asset, str):
          css_links.append(f'<link rel="stylesheet" href="/client/{asset.lstrip("/")}" />')
    css_html = "".join(f"\n    {link}" for link in css_links)
    js_src = f"/client/{js_file.lstrip('/')}"

    # Preload the entry module and the chunks it imports, so the browser fetches
    # them in parallel with HTML parsing instead of after parsing the entry
    # (the <script> tag sits at the end of the body). Vite's automatic
    # modulePreload only applies to index.html builds, not our SSR output, so we
    # inject these from the build manifest's import graph.
    preload_links = [f'<link rel="modulepreload" href="{js_src}" />']
    js_imports = client_info.get("imports", [])
    if isinstance(js_imports, list):
      for imp in js_imports:
        if isinstance(imp, str) and imp:
          preload_links.append(
            f'<link rel="modulepreload" href="/client/{imp.lstrip("/")}" />'
          )
    preload_html = "".join(f"\n    {link}" for link in preload_links)

    before_interactive_scripts = _render_before_interactive_scripts(page.scripts, nonce_attr)
    scripts_metadata = _serialize_scripts_metadata(page.scripts)

    prefix = """<!DOCTYPE html>
<html lang=\"en\">
  <head>
  <meta charset=\"utf-8\" />
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />{preload_html}{css_html}{before_interactive_scripts}{global_styles}{inline_styles_markup}{head_block}
  </head>
  <body>
  <div id=\"root\">""".format(
      preload_html=preload_html,
      css_html=css_html,
      before_interactive_scripts=before_interactive_scripts,
      global_styles=global_styles,
      inline_styles_markup=inline_styles_markup,
      head_block=head_block,
    )
    suffix = """
  </div>
  <script id=\"__PYXLE_PROPS__\" type=\"application/json\"{nonce_attr}>{props_payload}</script>
  <script id=\"__PYXLE_NAV_SEED__\" type=\"application/json\"{nonce_attr}>{nav_seed_payload}</script>
  <script{nonce_attr}>window.__PYXLE_PAGE_PATH__ = {page_path_literal};</script>
  <script{nonce_attr}>window.__PYXLE_LOADING_ASSET__ = {loading_asset_literal};</script>
  <script{nonce_attr}>window.__PYXLE_ERROR_ASSET__ = {error_asset_literal};</script>{nav_stale_script}{csrf_names_script}{auth_seed_script}
  <script{nonce_attr}>window.__PYXLE_SCRIPTS__ = {scripts_metadata};</script>
  <script type=\"module\" src=\"{js_src}\"></script>
  </body>
</html>
""".format(
      nonce_attr=nonce_attr,
      props_payload=props_payload,
      nav_seed_payload=nav_seed_payload,
      page_path_literal=page_path_literal,
      loading_asset_literal=loading_asset_literal,
      error_asset_literal=error_asset_literal,
      scripts_metadata=scripts_metadata,
      nav_stale_script=nav_stale_script,
      csrf_names_script=csrf_names_script,
      auth_seed_script=auth_seed_script,
      js_src=js_src,
    )
    return DocumentShell(prefix=prefix, suffix=suffix)

  vite_origin = _browser_vite_origin(settings, request_host)
  module_load_reporter = _render_module_load_reporter(vite_origin, nonce_attr)
  react_refresh_preamble = _render_react_refresh_preamble(vite_origin, nonce_attr)
  before_interactive_scripts = _render_before_interactive_scripts(page.scripts, nonce_attr)
  scripts_metadata = _serialize_scripts_metadata(page.scripts)

  prefix = """<!DOCTYPE html>
<html lang=\"en\">
  <head>
  <meta charset=\"utf-8\" />
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />{module_load_reporter}
  <script type=\"module\" src=\"{vite_origin}/@vite/client\"{nonce_attr}></script>{react_refresh_preamble}{before_interactive_scripts}{global_styles}{inline_styles_markup}{head_block}
  </head>
  <body>
  <div id=\"root\">""".format(
    vite_origin=vite_origin,
    nonce_attr=nonce_attr,
    module_load_reporter=module_load_reporter,
    react_refresh_preamble=react_refresh_preamble,
    before_interactive_scripts=before_interactive_scripts,
    global_styles=global_styles,
    inline_styles_markup=inline_styles_markup,
    head_block=head_block,
  )
  suffix = """
  </div>
  <script id=\"__PYXLE_PROPS__\" type=\"application/json\"{nonce_attr}>{props_payload}</script>
  <script id=\"__PYXLE_NAV_SEED__\" type=\"application/json\"{nonce_attr}>{nav_seed_payload}</script>
  <script{nonce_attr}>window.__PYXLE_PAGE_PATH__ = {page_path_literal};</script>
  <script{nonce_attr}>window.__PYXLE_LOADING_ASSET__ = {loading_asset_literal};</script>
  <script{nonce_attr}>window.__PYXLE_ERROR_ASSET__ = {error_asset_literal};</script>{nav_stale_script}{csrf_names_script}{auth_seed_script}
  <script{nonce_attr}>window.__PYXLE_SCRIPTS__ = {scripts_metadata};</script>
  <script type=\"module\" src=\"{vite_origin}/client-entry.js\"></script>
  </body>
</html>
""".format(
    nonce_attr=nonce_attr,
    props_payload=props_payload,
    nav_seed_payload=nav_seed_payload,
    page_path_literal=page_path_literal,
    loading_asset_literal=loading_asset_literal,
    error_asset_literal=error_asset_literal,
    scripts_metadata=scripts_metadata,
    nav_stale_script=nav_stale_script,
    csrf_names_script=csrf_names_script,
    auth_seed_script=auth_seed_script,
    vite_origin=vite_origin,
  )
  return DocumentShell(prefix=prefix, suffix=suffix)


def _render_nav_stale_script(settings: DevServerSettings, nonce_attr: str) -> str:
    """Render the ``__PYXLE_NAV_STALE_MS__`` bootstrap script, or ``""``.

    Bundles ``navigation.defaultPrefetchTtl`` (seconds) into the client as a
    millisecond default for the navigation cache. Returns an empty string when
    no default is configured, so the client keeps its built-in 2-minute
    fallback.
    """
    navigation = getattr(settings, "navigation", None)
    ttl_seconds = getattr(navigation, "default_prefetch_ttl", None)
    if ttl_seconds is None:
        return ""
    return f"\n  <script{nonce_attr}>window.__PYXLE_NAV_STALE_MS__ = {int(ttl_seconds) * 1000};</script>"


# Framework fallbacks baked into the generated client runtime
# (``pyxle/devserver/client_files.py``). Names matching these need no
# bootstrap script — the client falls back to them on its own.
_CSRF_FALLBACK_COOKIE_NAME = "pyxle-csrf"
_CSRF_DEFAULT_HEADER_NAME = "x-csrf-token"


def _render_csrf_names_script(settings: DevServerSettings, nonce_attr: str) -> str:
    """Render the ``__PYXLE_CSRF_COOKIE__`` / ``__PYXLE_CSRF_HEADER__``
    bootstrap script, or ``""``.

    The client runtime (``useAction`` / ``Form``) resolves the CSRF cookie
    and header names from these globals, falling back to the baked-in
    ``pyxle-csrf`` / ``x-csrf-token``. Without this script the effective
    names never reach the browser: the middleware sets the cookie under one
    name while the client keeps looking for another, so every action POST
    is rejected with 403.

    The effective cookie name is resolved exactly as the CSRF middleware
    resolves it: an explicit ``csrf.cookieName`` wins; otherwise the default
    is namespaced by the app's bind port (``pyxle-csrf-<port>``) so two
    Pyxle apps on one host never read each other's token. Because that
    auto-namespaced name differs from the client's baked-in fallback, it is
    injected too — only a pinned ``pyxle-csrf`` (or default header name)
    needs no script.
    """
    csrf = getattr(settings, "csrf", None)
    assignments: list[str] = []
    if csrf is not None:
        cookie_name = getattr(csrf, "cookie_name", None)
        if not (isinstance(cookie_name, str) and cookie_name):
            cookie_name = default_csrf_cookie_name(getattr(settings, "starlette_port", None))
        if cookie_name != _CSRF_FALLBACK_COOKIE_NAME:
            assignments.append(
                f"window.__PYXLE_CSRF_COOKIE__ = {_serialize_js_string(cookie_name)};"
            )
    header_name = getattr(csrf, "header_name", None)
    if (
        isinstance(header_name, str)
        and header_name
        and header_name.lower() != _CSRF_DEFAULT_HEADER_NAME
    ):
        assignments.append(f"window.__PYXLE_CSRF_HEADER__ = {_serialize_js_string(header_name)};")
    if not assignments:
        return ""
    return f"\n  <script{nonce_attr}>{' '.join(assignments)}</script>"


def _render_auth_seed_script(auth_seed: Any, nonce_attr: str) -> str:
    """Render the ``window.__PYXLE_AUTH__`` bootstrap script, or ``""``.

    ``auth_seed`` is the JSON-serializable blob an auth provider published on
    ``request.scope["pyxle.auth"]`` (the pyxle-auth plugin sets ``{"user":
    ..., "endpoints": {...}}``). The client ``useAuth`` hook reads the global
    to seed the signed-in user on the first frame and to discover the
    (possibly relocated) auth endpoints. Absent → no script, and ``useAuth``
    resolves the session over the network instead.
    """
    if auth_seed is _AUTH_SEED_ABSENT:
        return ""
    from pyxle.ssr._escape import escape_inline_json

    payload = escape_inline_json(
        json.dumps(auth_seed, ensure_ascii=False, separators=(",", ":"))
    )
    return f"\n  <script{nonce_attr}>window.__PYXLE_AUTH__ = {payload};</script>"


def _serialize_js_string(value: str) -> str:
    """Serialize a string as a JS literal safe for an inline ``<script>``."""
    from pyxle.ssr._escape import escape_inline_json

    return escape_inline_json(json.dumps(value, ensure_ascii=False))


def _serialize_props(props: dict[str, Any]) -> str:
    from pyxle.ssr._escape import escape_inline_json

    payload = json.dumps(props, ensure_ascii=False, separators=(",", ":"))
    return escape_inline_json(payload)


def render_head_markup(elements: tuple[str, ...], default_title: str = "Pyxle") -> str:
    """Render the merged head elements, inserting *default_title* if none is set.

    ``default_title`` is the app's own name (see
    ``DevServerSettings.document_title_default``) so an untitled page reads as
    the developer's product in the browser tab. The ``"Pyxle"`` fallback in the
    signature only covers callers that have no settings to hand.
    """
    if _head_contains_title(elements):
        title_markup = ""
    else:
        title_markup = f"\n    <title>{escape(default_title)}</title>"
    return title_markup + _render_custom_head(elements)


def _render_custom_head(elements: tuple[str, ...]) -> str:
    if not elements:
        return ""

    rendered: list[str] = []
    for fragment in elements:
        if not fragment:
            continue

        lines = fragment.splitlines() or [fragment]
        for line in lines:
            rendered.append(f"\n    {line}")

    return "".join(rendered)


def _head_contains_title(elements: tuple[str, ...]) -> bool:
    for fragment in elements:
        if "<title" in fragment.lower():
            return True
    return False


def _render_module_load_reporter(vite_origin: str, nonce_attr: str) -> str:
    """A dev-only listener that says when the page's modules never arrived.

    Every interactive part of a Pyxle page comes from a ``<script
    type="module">`` served by Vite, cross-origin. When Vite declines the
    document's origin the browser drops those responses and the page stays
    exactly as the server rendered it: complete, styled, and inert. Nothing in
    the page reports it — a refused module is not a JavaScript error, so no
    ``window.onerror`` handler and no framework overlay ever hears about it.

    Resource load failures do reach a *capturing* listener on ``window``, which
    is what this installs: one line in the console naming the module that never
    loaded, so the browser stops being the only place that says nothing. The
    server side of the same failure is
    :func:`pyxle.devserver.dev_origins.unhydratable_origin_warning`.
    """

    return """
    <script{nonce_attr}>
      window.addEventListener('error', function (event) {{
        var el = event.target;
        if (!el || el.tagName !== 'SCRIPT' || !el.src) {{ return; }}
        if (el.src.indexOf("{vite_origin}") !== 0) {{ return; }}
        console.error(
          '[Pyxle] ' + el.src + ' did not load, so this page will not become ' +
          'interactive. The usual cause is that the dev server does not serve ' +
          'modules to ' + window.location.origin + ': open the page at one of ' +
          'the addresses it printed at startup, or restart it with --host so ' +
          'it answers on this one.'
        );
      }}, true);
    </script>
""".format(vite_origin=vite_origin, nonce_attr=nonce_attr)


def _render_react_refresh_preamble(vite_origin: str, nonce_attr: str) -> str:
    return """
    <script type=\"module\"{nonce_attr}>
      import RefreshRuntime from \"{vite_origin}/@react-refresh\";
      RefreshRuntime.injectIntoGlobalHook(window);
      window.$RefreshReg$ = () => {{}};
      window.$RefreshSig$ = () => (type) => type;
      window.__vite_plugin_react_preamble_installed__ = true;
    </script>
""".format(vite_origin=vite_origin, nonce_attr=nonce_attr)


def _render_manifest_error(page: PageRoute) -> str:
    page_path = escape(page.path)
    return """<!DOCTYPE html>
<html lang=\"en\">
  <head>
    <meta charset=\"utf-8\" />
    <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
    <title>Pyxle • Missing Manifest Entry</title>
    <style>
      body {{ font-family: ui-sans-serif, system-ui; padding: 2rem; }}
      pre {{ background: #f3f4f6; padding: 1rem; border-radius: 0.5rem; }}
    </style>
  </head>
  <body>
    <main>
      <h1>Unable to locate page manifest entry</h1>
      <p>Pyxle could not find a compiled asset bundle for <code>{page_path}</code>.</p>
      <pre>Re-run `pyxle build` and ensure dist/page-manifest.json is deployed.</pre>
    </main>
  </body>
</html>
""".format(page_path=page_path)


_ERROR_DOCUMENT_STYLES = """    <style>
      body {
        font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, \"Segoe UI\", sans-serif;
        margin: 0;
        padding: 3rem 1.5rem;
        background: #111827;
        color: #f9fafb;
      }
      .pyxle-error {
        max-width: 48rem;
        margin: 0 auto;
        background: rgba(17, 24, 39, 0.65);
        border: 1px solid rgba(209, 213, 219, 0.2);
        border-radius: 0.75rem;
        padding: 2rem;
        box-shadow: 0 30px 60px rgba(15, 23, 42, 0.45);
      }
      .pyxle-error code {
        font-family: Menlo, Monaco, Consolas, \"Liberation Mono\", monospace;
        background: rgba(15, 23, 42, 0.6);
        padding: 0.25rem 0.5rem;
        border-radius: 0.5rem;
        color: #fca5a5;
      }
      .pyxle-hint {
        margin-top: 1.5rem;
        padding-top: 1.5rem;
        border-top: 1px solid rgba(209, 213, 219, 0.2);
        color: #9ca3af;
        font-size: 0.9375rem;
      }
    </style>"""


def render_error_document(
    *,
    settings: DevServerSettings,
    page: PageRoute,
    error: BaseException,
    status_code: int = 500,
    request_host: str | None = None,
) -> str:
    """Render a fallback HTML document when SSR fails.

    The output depends on ``settings.debug``:

    * **Dev mode** (``debug=True``): Returns a developer-friendly
      overlay containing the route path, exception type name, and
      exception message verbatim. Includes the Vite HMR client tag
      so the page reloads automatically when the developer fixes
      the error.

    * **Production mode** (``debug=False``): Returns a generic
      error page that does NOT include the exception type, message,
      route path, or any dev-mode tooling. Production responses
      must not leak internal state — exception messages may
      include database row IDs, API keys, file paths, or other
      sensitive details. Per ``CLAUDE.md`` rule 18, the
      production response is intentionally opaque; the actual
      error details are written to the server logs by the caller.
    """
    if not settings.debug:
        return _render_production_error_document(status_code)

    from pyxle.devserver._security import redact_sensitive_patterns

    vite_origin = _browser_vite_origin(settings, request_host)
    error_type = escape(error.__class__.__name__)
    raw_message = str(error) or error.__class__.__name__
    message = escape(redact_sensitive_patterns(raw_message))
    page_path = escape(page.path)

    return f"""<!DOCTYPE html>
<html lang=\"en\">
  <head>
    <meta charset=\"utf-8\" />
    <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
    <title>Pyxle • Error</title>
    <script type=\"module\" src=\"{vite_origin}/@vite/client\"></script>
{_ERROR_DOCUMENT_STYLES}
  </head>
  <body>
    <main class=\"pyxle-error\">
      <h1>Server Render Failed</h1>
      <p>While rendering <code>{page_path}</code>, Pyxle encountered a <strong>{error_type}</strong>.</p>
      <pre>{message}</pre>
      <p>Check your loader or component implementation and the server logs for full details.</p>
    </main>
  </body>
</html>
"""


#: What a production fallback says, per status. A loader raising
#: ``LoaderError(status_code=404)`` is stating a fact about the request, not
#: reporting a fault — and telling a visitor who followed a stale link that the
#: *server* failed sends them to complain to the wrong people, or to wait for a
#: recovery that is never coming.
#:
#: Deliberately short of a real 404 page: an application should answer a missing
#: resource itself, with its own layout and a way onward. This is the floor, not
#: the intended experience.
_STATUS_DOCUMENTS: Final[dict[int, tuple[str, str]]] = {
    400: ("Bad request", "This request could not be understood."),
    401: ("Sign in required", "This page needs you to be signed in."),
    402: ("Payment required", "This page needs an active subscription or payment."),
    403: ("Not available", "You do not have access to this page."),
    404: ("Not found", "There is nothing at this address."),
    405: ("Not allowed here", "This address does not accept that kind of request."),
    408: ("Request timed out", "The request took too long to send. Please try again."),
    409: ("Already changed", "Something changed before this request arrived. Reload and try again."),
    410: ("Gone", "This page used to exist and has been removed."),
    422: ("Could not process", "Some of the details in this request were not accepted."),
    429: ("Too many requests", "You have made too many requests. Please wait a moment and try again."),
    451: ("Unavailable for legal reasons", "This page cannot be shown for legal reasons."),
}

#: Fallback for a 4xx nobody has written wording for. The point of splitting
#: this from the 5xx text is that the *class* decides, not the table: a status
#: missing from the map above is still never described as a server fault, so
#: adding an entry is a refinement rather than a bug fix nobody remembers to
#: make. Blaming the server for a 429 tells a rate-limited visitor to wait for a
#: recovery that is not coming, and sends them to complain to the wrong people.
_CLIENT_ERROR_DOCUMENT: Final[tuple[str, str]] = (
    "Request not accepted",
    "The server did not accept this request.",
)

#: 5xx, and anything outside 4xx that reaches this path. The server *is* at
#: fault, and "try again later" is honest advice.
_SERVER_ERROR_DOCUMENT: Final[tuple[str, str]] = (
    "Server Error",
    "The server encountered an error while processing this request. "
    "Please try again later.",
)


def _status_document(status_code: int) -> tuple[str, str]:
    """Heading and detail for *status_code*, decided by class then refined.

    Specific wording wins where it exists; otherwise any 4xx is described as a
    request that was not accepted, and everything else as a server fault.
    """
    known = _STATUS_DOCUMENTS.get(status_code)
    if known is not None:
        return known
    if 400 <= status_code < 500:
        return _CLIENT_ERROR_DOCUMENT
    return _SERVER_ERROR_DOCUMENT


def _render_production_error_document(status_code: int = 500, *, hint_html: str = "") -> str:
    """Generic production fallback — leaks no internal state.

    Used when ``settings.debug`` is False. Intentionally omits the exception
    type, message, route path, and the dev-mode Vite client tag: an exception
    message may carry row ids, file paths or credentials (CLAUDE.md rule 18).

    The *status* is not internal state — the client already has it on the status
    line — so it decides the wording, by class: a 4xx is never described as a
    server fault, whether or not anyone has written specific wording for it
    (see :func:`_status_document`). 5xx keeps the opaque server-error text.

    ``hint_html`` appends developer-facing guidance and is only ever passed on
    a dev-mode path — never from :func:`render_error_document`'s production
    branch.
    """
    heading, detail = _status_document(status_code)
    return f"""<!DOCTYPE html>
<html lang=\"en\">
  <head>
    <meta charset=\"utf-8\" />
    <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
    <title>{heading}</title>
{_ERROR_DOCUMENT_STYLES}
  </head>
  <body>
    <main class=\"pyxle-error\">
      <h1>{heading}</h1>
      <p>{detail}</p>{hint_html}
    </main>
  </body>
</html>
"""


def render_not_found_document(*, debug: bool) -> str:
    """Pyxle's default 404 page, used when no ``not-found.pyxl`` answers.

    Reuses the same designed document as every other status fallback rather
    than Starlette's nine-byte ``text/plain`` body — a bare "Not Found" reads
    as if the framework fell over, not as if a page is simply missing.

    Under ``pyxle dev`` it also names the file that replaces it. That hint is
    strictly dev-only: in production the page stays as opaque as the other
    status documents.
    """
    hint_html = ""
    if debug:
        hint_html = (
            '\n      <p class="pyxle-hint">'
            "Pyxle is serving its built-in 404. Add "
            "<code>pages/not-found.pyxl</code> to replace it with your own page "
            "— it is picked up as soon as you save."
            "</p>"
        )
    return _render_production_error_document(404, hint_html=hint_html)


def _format_nonce_attr(value: str | None) -> str:
    if not value:
        return ""
    return f' nonce="{escape(value, quote=True)}"'


def _render_global_styles_markup(settings: DevServerSettings) -> str:
  if not settings.global_stylesheets:
    return ""
  fragments: list[str] = []
  for sheet, contents in load_inline_stylesheets(settings.global_stylesheets):
    escaped = _escape_style_contents(contents)
    fragments.append(f"\n  <style data-pyxle-style=\"{sheet.identifier}\">{escaped}</style>")
  return "".join(fragments)


def _escape_style_contents(value: str) -> str:
  if not value:
    return ""
  from pyxle.ssr._escape import escape_inline_json

  return escape_inline_json(value)


def _render_inline_styles_markup(styles: tuple[InlineStyleFragment, ...]) -> str:
  if not styles:
    return ""
  fragments: list[str] = []
  seen: set[str] = set()
  for style in styles:
    identifier = style.identifier
    if identifier in seen:
      continue
    seen.add(identifier)
    escaped_id = escape(identifier, quote=True)
    escaped_source = ""
    if style.source:
      escaped_source = f' data-pyxle-inline-source="{escape(style.source, quote=True)}"'
    escaped_contents = _escape_style_contents(style.contents)
    fragments.append(
      f"\n  <style data-pyxle-inline-style=\"{escaped_id}\"{escaped_source}>{escaped_contents}</style>"
    )
  return "".join(fragments)


def _is_unevaluated_script(script_dict: dict) -> bool:
  """Whether a statically-extracted ``<Script>`` still holds a JSX expression.

  ``<Script>`` declarations are harvested from ``.pyxl`` source at compile time,
  exactly like ``<Head>`` blocks, so ``<Script src={analyticsUrl} />`` arrives
  as the literal text ``{analyticsUrl}``. Emitting that produces a `<script>`
  pointing at a relative URL the browser requests and fails to find — the same
  failure the head merger drops, in a sibling code path.

  Dropping it loses nothing: the ``<Script>`` component loads the real src when
  it renders, and it deduplicates by src, so the evaluated load still happens
  exactly once.
  """
  return is_expression_value(script_dict.get("src")) or is_expression_value(
    script_dict.get("strategy")
  )


def _render_before_interactive_scripts(scripts: tuple[dict, ...], nonce_attr: str) -> str:
  """Render <script> tags for beforeInteractive strategy."""
  if not scripts:
    return ""

  fragments: list[str] = []
  for script_dict in scripts:
    if _is_unevaluated_script(script_dict):
      continue

    strategy = script_dict.get("strategy", "afterInteractive")
    if strategy != "beforeInteractive":
      continue

    src = script_dict.get("src")
    if not src:
      continue

    escaped_src = escape(src, quote=True)
    attrs: list[str] = [f'src="{escaped_src}"']
    
    if script_dict.get("async"):
      attrs.append("async")
    if script_dict.get("defer"):
      attrs.append("defer")
    if script_dict.get("module"):
      attrs.append('type="module"')
    elif script_dict.get("noModule"):
      attrs.append("nomodule")
    
    if nonce_attr:
      attrs.append(nonce_attr.strip())
    
    tag = f'<script {" ".join(attrs)}></script>'
    fragments.append(f"\n  {tag}")
  
  return "".join(fragments)


def _serialize_scripts_metadata(scripts: tuple[dict, ...]) -> str:
  """Serialize scripts metadata for client-side loading."""
  from pyxle.ssr._escape import escape_inline_json

  if not scripts:
    return "[]"

  # Filter out beforeInteractive scripts (already injected in head), and any
  # declaration still holding a JSX expression — the bootstrap loader would
  # inject `<script src="{analyticsUrl}">` into the document from this payload,
  # producing the same failing request in the browser that the head merger
  # drops on the server. The <Script> component loads the evaluated src itself.
  client_scripts = [
    s for s in scripts
    if s.get("strategy", "afterInteractive") != "beforeInteractive"
    and not _is_unevaluated_script(s)
  ]

  return escape_inline_json(json.dumps(client_scripts, ensure_ascii=False, separators=(",", ":")))

__all__ = [
  "DocumentShell",
  "ManifestLookupError",
  "build_document_shell",
  "render_document",
  "render_error_document",
  "render_head_markup",
  "render_not_found_document",
]
