from __future__ import annotations

from pathlib import Path

import pytest

from pyxle.devserver.scanner import (
    ReservedApiDirectoryError,
    SourceKind,
    is_source_file,
    scan_source_tree,
)
from pyxle.devserver.settings import DevServerSettings


@pytest.fixture
def project(tmp_path: Path) -> DevServerSettings:
    root = tmp_path / "project"
    (root / "pages").mkdir(parents=True)
    (root / "public").mkdir()
    return DevServerSettings.from_project_root(root)


def write_page(project: DevServerSettings, relative_path: str, content: str) -> Path:
    path = project.pages_dir / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def test_scan_source_tree_returns_sorted_entries(project: DevServerSettings) -> None:
    write_page(project, "about.pyxl", "<div>About</div>\n")
    write_page(project, "api/pulse.py", "async def endpoint(request):\n    return None\n")
    write_page(project, "team/index.pyxl", "<div>Team</div>\n")
    write_page(project, "components/layout.jsx", "export const Layout = () => null;\n")

    entries = scan_source_tree(project)

    assert [entry.relative_path.as_posix() for entry in entries] == [
        "about.pyxl",
        "api/pulse.py",
        "components/layout.jsx",
        "team/index.pyxl",
    ]

    kinds = [entry.kind for entry in entries]
    assert kinds == [SourceKind.PAGE, SourceKind.API, SourceKind.CLIENT_ASSET, SourceKind.PAGE]


def test_scan_source_tree_includes_hashes(project: DevServerSettings) -> None:
    page_path = write_page(project, "about.pyxl", "<div>About</div>\n")

    entries = scan_source_tree(project)

    assert len(entries) == 1
    entry = entries[0]
    assert entry.kind is SourceKind.PAGE
    assert entry.absolute_path == page_path
    assert entry.relative_path.as_posix() == "about.pyxl"
    assert len(entry.content_hash) == 64


def test_scan_source_tree_ignores_non_py_or_pyxl(project: DevServerSettings) -> None:
    write_page(project, "about.pyxl", "<div>About</div>\n")
    write_page(project, "api/pulse.py", "async def endpoint(request): return None\n")
    (project.pages_dir / "notes.txt").write_text("ignore me", encoding="utf-8")

    entries = scan_source_tree(project)

    assert len(entries) == 2
    assert all(entry.relative_path.suffix in {".py", ".pyxl"} for entry in entries)


def test_scan_source_tree_ignores_python_outside_api(project: DevServerSettings) -> None:
    write_page(project, "api/pulse.py", "async def endpoint(request): return None\n")
    write_page(project, "components/helpers/__init__.py", "value = 1\n")

    entries = scan_source_tree(project)

    paths = [entry.relative_path.as_posix() for entry in entries]
    assert paths == ["api/pulse.py"]


class TestApiDirectoriesNestAnywhere:
    """An ``api`` directory is server ground wherever it sits.

    Endpoints that have to live beneath a section — ``/s/{slug}/api/v2/…`` for
    a per-tenant status page, ``/(admin)/api/…`` behind a route group — used to
    be unreachable, because only a top-level ``pages/api/`` counted. The rule
    now reads off the URL: a ``.py`` file serves the path it maps to whenever
    that path has an ``api`` segment.
    """

    def test_a_nested_api_directory_yields_an_endpoint(
        self, project: DevServerSettings
    ) -> None:
        write_page(
            project,
            "s/[slug]/api/v2/summary.json.py",
            "async def endpoint(request): return None\n",
        )

        entries = scan_source_tree(project)

        assert [(e.kind, e.relative_path.as_posix()) for e in entries] == [
            (SourceKind.API, "s/[slug]/api/v2/summary.json.py")
        ]

    def test_python_beside_a_nested_page_is_still_ignored(
        self, project: DevServerSettings
    ) -> None:
        """The reason for the rule in the first place: a helper colocated with
        a page must not become a public endpoint."""
        write_page(project, "s/[slug]/helpers.py", "value = 1\n")
        write_page(project, "s/[slug]/index.pyxl", "<div>Status</div>\n")

        entries = scan_source_tree(project)

        assert [e.relative_path.as_posix() for e in entries] == ["s/[slug]/index.pyxl"]

    def test_client_assets_in_a_nested_api_directory_are_not_shipped(
        self, project: DevServerSettings
    ) -> None:
        """Server ground for Python and client ground for JSX would be an
        incoherent directory to reason about."""
        write_page(project, "s/api/widget.jsx", "export default () => null;\n")

        assert scan_source_tree(project) == []

    def test_a_file_named_api_is_not_an_endpoint(self, project: DevServerSettings) -> None:
        """Only directories count — otherwise ``pages/api.py`` silently
        becomes a route, which it never was."""
        write_page(project, "api.py", "value = 1\n")

        assert scan_source_tree(project) == []


class TestApiDirectoriesHoldNoPages:
    """The other half of "an ``api`` directory is server ground".

    The directory was half-reserved: a ``.pyxl`` in it published as a page,
    while the ``Chart.jsx`` beside it was dropped from the client build — so
    the page's own import failed, in Vite, as an unresolvable path inside
    ``.pyxle-build/`` that never mentioned ``api``. One rule now decides the
    whole directory, and the refusal names the file.
    """

    def test_a_pyxl_page_in_an_api_directory_is_refused(
        self, project: DevServerSettings
    ) -> None:
        write_page(project, "docs/api/overview.pyxl", "<div>Overview</div>\n")

        with pytest.raises(ReservedApiDirectoryError) as excinfo:
            scan_source_tree(project)

        message = str(excinfo.value)
        assert "pages/docs/api/overview.pyxl" in message
        assert "this page sits inside one" in message
        assert "Rename the directory" in message

    def test_a_page_in_a_nested_api_directory_is_refused(
        self, project: DevServerSettings
    ) -> None:
        """The rule reads the same at any depth as the endpoint rule does."""
        write_page(project, "s/[slug]/api/v2/summary.pyxl", "<div>Summary</div>\n")

        with pytest.raises(ReservedApiDirectoryError) as excinfo:
            scan_source_tree(project)

        assert "pages/s/[slug]/api/v2/summary.pyxl" in str(excinfo.value)

    def test_every_offending_page_is_named_in_path_order(
        self, project: DevServerSettings
    ) -> None:
        """One rename per report: an author fixing this wants the whole list,
        in an order that does not depend on the filesystem's walk order."""
        write_page(project, "docs/api/overview.pyxl", "<div>Overview</div>\n")
        write_page(project, "api/index.pyxl", "<div>Index</div>\n")
        write_page(project, "api/nested/deep.pyxl", "<div>Deep</div>\n")

        with pytest.raises(ReservedApiDirectoryError) as excinfo:
            scan_source_tree(project)

        message = str(excinfo.value)
        assert "these pages sit inside one" in message
        listed = [line.strip() for line in message.splitlines() if line.startswith("  pages/")]
        assert listed == [
            "pages/api/index.pyxl",
            "pages/api/nested/deep.pyxl",
            "pages/docs/api/overview.pyxl",
        ]

    def test_a_file_named_api_is_still_an_ordinary_page(
        self, project: DevServerSettings
    ) -> None:
        """Only directories count, here as everywhere else — ``pages/api.pyxl``
        serves ``/api`` and always has."""
        write_page(project, "api.pyxl", "<div>Api</div>\n")

        entries = scan_source_tree(project)

        assert [(e.kind, e.relative_path.as_posix()) for e in entries] == [
            (SourceKind.PAGE, "api.pyxl")
        ]

    def test_a_directory_merely_starting_with_api_is_untouched(
        self, project: DevServerSettings
    ) -> None:
        """A segment match, not a substring one."""
        write_page(project, "apiary/keepers.pyxl", "<div>Keepers</div>\n")

        entries = scan_source_tree(project)

        assert [(e.kind, e.relative_path.as_posix()) for e in entries] == [
            (SourceKind.PAGE, "apiary/keepers.pyxl")
        ]


class TestPrivateModulesInsideApiDirectories:
    """A leading underscore marks a module private, exactly as in Python.

    An ``api`` directory is server ground, but not every ``.py`` file in one is
    a URL. Colocating a helper beside the endpoints that import it is the
    obvious thing to do; without this rule that helper is registered as a
    route, exports no handler, and takes the whole app down at startup with an
    ``ApiRouteError``. Python already has a convention for "not part of the
    public surface" — a leading underscore — and Pyxle follows it.
    """

    def test_an_underscored_module_is_not_an_endpoint(
        self, project: DevServerSettings
    ) -> None:
        write_page(project, "api/health.py", "async def endpoint(request): return None\n")
        write_page(project, "api/_shared.py", "TIMEOUT = 5\n")

        entries = scan_source_tree(project)

        assert [e.relative_path.as_posix() for e in entries] == ["api/health.py"]

    def test_a_package_init_is_not_an_endpoint(self, project: DevServerSettings) -> None:
        """``__init__.py`` is the file Python itself drops into a directory —
        it must never be mistaken for an endpoint."""
        write_page(project, "api/__init__.py", "")
        write_page(project, "api/v1/__init__.py", "")
        write_page(project, "api/v1/summary.py", "async def endpoint(request): return None\n")

        entries = scan_source_tree(project)

        assert [e.relative_path.as_posix() for e in entries] == ["api/v1/summary.py"]

    def test_a_private_directory_is_skipped_wholesale(
        self, project: DevServerSettings
    ) -> None:
        """A private *directory* takes everything under it with it, so a
        package of helpers needs one underscore, not one per file."""
        write_page(project, "api/health.py", "async def endpoint(request): return None\n")
        write_page(project, "api/_internal/db.py", "value = 1\n")
        write_page(project, "api/_internal/deeper/queries.py", "value = 2\n")

        entries = scan_source_tree(project)

        assert [e.relative_path.as_posix() for e in entries] == ["api/health.py"]

    def test_an_underscored_directory_above_api_is_a_url_segment(
        self, project: DevServerSettings
    ) -> None:
        """Only the segments at or below ``api`` are read as Python. Above it
        the path is a URL, where an underscore carries no meaning — the page
        router has no private-name rule, and ``/_admin/api/health`` must keep
        working."""
        write_page(
            project,
            "_admin/api/health.py",
            "async def endpoint(request): return None\n",
        )

        entries = scan_source_tree(project)

        assert [(e.kind, e.relative_path.as_posix()) for e in entries] == [
            (SourceKind.API, "_admin/api/health.py")
        ]

    def test_underscored_pyxl_pages_are_untouched(
        self, project: DevServerSettings
    ) -> None:
        """The private-name rule is about Python modules in server ground. The
        page router does not have one, and this change does not invent it."""
        write_page(project, "_admin/index.pyxl", "<div>Admin</div>\n")

        entries = scan_source_tree(project)

        assert [(e.kind, e.relative_path.as_posix()) for e in entries] == [
            (SourceKind.PAGE, "_admin/index.pyxl")
        ]


def test_scan_source_tree_detects_client_assets(project: DevServerSettings) -> None:
    write_page(project, "components/layout.jsx", "export const Layout = () => null;\n")

    entries = scan_source_tree(project)

    assert len(entries) == 1
    entry = entries[0]
    assert entry.kind is SourceKind.CLIENT_ASSET
    assert entry.relative_path.as_posix() == "components/layout.jsx"


def test_scan_source_tree_ignores_internal_build_cache(project: DevServerSettings) -> None:
    write_page(project, "about.pyxl", "<div>About</div>\n")
    build_dir = project.pages_dir / ".pyxle-build" / "server" / "pages"
    build_dir.mkdir(parents=True, exist_ok=True)
    (build_dir / "about.py").write_text("from pyxle.runtime import server\n", encoding="utf-8")

    entries = scan_source_tree(project)

    assert [entry.relative_path.as_posix() for entry in entries] == ["about.pyxl"]


def test_scan_source_tree_returns_empty_when_pages_missing(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    (root / "public").mkdir()
    settings = DevServerSettings.from_project_root(root)

    assert scan_source_tree(settings) == []


class TestIsSourceFile:
    """Which files Pyxle builds, judged by suffix — used to keep editor
    scratch files out of the rebuild log."""

    @pytest.mark.parametrize(
        "name",
        ["index.pyxl", "api/pulse.py", "components/Badge.jsx", "styles/app.css", "data.json"],
    )
    def test_recognises_project_sources(self, name: str) -> None:
        assert is_source_file(Path(name)) is True

    @pytest.mark.parametrize(
        "name",
        ["sedo1AOsO", ".index.pyxl.swp", "index.pyxl~", "4913", "notes.txt"],
    )
    def test_rejects_editor_scratch_and_unrelated_files(self, name: str) -> None:
        assert is_source_file(Path(name)) is False

    def test_suffix_comparison_ignores_case(self) -> None:
        assert is_source_file(Path("Index.PYXL")) is True
