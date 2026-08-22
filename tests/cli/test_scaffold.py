from pathlib import Path

import pytest

from pyxle.cli.scaffold import (
    FilesystemWriter,
    InvalidImportAlias,
    InvalidProjectName,
    slugify_project_name,
    validate_import_alias,
    validate_project_name,
)


@pytest.mark.parametrize(
    ("value", "expected"),
    [("@/*", "@/*"), ("@", "@/*"), ("@/", "@/*"), ("~/*", "~/*"), ("  @/*  ", "@/*")],
)
def test_validate_import_alias_normalises(value: str, expected: str) -> None:
    assert validate_import_alias(value) == expected


@pytest.mark.parametrize("value", ["", "  ", "@/nested/*", "a b", "@/foo"])
def test_validate_import_alias_rejects_invalid(value: str) -> None:
    with pytest.raises(InvalidImportAlias):
        validate_import_alias(value)


def test_filesystem_writer_keep_root_requires_empty(tmp_path: Path) -> None:
    root = tmp_path / "inplace"
    root.mkdir()
    (root / "occupied.txt").write_text("x", encoding="utf-8")
    writer = FilesystemWriter(root)

    with pytest.raises(FileExistsError):
        writer.ensure_root(keep_root=True)

    # With force it keeps the directory (and its contents) but proceeds.
    writer.ensure_root(force=True, keep_root=True)
    assert root.is_dir()
    assert (root / "occupied.txt").exists()


def test_slugify_and_validate_happy_path():
    assert slugify_project_name("My Awesome_App") == "my-awesome-app"
    assert validate_project_name("My Awesome_App") == "my-awesome-app"


@pytest.mark.parametrize("value", ["", " ", "!!!", "..", "-demo", ".hidden"])
def test_slugify_rejects_invalid_names(value: str) -> None:
    with pytest.raises(InvalidProjectName):
        validate_project_name(value)


def test_filesystem_writer_handles_text_and_binary(tmp_path: Path) -> None:
    root = tmp_path / "demo"
    writer = FilesystemWriter(root)
    writer.ensure_root()

    with pytest.raises(FileExistsError):
        writer.ensure_root()

    writer.write("hello.txt", "hello world")
    writer.write("nested/data.bin", b"\x00\x01", binary=True)
    with pytest.raises(FileExistsError):
        writer.write("hello.txt", "again")
    writer.touch_directory("pages/api")

    assert (root / "hello.txt").read_text(encoding="utf-8") == "hello world"
    assert (root / "nested/data.bin").read_bytes() == b"\x00\x01"
    assert (root / "pages/api").is_dir()

    # Simulate force overwrite when a file exists at the target path.
    root.touch()
    writer = FilesystemWriter(root)
    writer.ensure_root(force=True)
    assert root.is_dir()

    # Force overwrite when a directory already exists.
    (root / "placeholder.txt").write_text("data", encoding="utf-8")
    writer.ensure_root(force=True)
    assert not (root / "placeholder.txt").exists()


def test_validate_project_name_rejects_a_path() -> None:
    """`pyxle init` takes a name, not a path.

    Slugifying a path turns every separator into a hyphen, so
    `pyxle init apps/my-app` silently created a directory literally called
    `apps-my-app` in the current directory — nowhere near where the user
    pointed, and with no error.
    """
    for candidate in (
        "apps/my-app",
        "./my-app",
        "../escape",
        "/tmp/somewhere/my-app",
        "apps\\my-app",
    ):
        with pytest.raises(InvalidProjectName) as excinfo:
            validate_project_name(candidate)
        assert "looks like a path" in str(excinfo.value), candidate

    # A plain name is untouched, and `.` still means "scaffold in place".
    assert validate_project_name("my-app") == "my-app"


# ---------------------------------------------------------------------------
# The root vite.config.js — it exists to be found by other people's tools, so
# it has to survive being imported before anything has been built.
# ---------------------------------------------------------------------------


def _node() -> str | None:
    import shutil

    return shutil.which("node")


@pytest.mark.skipif(_node() is None, reason="needs Node.js to import an ES module")
def test_scaffolded_root_vite_config_imports_before_any_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fresh project's root Vite config must not throw.

    The scaffold writes it so ``shadcn/ui`` framework detection, editor
    integrations and other tools that expect a config at the project root can
    find one. It re-exports ``.pyxle-build/client/vite.config.js`` — which does
    not exist until the first ``pyxle dev`` / ``pyxle build``. A static
    re-export therefore threw ``ERR_MODULE_NOT_FOUND`` on every freshly
    scaffolded project, so the tools it exists to satisfy found a config that
    crashes.
    """
    import json
    import subprocess

    from typer.testing import CliRunner

    from pyxle.cli import app

    runner = CliRunner()
    # `pyxle init` takes a *name* and creates it in the cwd — passing a path is
    # refused with a message saying exactly that.
    monkeypatch.chdir(tmp_path)
    project = tmp_path / "demo"
    result = runner.invoke(app, ["init", "demo", "--yes"], catch_exceptions=False)
    assert result.exit_code == 0, result.stdout

    config = project / "vite.config.js"
    assert config.exists()
    assert not (project / ".pyxle-build").exists(), "nothing should be built yet"

    proc = subprocess.run(
        [
            _node(),
            "-e",
            "import('./vite.config.js')"
            ".then(async (m) => { const c = await m.default();"
            " process.stdout.write(JSON.stringify(c)); })"
            ".catch((e) => { process.stderr.write(String(e.code || e.message));"
            " process.exit(1); })",
        ],
        cwd=project,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, (
        f"a freshly scaffolded root vite.config.js failed to import: {proc.stderr}"
    )
    # Nothing is built, so it resolves to an empty config rather than throwing.
    assert json.loads(proc.stdout or "{}") == {}
