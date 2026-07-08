"""Tests for the interactive/flag-driven `pyxle init` options and `init .`."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

import pyxle.cli as cli
from pyxle.cli import app
from pyxle.cli.init import DEFAULT_IMPORT_ALIAS, run_init
from pyxle.cli.logger import ConsoleLogger

runner = CliRunner()


def _files(root: Path) -> set[str]:
    return {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()}


# --------------------------------------------------------------------------- #
# run_init conditional file sets                                              #
# --------------------------------------------------------------------------- #


def test_run_init_default_scaffolds_plain_css_baseline(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    run_init("demo", False, "default", ConsoleLogger(), log_steps=False)
    files = _files(tmp_path / "demo")

    assert "pages/styles/app.css" in files
    assert "pages/components/Badge.jsx" in files
    assert "pages/components/Badge.module.css" in files
    assert "jsconfig.json" in files
    assert "vite.config.js" in files
    # No Tailwind / shadcn artifacts by default.
    assert "components.json" not in files
    assert "lib/utils.js" not in files

    css = (tmp_path / "demo" / "pages" / "styles" / "app.css").read_text(encoding="utf-8")
    assert "@import \"tailwindcss\"" not in css


def test_run_init_tailwind_scaffolds_v4_entry(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    run_init("demo", False, "default", ConsoleLogger(), tailwind=True, log_steps=False)
    files = _files(tmp_path / "demo")

    assert "pages/styles/app.css" in files
    # The CSS-Modules demo belongs to the plain baseline only.
    assert "pages/components/Badge.jsx" not in files
    assert "components.json" not in files

    css = (tmp_path / "demo" / "pages" / "styles" / "app.css").read_text(encoding="utf-8")
    assert '@import "tailwindcss";' in css


def test_run_init_shadcn_implies_tailwind_and_adds_files(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    # shadcn selected but tailwind left False — shadcn must imply Tailwind.
    run_init(
        "demo", False, "default", ConsoleLogger(), tailwind=False, shadcn=True, log_steps=False
    )
    files = _files(tmp_path / "demo")

    assert "components.json" in files
    assert "lib/utils.js" in files
    assert "pages/styles/app.css" in files

    manifest = (tmp_path / "demo" / "package.json").read_text(encoding="utf-8")
    assert "@tailwindcss/vite" in manifest  # tailwind implied
    assert "tailwind-merge" in manifest  # shadcn runtime dep

    css = (tmp_path / "demo" / "pages" / "styles" / "app.css").read_text(encoding="utf-8")
    assert '@import "tailwindcss";' in css
    assert "--primary" in css  # shadcn theme tokens


def test_run_init_custom_import_alias_threads_through(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    run_init(
        "demo",
        False,
        "default",
        ConsoleLogger(),
        shadcn=True,
        import_alias="~/*",
        log_steps=False,
    )
    jsconfig = (tmp_path / "demo" / "jsconfig.json").read_text(encoding="utf-8")
    assert '"~/*": ["./*"]' in jsconfig
    components = (tmp_path / "demo" / "components.json").read_text(encoding="utf-8")
    assert '"utils": "~/lib/utils"' in components


def test_run_init_rejects_invalid_import_alias(tmp_path, monkeypatch) -> None:
    import typer

    monkeypatch.chdir(tmp_path)
    with pytest.raises(typer.BadParameter):
        run_init(
            "demo", False, "default", ConsoleLogger(), import_alias="@/bad/*", log_steps=False
        )


# --------------------------------------------------------------------------- #
# `pyxle init .` — scaffold into the current directory                        #
# --------------------------------------------------------------------------- #


def test_run_init_dot_uses_cwd_and_derives_name(tmp_path, monkeypatch) -> None:
    target = tmp_path / "My Cool App"
    target.mkdir()
    monkeypatch.chdir(target)

    result = run_init(".", False, "default", ConsoleLogger(), log_steps=False)
    assert result == Path(".")

    # Scaffolded in place (no nested dir), name derived + slugified.
    assert (target / "pages" / "index.pyxl").exists()
    manifest = (target / "package.json").read_text(encoding="utf-8")
    assert '"name": "my-cool-app"' in manifest


def test_run_init_dot_requires_empty_dir(tmp_path, monkeypatch) -> None:
    import typer

    target = tmp_path / "occupied"
    target.mkdir()
    (target / "existing.txt").write_text("hi", encoding="utf-8")
    monkeypatch.chdir(target)

    with pytest.raises(typer.Exit):
        run_init(".", False, "default", ConsoleLogger(), log_steps=False)

    # With --force it scaffolds into the non-empty directory.
    run_init(".", True, "default", ConsoleLogger(), log_steps=False)
    assert (target / "pages" / "index.pyxl").exists()
    assert (target / "existing.txt").exists()  # in-place force never nukes the dir


# --------------------------------------------------------------------------- #
# CLI command: interactive prompts vs. flags vs. non-TTY defaults             #
# --------------------------------------------------------------------------- #


def test_cli_interactive_prompts_drive_scaffold(monkeypatch) -> None:
    monkeypatch.setattr(cli, "_stdin_is_interactive", lambda: True)
    answers = iter([True, True])  # Tailwind? yes; shadcn? yes
    monkeypatch.setattr(cli.typer, "confirm", lambda *a, **k: next(answers))
    monkeypatch.setattr(cli.typer, "prompt", lambda *a, **k: DEFAULT_IMPORT_ALIAS)

    with runner.isolated_filesystem():
        result = runner.invoke(app, ["init", "demo"], catch_exceptions=False)
        assert result.exit_code == 0, result.stdout
        project = Path("demo")
        assert (project / "components.json").exists()
        assert (project / "lib" / "utils.js").exists()


def test_cli_interactive_declines_tailwind_skips_shadcn_prompt(monkeypatch) -> None:
    monkeypatch.setattr(cli, "_stdin_is_interactive", lambda: True)
    confirm_calls: list[str] = []

    def fake_confirm(message, *a, **k):
        confirm_calls.append(message)
        return False  # Tailwind? no

    monkeypatch.setattr(cli.typer, "confirm", fake_confirm)
    monkeypatch.setattr(cli.typer, "prompt", lambda *a, **k: DEFAULT_IMPORT_ALIAS)

    with runner.isolated_filesystem():
        result = runner.invoke(app, ["init", "demo"], catch_exceptions=False)
        assert result.exit_code == 0, result.stdout
        # shadcn prompt must NOT be shown when Tailwind is declined.
        assert len(confirm_calls) == 1
        project = Path("demo")
        assert not (project / "components.json").exists()
        assert (project / "pages" / "components" / "Badge.jsx").exists()


def test_cli_non_tty_never_prompts_and_uses_flags(monkeypatch) -> None:
    monkeypatch.setattr(cli, "_stdin_is_interactive", lambda: False)

    def boom(*a, **k):  # pragma: no cover - must never be called
        raise AssertionError("prompted in non-interactive mode")

    monkeypatch.setattr(cli.typer, "confirm", boom)
    monkeypatch.setattr(cli.typer, "prompt", boom)

    with runner.isolated_filesystem():
        result = runner.invoke(
            app, ["init", "demo", "--tailwind", "--no-shadcn"], catch_exceptions=False
        )
        assert result.exit_code == 0, result.stdout
        project = Path("demo")
        assert (project / "pages" / "styles" / "app.css").exists()
        assert "@import" in (project / "pages" / "styles" / "app.css").read_text(encoding="utf-8")
        assert not (project / "components.json").exists()


def test_cli_yes_flag_accepts_defaults_without_prompting(monkeypatch) -> None:
    monkeypatch.setattr(cli, "_stdin_is_interactive", lambda: True)

    def boom(*a, **k):  # pragma: no cover - --yes must bypass prompts
        raise AssertionError("prompted despite --yes")

    monkeypatch.setattr(cli.typer, "confirm", boom)
    monkeypatch.setattr(cli.typer, "prompt", boom)

    with runner.isolated_filesystem():
        result = runner.invoke(app, ["init", "demo", "--yes"], catch_exceptions=False)
        assert result.exit_code == 0, result.stdout
        project = Path("demo")
        # Defaults => plain CSS baseline, no Tailwind.
        assert (project / "pages" / "components" / "Badge.jsx").exists()
        assert not (project / "components.json").exists()


def test_cli_init_dot_into_current_directory(monkeypatch) -> None:
    monkeypatch.setattr(cli, "_stdin_is_interactive", lambda: False)
    with runner.isolated_filesystem() as fs:
        # isolated_filesystem cwd basename is random; write into a named subdir.
        result = runner.invoke(app, ["init", "."], catch_exceptions=False)
        assert result.exit_code == 0, result.stdout
        assert (Path(fs) / "pages" / "index.pyxl").exists()
