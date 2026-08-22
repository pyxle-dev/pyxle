from __future__ import annotations

from pathlib import Path

import pytest

from pyxle.devserver.styles import (
    GlobalStyleConfigError,
    GlobalStylesheet,
    _make_identifier,
    _normalize_relative_path,
    inject_tailwind_sources,
    project_source_dirs,
    load_inline_stylesheets,
    resolve_global_stylesheets,
    sync_global_stylesheets,
)


def test_global_stylesheet_properties(tmp_path: Path) -> None:
    source = tmp_path / "assets" / "theme.css"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("body {}", encoding="utf-8")

    sheet = GlobalStylesheet(
        source_path=source,
        relative_path=Path("assets/theme.css"),
        identifier="shared-theme",
    )

    assert sheet.client_relative_path == Path("styles/shared-theme.css")
    assert sheet.import_specifier == "./styles/shared-theme.css"
    assert sheet.vite_url == "/styles/shared-theme.css"
    assert sheet.as_dict()["client_relative_path"] == "styles/shared-theme.css"


def test_resolve_global_stylesheets_filters_duplicates(tmp_path: Path) -> None:
    assets = tmp_path / "assets"
    assets.mkdir()
    base = assets / "base.css"
    base.write_text("body {}", encoding="utf-8")
    theme = assets / "theme.css"
    theme.write_text("h1 {}", encoding="utf-8")

    result = resolve_global_stylesheets(
        tmp_path,
        [" assets/base.css ", "", None, "assets/base.css", "assets/theme.css"],
    )

    assert {sheet.relative_path.as_posix() for sheet in result} == {
        "assets/base.css",
        "assets/theme.css",
    }
    identifiers = {sheet.identifier for sheet in result}
    assert all(identifier.startswith("pyxle-style-") for identifier in identifiers)


def test_resolve_global_stylesheets_validates_entries(tmp_path: Path) -> None:
    (tmp_path / "folder").mkdir()
    assert resolve_global_stylesheets(tmp_path, None) == ()
    with pytest.raises(GlobalStyleConfigError):
        resolve_global_stylesheets(tmp_path, [object()])
    with pytest.raises(GlobalStyleConfigError):
        resolve_global_stylesheets(tmp_path, ["missing.css"])
    with pytest.raises(GlobalStyleConfigError):
        resolve_global_stylesheets(tmp_path, ["folder"])


def test_normalize_relative_path_rejects_out_of_tree(tmp_path: Path) -> None:
    with pytest.raises(GlobalStyleConfigError):
        _normalize_relative_path(str(tmp_path / "absolute.css"))
    with pytest.raises(GlobalStyleConfigError):
        _normalize_relative_path("../outside.css")
    with pytest.raises(GlobalStyleConfigError):
        _normalize_relative_path("//")
    with pytest.raises(GlobalStyleConfigError):
        _normalize_relative_path("./")

    assert _normalize_relative_path("styles/./main.css") == Path("styles/main.css")
    assert _normalize_relative_path("./styles.css") == Path("styles.css")


def test_make_identifier_is_deterministic() -> None:
    first = _make_identifier("styles/main.css")
    second = _make_identifier("styles/main.css")
    other = _make_identifier("styles/other.css")

    assert first == second
    assert first != other


def test_sync_global_stylesheets_writes_only_changed_files(tmp_path: Path) -> None:
    assets = tmp_path / "assets"
    assets.mkdir()
    sheet_path = assets / "base.css"
    sheet_path.write_text("body {}", encoding="utf-8")
    [sheet] = resolve_global_stylesheets(tmp_path, ["assets/base.css"])

    client_root = tmp_path / "client"
    first = sync_global_stylesheets([sheet], client_root=client_root)
    assert first == ["assets/base.css"]

    # Second sync with identical contents should no-op
    second = sync_global_stylesheets([sheet], client_root=client_root)
    assert second == []

    sheet_path.write_text("body { color: red; }", encoding="utf-8")
    third = sync_global_stylesheets([sheet], client_root=client_root)
    assert third == ["assets/base.css"]


def test_sync_global_stylesheets_handles_unreadable_destination(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    assets = tmp_path / "assets"
    assets.mkdir()
    sheet_path = assets / "base.css"
    sheet_path.write_text("body {}", encoding="utf-8")
    [sheet] = resolve_global_stylesheets(tmp_path, ["assets/base.css"])
    client_root = tmp_path / "client"
    sync_global_stylesheets([sheet], client_root=client_root)

    destination = client_root / sheet.client_relative_path
    original_read_bytes = Path.read_bytes

    def fake_read_bytes(self: Path) -> bytes:  # pragma: no cover - helper for test only
        if self == destination:
            raise OSError("cannot read")
        return original_read_bytes(self)

    monkeypatch.setattr(Path, "read_bytes", fake_read_bytes)

    sheet_path.write_text("body { color: blue; }", encoding="utf-8")
    updated = sync_global_stylesheets([sheet], client_root=client_root)

    assert updated == ["assets/base.css"]


def test_load_inline_stylesheets_skips_missing_files(tmp_path: Path) -> None:
    assets = tmp_path / "assets"
    assets.mkdir()
    existing_path = assets / "inline.css"
    existing_path.write_text("body {}", encoding="utf-8")
    [existing] = resolve_global_stylesheets(tmp_path, ["assets/inline.css"])
    missing = GlobalStylesheet(
        source_path=tmp_path / "missing.css",
        relative_path=Path("missing.css"),
        identifier="missing",
    )

    payloads = load_inline_stylesheets([existing, missing])

    assert payloads == [(existing, "body {}")]


# ---------------------------------------------------------------------------
# Tailwind source roots
# ---------------------------------------------------------------------------


def _tailwind_project(tmp_path: Path) -> tuple[Path, Path]:
    """A project whose stylesheet is copied into the generated client dir."""
    (tmp_path / "components" / "ui").mkdir(parents=True)
    (tmp_path / "lib").mkdir()
    (tmp_path / "pages" / "styles").mkdir(parents=True)
    destination = tmp_path / ".pyxle-build" / "client" / "pages" / "styles" / "app.css"
    destination.parent.mkdir(parents=True)
    return tmp_path, destination


def test_tailwind_sources_point_back_at_the_project(tmp_path: Path) -> None:
    """Without this, every shadcn/ui component renders unstyled.

    Vite's root is the generated client directory, which holds the compiled
    pages and nothing else, and Tailwind v4 auto-detects its sources from that
    root. A class used only in the project's own ``components/`` — where shadcn
    puts everything it installs — is therefore never generated, and the
    component renders with no styling and no error anywhere.

    The paths must be relative to where the copy *landed*, not to the file the
    developer wrote: a hand-written ``@source "../../components"`` resolves
    inside ``.pyxle-build/`` and silently matches nothing.
    """
    project_root, destination = _tailwind_project(tmp_path)

    result = inject_tailwind_sources(
        '@import "tailwindcss";\nbody { margin: 0; }\n',
        destination=destination,
        project_root=project_root,
    )

    assert '@source "../../../../components";' in result
    assert '@source "../../../../lib";' in result
    # the original content survives, and the import still comes first
    assert result.index("@import") < result.index("@source")
    assert "body { margin: 0; }" in result


def test_a_stylesheet_without_tailwind_is_untouched(tmp_path: Path) -> None:
    """Plain CSS projects get no injected directives."""
    project_root, destination = _tailwind_project(tmp_path)
    css = "body { margin: 0; }\n"

    assert (
        inject_tailwind_sources(css, destination=destination, project_root=project_root)
        == css
    )


def test_injecting_twice_does_not_stack_directives(tmp_path: Path) -> None:
    """Rebuilds re-copy the stylesheet; the block must not accumulate."""
    project_root, destination = _tailwind_project(tmp_path)
    once = inject_tailwind_sources(
        '@import "tailwindcss";\n', destination=destination, project_root=project_root
    )
    twice = inject_tailwind_sources(
        once, destination=destination, project_root=project_root
    )

    assert once == twice
    assert twice.count("@source") == 2


def test_source_dirs_follow_jsconfig_and_skip_pages(tmp_path: Path) -> None:
    """``jsconfig.json`` is the project's own declaration of where source lives.

    ``pages`` is excluded deliberately — it is compiled into the Vite root, so
    Tailwind already sees it — and a listed directory that does not exist is
    skipped rather than emitted as a dead path.
    """
    (tmp_path / "components").mkdir()
    (tmp_path / "widgets").mkdir()
    (tmp_path / "pages").mkdir()
    (tmp_path / "jsconfig.json").write_text(
        '{"include": ["pages", "components", "widgets", "absent"]}', encoding="utf-8"
    )

    names = [directory.name for directory in project_source_dirs(tmp_path)]

    assert names == ["components", "widgets"]
    assert "pages" not in names
    assert "absent" not in names


def test_source_dirs_fall_back_when_jsconfig_is_missing(tmp_path: Path) -> None:
    (tmp_path / "components").mkdir()

    assert [d.name for d in project_source_dirs(tmp_path)] == ["components"]
