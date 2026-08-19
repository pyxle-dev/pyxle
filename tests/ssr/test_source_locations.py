"""An error names the file the author wrote, never the one Pyxle generated.

``pages/about.jsx:8:8`` is a path the developer never created, inside a build
directory they do not know exists, at a line number that is not theirs: a page's
JSX half starts wherever its Python half ended, so JSX line 1 is routinely line
19 or line 40 of the ``.pyxl``. Someone who opens their file at line 8 finds
unrelated code and concludes the error is nonsense.

A rebuild-time JSX syntax error is caught before a page is ever registered. This
covers the other path — a build that succeeded and an esbuild failure raised
while the SSR worker bundles the component for a render — where the raw
generated position used to reach the developer untouched.
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
        result = remap_generated_locations("pages/index.jsx:3:8: ERROR: boom", client_root)
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
            """A build that succeeded, then failed while bundling for a render —
            the path a rebuild-time syntax check never sees."""

            def __init__(self, root: Path) -> None:
                self.client_root = root

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
