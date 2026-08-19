"""Detection of unevaluated JSX expressions in compile-time-extracted markup.

`<Head>` blocks and `<Script>` declarations are harvested from `.pyxl` source
by the compiler, *before any of it has run*. What the compiler stores is
therefore JSX **source text**, not markup: a tag whose attributes or children
reference props still carries the `{...}` expression containers the author
wrote.

Emitting that text is always wrong, and wrong in ways nothing downstream can
repair:

* `href="{faviconUrl}"` is a **relative URL** to the browser. It requests it —
  one failed round trip per such tag, per page view.
* `og:image` unfurls as a broken picture in every chat client that reads it.
* `<title>{name}</title>` puts the literal `{name}` in the browser tab, and on
  a streamed page (where the head is flushed before the component renders) it
  is the *only* title the page ever gets.
* Deduplication cannot suppress any of it: a link's key is its `rel` plus
  `href`, and `href="{faviconUrl}"` never matches the rendered one, so the
  broken copy and the real one are both emitted.

**The rule this module implements:** a statically-extracted element is
unusable if *any* part of it — an attribute value, its child text, or the
position it occupies — still contains an unevaluated JSX expression. Such an
element is dropped, not repaired: the component render produces the same
element with its values filled in, so nothing is lost.

Detection runs on the **raw JSX source**, which is the only place the rule can
be applied correctly. Once an element has been through an HTML parser the
evidence is gone or scrambled: `href={base + path}` re-serialises as the
attribute `href="{base"` followed by the junk attributes `+` and `path}`, and
an expression that never used quotes is indistinguishable from one that did.

**What must survive.** Over-filtering is the worse failure: silently deleting
a page's JSON-LD is worse than the bug being fixed here. Two things guarantee
it does not happen.

* **A literal brace inside a quoted attribute value survives.**
  `content="Use {braces} in prose"` is a quoted string — data, not JSX — and
  quoted attribute values are skipped whole.
* **Only the compile-time-extracted JSX sources are filtered at all.** JSON-LD
  (`{"@context": …}`), a `<style>` rule and an inline script are brace-heavy by
  nature, and every one of them reaches the document through a source this
  rule never touches: the Python `HEAD` variable — of the page *and* of every
  layout above it — which has already run, and React's rendered output, which
  holds finished values. Braces there are content and are emitted verbatim.

  This is a guarantee about the *channel*, not about who filled it, so a
  caller must never pour an evaluated `HEAD` variable into the same collection
  as raw `<Head>` source: everything in that collection is then judged as
  source, and a layout's JSON-LD is deleted from every page under it. See
  :class:`pyxle.devserver.registry.LayoutHeadContribution`, which keeps the
  layout chain's two channels apart for exactly this reason, and the
  `layout_head_variable` parameter of
  :func:`pyxle.ssr.head_merger.merge_head_elements` that receives it.

  On the JSX-source side there is no ambiguity to protect against: JSX gives a
  brace in child position exactly one meaning — an expression container — and
  a literal `{` there is a syntax error the compiler never gets past. So
  `<script type="application/ld+json">{JSON.stringify(schema)}</script>` in a
  `<Head>` is an expression, its static copy is dropped, and the render emits
  the real JSON-LD in its place.

Note on the escaped form (`href=\\"{faviconUrl}\\"`): it exists, but never
here. It is produced *downstream* of this rule, when a merged head is
JSON-encoded into the `__PYXLE_NAV_SEED__` / navigation payload
(`headMarkup`), where the backslashes are correct JSON escaping. Filtering
that form would corrupt the hydration payload — which is why nothing in this
module looks for it.
"""

from __future__ import annotations

from collections.abc import Iterator

#: Tags with no closing tag, so no child text to scan. `<link>`/`<meta>` are
#: the head-relevant ones; the rest are here so a stray body tag inside a
#: `<Head>` block is scanned with the same geometry the browser would use.
_VOID_TAGS = frozenset(
    {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }
)

_TAG_NAME_END = frozenset({" ", "\t", "\n", "\r", "\f", "/", ">"})


def _scan_open_tag(source: str, start: int) -> tuple[int, str, bool, bool]:
    """Scan the opening tag beginning at ``source[start] == "<"``.

    Returns ``(index past the tag, lowercased tag name, self_closing,
    holds_expression)``. Attribute values in quotes are skipped whole, so a
    literal brace inside one is not mistaken for an expression, and a ``>``
    inside a quoted value or an expression does not end the tag early.
    """
    index = start + 1
    length = len(source)

    name_start = index
    while index < length and source[index] not in _TAG_NAME_END:
        index += 1
    tag = source[name_start:index].lower()

    holds_expression = False
    self_closing = False
    while index < length:
        char = source[index]
        if char in ('"', "'"):
            # A quoted attribute value: data, whatever it contains.
            closing = source.find(char, index + 1)
            index = length if closing == -1 else closing + 1
            continue
        if char == "{":
            # An attribute value (`href={url}`) or a spread (`{...props}`).
            holds_expression = True
            index = _skip_braces(source, index)
            continue
        if char == ">":
            self_closing = source[index - 1] == "/" if index > start else False
            return index + 1, tag, self_closing, holds_expression
        index += 1

    return length, tag, self_closing, holds_expression


def _skip_braces(source: str, start: int) -> int:
    """Return the index just past the balanced brace group at ``start``.

    Braces inside quoted strings are not counted, so `{JSON.stringify("{}")}`
    closes where it should. An unbalanced group runs to the end of the source.
    """
    depth = 0
    index = start
    length = len(source)
    while index < length:
        char = source[index]
        if char in ('"', "'", "`"):
            closing = source.find(char, index + 1)
            index = length if closing == -1 else closing + 1
            continue
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return index + 1
        index += 1
    return length


def _scan_element(source: str, start: int) -> tuple[int, bool]:
    """Scan the element beginning at ``source[start] == "<"``.

    Returns ``(index past the element, holds_expression)``, where
    ``holds_expression`` covers the opening tag *and* the child text.
    """
    after_tag, tag, self_closing, holds_expression = _scan_open_tag(source, start)
    if self_closing or tag in _VOID_TAGS or not tag:
        return after_tag, holds_expression

    closing_tag = f"</{tag}"
    lowered = source.lower()
    close_start = lowered.find(closing_tag, after_tag)
    if close_start == -1:
        children_end = len(source)
        element_end = len(source)
    else:
        children_end = close_start
        close_end = source.find(">", close_start)
        element_end = len(source) if close_end == -1 else close_end + 1

    if "{" in source[after_tag:children_end]:
        # A brace in child position is a JSX expression container — JSX has no
        # other meaning for it there, and a literal `{` is a syntax error the
        # compiler never gets past. This is what catches `<title>{name}</title>`,
        # whose literal text is otherwise the page's visible title.
        holds_expression = True

    return element_end, holds_expression


def iter_static_elements(block: str) -> Iterator[tuple[str, bool]]:
    """Split a compile-time-extracted block into ``(source, unevaluated)`` pairs.

    Boundaries are found in the raw JSX source, so an expression containing a
    space, a `>`, or a quote stays attached to the element that owns it.

    A brace group at the *top level* of the block — `{isPremium && <meta
    name="robots" content="noindex" />}` — is itself an unevaluated expression
    and is yielded as one. It must not be reached into: the elements inside it
    are conditional, and emitting them regardless is how a `noindex` ends up on
    a page whose condition said otherwise.
    """
    index = 0
    length = len(block)
    while index < length:
        char = block[index]
        if char == "<":
            end, unevaluated = _scan_element(block, index)
            yield block[index:end], unevaluated
            index = max(end, index + 1)
        elif char == "{":
            end = _skip_braces(block, index)
            yield block[index:end], True
            index = max(end, index + 1)
        else:
            index += 1


def block_holds_expression(block: str) -> bool:
    """Whether *block* contains any unevaluated JSX expression.

    A cheap pre-check: a block with no ``{`` at all cannot hold one, which is
    the common case for a fully static head and lets callers keep their
    existing path untouched.
    """
    if "{" not in block:
        return False
    return any(unevaluated for _, unevaluated in iter_static_elements(block))


def is_expression_value(value: object) -> bool:
    """Whether an extracted prop value is an unevaluated JSX expression.

    The JSX extractor reports a literal prop (`src="/a.js"`) as its value and a
    dynamic one (`src={analyticsUrl}`, `src={base + "/a.js"}`) as the raw
    expression wrapped in braces, so the whole value is the expression
    container. Non-string values (a boolean prop) are never expressions.
    """
    if not isinstance(value, str):
        return False
    stripped = value.strip()
    return (
        stripped.startswith("{")
        and stripped.endswith("}")
        and _skip_braces(stripped, 0) == len(stripped)
    )


__all__ = [
    "block_holds_expression",
    "is_expression_value",
    "iter_static_elements",
]
