"""Compile failures the dev server has to keep telling the truth about.

A ``pyxle dev`` rebuild that cannot compile a source file leaves the previous
pass' artifacts on disk. Serving those is how a broken project looks healthy:
the route answers ``200`` with the last version that compiled, so the browser
shows a page that no longer matches the file the developer is editing.

This module holds the three pieces that stop that happening:

* :class:`BuildFailure` — one file the last pass could not compile, with the
  location and a code frame captured at failure time.
* :class:`BuildFailureRegistry` — which sources are currently broken, and
  which routes each one takes down.
* :func:`render_build_failure_document` — the dev page served in place of the
  stale render, naming the file, the line and column, and the error.

Everything here is development-only. ``pyxle serve`` builds ahead of time and
refuses to start on a compile error, so a production request can never reach
this path.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from html import escape
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Final, Sequence

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .routes import PageRoute
    from .settings import DevServerSettings

#: Filenames that wrap every page beneath their directory. One of these failing
#: to compile takes down a whole subtree, not a single route.
_WRAPPER_FILENAMES = frozenset({"layout.pyxl", "template.pyxl"})

#: How many source lines of context to keep on each side of a failure's line.
_CODE_FRAME_CONTEXT = 2


@dataclass(frozen=True, slots=True)
class BuildFailure:
    """One source file the most recent build pass could not compile.

    ``page_relative_path`` is relative to ``pages/`` so it can be compared
    against :attr:`~pyxle.devserver.routes.PageRoute.source_relative_path`;
    ``display_path`` is relative to the project root because that is the form a
    developer recognises and an editor can open (``pages/about.pyxl``).
    """

    page_relative_path: Path
    display_path: str
    message: str
    line: int | None = None
    column: int | None = None
    #: Source lines around the failure, captured when the build failed so the
    #: request path never re-reads (and never disagrees with) the file.
    code_frame: str = ""
    #: The URL(s) this source would serve if it compiled, parameterless ones
    #: only. Needed for a page that has *never* compiled: it has no route of
    #: its own, so a dynamic or catch-all page answers its URL — with a
    #: perfectly healthy 200 for a file that does not build.
    url_paths: tuple[str, ...] = ()
    #: The parameterised route patterns (``/posts/{slug}``) this source would
    #: have served. Kept apart from :attr:`url_paths` because matching them
    #: costs a regex and is only ever correct where *no* route matched at all:
    #: a live route always outranks a pattern that was never registered.
    url_patterns: tuple[str, ...] = ()

    @property
    def location(self) -> str:
        """``pages/about.pyxl:7:9`` — the form terminals and editors linkify."""

        if self.line is None:
            return self.display_path
        if self.column is None:
            return f"{self.display_path}:{self.line}"
        return f"{self.display_path}:{self.line}:{self.column}"

    def describe(self) -> str:
        """One line naming both the location and the error."""

        return f"{self.location}: {self.message}"

    @property
    def is_wrapper(self) -> bool:
        """Whether this file is a layout/template, i.e. wraps a whole subtree."""

        return self.page_relative_path.name.lower() in _WRAPPER_FILENAMES


def build_code_frame(source: str, line: int | None, column: int | None) -> str:
    """Render the source lines around *line* with a caret under *column*.

    Returns ``""`` when there is no line to point at or the line is outside the
    source, so the caller can simply omit the frame.
    """

    if line is None:
        return ""
    lines = source.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    if line < 1 or line > len(lines):
        return ""

    first = max(1, line - _CODE_FRAME_CONTEXT)
    last = min(len(lines), line + _CODE_FRAME_CONTEXT)
    width = len(str(last))
    rendered: list[str] = []
    for number in range(first, last + 1):
        marker = ">" if number == line else " "
        rendered.append(f"{marker} {str(number).rjust(width)} | {lines[number - 1]}")
        if number == line and column is not None and column >= 1:
            pad = " " * (len(marker) + 1 + width + 3 + column - 1)
            rendered.append(f"{pad}^")
    return "\n".join(rendered)


class BuildFailureRegistry:
    """The set of sources the most recent dev build pass could not compile.

    Written from the watcher thread and read from the event loop, so the whole
    state is a single immutable tuple that is *replaced* rather than mutated —
    a reader either sees the previous pass' failures or the new ones, never a
    half-updated list, without taking a lock on the request path.
    """

    __slots__ = ("_failures",)

    def __init__(self) -> None:
        self._failures: tuple[BuildFailure, ...] = ()

    @property
    def failures(self) -> tuple[BuildFailure, ...]:
        return self._failures

    def replace(self, failures: Sequence[BuildFailure]) -> None:
        """Make *failures* the current set (an empty sequence clears it)."""

        self._failures = tuple(failures)

    def clear(self) -> None:
        self._failures = ()

    def find_for_url(self, url_path: str) -> BuildFailure | None:
        """The failure belonging to *url_path* itself, if any.

        Answers the case a page-level check cannot see: a source that has never
        compiled has no route, so its URL is picked up by whatever dynamic or
        catch-all page matches — which renders a healthy page for a file that
        does not build. Only parameterless URLs are matched, which is every
        page whose filename carries no ``[param]``.
        """

        for failure in self._failures:
            if url_path in failure.url_paths:
                return failure
        return None

    def find_for_unrouted_url(self, url_path: str) -> BuildFailure | None:
        """The failure that explains why *url_path* matched no route at all.

        Only for the 404 path. A source that has never compiled registers no
        route, so the request falls through to the 404 handler, which would
        otherwise advise the developer about routing — telling them to check a
        file that is present and correctly named while the compiler error the
        framework already has sits unmentioned.

        Broader than :meth:`find_for_url`: because nothing matched, a *dynamic*
        source that failed to compile is a candidate too. That inference is
        sound only here — had ``pages/posts/[slug].pyxl`` compiled, it would
        have answered ``/posts/hello``, so its absence is the reason there is
        no route. On a request a live route did match, the live route is the
        truth and a pattern must never override it.

        The most specific match wins: a static URL over any pattern, a concrete
        pattern over a catch-all, and a deeper pattern over a shallower one.
        """

        static = self.find_for_url(url_path)
        if static is not None:
            return static
        matches: list[tuple[tuple[int, int], str, int]] = []
        for index, failure in enumerate(self._failures):
            for pattern in failure.url_patterns:
                if _pattern_matches(pattern, url_path):
                    matches.append((_pattern_specificity(pattern), pattern, index))
        if not matches:
            return None
        return self._failures[min(matches)[2]]

    def find_for_page(self, page_relative_path: Path) -> BuildFailure | None:
        """The failure that stops *page_relative_path* rendering, if any.

        A page is blocked by its own source failing, or by a ``layout.pyxl`` /
        ``template.pyxl`` in its own directory or any ancestor — those wrap it,
        so a page that still compiles cannot be rendered without them. Nothing
        else counts: a broken ``about.pyxl`` must leave ``/`` alone.

        The nearest wrapper wins, because that is the one whose failure the
        developer is most likely looking at.
        """

        if not self._failures:
            return None
        target = PurePosixPath(page_relative_path.as_posix())
        wrappers: dict[str, BuildFailure] = {}
        for failure in self._failures:
            candidate = PurePosixPath(failure.page_relative_path.as_posix())
            if candidate == target:
                return failure
            if failure.is_wrapper and _wraps(candidate.parent, target):
                wrappers[candidate.parent.as_posix()] = failure
        if not wrappers:
            return None
        # Deepest wrapper directory = nearest ancestor of the page.
        nearest = max(wrappers, key=lambda key: (len(PurePosixPath(key).parts), key))
        return wrappers[nearest]


@lru_cache(maxsize=256)
def _compiled_pattern(pattern: str) -> re.Pattern[str]:
    """Starlette's own regex for *pattern*, so matching cannot drift from routing.

    Bounded LRU (256 entries, least-recently-used evicted): the keys are route
    patterns of failing sources, so the working set is tiny, and an entry is a
    pure function of its key — eviction only costs a recompile.
    """

    from starlette.routing import compile_path  # noqa: PLC0415 - lazy, 404 path only

    regex, _format, _convertors = compile_path(pattern)
    return regex


def _pattern_matches(pattern: str, url_path: str) -> bool:
    """Whether *url_path* would have been served by the route *pattern*."""

    return _compiled_pattern(pattern).match(url_path) is not None


def _pattern_specificity(pattern: str) -> tuple[int, int]:
    """Sort key ranking route patterns from most to least specific.

    A catch-all (``{slug:path}``) swallows whole subtrees, so it loses to any
    concrete pattern; among equals the one with more segments is the closer
    description of the URL.
    """

    return (1 if ":path}" in pattern else 0, -pattern.count("/"))


def _wraps(wrapper_dir: PurePosixPath, page: PurePosixPath) -> bool:
    """Whether a wrapper in *wrapper_dir* encloses the page at *page*."""

    if wrapper_dir in (PurePosixPath("."), PurePosixPath("")):
        return True
    return wrapper_dir in page.parents


def find_build_failure(
    registry: object, route: "PageRoute", *, url_path: str | None = None
) -> BuildFailure | None:
    """Look *route* up in a possibly-absent registry.

    Takes the registry loosely typed because it is read off ``app.state``,
    where it is only present for a dev server: production assembly never
    creates one, so the page handler's check compiles down to one attribute
    read and a ``None`` test.

    ``url_path`` covers the source that has never compiled and therefore has no
    route of its own: the request reaches some *other* page's handler (a
    catch-all, a dynamic segment), and only the URL says which file it should
    have belonged to.
    """

    if not isinstance(registry, BuildFailureRegistry):
        return None
    found = registry.find_for_page(route.source_relative_path)
    if found is not None or url_path is None:
        return found
    return registry.find_for_url(url_path)


def find_unrouted_build_failure(registry: object, url_path: str) -> BuildFailure | None:
    """The compile failure behind *url_path* matching no route, if any.

    The 404 counterpart to :func:`find_build_failure`. Same loose typing and
    the same reason for it: the registry is read off ``app.state``, where a
    production app never has one, so the check costs one attribute read and a
    ``None`` test before every ordinary 404.
    """

    if not isinstance(registry, BuildFailureRegistry):
        return None
    return registry.find_for_unrouted_url(url_path)


def render_build_failure_document(
    failure: BuildFailure,
    *,
    settings: "DevServerSettings",
    route_path: str | None = None,
    had_route: bool = True,
) -> str:
    """Render the dev page served in place of a stale, still-compiling render.

    Deliberately the same dark shell as the SSR failure document
    (:func:`pyxle.ssr.template.render_error_document`) — a developer should
    recognise "Pyxle is telling me something broke" from the shape of the page
    before reading a word of it — with the content a compile failure actually
    has: the file, the line and column, the compiler's message, and the source
    around it.

    ``had_route`` is False when the source has never compiled, so no route was
    ever registered for it. Only the closing hint differs, but it has to: there
    was no previous version to have been looking at, and what the developer saw
    instead was a 404 that blamed the address.

    The page reconnects to the dev overlay socket and reloads itself when the
    next rebuild succeeds, so fixing the file is the whole recovery procedure.
    """

    from pyxle.devserver._security import redact_sensitive_patterns  # noqa: PLC0415

    # Same styles, one copy: the two dev failure documents are the same object
    # to the reader, and drift between them would show as two different Pyxle
    # error pages for two flavours of the same event.
    from pyxle.ssr.template import _ERROR_DOCUMENT_STYLES  # noqa: PLC0415

    location = escape(failure.location)
    message = escape(redact_sensitive_patterns(failure.message))
    display_path = escape(failure.display_path)
    subject = (
        f"<code>{escape(route_path)}</code> cannot be served"
        if route_path
        else "this page cannot be served"
    )
    frame = ""
    if failure.code_frame:
        rendered = escape(redact_sensitive_patterns(failure.code_frame))
        frame = f'\n      <pre class="pyxle-frame">{rendered}</pre>'
    hint = _STALE_ROUTE_HINT if had_route else _NO_ROUTE_HINT

    return f"""<!DOCTYPE html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>Pyxle • Build failed</title>
{_ERROR_DOCUMENT_STYLES}
    <style>
      .pyxle-frame {{
        white-space: pre;
        overflow-x: auto;
        background: rgba(15, 23, 42, 0.6);
        border-radius: 0.5rem;
        padding: 1rem;
      }}
      .pyxle-hint {{ opacity: 0.75; }}
    </style>
  </head>
  <body>
    <main class="pyxle-error">
      <h1>Build failed</h1>
      <p>Pyxle could not compile <code>{display_path}</code>, so {subject}.</p>
      <p><code>{location}</code></p>
      <pre>{message}</pre>{frame}
      <p class="pyxle-hint">{hint}</p>
    </main>
    <script>{_RELOAD_SCRIPT}</script>
  </body>
</html>
"""


#: Shown when the route exists from an earlier, successful pass. Names the
#: previous render explicitly, because a page that looked healthy a moment ago
#: is the confusing part.
_STALE_ROUTE_HINT: Final[str] = (
    "Fix the file and save — this page reloads itself once the rebuild "
    "succeeds. The version you were seeing before was the last one that "
    "compiled, which is why it looked fine."
)

#: Shown when the source has never compiled, so no route was ever registered.
#: Says so outright: without it the developer is left with a 404 blaming the
#: address, and goes looking for a routing mistake that was never made.
_NO_ROUTE_HINT: Final[str] = (
    "Fix the file and save — this page reloads itself once the rebuild "
    "succeeds. Until it compiles there is no route for this address, so the "
    "file being in the right place with the right name is not the problem."
)


#: Reconnects to the dev overlay socket and reloads once a rebuild succeeds.
#: Only ``reload`` is acted on: ``error`` would replace this page with a less
#: specific overlay, and ``clear`` fires whenever any *other* route renders.
_RELOAD_SCRIPT = """
(function () {
  var proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  function connect() {
    var socket = new WebSocket(proto + '//' + window.location.host + '/__pyxle__/overlay');
    socket.onmessage = function (event) {
      try {
        if (JSON.parse(event.data).type === 'reload') {
          window.location.reload();
        }
      } catch (error) {
        /* a message this page does not understand is not its problem */
      }
    };
    socket.onclose = function () { window.setTimeout(connect, 1000); };
    socket.onerror = function () { socket.close(); };
  }
  connect();
})();
""".strip()


def format_failures(failures: Sequence[BuildFailure]) -> str:
    """Join failure descriptions for a single-line log or exception message."""

    return "; ".join(failure.describe() for failure in failures)


__all__ = [
    "BuildFailure",
    "BuildFailureRegistry",
    "build_code_frame",
    "find_build_failure",
    "find_unrouted_build_failure",
    "format_failures",
    "render_build_failure_document",
]
