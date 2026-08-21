"""One ``HEAD`` entry is one element, and the rest is never silently lost.

The head sanitiser rebuilds each entry from its first element only — the same
pass that discards markup injected after an attribute quote breakout — so a
second element in one entry is content the author wrote and no visitor ever
receives. This module's detector is what makes that audible: a build error
where the entry is a literal, a logged warning where it could only be computed.

The false-positive cases below matter as much as the true positives. A detector
that cries wolf on an inline ``<script>`` containing ``a < b``, or on a
``<style>`` full of braces, would either be switched off or would block a build
that was correct — and both outcomes end with head content going missing again.
"""

from __future__ import annotations

import pytest

from pyxle.compiler.head_elements import find_discarded_head_content


class TestSingleElementEntriesAreLeftAlone:
    """Anything that survives sanitisation intact must report nothing."""

    @pytest.mark.parametrize(
        "html",
        [
            '<meta name="a" content="1" />',
            '<meta name="a" content="1">',  # void element, unclosed
            "<title>My Page</title>",
            '<link rel="canonical" href="https://example.com/a" />',
            '<script type="application/ld+json">{"@type":"Organization"}</script>',
            "<style>.hero { color: red } .card { padding: 1rem }</style>",
            '<script>if (a < b) { document.write("<p>hi</p>") }</script>',
            '   <meta name="a" content="1" />   ',
            '<meta name="d" content="Braces {like these} are prose" />',
            "",
            "   ",
        ],
        ids=[
            "self-closed-meta", "unclosed-meta", "title", "link", "json-ld",
            "style-with-braces", "script-with-lt-and-markup", "surrounded-by-space",
            "literal-braces", "empty", "whitespace-only",
        ],
    )
    def test_nothing_is_reported_as_dropped(self, html: str) -> None:
        assert find_discarded_head_content(html) is None

    @pytest.mark.parametrize(
        "html",
        ["<script>unterminated", "no tags at all", "<<<>>>"],
        ids=["unterminated", "plain-text", "garbage"],
    )
    def test_it_fails_open_rather_than_blocking_a_build(self, html: str) -> None:
        """The detector's own limits must never cost someone a build. These are
        the sanitiser's problem (it fails closed); they are not a second
        element, so this reports nothing."""
        assert find_discarded_head_content(html) is None


class TestASecondElementIsReported:
    def test_two_metas_report_the_second(self) -> None:
        discarded = find_discarded_head_content(
            '<meta name="twin-a" content="FIRST" /><meta name="twin-b" content="SECOND" />'
        )
        assert discarded is not None
        assert "twin-b" in discarded
        assert "twin-a" not in discarded, "reported the element that survives"

    def test_the_shape_our_own_docs_used_to_teach(self) -> None:
        """`'<title>…</title><meta … />'` was the first example in the head
        guide. Anyone who copied it lost the meta and had no way to find out."""
        discarded = find_discarded_head_content(
            '<title>My Page</title><meta name="description" content="Page description" />'
        )
        assert discarded is not None
        assert "description" in discarded

    def test_whitespace_between_elements_does_not_hide_the_second(self) -> None:
        discarded = find_discarded_head_content(
            '<meta a="1"/>\n    <link rel="icon" href="/f.ico"/>'
        )
        assert discarded is not None
        assert "icon" in discarded

    def test_a_paired_element_followed_by_another(self) -> None:
        discarded = find_discarded_head_content(
            "<style>.a{color:red}</style><style>.b{color:blue}</style>"
        )
        assert discarded is not None
        assert ".b" in discarded

    def test_trailing_text_is_dropped_content_too(self) -> None:
        discarded = find_discarded_head_content(
            '<link rel="stylesheet" href="/a.css">oops trailing text'
        )
        assert discarded == "oops trailing text"

    def test_leading_text_is_dropped_content_too(self) -> None:
        assert find_discarded_head_content('lead<meta name="x" content="y"/>') == "lead"
