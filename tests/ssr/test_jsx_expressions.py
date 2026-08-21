"""Tests for unevaluated-JSX-expression detection in extracted markup."""

import pytest

from pyxle.ssr.jsx_expressions import (
    block_holds_expression,
    is_expression_value,
    iter_static_elements,
)


def elements(block):
    """(source, unevaluated) pairs, with the source whitespace-normalised."""
    return [(source.strip(), unevaluated) for source, unevaluated in iter_static_elements(block)]


# ---------------------------------------------------------------------------
# Expression containers, in every position JSX allows one
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("block", "reason"),
    [
        ("<link rel=\"icon\" href={faviconUrl} />", "bare identifier attribute"),
        ('<meta property="og:title" content={`${t} — Acme`} />', "template literal"),
        ('<link rel="canonical" href={base + path} />', "spaces in the expression"),
        ('<meta name="c" content={dark ? "#000" : "#fff"} />', "ternary"),
        ('<meta name="x" content={a > b} />', "a > that would end the tag early"),
        ("<meta name=\"x\" content={q('a\">b')} />", "a quote inside the expression"),
        ("<title>{name}</title>", "child text"),
        ("<title>Acme {name}</title>", "child text mixed with literal text"),
        ("<script src={url} />", "a script src"),
        ("<script>{JSON.stringify(schema)}</script>", "a script body"),
        ("<style>{`.a{color:${c}}`}</style>", "a style body"),
        ("<meta {...metaProps} />", "a spread with no attribute name"),
        ('{cond && <meta name="robots" content="noindex" />}', "a conditional element"),
        ("{/* a comment */}", "a comment container"),
    ],
)
def test_an_expression_marks_its_element_unevaluated(block, reason):
    assert block_holds_expression(block), reason
    assert all(unevaluated for _, unevaluated in elements(block)), reason


# ---------------------------------------------------------------------------
# The other direction: literal braces are content
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("block", "reason"),
    [
        ('<meta name="d" content="Use {braces} in prose" />', "quoted attribute value"),
        ("<meta name='d' content='{single} quoted' />", "single-quoted value"),
        ('<link rel="icon" href="/favicon.ico" />', "no braces at all"),
        ("<title>Acme Status</title>", "plain title text"),
    ],
)
def test_a_literal_brace_is_content(block, reason):
    assert not block_holds_expression(block), reason
    assert all(not unevaluated for _, unevaluated in elements(block)), reason


def test_a_nested_brace_group_closes_where_it_should():
    """`dangerouslySetInnerHTML={{__html: …}}` — the documented way to put
    JSON-LD in a head — nests one brace group inside another. The scan has to
    count depth, or it ends the expression at the inner `}` and reads the rest
    of the tag as markup."""
    block = (
        '<script type="application/ld+json" '
        "dangerouslySetInnerHTML={{__html: JSON.stringify(schema)}} />"
        "<title>Kept</title>"
    )
    assert elements(block) == [
        (
            '<script type="application/ld+json" '
            "dangerouslySetInnerHTML={{__html: JSON.stringify(schema)}} />",
            True,
        ),
        ("<title>Kept</title>", False),
    ]


def test_a_quoted_brace_and_an_expression_in_one_tag():
    """The quoted value is skipped whole, but the expression beside it still
    marks the element — one does not mask the other."""
    block = '<meta name="d" content="Use {braces}" data-x={value} />'
    assert block_holds_expression(block)


# ---------------------------------------------------------------------------
# Element boundaries, found in the source rather than after HTML parsing
# ---------------------------------------------------------------------------


def test_each_element_is_judged_separately():
    block = (
        "<title>{name}</title>"
        '<meta charset="utf-8" />'
        "<link rel=\"icon\" href={url} />"
        '<link rel="preconnect" href="https://fonts.example.com" />'
    )
    assert elements(block) == [
        ("<title>{name}</title>", True),
        ('<meta charset="utf-8" />', False),
        ("<link rel=\"icon\" href={url} />", True),
        ('<link rel="preconnect" href="https://fonts.example.com" />', False),
    ]


def test_a_greater_than_inside_an_expression_does_not_end_the_tag():
    """Without brace tracking the tag ends at the `>` inside the expression and
    the remainder is read as a second element."""
    block = '<meta name="x" content={a > b ? "y" : "z"} /><title>Kept</title>'
    assert elements(block) == [
        ('<meta name="x" content={a > b ? "y" : "z"} />', True),
        ("<title>Kept</title>", False),
    ]


def test_a_conditional_block_is_not_reached_into():
    """The elements inside a conditional are conditional. Emitting them anyway
    is how a `noindex` lands on a page whose condition said otherwise."""
    block = '{isPremium && <meta name="robots" content="noindex" />}<title>A</title>'
    assert elements(block) == [
        ('{isPremium && <meta name="robots" content="noindex" />}', True),
        ("<title>A</title>", False),
    ]


def test_an_unclosed_tag_does_not_loop_or_leak():
    assert elements("<title>never closed") == [("<title>never closed", False)]
    assert elements("<link rel=\"icon\" href={u}") == [('<link rel="icon" href={u}', True)]
    assert elements("{unbalanced") == [("{unbalanced", True)]
    assert elements("") == []
    assert elements("   \n  ") == []


def test_stray_text_between_elements_is_skipped():
    assert elements("noise <title>A</title> more") == [("<title>A</title>", False)]


# ---------------------------------------------------------------------------
# Extracted prop values (the <Script> path)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        "{analyticsUrl}",
        "{base + '/a.js'}",
        '{cdn ? "/a.js" : "/b.js"}',
        "  {spaced}  ",
        '{q("}")}',
        "{{nested: value}.value}",
    ],
)
def test_a_dynamic_prop_value_is_an_expression(value):
    assert is_expression_value(value)


@pytest.mark.parametrize(
    "value",
    [
        "/analytics.js",
        "https://cdn.example.com/a.js",
        "",
        "{unbalanced",
        "unbalanced}",
        "{a} and {b}",
        True,
        None,
        42,
    ],
)
def test_anything_else_is_a_literal(value):
    assert not is_expression_value(value)
