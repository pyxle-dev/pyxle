from __future__ import annotations

import json
from pathlib import Path

import pytest

from pyxle.devserver.builder import BuildFailed, build_once
from pyxle.devserver.settings import DevServerSettings


@pytest.fixture
def project(tmp_path: Path) -> DevServerSettings:
    root = tmp_path / "project"
    (root / "pages").mkdir(parents=True)
    (root / "public").mkdir()
    settings = DevServerSettings.from_project_root(root)
    create_sample_sources(settings)
    return settings


def create_sample_sources(settings: DevServerSettings) -> None:
    write_file(
        settings.pages_dir / "about.pyxl",
        "import React from 'react';\n\nexport default function About() {\n  return <div>About</div>;\n}\n",
    )
    write_file(
        settings.pages_dir / "api/pulse.py",
        "async def endpoint(request):\n    return {'message': 'hi'}\n",
    )
    write_file(
        settings.pages_dir / "components/layout.jsx",
        "export const Layout = ({ children }) => <div>{children}</div>;\n",
    )


def write_file(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def read_meta(settings: DevServerSettings) -> dict[str, object]:
    meta_path = settings.build_root / "meta.json"
    with meta_path.open("r", encoding="utf-8") as file:
        return json.load(file)


def test_build_once_compiles_pages_and_copies_api(project: DevServerSettings) -> None:
    summary = build_once(project)

    assert summary.compiled_pages == ["about.pyxl"]
    assert summary.copied_api_modules == ["api/pulse.py"]
    assert summary.copied_client_assets == ["components/layout.jsx"]
    assert summary.skipped == []
    assert summary.removed == []

    assert (project.build_root / "client/pages/about.jsx").exists()
    assert (project.build_root / "client/pages/components/layout.jsx").exists()
    assert (project.build_root / "server/pages/about.py").exists()
    assert (project.build_root / "metadata/pages/about.json").exists()
    assert (project.build_root / "server/api/pulse.py").exists()

    metadata = read_meta(project)
    assert set(metadata["sources"].keys()) == {"about.pyxl", "api/pulse.py", "components/layout.jsx"}


def test_build_once_skips_unchanged_sources(project: DevServerSettings) -> None:
    build_once(project)

    summary = build_once(project)

    assert summary.compiled_pages == []
    assert summary.copied_api_modules == []
    assert summary.copied_client_assets == []
    assert summary.removed == []
    assert set(summary.skipped) == {"about.pyxl", "api/pulse.py", "components/layout.jsx"}


def test_build_once_reacts_to_changes_and_deletions(project: DevServerSettings) -> None:
    build_once(project)

    # Modify the page and remove the API module.
    write_file(
        project.pages_dir / "about.pyxl",
        "import React from 'react';\n\nexport default function About() {\n  return <div>Updated</div>;\n}\n",
    )
    (project.pages_dir / "api/pulse.py").unlink()

    summary = build_once(project)

    assert summary.compiled_pages == ["about.pyxl"]
    assert summary.copied_api_modules == []
    assert summary.copied_client_assets == []
    assert summary.removed == ["api/pulse.py"]
    assert summary.skipped == ["components/layout.jsx"]

    # The API artifact should be removed while the page artifacts remain.
    assert not (project.build_root / "server/api/pulse.py").exists()
    assert (project.build_root / "server/pages/about.py").exists()

    metadata = read_meta(project)
    assert set(metadata["sources"].keys()) == {"about.pyxl", "components/layout.jsx"}


def test_build_once_force_rebuild_reprocesses_all_sources(project: DevServerSettings) -> None:
    build_once(project)

    summary = build_once(project, force_rebuild=True)

    assert summary.compiled_pages == ["about.pyxl"]
    assert summary.copied_api_modules == ["api/pulse.py"]
    assert summary.copied_client_assets == ["components/layout.jsx"]
    assert summary.skipped == []


def test_build_once_handles_page_removal(project: DevServerSettings) -> None:
    build_once(project)

    # The page's .jsx is recorded in the client source-map sidecar.
    sidecar_path = project.build_root / "client/pyxl-sourcemaps.json"
    assert "pages/about.jsx" in json.loads(sidecar_path.read_text(encoding="utf-8"))

    # Remove the page source to trigger artifact cleanup.
    (project.pages_dir / "about.pyxl").unlink()

    summary = build_once(project)

    assert summary.removed == ["about.pyxl"]
    assert not (project.build_root / "client/pages/about.jsx").exists()
    assert not (project.build_root / "server/pages/about.py").exists()
    assert not (project.build_root / "metadata/pages/about.json").exists()
    # …and the reconcile pass drops its now-stale sidecar entry.
    assert "pages/about.jsx" not in json.loads(
        sidecar_path.read_text(encoding="utf-8")
    )


def test_build_once_tracks_client_asset_changes(project: DevServerSettings) -> None:
    build_once(project)

    asset_path = project.pages_dir / "components/layout.jsx"
    asset_path.write_text("export const Layout = () => <main />;\n", encoding="utf-8")

    summary = build_once(project)

    assert summary.compiled_pages == []
    assert summary.copied_api_modules == []
    assert summary.copied_client_assets == ["components/layout.jsx"]
    assert summary.removed == []

    compiled_asset = project.build_root / "client/pages/components/layout.jsx"
    assert compiled_asset.exists()

    asset_path.unlink()
    summary = build_once(project)
    assert summary.removed == ["components/layout.jsx"]
    assert not compiled_asset.exists()


def test_build_once_syncs_global_stylesheets(tmp_path: Path) -> None:
    root = tmp_path / "styled"
    (root / "pages").mkdir(parents=True)
    (root / "public").mkdir()
    write_file(
        root / "pages" / "index.pyxl",
        "import React from 'react';\n\nexport default function Home() {\n  return <div>Home</div>;\n}\n",
    )
    style_path = root / "styles" / "global.css"
    style_path.parent.mkdir(parents=True, exist_ok=True)
    style_path.write_text("body { color: #333; }\n", encoding="utf-8")

    settings = DevServerSettings.from_project_root(
        root,
        global_stylesheets=("styles/global.css",),
    )

    summary = build_once(settings)
    assert summary.synced_stylesheets == ["styles/global.css"]
    generated = settings.client_build_dir / settings.global_stylesheets[0].client_relative_path
    assert generated.exists()
    assert "#333" in generated.read_text(encoding="utf-8")

    summary = build_once(settings)
    assert summary.synced_stylesheets == []

    style_path.write_text("body { color: #111; }\n", encoding="utf-8")
    summary = build_once(settings)
    assert summary.synced_stylesheets == ["styles/global.css"]
    assert "#111" in generated.read_text(encoding="utf-8")


def test_build_once_syncs_global_scripts(tmp_path: Path) -> None:
    root = tmp_path / "scripted"
    (root / "pages").mkdir(parents=True)
    (root / "public").mkdir()
    write_file(
        root / "pages" / "index.pyxl",
        "import React from 'react';\n\nexport default function Home() {\n  return <div>Home</div>;\n}\n",
    )
    script_path = root / "scripts" / "analytics.js"
    script_path.parent.mkdir(parents=True, exist_ok=True)
    script_path.write_text("console.log('analytics');\n", encoding="utf-8")

    settings = DevServerSettings.from_project_root(
        root,
        global_scripts=("scripts/analytics.js",),
    )

    summary = build_once(settings)
    assert summary.synced_scripts == ["scripts/analytics.js"]
    generated = settings.client_build_dir / settings.global_scripts[0].client_relative_path
    assert generated.exists()
    assert "analytics" in generated.read_text(encoding="utf-8")

    summary = build_once(settings)
    assert summary.synced_scripts == []

    script_path.write_text("console.log('updated');\n", encoding="utf-8")
    summary = build_once(settings)
    assert summary.synced_scripts == ["scripts/analytics.js"]
    assert "updated" in generated.read_text(encoding="utf-8")


def test_build_once_leaves_unchanged_generated_files_untouched(
    project: DevServerSettings,
) -> None:
    """A rebuild must not rewrite stable generated artifacts.

    Rewriting ``vite.config.js`` with identical content still bumps its mtime,
    and Vite answers a config-file change with a full dev-server restart — so
    an unchanged config (and unchanged ``meta.json``) must keep its mtime.
    """
    import time

    from pyxle.devserver.client_files import VITE_CONFIG_FILENAME

    build_once(project, force_rebuild=True)

    vite_config = project.client_build_dir / VITE_CONFIG_FILENAME
    meta_json = project.build_root / "meta.json"
    assert vite_config.exists()
    assert meta_json.exists()

    before = {
        path: path.stat().st_mtime_ns for path in (vite_config, meta_json)
    }
    time.sleep(0.02)

    build_once(project)

    for path, mtime in before.items():
        assert path.stat().st_mtime_ns == mtime, f"{path.name} was rewritten"


def test_build_once_serializes_concurrent_invocations(
    project: DevServerSettings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Overlapping build_once calls (debounce-timer race) run one at a time.

    A concurrent pass used to read ``meta.json`` while another pass was
    mid-write, misdiagnose the torn read as a schema mismatch, and wipe the
    build cache (recreating ``vite.config.js`` → spurious Vite restart).
    """
    import threading
    import time

    from pyxle.devserver import builder as builder_module

    active = 0
    max_active = 0
    gauge_lock = threading.Lock()
    real_scan = builder_module.scan_source_tree

    def tracking_scan(settings: DevServerSettings):
        nonlocal active, max_active
        with gauge_lock:
            active += 1
            max_active = max(max_active, active)
        time.sleep(0.05)
        with gauge_lock:
            active -= 1
        return real_scan(settings)

    monkeypatch.setattr(builder_module, "scan_source_tree", tracking_scan)

    errors: list[BaseException] = []

    def run_build() -> None:
        try:
            build_once(project)
        except BaseException as exc:  # pragma: no cover - fails the assertion below
            errors.append(exc)

    threads = [threading.Thread(target=run_build) for _ in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert errors == []
    assert max_active == 1, "build passes overlapped"


BROKEN_PAGE = (
    "@server\n"
    "async def load(request):\n"
    "    return {}\n"
    "        oops = 1\n"
    "\n"
    "import React from 'react';\n"
    "export default function Broken() { return <div />; }\n"
)


class TestCompileFailures:
    """A pass that hits an unparseable file must still build everything else.

    Aborting on the first failure is what used to freeze hot reload for the
    whole project: with one page unparseable, an edit to any file scanned after
    it never reached the browser.
    """

    def test_failure_names_the_file_line_and_column(
        self, project: DevServerSettings
    ) -> None:
        write_file(project.pages_dir / "broken.pyxl", BROKEN_PAGE)

        with pytest.raises(BuildFailed) as excinfo:
            build_once(project)

        failure = excinfo.value.failures[0]
        assert failure.display_path == "pages/broken.pyxl"
        assert failure.line == 4
        assert failure.column is not None
        assert "indent" in failure.message
        assert str(excinfo.value) == failure.describe()

    def test_failure_captures_a_code_frame_from_the_failing_source(
        self, project: DevServerSettings
    ) -> None:
        write_file(project.pages_dir / "broken.pyxl", BROKEN_PAGE)

        with pytest.raises(BuildFailed) as excinfo:
            build_once(project)

        assert "oops = 1" in excinfo.value.failures[0].code_frame

    def test_other_pages_still_compile(self, project: DevServerSettings) -> None:
        write_file(project.pages_dir / "broken.pyxl", BROKEN_PAGE)

        with pytest.raises(BuildFailed) as excinfo:
            build_once(project)

        summary = excinfo.value.summary
        assert "about.pyxl" in summary.compiled_pages
        assert "broken.pyxl" not in summary.compiled_pages
        assert (project.build_root / "client/pages/about.jsx").exists()

    def test_an_edit_elsewhere_lands_while_a_page_is_broken(
        self, project: DevServerSettings
    ) -> None:
        """The whole point of continuing: the working file's edit must apply."""
        write_file(project.pages_dir / "broken.pyxl", BROKEN_PAGE)
        with pytest.raises(BuildFailed):
            build_once(project)

        write_file(
            project.pages_dir / "about.pyxl",
            "import React from 'react';\n\n"
            "export default function About() {\n  return <div>EDITED</div>;\n}\n",
        )
        with pytest.raises(BuildFailed):
            build_once(project)

        assert "EDITED" in (project.build_root / "client/pages/about.jsx").read_text()

    def test_failed_source_is_retried_on_the_next_pass(
        self, project: DevServerSettings
    ) -> None:
        """A failed file must never be skipped as 'unchanged' next time.

        Its artifacts on disk are still the previous version's, so a skip would
        leave the stale build serving with nothing left to report it.
        """
        write_file(project.pages_dir / "broken.pyxl", BROKEN_PAGE)
        with pytest.raises(BuildFailed):
            build_once(project)

        assert read_meta(project)["sources"]["broken.pyxl"]["hash"] == "!build-failed"

        with pytest.raises(BuildFailed) as excinfo:
            build_once(project)
        assert [f.display_path for f in excinfo.value.failures] == ["pages/broken.pyxl"]

    def test_every_broken_file_is_reported(self, project: DevServerSettings) -> None:
        write_file(project.pages_dir / "broken.pyxl", BROKEN_PAGE)
        write_file(project.pages_dir / "alsobroken.pyxl", BROKEN_PAGE)

        with pytest.raises(BuildFailed) as excinfo:
            build_once(project)

        assert sorted(f.display_path for f in excinfo.value.failures) == [
            "pages/alsobroken.pyxl",
            "pages/broken.pyxl",
        ]

    def test_a_clean_pass_reports_no_failures(self, project: DevServerSettings) -> None:
        assert build_once(project).failures == []


class TestFailureUrlPaths:
    """A failure records the URL it would serve, so a page that has never
    compiled can still be traced back from the request that hits it."""

    def test_static_page_records_its_url(self, project: DevServerSettings) -> None:
        write_file(project.pages_dir / "brandnew.pyxl", BROKEN_PAGE)

        with pytest.raises(BuildFailed) as excinfo:
            build_once(project)

        assert excinfo.value.failures[0].url_paths == ("/brandnew",)

    def test_index_page_records_the_root_url(self, project: DevServerSettings) -> None:
        write_file(project.pages_dir / "index.pyxl", BROKEN_PAGE)

        with pytest.raises(BuildFailed) as excinfo:
            build_once(project)

        assert excinfo.value.failures[0].url_paths == ("/",)

    def test_a_layout_serves_no_url_of_its_own(self, project: DevServerSettings) -> None:
        write_file(project.pages_dir / "layout.pyxl", BROKEN_PAGE)

        with pytest.raises(BuildFailed) as excinfo:
            build_once(project)

        assert excinfo.value.failures[0].url_paths == ()
        assert excinfo.value.failures[0].url_patterns == ()

    def test_a_dynamic_page_records_a_pattern_not_a_url(
        self, project: DevServerSettings
    ) -> None:
        """Kept apart from the static URLs: matching a pattern is only sound on
        the 404 path, where no live route outranks it."""
        write_file(project.pages_dir / "[slug].pyxl", BROKEN_PAGE)

        with pytest.raises(BuildFailed) as excinfo:
            build_once(project)

        assert excinfo.value.failures[0].url_paths == ()
        assert excinfo.value.failures[0].url_patterns == ("/{slug}",)

    def test_a_nested_dynamic_page_records_its_full_pattern(
        self, project: DevServerSettings
    ) -> None:
        write_file(project.pages_dir / "posts/[slug].pyxl", BROKEN_PAGE)

        with pytest.raises(BuildFailed) as excinfo:
            build_once(project)

        assert excinfo.value.failures[0].url_patterns == ("/posts/{slug}",)

    def test_an_optional_catchall_records_both_of_its_paths(
        self, project: DevServerSettings
    ) -> None:
        """``[[...slug]].pyxl`` serves the bare prefix as well as the subtree,
        so the static alias and the pattern are recorded separately."""
        write_file(project.pages_dir / "docs/[[...slug]].pyxl", BROKEN_PAGE)

        with pytest.raises(BuildFailed) as excinfo:
            build_once(project)

        failure = excinfo.value.failures[0]
        assert failure.url_paths == ("/docs",)
        assert failure.url_patterns == ("/docs/{slug:path}",)


BROKEN_JSX_PAGE = (
    "import React from 'react';\n"
    "\n"
    "export default function Broken() {\n"
    "    return (\n"
    "        <main>\n"
    "            <p>unterminated</p\n"
    "        </main>\n"
    "    );\n"
    "}\n"
)


class TestJsxSyntaxIsADevBuildFailure:
    """A JSX typo must fail the dev rebuild, not log a green tick.

    It used to compile "successfully" — the extractor swallowed the parse
    error and returned empty metadata — and only surfaced at render time, as
    an esbuild message against the generated .jsx.
    """

    def test_dev_reports_it_against_the_pyxl_source(
        self, project: DevServerSettings
    ) -> None:
        write_file(project.pages_dir / "broken.pyxl", BROKEN_JSX_PAGE)

        with pytest.raises(BuildFailed) as excinfo:
            build_once(project)

        failure = excinfo.value.failures[0]
        assert failure.display_path == "pages/broken.pyxl"
        assert failure.line == 7
        assert "JSX syntax error" in failure.message

    def test_the_frame_shows_the_offending_source(
        self, project: DevServerSettings
    ) -> None:
        write_file(project.pages_dir / "broken.pyxl", BROKEN_JSX_PAGE)

        with pytest.raises(BuildFailed) as excinfo:
            build_once(project)

        assert "unterminated" in excinfo.value.failures[0].code_frame

    def test_other_pages_still_compile(self, project: DevServerSettings) -> None:
        write_file(project.pages_dir / "broken.pyxl", BROKEN_JSX_PAGE)

        with pytest.raises(BuildFailed) as excinfo:
            build_once(project)

        assert "about.pyxl" in excinfo.value.summary.compiled_pages

    def test_the_production_build_path_is_unchanged(
        self, project: DevServerSettings
    ) -> None:
        """`pyxle build` and `pyxle serve` run with debug=False. They must keep
        failing where they always did — in the bundler — so a disagreement
        between Babel and esbuild cannot newly break a working release."""
        from dataclasses import replace

        write_file(project.pages_dir / "broken.pyxl", BROKEN_JSX_PAGE)
        production = replace(project, debug=False)

        summary = build_once(production)

        assert "broken.pyxl" in summary.compiled_pages
        assert summary.failures == []

    def test_a_healthy_page_is_unaffected_in_both_modes(
        self, project: DevServerSettings
    ) -> None:
        from dataclasses import replace

        assert build_once(project, force_rebuild=True).failures == []
        assert build_once(replace(project, debug=False), force_rebuild=True).failures == []


def test_deleting_a_page_removes_its_route_entry_module(
    project: DevServerSettings,
) -> None:
    """Deleting a page and rebuilding used to fail the build.

    A page compiles to four artifacts. Three were cleaned up on removal; the
    route entry module was not. It imports ``../pages/<name>.jsx``, which *was*
    deleted, and Vite globs the whole routes directory — so the next build died
    with ``Could not resolve "../pages/about.jsx" from
    ".pyxle-build/client/routes/about.jsx"``: two paths inside a cache the
    author never created, naming neither the page they deleted nor a fix.
    Deleting a page is an ordinary thing to do, so this bricked the build for an
    ordinary action.
    """
    # A route entry module is only composed for a page that has a layout to
    # wrap it, so the fixture needs one for this to be the real shape.
    write_file(
        project.pages_dir / "layout.pyxl",
        "import React from 'react';\n\nexport default function Layout({ children }) {\n  return <div>{children}</div>;\n}\n",
    )
    build_once(project)

    route_entry = project.build_root / "client/routes/about.jsx"
    assert route_entry.exists(), "precondition: a built page has a route entry"

    (project.pages_dir / "about.pyxl").unlink()
    summary = build_once(project)

    assert summary.removed == ["about.pyxl"]
    # All four artifacts, not three.
    assert not route_entry.exists()
    assert not (project.build_root / "client/pages/about.jsx").exists()
    assert not (project.build_root / "server/pages/about.py").exists()
    assert not (project.build_root / "metadata/pages/about.json").exists()


def test_deleting_a_nested_page_removes_its_nested_route_entry(
    project: DevServerSettings,
) -> None:
    """The route tree mirrors ``pages/``, so the cleanup has to mirror it too —
    a flat-only fix would leave every nested page behind."""
    # A route entry module is only composed for a page that has a layout to
    # wrap it, so the fixture needs one for this to be the real shape.
    write_file(
        project.pages_dir / "layout.pyxl",
        "import React from 'react';\n\nexport default function Layout({ children }) {\n  return <div>{children}</div>;\n}\n",
    )
    write_file(
        project.pages_dir / "blog/post.pyxl",
        "import React from 'react';\n\nexport default function Post() {\n  return <div>Post</div>;\n}\n",
    )
    build_once(project)

    nested_entry = project.build_root / "client/routes/blog/post.jsx"
    assert nested_entry.exists(), "precondition: nested pages get nested entries"

    (project.pages_dir / "blog/post.pyxl").unlink()
    build_once(project)

    assert not nested_entry.exists()
