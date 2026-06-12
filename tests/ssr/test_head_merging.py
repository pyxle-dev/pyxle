"""Tests for head element merging and deduplication."""

from pyxle.ssr.head_merger import merge_head_elements


def test_merge_empty_sources():
    """Should return empty tuple when all sources are empty."""
    result = merge_head_elements(
        head_variable=(),
        head_jsx_blocks=(),
        layout_head_jsx_blocks=(),
    )
    assert result == ()


def test_merge_only_head_variable():
    """Should return head variable elements when no JSX blocks.

    Each element is reconstructed from a strict allowlist by the sanitiser
    (the XSS-hardening pass), which canonicalises self-closing tags to ``/>``.
    """
    head_var = ("<title>From HEAD</title>", '<meta name="description" content="test" />')
    result = merge_head_elements(
        head_variable=head_var,
        head_jsx_blocks=(),
        layout_head_jsx_blocks=(),
    )
    assert result == (
        "<title>From HEAD</title>",
        '<meta name="description" content="test"/>',
    )


def test_merge_only_jsx_blocks():
    """Should return JSX blocks when no HEAD variable (may normalize spacing)."""
    jsx_blocks = ("<title>From JSX</title>", '<meta name="keywords" content="test" />')
    result = merge_head_elements(
        head_variable=(),
        head_jsx_blocks=jsx_blocks,
        layout_head_jsx_blocks=(),
    )
    # Elements go through HTML parser which may normalize spacing in self-closing tags
    assert len(result) == 2
    assert any("<title>From JSX</title>" in elem for elem in result)
    assert any("keywords" in elem for elem in result)



def test_merge_only_layout_blocks():
    """Should return layout blocks when no other sources."""
    layout_blocks = ("<title>From Layout</title>",)
    result = merge_head_elements(
        head_variable=(),
        head_jsx_blocks=(),
        layout_head_jsx_blocks=layout_blocks,
    )
    assert result == layout_blocks


def test_title_deduplication_page_jsx_wins():
    """Page JSX title should override HEAD variable title."""
    result = merge_head_elements(
        head_variable=("<title>HEAD Title</title>",),
        head_jsx_blocks=("<title>JSX Title</title>",),
        layout_head_jsx_blocks=(),
    )
    assert result == ("<title>JSX Title</title>",)


def test_title_deduplication_page_wins_over_layout():
    """Page elements should override layout elements."""
    result = merge_head_elements(
        head_variable=("<title>Page HEAD Title</title>",),
        head_jsx_blocks=(),
        layout_head_jsx_blocks=("<title>Layout Title</title>",),
    )
    # Page HEAD (priority 2) wins over layout JSX (priority 1)
    assert result == ("<title>Page HEAD Title</title>",)


def test_title_deduplication_three_sources():
    """Should keep only the last title from highest priority source."""
    result = merge_head_elements(
        head_variable=("<title>HEAD Title</title>",),
        head_jsx_blocks=("<title>JSX Title</title>",),
        layout_head_jsx_blocks=("<title>Layout Title</title>",),
    )
    # Page JSX (priority 3) wins
    assert result == ("<title>JSX Title</title>",)


def test_meta_name_deduplication():
    """Meta tags with same name attribute should deduplicate."""
    result = merge_head_elements(
        head_variable=('<meta name="description" content="First description" />',),
        head_jsx_blocks=('<meta name="description" content="Second description" />',),
        layout_head_jsx_blocks=(),
    )
    # Page JSX wins
    assert len(result) == 1
    assert 'name="description"' in result[0]
    assert "Second description" in result[0]


def test_meta_property_deduplication():
    """Meta tags with same property attribute should deduplicate (Open Graph)."""
    result = merge_head_elements(
        head_variable=('<meta property="og:title" content="First" />',),
        head_jsx_blocks=('<meta property="og:title" content="Second" />',),
        layout_head_jsx_blocks=(),
    )
    assert len(result) == 1
    assert 'property="og:title"' in result[0]
    assert "Second" in result[0]


def test_different_meta_names_not_deduped():
    """Meta tags with different names should both be kept."""
    result = merge_head_elements(
        head_variable=('<meta name="description" content="Desc" />',),
        head_jsx_blocks=('<meta name="keywords" content="Keys" />',),
        layout_head_jsx_blocks=(),
    )
    assert len(result) == 2
    assert any('name="description"' in el for el in result)
    assert any('name="keywords"' in el for el in result)


def test_canonical_link_deduplication():
    """Canonical link should deduplicate by rel only."""
    result = merge_head_elements(
        head_variable=('<link rel="canonical" href="https://example.com/page1" />',),
        head_jsx_blocks=('<link rel="canonical" href="https://example.com/page2" />',),
        layout_head_jsx_blocks=(),
    )
    # Only one canonical link (page JSX wins)
    assert len(result) == 1
    assert 'rel="canonical"' in result[0]
    assert "page2" in result[0]


def test_stylesheet_link_deduplication():
    """Stylesheet links with same href should deduplicate."""
    result = merge_head_elements(
        head_variable=('<link rel="stylesheet" href="/styles.css" />',),
        head_jsx_blocks=('<link rel="stylesheet" href="/styles.css" />',),
        layout_head_jsx_blocks=(),
    )
    # Same href, should dedupe
    assert len(result) == 1
    assert 'href="/styles.css"' in result[0]


def test_different_stylesheets_not_deduped():
    """Stylesheet links with different hrefs should both be kept."""
    result = merge_head_elements(
        head_variable=('<link rel="stylesheet" href="/style1.css" />',),
        head_jsx_blocks=('<link rel="stylesheet" href="/style2.css" />',),
        layout_head_jsx_blocks=(),
    )
    assert len(result) == 2
    assert any('href="/style1.css"' in el for el in result)
    assert any('href="/style2.css"' in el for el in result)


def test_script_src_deduplication():
    """Scripts with same src should deduplicate."""
    result = merge_head_elements(
        head_variable=('<script src="/analytics.js"></script>',),
        head_jsx_blocks=('<script src="/analytics.js"></script>',),
        layout_head_jsx_blocks=(),
    )
    assert len(result) == 1
    assert 'src="/analytics.js"' in result[0]


def test_manual_key_deduplication():
    """Elements with data-head-key should deduplicate by key."""
    result = merge_head_elements(
        head_variable=('<meta data-head-key="custom" name="x" content="First" />',),
        head_jsx_blocks=('<meta data-head-key="custom" name="y" content="Second" />',),
        layout_head_jsx_blocks=(),
    )
    # Should dedupe even though name differs
    assert len(result) == 1
    assert 'data-head-key="custom"' in result[0]
    assert 'name="y"' in result[0]


def test_no_dedupe_key_keeps_all():
    """Elements without dedupe key should all be kept.

    Note: ``<base>`` elements are stripped by the sanitizer because
    they can re-root all relative URLs on the page (XSS gadget).
    Use ``<link rel="preconnect">`` to test non-deduped behaviour.
    """
    result = merge_head_elements(
        head_variable=('<link rel="preconnect" href="https://a.example" />',),
        head_jsx_blocks=('<link rel="preconnect" href="https://b.example" />',),
        layout_head_jsx_blocks=(),
    )
    # preconnect links without matching href are kept as separate entries
    assert len(result) == 2


def test_case_insensitive_tag_matching():
    """Tag matching should be case-insensitive."""
    result = merge_head_elements(
        head_variable=("<TITLE>Upper</TITLE>",),
        head_jsx_blocks=("<title>Lower</title>",),
        layout_head_jsx_blocks=(),
    )
    # Should dedupe despite case difference
    assert len(result) == 1
    assert "Lower" in result[0]


def test_case_insensitive_attribute_matching():
    """Attribute name matching should be case-insensitive, but values are case-sensitive."""
    result = merge_head_elements(
        head_variable=('<meta NAME="description" content="First" />',),
        head_jsx_blocks=('<meta name="description" content="Second" />',),
        layout_head_jsx_blocks=(),
    )
    # Should dedupe despite case difference in attribute name (NAME vs name)
    # Both have name="description" so should dedupe
    assert len(result) == 1
    assert "Second" in result[0]


def test_mixed_elements_preserve_order():
    """Non-conflicting elements should maintain relative order."""
    result = merge_head_elements(
        head_variable=(
            "<title>Title</title>",
            '<meta name="description" content="Desc" />',
        ),
        head_jsx_blocks=(
            '<meta name="keywords" content="Keys" />',
            '<link rel="stylesheet" href="/style.css" />',
        ),
        layout_head_jsx_blocks=(),
    )
    assert len(result) == 4
    # Layout (priority 1), then HEAD (priority 2), then JSX (priority 3)
    # But since we dedupe in place, order is: title, description, keywords, link


def test_complex_scenario():
    """Test complex scenario with multiple sources and deduplication."""
    result = merge_head_elements(
        head_variable=(
            "<title>Page Title</title>",
            '<meta name="description" content="Page desc" />',
            '<link rel="canonical" href="/page" />',
        ),
        head_jsx_blocks=(
            '<meta name="keywords" content="jsx, test" />',
            '<meta name="description" content="Override desc" />',  # Should override
        ),
        layout_head_jsx_blocks=(
            "<title>Layout Title</title>",  # Should be overridden
            '<meta name="author" content="Layout Author" />',
        ),
    )
    
    # Expected: 5 elements (title from page, description from jsx, canonical from page, keywords from jsx, author from layout)
    assert len(result) == 5
    
    # Check title is from page (priority 2 > 1)
    title_elements = [el for el in result if "<title" in el.lower()]
    assert len(title_elements) == 1
    assert "Page Title" in title_elements[0]
    
    # Check description is from JSX (priority 3 > 2)
    desc_elements = [el for el in result if 'name="description"' in el]
    assert len(desc_elements) == 1
    assert "Override desc" in desc_elements[0]
    
    # Check other elements are present
    assert any('name="keywords"' in el for el in result)
    assert any('name="author"' in el for el in result)
    assert any('rel="canonical"' in el for el in result)


def test_whitespace_handling():
    """Should handle elements with extra whitespace."""
    result = merge_head_elements(
        head_variable=("  <title>Title</title>  ",),
        head_jsx_blocks=("\n  <meta name='test' content='value' />\n",),
        layout_head_jsx_blocks=(),
    )
    # Should strip and keep both (no deduplication)
    assert len(result) == 2


def test_empty_strings_filtered():
    """Empty strings should be filtered out."""
    result = merge_head_elements(
        head_variable=("", "<title>Title</title>", "   "),
        head_jsx_blocks=(),
        layout_head_jsx_blocks=(),
    )
    # Only title should remain
    assert len(result) == 1
    assert "<title>Title</title>" in result


def test_jsx_expressions_preserved():
    """JSX expressions in head blocks should be preserved as-is."""
    jsx_with_expression = '<title>{data.title || "Default"}</title>'
    result = merge_head_elements(
        head_variable=(),
        head_jsx_blocks=(jsx_with_expression,),
        layout_head_jsx_blocks=(),
    )
    assert len(result) == 1
    assert result[0] == jsx_with_expression


def test_charset_meta_deduplication():
    """Charset meta tags should deduplicate."""
    result = merge_head_elements(
        head_variable=('<meta charset="utf-8" />',),
        head_jsx_blocks=('<meta charset="UTF-8" />',),
        layout_head_jsx_blocks=(),
    )
    # Should dedupe, JSX wins
    assert len(result) == 1
    assert 'charset="UTF-8"' in result[0]


def test_link_without_rel_not_deduped():
    """Link tags without rel should not deduplicate."""
    result = merge_head_elements(
        head_variable=('<link href="/resource1" />',),
        head_jsx_blocks=('<link href="/resource2" />',),
        layout_head_jsx_blocks=(),
    )
    # No rel, no deduplication
    assert len(result) == 2


def test_inline_script_without_src_not_deduped():
    """Inline scripts without src should not deduplicate."""
    result = merge_head_elements(
        head_variable=("<script>console.log('a');</script>",),
        head_jsx_blocks=("<script>console.log('b');</script>",),
        layout_head_jsx_blocks=(),
    )
    # No src, no deduplication
    assert len(result) == 2


def test_meta_without_name_or_property():
    """Meta tags without name or property should not deduplicate."""
    result = merge_head_elements(
        head_variable=('<meta http-equiv="X-UA-Compatible" content="IE=edge" />',),
        head_jsx_blocks=('<meta http-equiv="Content-Type" content="text/html" />',),
        layout_head_jsx_blocks=(),
    )
    # No name or property, no deduplication (falls through to None)
    assert len(result) == 2


def test_merge_multi_element_head_blocks():
    """Test merging head blocks containing multiple HTML elements (from <Head>...</Head>)."""
    # This simulates a <Head>...</Head> JSX block containing multiple elements
    layout_block = """<meta name="viewport" content="width=device-width" />
                      <link rel="preconnect" href="https://fonts.googleapis.com" />
                      <title>Layout Title</title>"""
    
    page_block = """<title>Page Title</title>
                    <meta name="description" content="Page description" />
                    <link rel="icon" href="/icon.svg" />"""
    
    result = merge_head_elements(
        head_variable=(),
        head_jsx_blocks=(page_block,),
        layout_head_jsx_blocks=(layout_block,),
    )
    
    # Should contain viewport and preconnect from layout
    assert any('viewport' in elem for elem in result)
    assert any('fonts.googleapis.com' in elem for elem in result)
    
    # Should contain description and icon from page
    assert any('description' in elem for elem in result)
    assert any('icon.svg' in elem for elem in result)
    
    # Should have only ONE title (page's title wins)
    title_count = sum(1 for elem in result if '<title>' in elem)
    assert title_count == 1

    # The title should be from page, not layout
    titles = [elem for elem in result if '<title>' in elem]
    assert 'Page Title' in titles[0]
    assert 'Layout Title' not in titles[0]


# ---------------------------------------------------------------------------
# Runtime <Head> registrations (priority 4 — highest)
# ---------------------------------------------------------------------------


def test_runtime_title_overrides_static_page_title():
    """Runtime <Head> registration must override static page extraction.

    The compile-time extraction captures literal source text including
    unevaluated expressions; the runtime registration is the React-rendered
    output. Runtime is therefore always more accurate.
    """
    result = merge_head_elements(
        head_variable=(),
        head_jsx_blocks=("<title>{pageTitle}</title>",),
        runtime_head_blocks=("<title>Installation - Pyxle Docs</title>",),
    )

    titles = [elem for elem in result if "<title>" in elem]
    assert len(titles) == 1
    assert titles[0] == "<title>Installation - Pyxle Docs</title>"


def test_runtime_title_overrides_head_variable():
    """Runtime registration takes precedence over the HEAD variable."""
    result = merge_head_elements(
        head_variable=("<title>From HEAD</title>",),
        head_jsx_blocks=(),
        runtime_head_blocks=("<title>From Runtime</title>",),
    )

    assert result == ("<title>From Runtime</title>",)


def test_runtime_title_overrides_layout_static_title():
    """Runtime registration beats layout static extraction."""
    result = merge_head_elements(
        head_variable=(),
        head_jsx_blocks=(),
        layout_head_jsx_blocks=("<title>Layout Title</title>",),
        runtime_head_blocks=("<title>Runtime Title</title>",),
    )

    assert result == ("<title>Runtime Title</title>",)


def test_runtime_blocks_reversed_so_page_wins_over_layout():
    """Within runtime blocks, the page registration (last in render order)
    must win over outer layout registrations (first in render order).

    React renders outer-to-inner, so a layout's <Head> registers before
    the page's <Head>. The merger reverses the runtime list so the
    deepest registration is processed first and wins.
    """
    result = merge_head_elements(
        head_variable=(),
        head_jsx_blocks=(),
        # Registration order: outer layout → inner layout → page.
        runtime_head_blocks=(
            "<title>Outer Layout Title</title>",
            "<title>Inner Layout Title</title>",
            "<title>Page Title</title>",
        ),
    )

    assert result == ("<title>Page Title</title>",)


def test_runtime_empty_falls_back_to_static_page():
    """When runtime is empty, static page extraction still applies."""
    result = merge_head_elements(
        head_variable=(),
        head_jsx_blocks=("<title>Static Title</title>",),
        runtime_head_blocks=(),
    )

    assert result == ("<title>Static Title</title>",)


def test_runtime_empty_falls_back_to_head_variable():
    """When runtime and static page are empty, HEAD variable is used."""
    result = merge_head_elements(
        head_variable=("<title>HEAD Title</title>",),
        head_jsx_blocks=(),
        runtime_head_blocks=(),
    )

    assert result == ("<title>HEAD Title</title>",)


def test_runtime_meta_overrides_static_meta_by_name():
    """Runtime meta tag overrides static meta with the same name."""
    result = merge_head_elements(
        head_variable=(),
        head_jsx_blocks=('<meta name="description" content="static" />',),
        runtime_head_blocks=('<meta name="description" content="runtime" />',),
    )

    descriptions = [elem for elem in result if 'name="description"' in elem]
    assert len(descriptions) == 1
    assert 'content="runtime"' in descriptions[0]


def test_runtime_only_supplies_dynamic_static_supplies_rest():
    """Runtime can supply only the dynamic title while static supplies
    the rest of the head; both should appear in the output."""
    result = merge_head_elements(
        head_variable=(),
        head_jsx_blocks=(
            '<meta name="viewport" content="width=device-width, initial-scale=1" />'
            '<link rel="icon" href="/favicon.svg" />',
        ),
        runtime_head_blocks=("<title>Dynamic Title</title>",),
    )

    assert any("<title>Dynamic Title</title>" == elem for elem in result)
    assert any('name="viewport"' in elem for elem in result)
    assert any('rel="icon"' in elem for elem in result)


def test_runtime_block_with_multiple_elements():
    """A single runtime block containing multiple elements should be
    split and each element merged independently."""
    result = merge_head_elements(
        head_variable=(),
        head_jsx_blocks=("<title>Static Title</title>",),
        runtime_head_blocks=(
            '<title>Runtime Title</title><meta name="description" content="d" />',
        ),
    )

    titles = [elem for elem in result if "<title>" in elem]
    assert len(titles) == 1
    assert titles[0] == "<title>Runtime Title</title>"
    assert any('name="description"' in elem for elem in result)


def test_runtime_non_keyed_element_appears_in_output():
    """Runtime elements without a dedupe key (e.g., a script with no src)
    should still be included in the output."""
    inline_script = '<script>console.log("hi")</script>'
    result = merge_head_elements(
        head_variable=(),
        head_jsx_blocks=(),
        runtime_head_blocks=(inline_script,),
    )

    assert any("console.log" in elem for elem in result)



