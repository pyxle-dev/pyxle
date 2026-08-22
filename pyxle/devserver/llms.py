"""AI accessibility: per-page markdown responses and an ``/llms.txt`` index.

Enabled with the ``llms`` block in ``pyxle.config.json`` (see
:class:`pyxle.config.LlmsConfig`). When on, the framework serves a markdown
rendition of each page at its URL with ``.md`` appended — and to requests whose
``Accept`` header prefers ``text/markdown``, negotiated with RFC 9110 q-values
(see :func:`markdown_is_acceptable`) — advertises the index via ``Link`` /
``X-Llms-Txt`` discovery headers, and serves a generated ``/llms.txt``
(overridable by a static ``public/llms.txt``, which the static-asset middleware
serves first).

A page's markdown is resolved in order, first hit wins:

1. a co-located ``<page>.md`` file next to the ``.pyxl`` source,
2. a ``to_markdown`` handler in the page's own server module,
3. a ``to_markdown`` in the nearest ancestor ``llms.py`` — a per-directory
   module that covers a whole route subtree (closest ancestor wins, like
   ``layout.pyxl``); ``pages/llms.py`` at the root is the app-wide handler,
4. only if ``auto_convert`` is on, a best-effort HTML→markdown conversion,
5. otherwise the ``.md`` URL redirects to the page itself.

``/llms.txt`` is served from a static ``public/llms.txt``, else a ``llms_txt``
function in the root ``pages/llms.py``, else a generated index of the app's
pages. A root ``pages/llms.py`` may also define ``wrap_markdown(ctx, markdown)``
to frame every ``.md`` response with a header/footer (e.g. agent navigation and
search hints).

The whole feature is off by default and adds nothing to the page hot path — the
markdown routes are separate Starlette routes hit only for ``.md`` URLs.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import math
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional
from urllib.parse import urlsplit, urlunsplit

from starlette.datastructures import MutableHeaders
from starlette.requests import Request
from starlette.responses import PlainTextResponse, RedirectResponse, Response
from starlette.routing import Route

from .routes import PageRoute, RouteTable

logger = logging.getLogger("pyxle.devserver.llms")

#: Media type used for every markdown response the feature emits.
MARKDOWN_MEDIA_TYPE = "text/markdown; charset=utf-8"

#: Conventional name of a page's / directory's markdown handler.
LOCAL_HANDLER_NAME = "to_markdown"

#: Conventional name of the ``/llms.txt`` generator in the root ``pages/llms.py``.
LLMS_TXT_HOOK_NAME = "llms_txt"

#: Conventional name of the markdown wrapper (header/footer) in root ``pages/llms.py``.
WRAP_HOOK_NAME = "wrap_markdown"

#: Well-known path for the index.
LLMS_TXT_PATH = "/llms.txt"


def is_enabled(config: Any) -> bool:
    """Return ``True`` when a resolved ``LlmsConfig`` has the feature enabled."""
    return bool(config is not None and getattr(config, "enabled", False))


# ---------------------------------------------------------------------------
# Handler context
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MarkdownContext:
    """Context passed to a ``to_markdown`` or global markdown handler.

    ``request`` is the incoming Starlette request (a ``.md`` URL or an
    ``Accept: text/markdown`` request). ``path`` is the canonical page path the
    markdown represents (without the ``.md`` suffix). ``render_html`` renders the
    original page — running its loader and SSR — and returns the body HTML;
    ``run_loader`` runs only the page's ``@server`` loader and returns its data,
    skipping the render. Call whichever you need — both are lazy.
    """

    request: Request
    path: str
    _render_html: Callable[[], Awaitable[str]]
    _run_loader: Callable[[], Awaitable[Any]]

    async def render_html(self) -> str:
        """Render the original page and return its body HTML."""
        return await self._render_html()

    async def run_loader(self) -> Any:
        """Run the page's ``@server`` loader and return its data (no render).

        A cheaper alternative to :meth:`render_html` when you only need the
        loader's return value. Returns ``{}`` for a page with no loader.
        """
        return await self._run_loader()


async def _call_handler(handler: Callable[..., Any], ctx: MarkdownContext) -> Optional[str]:
    """Invoke a markdown handler (sync or async) and validate its result."""
    result = handler(ctx)
    if inspect.isawaitable(result):
        result = await result
    if result is None:
        return None
    if not isinstance(result, str):
        raise TypeError(
            f"Markdown handler {getattr(handler, '__qualname__', handler)!r} must "
            f"return a string or None, got {type(result).__name__}."
        )
    return result


# ---------------------------------------------------------------------------
# Resolution ladder
# ---------------------------------------------------------------------------


def colocated_markdown_path(page: PageRoute) -> Path:
    """Return the co-located ``<page>.md`` path next to a page's ``.pyxl`` source."""
    return page.source_absolute_path.with_suffix(".md")


def _read_text_if_file(path: Path) -> Optional[str]:
    try:
        if path.is_file():
            return path.read_text(encoding="utf-8")
    except OSError:
        return None
    return None


async def _read_colocated_markdown(page: PageRoute) -> Optional[str]:
    return await asyncio.to_thread(_read_text_if_file, colocated_markdown_path(page))


def _load_local_handler(page: PageRoute, *, debug: bool) -> Optional[Callable[..., Any]]:
    """Return the page's ``to_markdown`` server-module function, if defined."""
    from pyxle.ssr.view import _import_server_module  # lazy: avoid import cycle

    try:
        module = _import_server_module(page.module_key, page.server_module_path, debug=debug)
    except Exception:  # pragma: no cover - defensive; missing module falls through
        logger.debug("Could not import server module for %s", page.path, exc_info=True)
        return None
    handler = getattr(module, LOCAL_HANDLER_NAME, None)
    return handler if callable(handler) else None


#: Conventional per-directory module hosting AI hooks (currently ``to_markdown``)
#: for a whole route subtree. The nearest ancestor to the page wins.
DIRECTORY_MODULE_NAME = "llms.py"


def _directory_handler_candidates(page: PageRoute, settings: Any):
    """Yield ``(llms.py path, module_key)`` from the page's dir up to the root."""
    pages_dir = Path(getattr(settings, "pages_dir"))
    parts = page.source_relative_path.parent.parts
    for depth in range(len(parts), -1, -1):
        sub_parts = parts[:depth]
        directory = pages_dir.joinpath(*sub_parts) if sub_parts else pages_dir
        tag = "_".join(sub_parts) or "root"
        yield directory / DIRECTORY_MODULE_NAME, f"_pyxle_llms_{tag}"


def _iter_directory_handlers(page: PageRoute, settings: Any, *, debug: bool):
    """Yield ``llms.py`` ``to_markdown`` handlers, nearest ancestor first.

    Walks from the page's own directory up to ``pages/``. Each handler may
    decline (return ``None``) to defer to a broader ancestor, so more than one
    can participate — the closest one that returns a string wins.
    """
    from pyxle.ssr.view import _import_server_module  # lazy: avoid import cycle

    for path, module_key in _directory_handler_candidates(page, settings):
        if not path.is_file():
            continue
        try:
            module = _import_server_module(module_key, path, debug=debug)
        except Exception:  # pragma: no cover - defensive; a broken handler module
            logger.exception("Failed to import directory markdown handler %s", path)
            continue
        handler = getattr(module, LOCAL_HANDLER_NAME, None)
        if callable(handler):
            yield handler


def _load_root_llms_attr(settings: Any, attr: str, *, debug: bool) -> Optional[Callable[..., Any]]:
    """Return a callable named ``attr`` from the root ``pages/llms.py``, if any."""
    from pyxle.ssr.view import _import_server_module  # lazy: avoid import cycle

    path = Path(getattr(settings, "pages_dir")) / DIRECTORY_MODULE_NAME
    if not path.is_file():
        return None
    try:
        module = _import_server_module("_pyxle_llms_root", path, debug=debug)
    except Exception:  # pragma: no cover - defensive; a broken module
        logger.exception("Failed to import root %s", path)
        return None
    fn = getattr(module, attr, None)
    return fn if callable(fn) else None


def strip_md_suffix(path: str) -> str:
    """Map a ``.md`` request path back to its canonical page path.

    ``/about.md`` -> ``/about``; ``/index.md`` (the root) -> ``/``.
    """
    base = path[:-3] if path.endswith(".md") else path
    if base == "/index":
        return "/"
    if base.endswith("/index"):
        base = base[: -len("index")]  # keep the trailing slash
    return base or "/"


async def resolve_page_markdown(
    *,
    request: Request,
    page: PageRoute,
    settings: Any,
    renderer: Any,
    config: Any,
) -> Optional[str]:
    """Resolve a page's markdown, then apply the optional wrap hook.

    Returns the markdown string, or ``None`` when no source resolves (the caller
    then redirects the ``.md`` URL to the page itself).
    """
    debug = bool(getattr(settings, "debug", False))

    async def _render() -> str:
        from pyxle.ssr.view import render_page_body_html  # lazy: avoid import cycle

        body_html, _status = await render_page_body_html(
            request=request, settings=settings, page=page, renderer=renderer
        )
        return body_html

    async def _loader() -> Any:
        from pyxle.ssr.view import run_page_loader  # lazy: avoid import cycle

        return await run_page_loader(request=request, settings=settings, page=page)

    ctx = MarkdownContext(
        request=request,
        path=strip_md_suffix(request.url.path),
        _render_html=_render,
        _run_loader=_loader,
    )

    markdown = await _resolve_source_markdown(
        ctx, page=page, settings=settings, config=config, debug=debug
    )
    if markdown is None:
        return None

    wrapped = await _apply_wrap_hook(markdown, ctx, settings, debug=debug)
    return wrapped if wrapped is not None else markdown


async def _resolve_source_markdown(
    ctx: MarkdownContext,
    *,
    page: PageRoute,
    settings: Any,
    config: Any,
    debug: bool,
) -> Optional[str]:
    """Run the markdown resolution ladder (no wrap). First hit wins, else None."""
    # 1. Co-located <page>.md
    colocated = await _read_colocated_markdown(page)
    if colocated is not None:
        return colocated

    # 2. Page-local `to_markdown` in the page's own server module
    local = _load_local_handler(page, debug=debug)
    if local is not None:
        result = await _call_handler(local, ctx)
        if result is not None:
            return result

    # 3. Ancestor `llms.py` handlers (nearest first), each covering a route
    #    subtree; a handler may return None to defer to a broader ancestor.
    for handler in _iter_directory_handlers(page, settings, debug=debug):
        result = await _call_handler(handler, ctx)
        if result is not None:
            return result

    # 4. Best-effort HTML -> markdown (opt-in only)
    if getattr(config, "auto_convert", False):
        return html_to_markdown(await ctx.render_html())

    # 5. Nothing resolved.
    return None


async def _apply_wrap_hook(
    markdown: str, ctx: MarkdownContext, settings: Any, *, debug: bool
) -> Optional[str]:
    """Apply the root ``pages/llms.py`` ``wrap_markdown`` hook, if defined.

    Lets an app frame every ``.md`` response with a header/footer (e.g. agent
    navigation/search hints). Returns the wrapped markdown, or ``None`` to keep
    the resolved markdown unchanged.
    """
    hook = _load_root_llms_attr(settings, WRAP_HOOK_NAME, debug=debug)
    if hook is None:
        return None
    result = hook(ctx, markdown)
    if inspect.isawaitable(result):
        result = await result
    if result is None:
        return None
    if not isinstance(result, str):
        raise TypeError(
            f"{WRAP_HOOK_NAME} must return a string or None, got {type(result).__name__}."
        )
    return result


# ---------------------------------------------------------------------------
# Best-effort HTML -> markdown (dependency-free, opt-in via auto_convert)
# ---------------------------------------------------------------------------

_SKIP_TAGS = frozenset({"script", "style", "head", "noscript", "svg", "template"})
_HEADING_TAGS = {f"h{n}": "#" * n for n in range(1, 7)}
_BLOCK_TAGS = frozenset(
    {
        "p", "div", "section", "article", "header", "footer", "main", "nav",
        "aside", "figure", "figcaption", "table", "thead", "tbody", "tr",
        "blockquote", "form", "details", "summary",
    }
)


def is_api_url_path(path: str) -> bool:
    """Return ``True`` when a URL path addresses an API route.

    The server's rule, read off the URL instead of the source file: a path
    addresses an endpoint when any of its segments is exactly ``api``, at any
    depth. Mirrors :func:`pyxle.devserver.scanner._in_api_directory`, which
    decides the same thing from the file's path — ``pages/s/[slug]/api/v2/
    summary.json.py`` serves ``/s/{slug}/api/v2/summary.json``, so a test on
    the literal prefix ``/api/`` recognises only the top-level endpoints.
    Keep this in step with that function, and with ``IS_API_PATH_JS`` in
    :mod:`pyxle.devserver.client_files`, which is the same rule in JavaScript.
    """

    return "api" in path.split("/")


def _rewrite_internal_href(href: str) -> str:
    """Rewrite an internal page href to its ``.md`` URL; leave everything else.

    Rewrites relative and root-relative links to extensionless page paths —
    ``/about?x=1#y`` → ``/about.md?x=1#y``, ``/`` → ``/index.md`` — keeping
    query strings and fragments intact. Left untouched: URLs with a scheme or
    host (``https://…``, ``mailto:``, ``tel:``, protocol-relative ``//host``),
    API routes at any depth (``/api/health``, ``/s/acme/api/v2/export``),
    fragment- or query-only links, and paths whose last segment has a file
    extension (assets, and links already ending in ``.md``).
    """
    try:
        parts = urlsplit(href)
    except ValueError:
        return href
    if parts.scheme or parts.netloc:
        return href
    path = parts.path
    if not path:
        return href
    if is_api_url_path(path):
        return href
    last_segment = path.rstrip("/").rsplit("/", 1)[-1]
    if "." in last_segment:
        return href
    md_path = "/index.md" if path == "/" else path.rstrip("/") + ".md"
    return urlunsplit(("", "", md_path, parts.query, parts.fragment))


class _MarkdownExtractor(HTMLParser):
    """Convert a fragment of rendered HTML into approximate markdown.

    Deliberately small and dependency-free: it covers the common structural
    tags (headings, paragraphs, lists, links, emphasis, code, blockquotes,
    rules) and drops everything it doesn't understand to plain text. With
    ``rewrite_links`` internal page hrefs are rewritten to their ``.md``
    renditions (see :func:`_rewrite_internal_href`). This backs the opt-in
    ``auto_convert`` fallback only — author-provided markdown or a handler
    always produces cleaner output.
    """

    def __init__(self, *, rewrite_links: bool = False) -> None:
        super().__init__(convert_charrefs=True)
        self._rewrite_links = rewrite_links
        self._out: list[str] = []
        self._skip_depth = 0
        self._pre_depth = 0
        self._list_stack: list[dict[str, Any]] = []  # {"ordered": bool, "n": int}
        self._quote_depth = 0
        self._pending_prefix: str | None = None

    # -- helpers ----------------------------------------------------------
    def _emit(self, text: str) -> None:
        self._out.append(text)

    def _newline(self, count: int = 1) -> None:
        self._out.append("\n" * count)

    # -- tag handling -----------------------------------------------------
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self._skip_depth:
            if tag in _SKIP_TAGS:
                self._skip_depth += 1
            return
        if tag in _SKIP_TAGS:
            self._skip_depth = 1
            return
        if tag in _HEADING_TAGS:
            self._newline(2)
            self._emit(_HEADING_TAGS[tag] + " ")
        elif tag == "br":
            self._newline()
        elif tag == "hr":
            self._newline(2)
            self._emit("---")
            self._newline(2)
        elif tag in ("strong", "b"):
            self._emit("**")
        elif tag in ("em", "i"):
            self._emit("*")
        elif tag == "code" and not self._pre_depth:
            self._emit("`")
        elif tag == "pre":
            self._pre_depth += 1
            self._newline(2)
            self._emit("```\n")
        elif tag == "a":
            href = _attr(attrs, "href") or ""
            if href and self._rewrite_links:
                href = _rewrite_internal_href(href)
            self._emit("[")
            self._pending_prefix = href
        elif tag in ("ul", "ol"):
            self._list_stack.append({"ordered": tag == "ol", "n": 0})
        elif tag == "li":
            self._newline()
            marker = "- "
            if self._list_stack:
                top = self._list_stack[-1]
                if top["ordered"]:
                    top["n"] += 1
                    marker = f"{top['n']}. "
                self._emit("  " * (len(self._list_stack) - 1) + marker)
            else:
                self._emit(marker)
        elif tag == "blockquote":
            self._quote_depth += 1
            self._newline(2)
        elif tag in _BLOCK_TAGS:
            self._newline(2)

    def handle_endtag(self, tag: str) -> None:
        if self._skip_depth:
            if tag in _SKIP_TAGS:
                self._skip_depth -= 1
            return
        if tag in _HEADING_TAGS:
            self._newline(2)
        elif tag in ("strong", "b"):
            self._emit("**")
        elif tag in ("em", "i"):
            self._emit("*")
        elif tag == "code" and not self._pre_depth:
            self._emit("`")
        elif tag == "pre":
            self._pre_depth = max(0, self._pre_depth - 1)
            self._emit("\n```")
            self._newline(2)
        elif tag == "a" and self._pending_prefix is not None:
            href = self._pending_prefix
            self._pending_prefix = None
            self._emit(f"]({href})" if href else "]")
        elif tag in ("ul", "ol"):
            if self._list_stack:
                self._list_stack.pop()
            if not self._list_stack:
                self._newline(2)
        elif tag == "blockquote":
            self._quote_depth = max(0, self._quote_depth - 1)
            self._newline(2)
        elif tag in _BLOCK_TAGS:
            self._newline(2)

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        if self._pre_depth:
            self._emit(data)
            return
        text = " ".join(data.split())
        if not text:
            return
        # Preserve a single leading/trailing space when the source had one, so
        # inline elements don't get glued to adjacent words.
        if data[:1].isspace():
            text = " " + text
        if data[-1:].isspace():
            text = text + " "
        self._emit(text)

    def result(self) -> str:
        text = "".join(self._out)
        # Collapse 3+ blank lines and trim.
        lines = [line.rstrip() for line in text.split("\n")]
        collapsed: list[str] = []
        blanks = 0
        for line in lines:
            if line:
                blanks = 0
                collapsed.append(line)
            else:
                blanks += 1
                if blanks <= 1:
                    collapsed.append("")
        return "\n".join(collapsed).strip() + "\n"


def _attr(attrs: list[tuple[str, str | None]], name: str) -> str | None:
    for key, value in attrs:
        if key == name:
            return value
    return None


def html_to_markdown(html: str, *, rewrite_links: bool = True) -> str:
    """Convert rendered HTML to approximate markdown (best-effort, lossy).

    By default internal page links are rewritten to their ``.md`` renditions
    (``/about`` → ``/about.md``, preserving query strings and fragments) so an
    agent following links stays on the markdown channel; pass
    ``rewrite_links=False`` to keep hrefs verbatim. External URLs,
    ``mailto:``/``tel:``, API routes at any depth, asset paths with file
    extensions, and links already ending in ``.md`` are never touched.

    Relative (non root-relative) hrefs are rewritten in place, so on a
    directory-index page — whose markdown rendition serves from ``/deep.md``
    rather than ``/deep/`` — a link like ``child`` resolves against the
    shifted base. Prefer root-relative hrefs (``/deep/child``) in pages meant
    for markdown consumption.
    """
    parser = _MarkdownExtractor(rewrite_links=rewrite_links)
    parser.feed(html)
    parser.close()
    return parser.result()


# ---------------------------------------------------------------------------
# /llms.txt index
# ---------------------------------------------------------------------------


def _humanize_segment(segment: str) -> str:
    words = segment.replace("-", " ").replace("_", " ").split()
    return " ".join(word[:1].upper() + word[1:] for word in words) if words else segment


def _label_for(page: PageRoute) -> str:
    path = page.path.rstrip("/")
    if not path:
        return "Home"
    return _humanize_segment(path.rsplit("/", 1)[-1])


def _md_url_for(page_path: str) -> str:
    return "/index.md" if page_path == "/" else page_path.rstrip("/") + ".md"


def _listable_pages(routes: RouteTable) -> list[PageRoute]:
    """Concrete (non-parameterised) pages, de-duplicated and sorted by path."""
    seen: set[str] = set()
    pages: list[PageRoute] = []
    for page in routes.pages:
        if "{" in page.path or page.path in seen:
            continue
        seen.add(page.path)
        pages.append(page)
    pages.sort(key=lambda p: p.path)
    return pages


def _default_title(settings: Any) -> str:
    root = getattr(settings, "project_root", None)
    if root is not None:
        return _humanize_segment(Path(root).name) or "Pyxle app"
    return "Pyxle app"


@dataclass(frozen=True)
class LlmsPageInfo:
    """A concrete (non-parameterised) page surfaced to a ``llms_txt`` hook."""

    path: str
    md_url: str
    title: str


@dataclass(frozen=True)
class LlmsTxtContext:
    """Context passed to a root ``pages/llms.py`` ``llms_txt`` hook.

    ``pages`` lists the app's concrete pages; ``render_default`` returns the
    framework's generated index, so a hook can return it verbatim, tweak it, or
    build a fully custom index (for example from a docs manifest).
    """

    request: Request
    pages: tuple[LlmsPageInfo, ...]
    _render_default: Callable[[], str]

    def render_default(self) -> str:
        """Return the framework's generated ``/llms.txt``.

        Performs filesystem checks and module imports. Sync ``llms_txt`` hooks
        are invoked in a worker thread, so calling this from one is always
        safe; an *async* hook runs on the event loop and should wrap it —
        ``await asyncio.to_thread(ctx.render_default)``.
        """
        return self._render_default()


def _page_infos(routes: RouteTable) -> tuple[LlmsPageInfo, ...]:
    return tuple(
        LlmsPageInfo(path=page.path, md_url=_md_url_for(page.path), title=_label_for(page))
        for page in _listable_pages(routes)
    )


def _page_serves_markdown(page: PageRoute, *, settings: Any, config: Any, debug: bool) -> bool:
    """Statically walk the resolution ladder: does ``page`` have a markdown source?

    Mirrors :func:`_resolve_source_markdown` without invoking handlers: a
    co-located ``.md`` file, a page-local ``to_markdown``, an ancestor
    ``llms.py`` ``to_markdown``, or ``auto_convert`` means the page's ``.md``
    URL serves markdown; otherwise it just redirects to the HTML page. A
    handler that exists but declines at request time counts as markdown — its
    answer can't be known without running it.
    """
    if getattr(config, "auto_convert", False):
        return True
    if colocated_markdown_path(page).is_file():
        return True
    if _load_local_handler(page, debug=debug) is not None:
        return True
    return next(_iter_directory_handlers(page, settings, debug=debug), None) is not None


def build_llms_txt(
    *,
    routes: RouteTable,
    settings: Any,
    config: Any = None,
    base_url: str = "",
) -> str:
    """Generate a spec-shaped ``/llms.txt`` index from the route table.

    Produces an H1 title (the project directory name) and a ``## Pages`` list.
    Each concrete page is checked against the same resolution ladder ``.md``
    requests use: pages with a markdown source (every page when
    ``config.auto_convert`` is on) are linked at their ``.md`` URL, while pages
    whose ``.md`` URL would just redirect are listed at their canonical HTML
    URL instead — the index never advertises markdown that isn't there.

    ``base_url`` (e.g. ``"https://example.com"``, no trailing slash) makes the
    links absolute; when empty, links are root-relative. Dynamic
    (parameterised) routes are omitted since they have no single URL to list —
    apps with dynamic content should provide a ``llms_txt`` hook or a static
    ``public/llms.txt``.

    Performs filesystem checks and module imports, so call it off the event
    loop — the built-in route runs it via ``asyncio.to_thread``.
    """
    debug = bool(getattr(settings, "debug", False))
    base = base_url.rstrip("/")
    lines = [f"# {_default_title(settings)}", "", "## Pages", ""]
    for page in _listable_pages(routes):
        if _page_serves_markdown(page, settings=settings, config=config, debug=debug):
            url = base + _md_url_for(page.path)
        else:
            url = base + page.path
        lines.append(f"- [{_label_for(page)}]({url})")
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Accept-header negotiation + discovery headers
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _MediaRange:
    """One parsed ``Accept`` media-range: lowercased type/subtype plus q-weight."""

    type: str
    subtype: str
    q: float


def _split_accept_items(header: str) -> list[str]:
    """Split an ``Accept`` header on commas, honouring quoted-string parameters."""
    items: list[str] = []
    current: list[str] = []
    in_quotes = False
    escaped = False
    for char in header:
        if escaped:
            current.append(char)
            escaped = False
        elif char == "\\" and in_quotes:
            current.append(char)
            escaped = True
        elif char == '"':
            in_quotes = not in_quotes
            current.append(char)
        elif char == "," and not in_quotes:
            items.append("".join(current))
            current = []
        else:
            current.append(char)
    items.append("".join(current))
    return items


def _parse_q(raw: str) -> float:
    """Parse a ``q`` parameter value: default 1.0 on malformed input, clamped 0..1."""
    try:
        q = float(raw)
    except ValueError:
        return 1.0
    if math.isnan(q):
        return 1.0
    return min(max(q, 0.0), 1.0)


def _parse_accept(header: str) -> tuple[_MediaRange, ...]:
    """Parse an ``Accept`` header into media-ranges; malformed ranges are dropped."""
    ranges: list[_MediaRange] = []
    for item in _split_accept_items(header):
        media, _, params = item.partition(";")
        type_, sep, subtype = media.strip().lower().partition("/")
        type_ = type_.strip()
        subtype = subtype.strip()
        if not sep or not type_ or not subtype or "/" in subtype:
            continue
        q = 1.0
        for param in params.split(";"):
            key, eq, value = param.partition("=")
            if key.strip().lower() == "q" and eq:
                q = _parse_q(value.strip().strip('"'))
                break  # parameters after q are accept-ext (RFC 9110 §12.4.2)
        ranges.append(_MediaRange(type_, subtype, q))
    return tuple(ranges)


def _acceptance_q(
    ranges: tuple[_MediaRange, ...], type_: str, subtype: str, *, exact_only: bool = False
) -> float:
    """Effective q-weight of ``type_/subtype`` under ``ranges``.

    The most specific matching range wins (exact ``type/subtype`` over
    ``type/*`` over ``*/*``, per RFC 9110 §12.5.1); among equally specific
    ranges the last declaration wins. Returns ``0.0`` when nothing matches —
    the media type is not acceptable. With ``exact_only`` wildcard ranges are
    ignored, so only an explicit ``type/subtype`` entry can make it acceptable.
    """
    exact: Optional[float] = None
    subtype_wildcard: Optional[float] = None
    full_wildcard: Optional[float] = None
    for media_range in ranges:
        if media_range.type == type_ and media_range.subtype == subtype:
            exact = media_range.q
        elif media_range.type == type_ and media_range.subtype == "*":
            subtype_wildcard = media_range.q
        elif media_range.type == "*" and media_range.subtype == "*":
            full_wildcard = media_range.q
    if exact is not None:
        return exact
    if exact_only:
        return 0.0
    if subtype_wildcard is not None:
        return subtype_wildcard
    if full_wildcard is not None:
        return full_wildcard
    return 0.0


def markdown_is_acceptable(accept: str) -> bool:
    """Decide whether ``text/markdown`` should be served for an ``Accept`` header.

    Implements RFC 9110 §12.5.1 negotiation for the one choice the feature
    makes — markdown vs HTML:

    - Media-ranges match by exact ``type/subtype`` token, never by substring;
      wildcards (``*/*``, ``text/*``) never select markdown, so browser Accept
      headers keep getting HTML.
    - ``q`` weights are honoured: default ``1.0``, clamped to ``0..1``, and
      ``q=0`` means *not acceptable*.
    - Markdown is served iff ``text/markdown`` is explicitly acceptable
      (``q > 0``) **and** weighted at least as high as the effective weight of
      ``text/html`` (which does benefit from wildcard ranges).
    - Never raises: malformed ranges are ignored and an unusable header falls
      back to HTML.
    """
    if not accept:
        return False
    ranges = _parse_accept(accept)
    markdown_q = _acceptance_q(ranges, "text", "markdown", exact_only=True)
    if markdown_q <= 0.0:
        return False
    return markdown_q >= _acceptance_q(ranges, "text", "html")


def wants_markdown(request: Request) -> bool:
    """Return ``True`` when a request's ``Accept`` header prefers ``text/markdown``.

    Browsers never send ``text/markdown`` in ``Accept``, so this only fires for
    agents that opt in — the canonical HTML URL is unchanged for humans. The
    negotiation rules live in :func:`markdown_is_acceptable`.
    """
    return markdown_is_acceptable(request.headers.get("accept", ""))


class LlmsDiscoveryMiddleware:
    """Advertise the ``/llms.txt`` index via ``Link`` and ``X-Llms-Txt`` headers.

    Pure ASGI (never buffers the body) so it is safe in front of streaming SSR.
    """

    def __init__(self, app: Any, *, index_path: str = LLMS_TXT_PATH) -> None:
        self.app = app
        self._index = index_path
        self._link = f'<{index_path}>; rel="llms-txt"'

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        async def send_wrapper(message: Any) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(raw=message.setdefault("headers", []))
                existing = headers.get("link")
                headers["link"] = f"{existing}, {self._link}" if existing else self._link
                headers["x-llms-txt"] = self._index
            await send(message)

        await self.app(scope, receive, send_wrapper)


# ---------------------------------------------------------------------------
# Route construction
# ---------------------------------------------------------------------------


def markdown_route_path(page_path: str) -> str:
    """Return the ``.md`` route pattern for a page route path.

    ``/`` -> ``/index.md``; ``/about`` -> ``/about.md``;
    ``/docs/{slug:path}`` -> ``/docs/{slug:path}.md``.
    """
    if page_path == "/":
        return "/index.md"
    return page_path.rstrip("/") + ".md"


def make_markdown_route_handler(
    page: PageRoute,
    *,
    settings: Any,
    renderer: Any,
    config: Any,
) -> Callable[[Request], Awaitable[Response]]:
    """Build the Starlette handler serving ``<page>.md``."""

    async def handler(request: Request) -> Response:
        try:
            markdown = await resolve_page_markdown(
                request=request,
                page=page,
                settings=settings,
                renderer=renderer,
                config=config,
            )
        except Exception:
            # Never surface internals on the .md channel — log and gracefully
            # fall back to the HTML page, which carries the same content.
            logger.exception("Markdown rendering failed for %s", request.url.path)
            markdown = None
        if markdown is None:
            return RedirectResponse(strip_md_suffix(request.url.path), status_code=307)
        return PlainTextResponse(markdown, media_type=MARKDOWN_MEDIA_TYPE)

    handler.__name__ = f"markdown_{page.module_key.replace('.', '_')}"
    return handler


def build_markdown_routes(
    routes: RouteTable,
    *,
    settings: Any,
    renderer: Any,
    config: Any,
) -> list[Route]:
    """Build one ``.md`` route per page route (registered before page routes)."""
    built: list[Route] = []
    seen: set[str] = set()
    for page in routes.pages:
        md_path = markdown_route_path(page.path)
        if md_path in seen:
            continue
        seen.add(md_path)
        built.append(
            Route(
                md_path,
                make_markdown_route_handler(
                    page, settings=settings, renderer=renderer, config=config
                ),
                methods=["GET", "HEAD"],
            )
        )
    return built


def _load_llms_txt_hook(settings: Any, *, debug: bool) -> Optional[Callable[..., Any]]:
    """Return the ``llms_txt`` function from the root ``pages/llms.py``, if any."""
    return _load_root_llms_attr(settings, LLMS_TXT_HOOK_NAME, debug=debug)


def make_llms_txt_route(routes: RouteTable, *, settings: Any) -> Route:
    """Build the ``/llms.txt`` route.

    Resolution order: a static ``public/llms.txt`` (served by the static-asset
    middleware before this route ever runs) → a ``llms_txt`` hook in the root
    ``pages/llms.py`` → a generated index of the app's pages. Generated links
    are absolute, derived from the client's scheme (``X-Forwarded-Proto``
    where present) and host, so they stay ``https://`` behind a TLS-terminating
    proxy on any host -- not only one uvicorn happens to trust.
    """
    config = getattr(settings, "llms", None)

    async def handler(request: Request) -> Response:
        debug = bool(getattr(settings, "debug", False))
        # The client's scheme, not the socket's: behind a TLS-terminating
        # proxy on another host these links were emitted as ``http://`` on an
        # HTTPS site -- a downgrade handed to the exact audience, agents, that
        # follows them literally.
        from pyxle.devserver.dev_origins import forwarded_scheme  # noqa: PLC0415

        base_url = f"{forwarded_scheme(request.scope)}://{request.url.netloc}"

        def render_default() -> str:
            return build_llms_txt(
                routes=routes, settings=settings, config=config, base_url=base_url
            )

        hook = _load_llms_txt_hook(settings, debug=debug)
        if hook is not None:
            ctx = LlmsTxtContext(
                request=request,
                pages=_page_infos(routes),
                _render_default=render_default,
            )
            try:
                # Sync hooks run in a worker thread so a hook that calls
                # ``ctx.render_default()`` (filesystem checks + module imports)
                # never blocks the event loop. Async hooks own their loop
                # discipline — see ``LlmsTxtContext.render_default``.
                if inspect.iscoroutinefunction(hook):
                    result = await hook(ctx)
                else:
                    result = await asyncio.to_thread(hook, ctx)
                if inspect.isawaitable(result):
                    result = await result
            except Exception:
                logger.exception("Root llms.py 'llms_txt' hook failed")
                result = None
            if isinstance(result, str):
                return PlainTextResponse(result, media_type=MARKDOWN_MEDIA_TYPE)
        return PlainTextResponse(
            await asyncio.to_thread(render_default), media_type=MARKDOWN_MEDIA_TYPE
        )

    handler.__name__ = "llms_txt"
    return Route(LLMS_TXT_PATH, handler, methods=["GET", "HEAD"])


__all__ = [
    "LLMS_TXT_PATH",
    "MARKDOWN_MEDIA_TYPE",
    "LlmsDiscoveryMiddleware",
    "LlmsPageInfo",
    "LlmsTxtContext",
    "MarkdownContext",
    "build_llms_txt",
    "build_markdown_routes",
    "colocated_markdown_path",
    "html_to_markdown",
    "is_enabled",
    "make_llms_txt_route",
    "make_markdown_route_handler",
    "markdown_is_acceptable",
    "markdown_route_path",
    "resolve_page_markdown",
    "strip_md_suffix",
    "wants_markdown",
]
