"""An error names the file the author wrote, never the one Pyxle generated.

``pages/about.jsx:8:8`` is a path the developer never created, inside a build
directory they do not know exists, at a line number that is not theirs: a page's
JSX half starts wherever its Python half ended, so JSX line 1 is routinely line
19 or line 40 of the ``.pyxl``. Someone who opens their file at line 8 finds
unrelated code and concludes the error is nonsense.

Two callers pass error text through this map: ``pyxle build``, with the stderr
Vite exited on, and the SSR worker pool, with the message a failed render came
back with.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pyxle.ssr.source_locations import remap_generated_locations


@pytest.fixture
def client_root(tmp_path: Path) -> Path:
    """A client build dir carrying the compiler's real sidecar shape."""
    root = tmp_path / ".pyxle-build" / "client"
    (root / "pages").mkdir(parents=True)
    sidecar = {
        # A page whose Python half occupies lines 1-18: JSX line 1 is line 19.
        "pages/index.jsx": {
            "pyxl": "../../pages/index.pyxl",
            "lines": [19, 20, 21, 22, 23, 24, 25, 26],
        },
        "pages/nested/deep.jsx": {
            "pyxl": "../../pages/nested/deep.pyxl",
            "lines": [3, 4, 5],
        },
    }
    (root / "pyxl-sourcemaps.json").write_text(json.dumps(sidecar), encoding="utf-8")
    return root


@pytest.fixture
def copied_component(client_root: Path) -> Path:
    """A ``.jsx`` the developer wrote, and the build's verbatim copy of it.

    ``pages/components/Bad.jsx`` is not compiled from anything — the builder
    copies it into the client tree byte for byte — so the two files agree line
    for line and the sidecar knows nothing about either.
    """
    pages_root = client_root.parent.parent / "pages"
    body = "export default function Bad() {\n  return <span>hi</span>;\n}\n"
    author = pages_root / "components" / "Bad.jsx"
    author.parent.mkdir(parents=True)
    author.write_text(body, encoding="utf-8")

    copy = client_root / "pages" / "components" / "Bad.jsx"
    copy.parent.mkdir(parents=True, exist_ok=True)
    copy.write_text(body, encoding="utf-8")
    return pages_root


class TestPositionsBecomeTheAuthorsFile:
    def test_line_and_column_are_rewritten(self, client_root: Path) -> None:
        message = 'pages/index.jsx:3:8: ERROR: Expected ">" but found "<"'
        assert remap_generated_locations(message, client_root) == (
            'pages/index.pyxl:21:8: ERROR: Expected ">" but found "<"'
        )

    def test_the_line_offset_is_the_whole_point(self, client_root: Path) -> None:
        """JSX line 1 is not file line 1. Reporting it as line 1 sends the
        developer to their imports to look for a JSX bug."""
        assert "pages/index.pyxl:19" in remap_generated_locations(
            "pages/index.jsx:1:0: ERROR: boom", client_root
        )

    def test_a_position_without_a_column_still_maps(self, client_root: Path) -> None:
        assert remap_generated_locations("pages/index.jsx:2: oops", client_root) == (
            "pages/index.pyxl:20: oops"
        )

    def test_several_positions_in_one_message(self, client_root: Path) -> None:
        message = "pages/index.jsx:1:0: first\npages/nested/deep.jsx:2:4: second"
        result = remap_generated_locations(message, client_root)
        assert "pages/index.pyxl:19:0: first" in result
        assert "pages/nested/deep.pyxl:4:4: second" in result

    def test_an_absolute_generated_path_is_matched_by_suffix(
        self, client_root: Path
    ) -> None:
        """esbuild may report an absolute path into ``.pyxle-build``. The
        developer must still be shown their own file."""
        absolute = f"{client_root}/pages/index.jsx:3:8: ERROR: boom"
        assert "pages/index.pyxl:21:8" in remap_generated_locations(absolute, client_root)

    def test_the_build_directory_is_not_named_to_the_developer(
        self, client_root: Path
    ) -> None:
        message = f"{client_root}/pages/index.jsx:3:8: ERROR: boom"
        assert ".pyxle-build" in message, "the input must name what we claim to strip"
        result = remap_generated_locations(message, client_root)
        assert ".pyxle-build" not in result
        assert ".jsx" not in result


class TestAnUnmappablePositionSaysSo:
    """Silence is the failure mode: presenting an artifact path as the author's
    file is worse than admitting the position is approximate."""

    def test_an_unknown_file_is_labelled_generated(self, client_root: Path) -> None:
        result = remap_generated_locations("pages/ghost.jsx:4:2: ERROR: boom", client_root)
        assert "pages/ghost.jsx:4:2 (generated)" in result

    def test_a_line_outside_the_map_names_the_source_and_admits_the_gap(
        self, client_root: Path
    ) -> None:
        """An error inside code the compiler emitted, not code the author wrote.
        Name their page so they know which one, and be explicit that the
        position belongs to the generated module."""
        result = remap_generated_locations("pages/index.jsx:99:1: ERROR: boom", client_root)
        assert "pages/index.pyxl" in result
        assert "in generated output at pages/index.jsx:99:1" in result

    def test_a_missing_sidecar_labels_rather_than_lies(self, tmp_path: Path) -> None:
        empty = tmp_path / "client"
        empty.mkdir()
        result = remap_generated_locations("pages/index.jsx:3:8: ERROR: boom", empty)
        assert "(generated)" in result

    def test_no_client_root_still_labels(self) -> None:
        assert "(generated)" in remap_generated_locations("a.jsx:1:1: boom", None)


class TestItLeavesEverythingElseAlone:
    @pytest.mark.parametrize(
        "message",
        [
            "TypeError: Cannot read properties of undefined",
            "Loader for /about raised ValueError",
            "",
            "a message mentioning pages/index.pyxl:19 already",
        ],
    )
    def test_messages_without_a_generated_position_are_untouched(
        self, client_root: Path, message: str
    ) -> None:
        assert remap_generated_locations(message, client_root) == message

    def test_the_sidecar_is_reread_after_a_rebuild(self, client_root: Path) -> None:
        """The map changes whenever a page's Python half grows or shrinks. A
        cached copy that outlived the rebuild would report the old line."""
        assert "pages/index.pyxl:21" in remap_generated_locations(
            "pages/index.jsx:3:8: boom", client_root
        )

        sidecar = client_root / "pyxl-sourcemaps.json"
        sidecar.write_text(
            json.dumps(
                {"pages/index.jsx": {"pyxl": "../../pages/index.pyxl", "lines": [50, 51, 52]}}
            ),
            encoding="utf-8",
        )
        import os

        stat = sidecar.stat()
        os.utime(sidecar, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))

        assert "pages/index.pyxl:52" in remap_generated_locations(
            "pages/index.jsx:3:8: boom", client_root
        )


class TestAComponentTheAuthorWroteIsNotCalledGenerated:
    """Not everything under the client root was generated.

    A plain ``.jsx`` beside the pages is *copied* into the build tree, so line 4
    of the copy is line 4 of the developer's own file. Labelling that position
    "(generated)" tells them the code is compiler output when it is theirs,
    verbatim, at exactly that line — the same lie this module exists to prevent,
    pointed at the other kind of file.
    """

    def test_the_authors_own_file_is_named_and_the_position_kept(
        self, client_root: Path, copied_component: Path
    ) -> None:
        message = (
            f"{client_root}/pages/components/Bad.jsx:5:6: "
            'ERROR: Unexpected closing "div" tag'
        )
        result = remap_generated_locations(message, client_root, copied_component)

        assert result.startswith("pages/components/Bad.jsx:5:6:"), result
        assert "(generated)" not in result
        assert ".pyxle-build" not in result

    def test_a_relative_report_of_the_same_file_is_recognised(
        self, client_root: Path, copied_component: Path
    ) -> None:
        """esbuild reports an absolute path, Rollup a build-relative one."""
        result = remap_generated_locations(
            "pages/components/Bad.jsx:2:9: ERROR: boom", client_root, copied_component
        )
        assert result == "pages/components/Bad.jsx:2:9: ERROR: boom"

    def test_a_generated_page_is_still_labelled_when_the_map_is_gone(
        self, tmp_path: Path
    ) -> None:
        """The rule may not be "under pages/ and unknown, so it is the
        author's". A production ``dist/`` ships the compiled modules without the
        sidecar; calling ``pages/index.jsx`` the author's file there would
        report a JSX-relative line as a ``.pyxl`` one."""
        client = tmp_path / "dist" / "client"
        (client / "pages").mkdir(parents=True)
        (client / "pages" / "index.jsx").write_text("x\n", encoding="utf-8")
        pages = tmp_path / "dist" / "app" / "pages"
        pages.mkdir(parents=True)
        (pages / "index.pyxl").write_text("x\n", encoding="utf-8")

        result = remap_generated_locations(
            "pages/index.jsx:3:8: ERROR: boom", client, pages
        )
        assert "(generated)" in result

    def test_without_a_pages_root_it_falls_back_to_labelling(
        self, client_root: Path, copied_component: Path
    ) -> None:
        """The copied-asset answer needs the source directory to be provable.
        Absent it, the conservative label is still the honest answer."""
        result = remap_generated_locations(
            "pages/components/Bad.jsx:5:6: ERROR: boom", client_root
        )
        assert "(generated)" in result


class TestABarePathIsNeverRewritten:
    """The line number is the only thing separating a position a compiler
    reported from a ``.jsx`` the developer typed themselves.

    Without it, this module cannot tell a build-artifact path from the author's
    own words — an import specifier, or a line of their source quoted back
    inside a code frame. Rewriting either edits the developer's text in the very
    message meant to help them read it, which is the same false signal this
    module exists to prevent, aimed at the author instead of the artifact. So a
    bare path is left alone, always, even when it would resolve.

    A ``.jsx`` inside a URL is a separate rule with a separate reason — see
    :class:`TestAUrlIsNeverRewritten` — because a URL routinely *does* carry a
    coordinate, so the line number cannot protect it.
    """

    def test_a_quoted_import_in_a_code_frame_is_left_byte_for_byte(
        self, client_root: Path, copied_component: Path
    ) -> None:
        """esbuild echoes the offending source line back in its code frame. That
        line is the developer's own text and must survive verbatim — rewriting
        the specifier inside it makes Pyxle appear to quote code they never
        wrote."""
        frame = "    2 |  import Bad from '/pages/components/Bad.jsx';"
        assert (
            remap_generated_locations(frame, client_root, copied_component) == frame
        )

    def test_rollups_unresolved_import_passes_through_unchanged(
        self, client_root: Path
    ) -> None:
        """The documented limitation, pinned so it cannot be silently traded for
        the corruption above. Rollup names a build artifact and no line; the
        message reaches the developer exactly as Rollup wrote it, and
        ``docs/architecture/build-and-serve.md`` tells them how to read it."""
        message = (
            'Could not resolve "./components/DoesNotExist.jsx" '
            'from ".pyxle-build/client/pages/index.jsx"'
        )
        assert remap_generated_locations(message, client_root) == message

    def test_a_position_is_still_labelled_when_it_cannot_be_mapped(
        self, client_root: Path
    ) -> None:
        """Requiring the coordinate must not cost the label on the case that
        has one."""
        result = remap_generated_locations("pages/ghost.jsx:4:2: boom", client_root)
        assert "pages/ghost.jsx:4:2 (generated)" in result


class TestAUrlIsNeverRewritten:
    """A ``.jsx`` reached over a URL keeps every byte, coordinate and all.

    The path pattern's character class excludes ``:``, so a match inside a URL
    begins *after* the scheme: ``5176/pages/index.jsx:3:9`` out of
    ``http://localhost:5176/pages/index.jsx:3:9``. Rewriting that leaves
    ``http://localhost:pages/index.pyxl:21:9`` — the port eaten and the link
    dead. Requiring a coordinate cannot help here, because the URL has one.
    """

    @pytest.mark.parametrize(
        "frame",
        [
            # V8, module served by the Vite dev server.
            "    at HomePage (http://localhost:5176/pages/index.jsx:3:9)",
            # Same, with Vite's cache-busting query on the module URL.
            "    at HomePage (http://localhost:5176/pages/index.jsx?t=1755712345678:3:9)",
            # Anonymous frame: no function name, the URL leads the line.
            "    at http://127.0.0.1:9721/pages/index.jsx:3:9",
            "    at HomePage (https://preview.example.com/pages/index.jsx:3:9)",
            # Node resolves a bundled module to a file URL.
            "    at HomePage (file:///app/.pyxle-build/client/pages/index.jsx:3:9)",
            # Vite serves anything outside its root through /@fs/.
            (
                "    at HomePage (http://localhost:5176/@fs/app/.pyxle-build"
                "/client/pages/index.jsx:3:9)"
            ),
            # No coordinate at all: a plain link in prose.
            "see the docs at https://example.com/guide/pages/index.jsx for help",
        ],
    )
    def test_a_url_survives_byte_for_byte(self, client_root: Path, frame: str) -> None:
        assert remap_generated_locations(frame, client_root) == frame

    def test_the_same_position_without_a_scheme_is_still_remapped(
        self, client_root: Path
    ) -> None:
        """The control. Without it every assertion above would also pass on a
        function that returned its argument, and the URL rule would be
        indistinguishable from the remapper being broken."""
        assert remap_generated_locations(
            "    at HomePage (pages/index.jsx:3:9)", client_root
        ) == "    at HomePage (pages/index.pyxl:21:9)"

    def test_a_url_and_a_real_position_in_one_message(self, client_root: Path) -> None:
        """A build error quotes a docs link and reports a position. The link
        must survive and the position must still be translated — leaving the
        URL alone may not become "give up on the whole message"."""
        message = (
            "pages/index.jsx:3:8: ERROR: boom\n"
            "  see https://pyxle.dev/guide/pages/index.jsx:3:9 for help"
        )
        result = remap_generated_locations(message, client_root)
        assert result == (
            "pages/index.pyxl:21:8: ERROR: boom\n"
            "  see https://pyxle.dev/guide/pages/index.jsx:3:9 for help"
        )

    def test_a_position_glued_to_a_url_is_swallowed_by_it(
        self, client_root: Path
    ) -> None:
        """A known narrowing, pinned so it is not traded away by accident.

        The URL match runs to the last ``.jsx`` in the run of characters it
        accepts, so a position joined to a URL by a comma is matched as part of
        the URL and goes untranslated. Excluding the comma instead would make
        the URL pattern miss a URL that contains one, and the path pattern
        would then rewrite that URL's tail — a broken link rather than a missed
        translation, which is the worse of the two.
        """
        glued = "http://h/a.jsx:1:2,pages/index.jsx:5:63"
        assert remap_generated_locations(glued, client_root) == glued

    def test_a_scheme_relative_path_is_not_mistaken_for_a_url(
        self, client_root: Path
    ) -> None:
        """``/@fs/…`` with the origin stripped is a filesystem path, not a URL:
        it names the generated module on disk, so it maps like any other."""
        assert remap_generated_locations(
            "    at HomePage (/@fs/app/.pyxle-build/client/pages/index.jsx:3:9)",
            client_root,
        ) == "    at HomePage (pages/index.pyxl:21:9)"


class TestTheRendererAppliesIt:
    """Wiring: the remap has to happen where the error is raised, so every
    consumer — the terminal log, the dev overlay, the error document — shows the
    author's file without each having to remember to translate."""

    @pytest.fixture
    def anyio_backend(self) -> str:  # pragma: no cover - fixture wiring
        return "asyncio"

    @pytest.mark.anyio
    async def test_a_worker_build_failure_is_reported_against_the_pyxl(
        self, client_root: Path
    ) -> None:
        from pyxle.ssr.renderer import ComponentRenderError, pool_render_factory

        class FakePool:
            """A build that succeeded, then failed while bundling for a render."""

            def __init__(self, root: Path) -> None:
                self.client_root = root
                self.pages_root = root.parent.parent / "pages"

            async def render(self, *_args, **_kwargs) -> dict:
                return {
                    "ok": False,
                    "message": (
                        'Build failed with 1 error:\n'
                        'pages/index.jsx:3:8: ERROR: Expected ">" but found "<"'
                    ),
                }

        render = pool_render_factory(FakePool(client_root))(
            client_root / "pages" / "index.jsx"
        )

        with pytest.raises(ComponentRenderError) as excinfo:
            await render({})

        message = str(excinfo.value)
        assert "pages/index.pyxl:21:8" in message, message
        assert ".jsx" not in message, "the generated artifact was still named"
