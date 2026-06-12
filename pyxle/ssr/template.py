"""HTML document template helpers for SSR responses."""

from __future__ import annotations

import json
from dataclasses import dataclass
from html import escape
from typing import Any

from pyxle.devserver.routes import PageRoute
from pyxle.devserver.settings import DevServerSettings
from pyxle.devserver.styles import load_inline_stylesheets

from .renderer import InlineStyleFragment


def _browser_vite_origin(settings: DevServerSettings) -> str:
    """Return a browser-connectable origin for the Vite dev server.

    Bind addresses like ``0.0.0.0`` or ``::`` are valid for *listening* but
    browsers cannot connect to them.  Replace with ``localhost`` so that
    ``<script src="...">`` tags in the HTML actually resolve.
    """
    host = settings.vite_host
    if host in ("0.0.0.0", "::", ""):
        host = "localhost"
    return f"http://{host}:{settings.vite_port}"


@dataclass(frozen=True)
class DocumentShell:
  """Represents the static prefix/suffix for an HTML document."""

  prefix: str
  suffix: str


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
) -> DocumentShell:
  props_payload = _serialize_props(props)
  page_path_literal = json.dumps(page.client_asset_path)
  head_injections = render_head_markup(head_elements)
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

  if not settings.debug and settings.page_manifest is not None:
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
    
    before_interactive_scripts = _render_before_interactive_scripts(page.scripts, nonce_attr)
    scripts_metadata = _serialize_scripts_metadata(page.scripts)

    prefix = """<!DOCTYPE html>
<html lang=\"en\">
  <head>
  <meta charset=\"utf-8\" />
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />{css_html}{before_interactive_scripts}{global_styles}{inline_styles_markup}{head_block}
  </head>
  <body>
  <div id=\"root\">""".format(
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
  <script{nonce_attr}>window.__PYXLE_PAGE_PATH__ = {page_path_literal};</script>{nav_stale_script}{csrf_names_script}
  <script{nonce_attr}>window.__PYXLE_SCRIPTS__ = {scripts_metadata};</script>
  <script type=\"module\" src=\"{js_src}\"></script>
  </body>
</html>
""".format(
      nonce_attr=nonce_attr,
      props_payload=props_payload,
      nav_seed_payload=nav_seed_payload,
      page_path_literal=page_path_literal,
      scripts_metadata=scripts_metadata,
      nav_stale_script=nav_stale_script,
      csrf_names_script=csrf_names_script,
      js_src=js_src,
    )
    return DocumentShell(prefix=prefix, suffix=suffix)

  vite_origin = _browser_vite_origin(settings)
  react_refresh_preamble = _render_react_refresh_preamble(vite_origin, nonce_attr)
  before_interactive_scripts = _render_before_interactive_scripts(page.scripts, nonce_attr)
  scripts_metadata = _serialize_scripts_metadata(page.scripts)
  
  prefix = """<!DOCTYPE html>
<html lang=\"en\">
  <head>
  <meta charset=\"utf-8\" />
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
  <script type=\"module\" src=\"{vite_origin}/@vite/client\"{nonce_attr}></script>{react_refresh_preamble}{before_interactive_scripts}{global_styles}{inline_styles_markup}{head_block}
  </head>
  <body>
  <div id=\"root\">""".format(
    vite_origin=vite_origin,
    nonce_attr=nonce_attr,
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
  <script{nonce_attr}>window.__PYXLE_PAGE_PATH__ = {page_path_literal};</script>{nav_stale_script}{csrf_names_script}
  <script{nonce_attr}>window.__PYXLE_SCRIPTS__ = {scripts_metadata};</script>
  <script type=\"module\" src=\"{vite_origin}/client-entry.js\"></script>
  </body>
</html>
""".format(
    nonce_attr=nonce_attr,
    props_payload=props_payload,
    nav_seed_payload=nav_seed_payload,
    page_path_literal=page_path_literal,
    scripts_metadata=scripts_metadata,
    nav_stale_script=nav_stale_script,
    csrf_names_script=csrf_names_script,
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


# Framework defaults baked into the generated client runtime
# (``pyxle/devserver/client_files.py``). Names matching these need no
# bootstrap script — the client falls back to them on its own.
_CSRF_DEFAULT_COOKIE_NAME = "pyxle-csrf"
_CSRF_DEFAULT_HEADER_NAME = "x-csrf-token"


def _render_csrf_names_script(settings: DevServerSettings, nonce_attr: str) -> str:
    """Render the ``__PYXLE_CSRF_COOKIE__`` / ``__PYXLE_CSRF_HEADER__``
    bootstrap script, or ``""``.

    The client runtime (``useAction`` / ``Form``) resolves the CSRF cookie
    and header names from these globals, falling back to the framework
    defaults. Without this script a customised ``csrf.cookieName`` /
    ``csrf.headerName`` never reaches the browser: the middleware sets the
    cookie under the configured name while the client keeps looking for
    ``pyxle-csrf``, so every action POST is rejected with 403. Only
    non-default names are emitted, keeping default-config pages unchanged.
    """
    csrf = getattr(settings, "csrf", None)
    assignments: list[str] = []
    cookie_name = getattr(csrf, "cookie_name", None)
    if isinstance(cookie_name, str) and cookie_name and cookie_name != _CSRF_DEFAULT_COOKIE_NAME:
        assignments.append(f"window.__PYXLE_CSRF_COOKIE__ = {_serialize_js_string(cookie_name)};")
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


def _serialize_js_string(value: str) -> str:
    """Serialize a string as a JS literal safe for an inline ``<script>``."""
    from pyxle.ssr._escape import escape_inline_json

    return escape_inline_json(json.dumps(value, ensure_ascii=False))


def _serialize_props(props: dict[str, Any]) -> str:
    from pyxle.ssr._escape import escape_inline_json

    payload = json.dumps(props, ensure_ascii=False, separators=(",", ":"))
    return escape_inline_json(payload)


def render_head_markup(elements: tuple[str, ...]) -> str:
    default_title = "" if _head_contains_title(elements) else "\n    <title>Pyxle</title>"
    return default_title + _render_custom_head(elements)


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
    </style>"""


def render_error_document(
    *,
    settings: DevServerSettings,
    page: PageRoute,
    error: BaseException,
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
        return _render_production_error_document()

    from pyxle.devserver._security import redact_sensitive_patterns

    vite_origin = _browser_vite_origin(settings)
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


def _render_production_error_document() -> str:
    """Generic production error page — leaks no internal state.

    Used when ``settings.debug`` is False. Intentionally omits the
    exception type, message, route path, and the dev-mode Vite
    client tag.
    """
    return f"""<!DOCTYPE html>
<html lang=\"en\">
  <head>
    <meta charset=\"utf-8\" />
    <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
    <title>Server Error</title>
{_ERROR_DOCUMENT_STYLES}
  </head>
  <body>
    <main class=\"pyxle-error\">
      <h1>Server Error</h1>
      <p>The server encountered an error while processing this request. Please try again later.</p>
    </main>
  </body>
</html>
"""


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


def _render_before_interactive_scripts(scripts: tuple[dict, ...], nonce_attr: str) -> str:
  """Render <script> tags for beforeInteractive strategy."""
  if not scripts:
    return ""
  
  fragments: list[str] = []
  for script_dict in scripts:
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

  # Filter out beforeInteractive scripts (already injected in head)
  client_scripts = [
    s for s in scripts
    if s.get("strategy", "afterInteractive") != "beforeInteractive"
  ]

  return escape_inline_json(json.dumps(client_scripts, ensure_ascii=False, separators=(",", ":")))

__all__ = [
  "DocumentShell",
  "ManifestLookupError",
  "build_document_shell",
  "render_document",
  "render_error_document",
  "render_head_markup",
]
