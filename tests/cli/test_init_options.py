"""Tests for the interactive/flag-driven `pyxle init` options and `init .`."""

from __future__ import annotations

import json
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
    # TS 6 deprecates baseUrl (removed in TS 7); paths are tsconfig-relative and
    # must stand alone so fresh projects open warning-free in editors.
    assert "baseUrl" not in jsconfig
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
    # Tailwind? yes; shadcn? yes; customize alias? no
    answers = iter([True, True, False])
    monkeypatch.setattr(cli, "_prompt_yes_no", lambda *a, **k: next(answers))
    monkeypatch.setattr(
        cli, "_prompt_text", lambda *a, **k: (_ for _ in ()).throw(AssertionError("alias input shown without customize=Yes"))
    )

    with runner.isolated_filesystem():
        result = runner.invoke(app, ["init", "demo"], catch_exceptions=False)
        assert result.exit_code == 0, result.stdout
        project = Path("demo")
        assert (project / "components.json").exists()
        assert (project / "lib" / "utils.js").exists()


def test_cli_interactive_declines_tailwind_skips_shadcn_prompt(monkeypatch) -> None:
    monkeypatch.setattr(cli, "_stdin_is_interactive", lambda: True)
    confirm_calls: list[str] = []

    def fake_yes_no(message, *a, **k):
        confirm_calls.append(message)
        return False  # decline everything

    monkeypatch.setattr(cli, "_prompt_yes_no", fake_yes_no)
    monkeypatch.setattr(cli, "_prompt_text", lambda *a, **k: DEFAULT_IMPORT_ALIAS)

    with runner.isolated_filesystem():
        result = runner.invoke(app, ["init", "demo"], catch_exceptions=False)
        assert result.exit_code == 0, result.stdout
        # shadcn prompt must NOT be shown when Tailwind is declined; the
        # customize-alias question still appears (2 questions total).
        assert len(confirm_calls) == 2
        assert not any("shadcn" in message for message in confirm_calls)
        project = Path("demo")
        assert not (project / "components.json").exists()
        assert (project / "pages" / "components" / "Badge.jsx").exists()


def test_cli_non_tty_never_prompts_and_uses_flags(monkeypatch) -> None:
    monkeypatch.setattr(cli, "_stdin_is_interactive", lambda: False)

    def boom(*a, **k):  # pragma: no cover - must never be called
        raise AssertionError("prompted in non-interactive mode")

    monkeypatch.setattr(cli, "_prompt_yes_no", boom)
    monkeypatch.setattr(cli, "_prompt_text", boom)

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

    monkeypatch.setattr(cli, "_prompt_yes_no", boom)
    monkeypatch.setattr(cli, "_prompt_text", boom)

    with runner.isolated_filesystem():
        result = runner.invoke(app, ["init", "demo", "--yes"], catch_exceptions=False)
        assert result.exit_code == 0, result.stdout
        project = Path("demo")
        # Defaults => plain CSS baseline, no Tailwind.
        assert (project / "pages" / "components" / "Badge.jsx").exists()
        assert not (project / "components.json").exists()


def test_cli_interactive_customize_alias_shows_text_input(monkeypatch) -> None:
    monkeypatch.setattr(cli, "_stdin_is_interactive", lambda: True)
    # Tailwind? no; customize alias? yes
    answers = iter([False, True])
    monkeypatch.setattr(cli, "_prompt_yes_no", lambda *a, **k: next(answers))
    monkeypatch.setattr(cli, "_prompt_text", lambda *a, **k: "~/*")

    with runner.isolated_filesystem():
        result = runner.invoke(app, ["init", "demo"], catch_exceptions=False)
        assert result.exit_code == 0, result.stdout
        jsconfig = json.loads(Path("demo/jsconfig.json").read_text(encoding="utf-8"))
        assert "~/*" in jsconfig["compilerOptions"]["paths"]


def test_cli_init_dot_into_current_directory(monkeypatch) -> None:
    monkeypatch.setattr(cli, "_stdin_is_interactive", lambda: False)
    with runner.isolated_filesystem() as fs:
        # isolated_filesystem cwd basename is random; write into a named subdir.
        result = runner.invoke(app, ["init", "."], catch_exceptions=False)
        assert result.exit_code == 0, result.stdout
        assert (Path(fs) / "pages" / "index.pyxl").exists()


def test_cli_init_without_argument_fails(monkeypatch) -> None:
    """A bare `pyxle init` must require an explicit target, not silently
    scaffold into the current directory. The error names both `.` and a name,
    and fails *before* any prompt so the user isn't walked through questions
    only to hit an error."""
    monkeypatch.setattr(cli, "_stdin_is_interactive", lambda: True)

    def _boom(*_a, **_k):  # pragma: no cover - must never be reached
        raise AssertionError("bare `pyxle init` must fail before prompting")

    monkeypatch.setattr(cli, "_prompt_yes_no", _boom)

    with runner.isolated_filesystem() as fs:
        result = runner.invoke(app, ["init"], catch_exceptions=False)
        assert result.exit_code == 1, result.stdout
        assert "Missing target directory" in result.stdout
        assert "pyxle init ." in result.stdout
        # Nothing was scaffolded into the current directory.
        assert not (Path(fs) / "pages").exists()


# --------------------------------------------------------------------------- #
# Prompt seams (questionary-backed)                                            #
# --------------------------------------------------------------------------- #


class _FakeAsk:
    def __init__(self, value):
        self._value = value

    def ask(self):
        return self._value


def _fake_questionary(monkeypatch, *, select=None, text=None):
    import sys
    from types import ModuleType

    fake = ModuleType("questionary")
    fake.select = lambda *a, **k: _FakeAsk(select)
    fake.text = lambda *a, **k: _FakeAsk(text)
    monkeypatch.setitem(sys.modules, "questionary", fake)


def test_prompt_yes_no_maps_selection(monkeypatch) -> None:
    _fake_questionary(monkeypatch, select="Yes")
    assert cli._prompt_yes_no("Q?") is True
    _fake_questionary(monkeypatch, select="No")
    assert cli._prompt_yes_no("Q?") is False


def test_prompt_yes_no_abort_exits_cleanly(monkeypatch) -> None:
    import typer

    _fake_questionary(monkeypatch, select=None)  # Ctrl-C / EOF
    with pytest.raises(typer.Exit):
        cli._prompt_yes_no("Q?")


def test_prompt_text_falls_back_to_default_and_aborts(monkeypatch) -> None:
    import typer

    _fake_questionary(monkeypatch, text="  ")
    assert cli._prompt_text("Q?", default="@/*") == "@/*"
    _fake_questionary(monkeypatch, text="~/*")
    assert cli._prompt_text("Q?", default="@/*") == "~/*"
    _fake_questionary(monkeypatch, text=None)
    with pytest.raises(typer.Exit):
        cli._prompt_text("Q?", default="@/*")


# --------------------------------------------------------------------------- #
# Interactive-flow guardrails (found by driving the real prompts on a pty)     #
# --------------------------------------------------------------------------- #


def test_prompt_text_validates_in_place_instead_of_aborting(monkeypatch) -> None:
    """A bad import alias must be rejected *by the prompt* so the user can fix
    it, rather than aborting the command and discarding every earlier answer."""
    captured: dict[str, object] = {}

    class _CapturingText:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def ask(self):
            return "@/*"

    import sys
    from types import ModuleType

    fake = ModuleType("questionary")
    fake.select = lambda *a, **k: _FakeAsk(None)
    fake.text = lambda *a, **k: _CapturingText(**k)
    monkeypatch.setitem(sys.modules, "questionary", fake)

    cli._prompt_text("Import alias", default="@/*", validate=cli._validate_alias_input)

    validator = captured["validate"]
    assert callable(validator)
    # Valid input passes...
    assert validator("~/*") is True
    # ...blank falls back to the default, which is valid...
    assert validator("   ") is True
    # ...and an unusable alias returns the message the prompt shows in place.
    problem = validator("@/*~")
    assert isinstance(problem, str)
    assert "Invalid import alias" in problem


def test_init_rejects_bad_project_name_before_prompting(tmp_path, monkeypatch) -> None:
    """A typo'd name must fail before any question is asked."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "_stdin_is_interactive", lambda: True)

    def _boom(*_a, **_k):
        raise AssertionError("prompted before validating the target")

    monkeypatch.setattr(cli, "_prompt_yes_no", _boom)
    monkeypatch.setattr(cli, "_prompt_text", _boom)

    result = runner.invoke(app, ["init", ".hidden"], catch_exceptions=False)

    assert result.exit_code == 1
    assert "cannot start with" in result.stdout


def test_init_rejects_existing_directory_before_prompting(tmp_path, monkeypatch) -> None:
    """An occupied destination must fail before any question is asked —
    answering three prompts and then losing them is the worst possible order."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "taken").mkdir()
    monkeypatch.setattr(cli, "_stdin_is_interactive", lambda: True)

    def _boom(*_a, **_k):
        raise AssertionError("prompted before validating the target")

    monkeypatch.setattr(cli, "_prompt_yes_no", _boom)
    monkeypatch.setattr(cli, "_prompt_text", _boom)

    result = runner.invoke(app, ["init", "taken"], catch_exceptions=False)

    assert result.exit_code == 1
    assert "already exists" in result.stdout


def test_init_force_still_prompts_for_an_existing_directory(tmp_path, monkeypatch) -> None:
    """--force means "overwrite it", so the early check must not block it."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "taken").mkdir()
    monkeypatch.setattr(cli, "_stdin_is_interactive", lambda: True)
    monkeypatch.setattr(cli, "_prompt_yes_no", lambda *a, **k: False)

    result = runner.invoke(app, ["init", "taken", "--force"], catch_exceptions=False)

    assert result.exit_code == 0
    assert (tmp_path / "taken" / "pages" / "index.pyxl").is_file()


def test_init_wires_alias_validation_into_the_prompt(tmp_path, monkeypatch) -> None:
    """The alias prompt must be handed a validator — without it, a mistyped
    alias aborts the whole command instead of being corrected in place."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "_stdin_is_interactive", lambda: True)

    answers = iter([False, True])  # no Tailwind, yes customize alias
    monkeypatch.setattr(cli, "_prompt_yes_no", lambda *a, **k: next(answers))

    seen: dict[str, object] = {}

    def fake_text(question, *, default, validate=None):
        seen["validate"] = validate
        return default

    monkeypatch.setattr(cli, "_prompt_text", fake_text)

    result = runner.invoke(app, ["init", "demo"], catch_exceptions=False)

    assert result.exit_code == 0
    validator = seen["validate"]
    assert validator is not None, "alias prompt got no validator"
    with pytest.raises(ValueError):
        validator("@/*~")


def test_scaffold_writes_app_name_into_config(tmp_path, monkeypatch) -> None:
    """`pyxle init` records the project name so untitled pages are titled after
    the app rather than after the framework."""
    monkeypatch.chdir(tmp_path)
    run_init("demo", False, "default", ConsoleLogger(), log_steps=False)

    config = json.loads((tmp_path / "demo" / "pyxle.config.json").read_text(encoding="utf-8"))
    assert config["name"] == "demo"


def test_scaffold_config_survives_a_name_with_quotes(tmp_path, monkeypatch) -> None:
    """The name reaches a JSON file, so it must be JSON-encoded — an unescaped
    quote would leave every command unable to read the config."""
    monkeypatch.chdir(tmp_path)
    run_init('My "Quoted" App', False, "default", ConsoleLogger(), log_steps=False)

    config = json.loads(
        (tmp_path / "my-quoted-app" / "pyxle.config.json").read_text(encoding="utf-8")
    )
    assert config["name"] == 'My "Quoted" App'


def test_scaffold_pages_never_interpolate_the_name_into_jsx(tmp_path, monkeypatch) -> None:
    """A name is arbitrary text; a `.pyxl` template is JSX. Pasting one into the
    other would make `pyxle init 'a<b'` emit a project that does not compile —
    the title default reads the (escaped) config value instead."""
    monkeypatch.chdir(tmp_path)
    run_init("a<b {x} app", False, "default", ConsoleLogger(), log_steps=False)

    page = (tmp_path / "a-b-x-app" / "pages" / "index.pyxl").read_text(encoding="utf-8")
    assert "a<b" not in page
    assert "{x}" not in page


def test_prompt_text_without_a_validator_accepts_anything(monkeypatch) -> None:
    """`_prompt_text` is also used without a validator; that path must not
    reject input (the validator seam returns "valid" for every value)."""
    captured: dict[str, object] = {}

    class _CapturingText:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def ask(self):
            return "whatever"

    import sys
    from types import ModuleType

    fake = ModuleType("questionary")
    fake.select = lambda *a, **k: _FakeAsk(None)
    fake.text = lambda *a, **k: _CapturingText(**k)
    monkeypatch.setitem(sys.modules, "questionary", fake)

    assert cli._prompt_text("Q?", default="@/*") == "whatever"
    assert captured["validate"]("literally anything") is True


def test_init_in_place_rejects_an_underivable_directory_name(tmp_path, monkeypatch) -> None:
    """`pyxle init .` derives the project name from the current directory. When
    that name cannot be made into a valid slug, say so before prompting rather
    than after."""
    weird = tmp_path / "..."
    weird.mkdir()
    monkeypatch.chdir(weird)
    monkeypatch.setattr(cli, "_stdin_is_interactive", lambda: True)

    def _boom(*_a, **_k):
        raise AssertionError("prompted before validating the target")

    monkeypatch.setattr(cli, "_prompt_yes_no", _boom)
    monkeypatch.setattr(cli, "_prompt_text", _boom)

    result = runner.invoke(app, ["init", "."], catch_exceptions=False)

    assert result.exit_code == 1
    assert "Cannot derive a valid project name" in result.stdout


def test_init_in_place_accepts_a_normal_directory_name(tmp_path, monkeypatch) -> None:
    """The in-place guard must not block the ordinary `pyxle init .`."""
    workdir = tmp_path / "my-app"
    workdir.mkdir()
    monkeypatch.chdir(workdir)
    monkeypatch.setattr(cli, "_stdin_is_interactive", lambda: True)
    monkeypatch.setattr(cli, "_prompt_yes_no", lambda *a, **k: False)

    result = runner.invoke(app, ["init", "."], catch_exceptions=False)

    assert result.exit_code == 0
    assert (workdir / "pages" / "index.pyxl").is_file()
