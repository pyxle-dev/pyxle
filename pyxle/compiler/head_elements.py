"""What a head-element string carries beyond its first element.

A ``HEAD`` entry is **one** element. The head sanitiser
(``pyxle.ssr.head_merger.sanitize_head_element``) parses each entry and rebuilds
it from its first element only, dropping whatever follows — the same pass that
discards markup injected after an attribute quote breakout. That is a security
boundary, not an oversight, so the answer to a two-element entry is to tell the
author to split it rather than to teach the sanitiser to keep both.

This module answers only "what would be dropped?". The compiler uses it to
refuse a literal that would lose content before it can ever be deployed; the SSR
path uses it to warn about a value that could only be known at render time. It
lives under ``compiler/`` because the compiler imports nothing from the rest of
Pyxle, and one shared definition is what keeps the build-time and render-time
halves from drifting apart — two consumers judging the same concept by two rules
is the shape of the bug this whole area keeps producing.
"""

from __future__ import annotations

from html.parser import HTMLParser

__all__ = ["find_discarded_head_content"]

# HTML void elements: no closing tag, so the element ends with its start tag.
# ``meta`` and ``link`` are the ones that matter for a head, but the full set
# keeps the scanner honest if a stray ``<img>`` shows up (the sanitiser drops it
# for being outside the head allowlist — that is a separate, already-loud rule).
_VOID_ELEMENTS = frozenset(
    {
        "area", "base", "br", "col", "embed", "hr", "img", "input",
        "link", "meta", "param", "source", "track", "wbr",
    }
)


class _FirstElementScanner(HTMLParser):
    """Locate where the first top-level element starts and ends.

    Nested tags are followed by depth so an element containing markup is still
    one element. ``<script>``/``<style>`` bodies are handled by
    :class:`~html.parser.HTMLParser`'s own CDATA mode, so a ``<`` inside inline
    code is text rather than a tag.
    """

    def __init__(self, text: str) -> None:
        super().__init__(convert_charrefs=False)
        self._text = text
        self._line_starts: list[int] = [0]
        for line in text.split("\n"):
            self._line_starts.append(self._line_starts[-1] + len(line) + 1)
        self._depth = 0
        self.first_start: int | None = None
        self.first_end: int | None = None

    def _offset(self) -> int:
        line, column = self.getpos()
        return self._line_starts[line - 1] + column

    def _note_start(self) -> None:
        if self.first_start is None:
            self.first_start = self._offset()

    def handle_starttag(self, tag: str, attrs: object) -> None:
        if self.first_end is not None:
            return
        if tag.lower() in _VOID_ELEMENTS:
            self.handle_startendtag(tag, attrs)
            return
        if self._depth == 0:
            self._note_start()
        self._depth += 1

    def handle_startendtag(self, tag: str, attrs: object) -> None:
        if self.first_end is not None:
            return
        if self._depth == 0:
            self._note_start()
            self.first_end = self._offset() + len(self.get_starttag_text() or "")

    def handle_endtag(self, tag: str) -> None:
        if self.first_end is not None or tag.lower() in _VOID_ELEMENTS:
            return
        self._depth -= 1
        if self._depth <= 0:
            self._depth = 0
            closing = self._text.find(">", self._offset())
            self.first_end = len(self._text) if closing == -1 else closing + 1


def find_discarded_head_content(html: str) -> str | None:
    """Return the part of *html* the head sanitiser will drop, or ``None``.

    ``None`` means the string is a single head element and survives intact.
    A returned string is content that exists in the source and will never
    reach the document — a second ``<meta>``, a trailing ``<link>``, stray text
    beside the element.

    Fails open: anything this cannot parse returns ``None`` rather than
    blocking a build on the detector's own limitations.
    """
    text = html.strip()
    if not text:
        return None

    scanner = _FirstElementScanner(text)
    try:
        scanner.feed(text)
        scanner.close()
    except Exception:  # pragma: no cover - HTMLParser is lenient by design
        return None

    # No element at all, or one that never terminates: the sanitiser's own
    # rules cover both, and neither is the "second element" case.
    if scanner.first_start is None or scanner.first_end is None:
        return None

    discarded = [
        text[: scanner.first_start].strip(),
        text[scanner.first_end :].strip(),
    ]
    remainder = " ".join(part for part in discarded if part)
    return remainder or None
