"""Tests for the dev server's compile-failure bookkeeping and error page."""

from __future__ import annotations

from pathlib import Path

import pytest

from pyxle.devserver.build_errors import (
    BuildFailure,
    BuildFailureRegistry,
    build_code_frame,
    find_build_failure,
    find_unrouted_build_failure,
    format_failures,
    render_build_failure_document,
)
from pyxle.devserver.settings import DevServerSettings


@pytest.fixture
def settings(tmp_path: Path) -> DevServerSettings:
    root = tmp_path / "project"
    (root / "pages").mkdir(parents=True)
    (root / "public").mkdir()
    return DevServerSettings.from_project_root(root)


def make_failure(
    page_relative: str,
    *,
    message: str = "unexpected indent",
    line: int | None = 7,
    column: int | None = 9,
    code_frame: str = "",
) -> BuildFailure:
    return BuildFailure(
        page_relative_path=Path(page_relative),
        display_path=f"pages/{page_relative}",
        message=message,
        line=line,
        column=column,
        code_frame=code_frame,
    )


class TestBuildFailureLocation:
    def test_location_includes_line_and_column(self) -> None:
        failure = make_failure("about.pyxl")

        assert failure.location == "pages/about.pyxl:7:9"
        assert failure.describe() == "pages/about.pyxl:7:9: unexpected indent"

    def test_location_omits_column_when_unknown(self) -> None:
        failure = make_failure("about.pyxl", column=None)

        assert failure.location == "pages/about.pyxl:7"

    def test_location_is_the_bare_path_when_line_is_unknown(self) -> None:
        failure = make_failure("about.pyxl", line=None, column=None)

        assert failure.location == "pages/about.pyxl"
        assert failure.describe() == "pages/about.pyxl: unexpected indent"

    @pytest.mark.parametrize(
        ("relative", "expected"),
        [
            ("layout.pyxl", True),
            ("blog/template.pyxl", True),
            ("blog/Layout.PYXL", True),
            ("about.pyxl", False),
            ("blog/layouts.pyxl", False),
        ],
    )
    def test_is_wrapper_identifies_layouts_and_templates(
        self, relative: str, expected: bool
    ) -> None:
        assert make_failure(relative).is_wrapper is expected

    def test_format_failures_joins_descriptions(self) -> None:
        joined = format_failures(
            [make_failure("about.pyxl"), make_failure("blog/index.pyxl", line=2, column=None)]
        )

        assert joined == (
            "pages/about.pyxl:7:9: unexpected indent; "
            "pages/blog/index.pyxl:2: unexpected indent"
        )


class TestCodeFrame:
    def test_frame_marks_the_failing_line_and_column(self) -> None:
        source = "one\ntwo\nthree\nfour\nfive\nsix\n"

        frame = build_code_frame(source, 3, 2)

        assert frame.splitlines() == [
            "  1 | one",
            "  2 | two",
            "> 3 | three",
            "       ^",
            "  4 | four",
            "  5 | five",
        ]

    def test_frame_without_a_column_has_no_caret(self) -> None:
        frame = build_code_frame("alpha\nbeta\n", 1, None)

        assert "^" not in frame
        assert "> 1 | alpha" in frame

    def test_frame_handles_crlf_sources(self) -> None:
        frame = build_code_frame("alpha\r\nbeta\r\n", 2, 1)

        assert "> 2 | beta" in frame

    @pytest.mark.parametrize("line", [None, 0, 99])
    def test_frame_is_empty_when_there_is_nothing_to_point_at(self, line) -> None:
        assert build_code_frame("alpha\nbeta\n", line, 1) == ""


class TestBuildFailureRegistry:
    def test_empty_registry_blocks_nothing(self) -> None:
        registry = BuildFailureRegistry()

        assert registry.failures == ()
        assert registry.find_for_page(Path("about.pyxl")) is None

    def test_page_is_blocked_by_its_own_failure(self) -> None:
        registry = BuildFailureRegistry()
        registry.replace([make_failure("about.pyxl")])

        found = registry.find_for_page(Path("about.pyxl"))

        assert found is not None
        assert found.display_path == "pages/about.pyxl"

    def test_a_broken_page_leaves_every_other_page_alone(self) -> None:
        """The blast radius of one unparseable page is exactly that page."""
        registry = BuildFailureRegistry()
        registry.replace([make_failure("about.pyxl")])

        assert registry.find_for_page(Path("index.pyxl")) is None
        assert registry.find_for_page(Path("blog/index.pyxl")) is None

    def test_root_layout_blocks_every_page(self) -> None:
        registry = BuildFailureRegistry()
        registry.replace([make_failure("layout.pyxl")])

        assert registry.find_for_page(Path("index.pyxl")) is not None
        assert registry.find_for_page(Path("blog/post.pyxl")) is not None

    def test_nested_layout_blocks_only_its_subtree(self) -> None:
        registry = BuildFailureRegistry()
        registry.replace([make_failure("blog/layout.pyxl")])

        assert registry.find_for_page(Path("blog/post.pyxl")) is not None
        assert registry.find_for_page(Path("blog/deep/post.pyxl")) is not None
        assert registry.find_for_page(Path("index.pyxl")) is None
        assert registry.find_for_page(Path("shop/item.pyxl")) is None

    def test_nearest_broken_wrapper_wins(self) -> None:
        registry = BuildFailureRegistry()
        registry.replace(
            [make_failure("layout.pyxl"), make_failure("blog/template.pyxl")]
        )

        found = registry.find_for_page(Path("blog/post.pyxl"))

        assert found is not None
        assert found.display_path == "pages/blog/template.pyxl"

    def test_own_failure_wins_over_a_broken_layout(self) -> None:
        registry = BuildFailureRegistry()
        registry.replace([make_failure("layout.pyxl"), make_failure("about.pyxl")])

        found = registry.find_for_page(Path("about.pyxl"))

        assert found is not None
        assert found.display_path == "pages/about.pyxl"

    def test_replace_drops_the_previous_pass_failures(self) -> None:
        registry = BuildFailureRegistry()
        registry.replace([make_failure("about.pyxl")])

        registry.replace([])

        assert registry.find_for_page(Path("about.pyxl")) is None

    def test_clear_empties_the_registry(self) -> None:
        registry = BuildFailureRegistry()
        registry.replace([make_failure("about.pyxl")])

        registry.clear()

        assert registry.failures == ()


class TestFindBuildFailure:
    class _Route:
        def __init__(self, relative: str) -> None:
            self.source_relative_path = Path(relative)

    def test_absent_registry_never_blocks(self) -> None:
        """Production never builds a registry, so the check must tolerate None."""
        assert find_build_failure(None, self._Route("about.pyxl")) is None
        assert find_build_failure(object(), self._Route("about.pyxl")) is None

    def test_registry_is_consulted_for_the_route_source(self) -> None:
        registry = BuildFailureRegistry()
        registry.replace([make_failure("about.pyxl")])

        assert find_build_failure(registry, self._Route("about.pyxl")) is not None
        assert find_build_failure(registry, self._Route("index.pyxl")) is None


class TestBuildFailureDocument:
    def test_document_names_file_line_column_and_message(
        self, settings: DevServerSettings
    ) -> None:
        failure = make_failure("about.pyxl", code_frame="> 7 |     oops = 1")

        html = render_build_failure_document(
            failure, settings=settings, route_path="/about"
        )

        assert "Build failed" in html
        assert "pages/about.pyxl" in html
        assert "pages/about.pyxl:7:9" in html
        assert "unexpected indent" in html
        assert "&gt; 7 |     oops = 1" in html
        assert "/about" in html

    def test_document_reloads_itself_on_the_next_successful_rebuild(
        self, settings: DevServerSettings
    ) -> None:
        html = render_build_failure_document(make_failure("about.pyxl"), settings=settings)

        assert "/__pyxle__/overlay" in html
        assert "window.location.reload()" in html

    def test_document_escapes_the_compiler_message(
        self, settings: DevServerSettings
    ) -> None:
        failure = make_failure("about.pyxl", message="<script>alert(1)</script>")

        html = render_build_failure_document(failure, settings=settings)

        assert "<script>alert(1)</script>" not in html
        assert "&lt;script&gt;" in html

    def test_document_omits_the_frame_when_none_was_captured(
        self, settings: DevServerSettings
    ) -> None:
        html = render_build_failure_document(make_failure("about.pyxl"), settings=settings)

        assert '<pre class="pyxle-frame">' not in html

    def test_document_works_without_a_route_path(
        self, settings: DevServerSettings
    ) -> None:
        html = render_build_failure_document(make_failure("about.pyxl"), settings=settings)

        assert "this page cannot be served" in html


class TestNeverCompiledSource:
    """A file that has never compiled has no route, so another page answers
    its URL — a catch-all page will render a healthy 200 for it."""

    class _Route:
        def __init__(self, relative: str) -> None:
            self.source_relative_path = Path(relative)

    @staticmethod
    def _registry() -> BuildFailureRegistry:
        registry = BuildFailureRegistry()
        registry.replace(
            [
                BuildFailure(
                    page_relative_path=Path("brandnew.pyxl"),
                    display_path="pages/brandnew.pyxl",
                    message="unexpected indent",
                    line=7,
                    column=6,
                    url_paths=("/brandnew",),
                )
            ]
        )
        return registry

    def test_url_lookup_finds_the_source_that_should_own_it(self) -> None:
        assert self._registry().find_for_url("/brandnew") is not None

    def test_url_lookup_leaves_other_urls_alone(self) -> None:
        assert self._registry().find_for_url("/somewhere-else") is None

    def test_catchall_route_serving_a_broken_url_reports_the_failure(self) -> None:
        catchall = self._Route("[...slug].pyxl")

        found = find_build_failure(self._registry(), catchall, url_path="/brandnew")

        assert found is not None
        assert found.display_path == "pages/brandnew.pyxl"

    def test_catchall_still_serves_urls_that_belong_to_nothing_broken(self) -> None:
        catchall = self._Route("[...slug].pyxl")

        assert find_build_failure(self._registry(), catchall, url_path="/other") is None

    def test_the_page_own_failure_is_preferred_over_a_url_match(self) -> None:
        registry = self._registry()
        registry.replace([*registry.failures, make_failure("about.pyxl")])

        found = find_build_failure(
            registry, self._Route("about.pyxl"), url_path="/brandnew"
        )

        assert found is not None
        assert found.display_path == "pages/about.pyxl"


def make_unrouted_failure(
    page_relative: str,
    *,
    url_paths: tuple[str, ...] = (),
    url_patterns: tuple[str, ...] = (),
) -> BuildFailure:
    """A failure for a source that never compiled, so it owns URLs, not a route."""

    return BuildFailure(
        page_relative_path=Path(page_relative),
        display_path=f"pages/{page_relative}",
        message="invalid syntax",
        line=6,
        column=9,
        url_paths=url_paths,
        url_patterns=url_patterns,
    )


class TestUnroutedUrlLookup:
    """The 404 path: a URL matched no route *because* its page does not build.

    Without this the request reaches the ordinary 404, whose only advice is
    about routing — a dead end when the file is present and correctly named.
    """

    def test_static_url_of_a_never_compiled_page_is_matched(self) -> None:
        registry = BuildFailureRegistry()
        registry.replace([make_unrouted_failure("about.pyxl", url_paths=("/about",))])

        found = registry.find_for_unrouted_url("/about")

        assert found is not None
        assert found.display_path == "pages/about.pyxl"

    def test_a_url_no_broken_page_claims_is_an_ordinary_404(self) -> None:
        registry = BuildFailureRegistry()
        registry.replace([make_unrouted_failure("about.pyxl", url_paths=("/about",))])

        assert registry.find_for_unrouted_url("/elsewhere") is None

    def test_an_empty_registry_claims_nothing(self) -> None:
        assert BuildFailureRegistry().find_for_unrouted_url("/about") is None

    def test_dynamic_page_claims_the_urls_it_would_have_served(self) -> None:
        """``pages/posts/[slug].pyxl`` would have answered ``/posts/hello``, so
        its failure to compile is why nothing did."""
        registry = BuildFailureRegistry()
        registry.replace(
            [make_unrouted_failure("posts/[slug].pyxl", url_patterns=("/posts/{slug}",))]
        )

        found = registry.find_for_unrouted_url("/posts/hello")

        assert found is not None
        assert found.display_path == "pages/posts/[slug].pyxl"

    def test_a_dynamic_pattern_does_not_claim_urls_outside_it(self) -> None:
        registry = BuildFailureRegistry()
        registry.replace(
            [make_unrouted_failure("posts/[slug].pyxl", url_patterns=("/posts/{slug}",))]
        )

        assert registry.find_for_unrouted_url("/posts/a/b") is None
        assert registry.find_for_unrouted_url("/shop/hello") is None

    def test_catchall_pattern_claims_its_whole_subtree(self) -> None:
        registry = BuildFailureRegistry()
        registry.replace(
            [make_unrouted_failure("docs/[...path].pyxl", url_patterns=("/docs/{path:path}",))]
        )

        assert registry.find_for_unrouted_url("/docs/a/b/c") is not None
        assert registry.find_for_unrouted_url("/guides/a") is None

    def test_a_static_url_outranks_a_pattern_that_also_matches(self) -> None:
        registry = BuildFailureRegistry()
        registry.replace(
            [
                make_unrouted_failure("[...slug].pyxl", url_patterns=("/{slug:path}",)),
                make_unrouted_failure("about.pyxl", url_paths=("/about",)),
            ]
        )

        found = registry.find_for_unrouted_url("/about")

        assert found is not None
        assert found.display_path == "pages/about.pyxl"

    def test_a_concrete_pattern_outranks_a_catchall(self) -> None:
        registry = BuildFailureRegistry()
        registry.replace(
            [
                make_unrouted_failure("[...slug].pyxl", url_patterns=("/{slug:path}",)),
                make_unrouted_failure("posts/[slug].pyxl", url_patterns=("/posts/{slug}",)),
            ]
        )

        found = registry.find_for_unrouted_url("/posts/hello")

        assert found is not None
        assert found.display_path == "pages/posts/[slug].pyxl"

    def test_the_deeper_catchall_wins_over_the_shallower_one(self) -> None:
        registry = BuildFailureRegistry()
        registry.replace(
            [
                make_unrouted_failure("[...slug].pyxl", url_patterns=("/{slug:path}",)),
                make_unrouted_failure(
                    "docs/[...path].pyxl", url_patterns=("/docs/{path:path}",)
                ),
            ]
        )

        found = registry.find_for_unrouted_url("/docs/intro")

        assert found is not None
        assert found.display_path == "pages/docs/[...path].pyxl"

    def test_absent_registry_never_claims_a_url(self) -> None:
        """Production never builds a registry, so the 404 check tolerates None."""
        assert find_unrouted_build_failure(None, "/about") is None
        assert find_unrouted_build_failure(object(), "/about") is None

    def test_helper_delegates_to_a_real_registry(self) -> None:
        registry = BuildFailureRegistry()
        registry.replace([make_unrouted_failure("about.pyxl", url_paths=("/about",))])

        assert find_unrouted_build_failure(registry, "/about") is not None
        assert find_unrouted_build_failure(registry, "/other") is None


class TestBuildFailureHint:
    """The closing hint has to match how the developer got here."""

    def test_a_route_that_used_to_work_explains_the_page_they_were_seeing(
        self, settings: DevServerSettings
    ) -> None:
        html = render_build_failure_document(make_failure("about.pyxl"), settings=settings)

        assert "the last one that compiled" in html
        assert "no route for this address" not in html

    def test_a_page_that_never_compiled_says_routing_is_not_the_problem(
        self, settings: DevServerSettings
    ) -> None:
        """The whole point of the fix: the file *is* in pages/ and *is* named
        correctly, so the hint must not send the reader looking there."""
        html = render_build_failure_document(
            make_failure("about.pyxl"), settings=settings, had_route=False
        )

        assert "no route for this address" in html
        assert "right place with the right name is not the problem" in html
        assert "the last one that compiled" not in html
