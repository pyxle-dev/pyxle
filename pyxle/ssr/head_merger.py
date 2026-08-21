"""Head element merging and deduplication utilities."""

from __future__ import annotations

import re
from html.parser import HTMLParser

from pyxle.ssr.jsx_expressions import block_holds_expression, iter_static_elements


class HeadElementAttributeParser(HTMLParser):
    """Parse HTML elements to extract attributes for deduplication."""

    def __init__(self):
        super().__init__()
        self.tag_name: str | None = None
        self.attributes: dict[str, str] = {}
        self.found = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        """Capture the first tag and its attributes."""
        if not self.found:
            self.tag_name = tag.lower()
            # Convert attrs list to dict, handling None values for boolean attrs
            self.attributes = {name.lower(): (value or name) for name, value in attrs}
            self.found = True

    def get_tag_and_attributes(self, html: str) -> tuple[str | None, dict[str, str]]:
        """Parse HTML and return (tag_name, attributes_dict)."""
        try:
            self.feed(html)
            return self.tag_name, self.attributes
        except Exception:
            # If parsing fails, return None
            return None, {}


class HeadElementSplitter(HTMLParser):
    """Split an HTML head block into individual element strings."""

    def __init__(self):
        super().__init__()
        self.elements: list[str] = []
        self.current_element: list[str] = []
        self.current_tag: str | None = None
        self.depth: int = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        """Start a new element."""
        if self.depth == 0:
            # Reconstruct the tag with attributes
            attr_parts = []
            for name, value in attrs:
                if value:
                    attr_parts.append(f'{name}="{value}"')
                else:
                    attr_parts.append(name)
            attrs_str = " " + " ".join(attr_parts) if attr_parts else ""
            
            is_self_closing = self._is_self_closing(tag)
            if is_self_closing:
                self.current_element = [f"<{tag}{attrs_str}/>"]
            else:
                self.current_element = [f"<{tag}{attrs_str}>"]
            
            self.current_tag = tag.lower()
            if is_self_closing:
                self._save_element()
            else:
                self.depth = 1

    def handle_endtag(self, tag: str) -> None:
        """End the current element."""
        if self.depth > 0 and tag.lower() == self.current_tag:
            self.current_element.append(f"</{tag}>")
            self.depth -= 1
            if self.depth == 0:
                self._save_element()

    def handle_data(self, data: str) -> None:
        """Add data between tags."""
        if self.depth > 0:
            self.current_element.append(data)

    def _is_self_closing(self, tag: str) -> bool:
        """Check if tag is self-closing."""
        return tag.lower() in {"meta", "link", "br", "hr", "img", "input", "area", "base", "col", "embed", "source", "track", "wbr"}

    def _save_element(self) -> None:
        """Save the current element and reset."""
        if self.current_element:
            element = "".join(self.current_element).strip()
            if element:
                self.elements.append(element)
        self.current_element = []
        self.current_tag = None

    def split(self, html_block: str) -> list[str]:
        """Parse and split the HTML block into individual elements."""
        try:
            self.feed(html_block)
            return self.elements
        except Exception:
            # If parsing fails, return empty list (elements might be malformed)
            return []


def _needs_splitting(html_block: str) -> bool:
    """Check if a head block contains multiple top-level HTML elements."""
    parser = HeadElementSplitter()
    parser.split(html_block)
    return len(parser.elements) > 1


def _split_head_block_into_elements(html_block: str) -> list[str]:
    """Split a head block containing multiple HTML elements into individual element strings.
    
    Uses HTMLParser to robustly handle both self-closing tags (<meta />, <link />)
    and paired tags (<title>...</title>).
    """
    splitter = HeadElementSplitter()
    return splitter.split(html_block)


def _split_static_block_into_elements(html_block: str) -> list[str]:
    """Split a *compile-time-extracted* head block, dropping unevaluated tags.

    ``<Head>`` blocks are harvested from ``.pyxl`` source before any of it has
    run, so a tag whose attributes or children reference props arrives here as
    JSX source text. Emitting it puts ``href="{faviconUrl}"`` — a relative URL
    the browser will request and fail to find — or a literal ``{name}`` page
    title into the document, and deduplication cannot suppress either, because
    neither matches the rendered tag it duplicates.

    Such tags are dropped, not repaired: the component render produces the same
    tags with their values in them (see :func:`merge_head_elements`). A block
    with no braces at all cannot hold an expression and takes the plain path
    unchanged.
    """
    if not block_holds_expression(html_block):
        return _split_head_block_into_elements(html_block)

    elements: list[str] = []
    for source, unevaluated in iter_static_elements(html_block):
        if unevaluated:
            continue
        elements.extend(_split_head_block_into_elements(source))
    return elements


def _extract_dedupe_key(html: str) -> str | None:
    """Extract deduplication key from HTML element string."""
    html = html.strip()
    if not html:
        return None

    # Parse the HTML element to extract tag and attributes
    parser = HeadElementAttributeParser()
    tag_name, attrs = parser.get_tag_and_attributes(html)

    if not tag_name:
        return None

    # Manual key: data-head-key="X"
    if "data-head-key" in attrs:
        return f"key:{attrs['data-head-key']}"

    # Title tag
    if tag_name == "title":
        return "title"

    # Meta tag — attribute values are lowercased for case-insensitive
    # dedup so that ``<meta name="Robots">`` and ``<meta name="robots">``
    # collapse to the same key.
    if tag_name == "meta":
        # Meta tag with name
        if "name" in attrs:
            return f"meta:name:{attrs['name'].lower()}"
        # Meta tag with property
        if "property" in attrs:
            return f"meta:property:{attrs['property'].lower()}"
        # Meta tag with charset (dedupe by type)
        if "charset" in attrs:
            return "meta:charset"

    # Link tag
    if tag_name == "link":
        rel = attrs.get("rel", "").lower()
        href = attrs.get("href", "")
        # For canonical, dedupe by rel only (only one canonical)
        if rel == "canonical":
            return "link:canonical"
        # For others, dedupe by rel + href
        if rel:
            return f"link:{rel}:{href}"

    # Script tag with src
    if tag_name == "script":
        if "src" in attrs:
            return f"script:src:{attrs['src']}"

    # No deduplication key (keep all instances)
    return None


# ---------------------------------------------------------------------------
# XSS sanitization for HEAD elements
# ---------------------------------------------------------------------------

# Matches event-handler attributes: onclick="...", onerror='...', onload=val
# The leading character class ``[\s/]`` covers both whitespace and the ``/``
# that HTML5 permits as an attribute-name separator (e.g. ``<img/onclick=…>``).
_EVENT_HANDLER_ATTR_RE = re.compile(
    r"""[\s/]+on[a-z]+\s*=\s*(?:"[^"]*"|'[^']*'|\S+)""",
    re.IGNORECASE,
)

# Matches javascript:/vbscript:/data: protocols in href/src/action attributes.
# ``data:`` URIs can carry full HTML documents and are a potent XSS vector
# when injected into ``<iframe src>`` or ``<link>`` elements.
_DANGEROUS_URL_ATTR_RE = re.compile(
    r"""((?:href|src|action)\s*=\s*['"]?)\s*(javascript|vbscript|data)\s*:""",
    re.IGNORECASE,
)

# Detects ``<base`` at the start of the element — ``<base href>`` lets an
# attacker re-root every relative URL on the page.
_BASE_TAG_RE = re.compile(r"^<base\b", re.IGNORECASE)

# Opening and closing <title> tag patterns
_TITLE_OPEN_RE = re.compile(r"<title[^>]*>", re.IGNORECASE)
_TITLE_CLOSE_RE = re.compile(r"</title\s*>", re.IGNORECASE)

# Tags permitted in the document <head> when a head element is built from a
# raw string (the Python ``HEAD`` variable / callable path, which — unlike the
# React ``<Head>`` JSX path — does not benefit from React's escaping). Anything
# outside this set (``<iframe>``, ``<object>``, ``<base>``, …) is dropped.
# ``script`` and ``style`` are allowed because inline init scripts / critical
# CSS in the head are a supported, trusted-author feature; their *inner*
# content is the developer's own code and is preserved verbatim.
_ALLOWED_HEAD_TAGS = frozenset({"meta", "link", "script", "style"})
_VOID_HEAD_TAGS = frozenset({"meta", "link"})
_URL_ATTRS = frozenset({"href", "src", "action"})
_DANGEROUS_URL_SCHEMES = ("javascript:", "vbscript:", "data:")
_SCHEME_NOISE_RE = re.compile(r"\s")
# A valid HTML attribute name excludes whitespace, quotes, and the ``> / =``
# delimiters (and control chars). Names with anything else are dropped so a
# crafted name can't break out of the tag's attribute quoting.
_VALID_ATTR_NAME_RE = re.compile(r"^[^\s\"'>/=\x00-\x1f]+$")


class _SingleHeadElementParser(HTMLParser):
    """Capture the first tag, its attributes, and (for paired tags) its inner
    content from a head-element string — discarding any trailing markup an
    attacker may have injected after a quote breakout."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.tag: str | None = None
        self.attrs: list[tuple[str, str | None]] = []
        self._inner: list[str] = []
        self._closed = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self.tag is None:
            self.tag = tag.lower()
            self.attrs = attrs

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self.tag is None:
            self.tag = tag.lower()
            self.attrs = attrs
            self._closed = True

    def handle_data(self, data: str) -> None:
        if self.tag is not None and not self._closed:
            self._inner.append(data)

    def handle_endtag(self, tag: str) -> None:
        if self.tag is not None and not self._closed and tag.lower() == self.tag:
            self._closed = True

    @property
    def inner(self) -> str:
        return "".join(self._inner)


def _has_dangerous_url_scheme(value: str) -> bool:
    """True if ``value`` resolves to a javascript:/vbscript:/data: URL.

    Entity-decodes and strips whitespace/control characters first so that
    obfuscations like ``java&#9;script:`` or ``  JavaScript:`` are caught.
    """
    from html import unescape as html_unescape

    collapsed = _SCHEME_NOISE_RE.sub("", html_unescape(value)).lower()
    return collapsed.startswith(_DANGEROUS_URL_SCHEMES)


def _render_safe_attributes(attrs: list[tuple[str, str | None]]) -> str:
    """Re-serialise attributes safely: drop ``on*`` handlers, neutralise
    dangerous URL schemes, and HTML-escape every value (quote=True) so a value
    can never break out of its quotes and inject markup."""
    from html import escape as html_escape

    parts: list[str] = []
    for name, value in attrs:
        if not _VALID_ATTR_NAME_RE.match(name):
            continue  # structurally-unsafe attribute name
        lname = name.lower()
        if lname.startswith("on"):
            continue  # event-handler attribute
        if value is None:
            parts.append(name)  # boolean attribute (e.g. ``defer``)
            continue
        if lname in _URL_ATTRS and _has_dangerous_url_scheme(value):
            value = ""
        parts.append(f'{name}="{html_escape(value, quote=True)}"')
    return " ".join(parts)


def _reconstruct_head_element(html: str, tag: str) -> str:
    """Rebuild a non-title head element from a strict tag allowlist with every
    attribute value escaped. Returns ``""`` for disallowed tags, meta-refresh
    redirects, or unparseable input (fail closed)."""
    if tag not in _ALLOWED_HEAD_TAGS:
        return ""

    parser = _SingleHeadElementParser()
    try:
        parser.feed(html)
        parser.close()
    except Exception:
        return ""
    if parser.tag != tag:
        return ""

    # Reject <meta http-equiv="refresh"> — a data-controlled value is an
    # open-redirect / refresh-based injection vector.
    if tag == "meta":
        for name, value in parser.attrs:
            if name.lower() == "http-equiv" and (value or "").strip().lower() == "refresh":
                return ""

    attrs_str = _render_safe_attributes(parser.attrs)
    spacer = f" {attrs_str}" if attrs_str else ""
    if tag in _VOID_HEAD_TAGS:
        return f"<{tag}{spacer}/>"
    # Paired tag (script/style): inner content is trusted author code and is
    # preserved verbatim; any markup injected after the matching close tag was
    # dropped by the parser.
    return f"<{tag}{spacer}>{parser.inner}</{tag}>"


def sanitize_head_element(html: str) -> str:
    """Sanitize a single HEAD element to prevent XSS injection.

    The raw-string HEAD path (the Python ``HEAD`` variable / callable) does
    not pass through React's escaping, so a developer who interpolates loader
    data into a head string — the documented dynamic-meta-tags recipe — could
    otherwise inject markup. Protection:

    * ``<base>`` and any tag outside the head allowlist
      (``title``/``meta``/``link``/``script``/``style``) are dropped.
    * ``<title>`` text content has ``<``/``>`` escaped so injected tags become
      inert text.
    * Every other element is parsed and rebuilt from the first tag only, with
      all attribute values HTML-escaped (closing the quote-breakout vector),
      ``on*`` handlers removed, ``javascript:``/``vbscript:``/``data:`` URLs
      neutralised, and ``<meta http-equiv=refresh>`` rejected. Trailing markup
      injected after a breakout is discarded.

    Inline ``<script>``/``<style>`` *content* is treated as trusted author
    code and preserved verbatim — do not interpolate untrusted data into it.
    """
    html = html.strip()
    if not html:
        return html

    # Reject <base> outright — it re-roots every relative asset on the page.
    if _BASE_TAG_RE.match(html):
        return ""

    tag, _ = HeadElementAttributeParser().get_tag_and_attributes(html)
    if not tag:
        return ""

    if tag == "title":
        return _sanitize_title_element(html)

    return _reconstruct_head_element(html, tag)


def _sanitize_title_element(html: str) -> str:
    """Sanitise a ``<title>`` element: escape its text content, strip ``on*``
    handlers, and neutralise dangerous URL schemes (kept as a regex pass so an
    injected early ``</title>`` is escaped into inert text rather than parsed
    as a real close tag)."""
    # Escape angle brackets inside the title text content.
    html = _escape_title_text_content(html)

    # Strip event-handler attributes (raw, then entity-decoded re-check).
    html = _EVENT_HANDLER_ATTR_RE.sub("", html)
    from html import unescape as html_unescape

    decoded = html_unescape(html)
    if _EVENT_HANDLER_ATTR_RE.search(decoded):
        html = _EVENT_HANDLER_ATTR_RE.sub("", decoded)

    # Neutralise dangerous protocol URLs in any attributes.
    return _DANGEROUS_URL_ATTR_RE.sub(r"\1", html)


def _escape_title_text_content(html: str) -> str:
    """Escape ``<`` and ``>`` inside a ``<title>`` and drop trailing markup.

    Everything between the opening ``<title>`` and the *last* ``</title>`` is
    treated as title text and angle-bracket-escaped; the title is then closed
    with a single clean ``</title>`` and **anything after that close tag is
    discarded**. This is the key XSS guard: a single injected ``</title>``
    followed by, say, ``<script>`` would otherwise end the title's RCDATA and
    leave the script live in ``<head>`` — returning the suffix verbatim (the
    previous behaviour) leaked exactly that. Dropping the suffix mirrors how
    the non-title path discards markup injected after a quote breakout.
    """
    open_match = _TITLE_OPEN_RE.search(html)
    if open_match is None:
        return html

    prefix = html[: open_match.end()]
    close_matches = list(_TITLE_CLOSE_RE.finditer(html))
    if close_matches:
        # Title text runs up to the last </title>; anything after it is
        # discarded (injected markup) and a single clean close is emitted.
        content = html[open_match.end() : close_matches[-1].start()]
    else:
        # No close tag at all: treat the remainder as title text, escape it,
        # and append a clean close so the title can never swallow the rest of
        # the document (or leave attacker markup unescaped) as RCDATA.
        content = html[open_match.end() :]

    # Only escape angle brackets — preserve existing character entities.
    escaped = content.replace("<", "&lt;").replace(">", "&gt;")
    return prefix + escaped + "</title>"


def merge_head_elements(
    *,
    head_variable: tuple[str, ...],
    head_jsx_blocks: tuple[str, ...],
    layout_head_jsx_blocks: tuple[str, ...] = (),
    layout_head_variable: tuple[str, ...] = (),
    runtime_head_blocks: tuple[str, ...] = (),
) -> tuple[str, ...]:
    """Merge HEAD elements from every source with deduplication.

    Each level contributes through the same two channels: a ``HEAD`` variable
    (Python that has already run — finished HTML) and ``<Head>`` JSX blocks
    (compile-time-extracted source, filtered for unevaluated expressions).
    Layout contributions are lower priority than the page's.

    Precedence order (higher priority overrides lower):

    1. Layout contributions — from ancestor ``layout.pyxl`` and
       ``template.pyxl`` files. Their JSX blocks are examined first, so a
       layout's ``<Head>`` beats its own ``HEAD`` variable on a shared key,
       mirroring the page-level ordering below.
    2. Page ``HEAD`` variable — server-side declaration in the page module.
    3. Page JSX blocks — static extraction of ``<Head>`` blocks in the
       page file at compile time.
    4. Runtime ``<Head>`` registrations — produced when the ``<Head>``
       component executes during SSR and calls ``renderToStaticMarkup``
       on its children. These reflect the actual rendered output,
       including evaluated JSX expressions, so they always win over
       static extraction.

    Deduplication rules (higher priority always wins, first occurrence
    wins within the same priority):

    - ``<title>`` — dedupe by tag name
    - ``<meta name="X">`` — dedupe by ``name`` attribute
    - ``<meta property="X">`` — dedupe by ``property`` attribute
    - ``<meta charset>`` — only one allowed
    - ``<link rel="canonical">`` — only one allowed
    - ``<link rel="X" href="Y">`` — dedupe by ``rel`` + ``href``
    - ``<script src="X">`` — dedupe by ``src``
    - ``data-head-key="X"`` — manual deduplication key

    Elements without a dedupe key are kept (e.g. preconnect links).

    Note: Head blocks may contain multiple HTML elements in a single
    string (from ``<Head>...</Head>`` JSX blocks). This function splits
    them into individual elements before deduplication.

    Runtime ordering note: ``runtime_head_blocks`` arrives in React
    render order (outer layouts register first, the page registers
    last). We process this list in *reverse* so the deepest registration
    (the page) is examined first and wins via the standard
    "first-occurrence-wins-within-priority" rule. This matches the
    react-helmet convention that components closer to the leaf win.
    """

    # Dictionary: dedupe_key -> (html, priority)
    # Higher priority values override lower priority values
    seen_keys: dict[str | None, tuple[str, int]] = {}

    # Split head blocks into individual elements, then sanitise each one.
    # The two JSX sources are compile-time extractions, so they are filtered
    # for unevaluated expressions. The two ``HEAD`` variables are Python that
    # has already run and ``runtime_head_blocks`` is React's rendered output —
    # all three hold finished values, and braces in them are literal content (a
    # JSON-LD payload, a CSS rule) that must reach the document intact.
    layout_elements = []
    for block in layout_head_jsx_blocks:
        for el in _split_static_block_into_elements(block):
            layout_elements.append(sanitize_head_element(el))
    # The layout's ``HEAD`` variable shares the layout's priority tier but not
    # its filtering: it is finished HTML, exactly like the page-level
    # ``head_variable`` below.
    layout_elements.extend(sanitize_head_element(el) for el in layout_head_variable)

    head_var_elements = [sanitize_head_element(el) for el in head_variable]

    page_elements = []
    for block in head_jsx_blocks:
        for el in _split_static_block_into_elements(block):
            page_elements.append(sanitize_head_element(el))

    # Runtime blocks: reversed so the deepest (page) registration is
    # processed first and wins over outer (layout) registrations within
    # the same priority tier. See the docstring for rationale.
    runtime_elements = []
    for block in reversed(runtime_head_blocks):
        for el in _split_head_block_into_elements(block):
            runtime_elements.append(sanitize_head_element(el))

    # Priority 1: Layout contributions — JSX blocks then HEAD variable (lowest priority)
    for element in layout_elements:
        element = element.strip()
        if element:
            dedupe_key = _extract_dedupe_key(element)
            if dedupe_key is None:
                # No dedupe key, we'll handle separately (always include non-deupeable items)
                if None not in seen_keys:
                    seen_keys[None] = (element, 1)
            else:
                # Store if we haven't seen this key or if this has higher priority
                if dedupe_key not in seen_keys:
                    seen_keys[dedupe_key] = (element, 1)

    # Priority 2: Page HEAD variable
    for element in head_var_elements:
        element = element.strip()
        if element:
            dedupe_key = _extract_dedupe_key(element)
            if dedupe_key is None:
                # No dedupe key, always add to result later
                # Use a special marker to track non-deupeable items
                pass
            elif dedupe_key not in seen_keys or seen_keys[dedupe_key][1] < 2:
                # Override if this is the first occurrence or has higher priority
                seen_keys[dedupe_key] = (element, 2)

    # Priority 3: Page JSX blocks (static compile-time extraction)
    for element in page_elements:
        element = element.strip()
        if element:
            dedupe_key = _extract_dedupe_key(element)
            if dedupe_key is None:
                # No dedupe key, always add to result later
                pass
            elif dedupe_key not in seen_keys or seen_keys[dedupe_key][1] < 3:
                # Override if this is the first occurrence or has higher priority
                seen_keys[dedupe_key] = (element, 3)

    # Priority 4: Runtime <Head> registrations (highest priority)
    #
    # These come from <Head> components executing during SSR. They
    # contain fully evaluated JSX (including expressions like
    # ``{pageTitle}``) and therefore always supersede the static
    # extraction in priority 3 when the same dedupe key is present.
    for element in runtime_elements:
        element = element.strip()
        if element:
            dedupe_key = _extract_dedupe_key(element)
            if dedupe_key is None:
                # No dedupe key, always add to result later
                pass
            elif dedupe_key not in seen_keys or seen_keys[dedupe_key][1] < 4:
                seen_keys[dedupe_key] = (element, 4)

    # Build result: include all deduped elements in order, plus non-deupeable items
    result: list[str] = []
    non_deupeable: list[str] = []

    # Collect non-deupeable items from all sources
    for element in layout_elements:
        element = element.strip()
        if element and _extract_dedupe_key(element) is None:
            non_deupeable.append(element)

    for element in head_var_elements:
        element = element.strip()
        if element and _extract_dedupe_key(element) is None:
            non_deupeable.append(element)

    for element in page_elements:
        element = element.strip()
        if element and _extract_dedupe_key(element) is None:
            non_deupeable.append(element)

    for element in runtime_elements:
        element = element.strip()
        if element and _extract_dedupe_key(element) is None:
            non_deupeable.append(element)

    # Add deduped items first (in order they were first seen)
    for key in seen_keys:
        if key is not None:  # Skip the None marker
            html, _ = seen_keys[key]
            result.append(html)

    # Add non-deupeable items (e.g., preconnect links without href)
    result.extend(non_deupeable)

    return tuple(result)


__all__ = ["merge_head_elements", "sanitize_head_element"]

