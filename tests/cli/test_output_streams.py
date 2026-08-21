"""The stdout/stderr contract for commands whose stdout carries data.

``pyxle openapi`` and ``pyxle routes --json`` are designed to be redirected or
piped, so their stdout is a *data channel*: it must contain the document and
nothing else, and it must stay empty when the command fails. A message written
to stdout there lands inside the artifact — the redirect captures it, the file
is corrupt, and the terminal stays silent, so a failure reads as a successful
run that produced a strange file.

These tests use a runner with separated streams; the default one folds stderr
into ``result.stdout``, which is the exact distinction under test.
"""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from pyxle.cli import app
from pyxle.cli.logger import ConsoleLogger, LogFormat, Verbosity

runner = CliRunner(mix_stderr=False)

_PLAIN_PAGE = """import React from 'react';
export default function Page() { return <div>Hello</div>; }
"""


def _scaffold(project: Path, page_source: str = _PLAIN_PAGE) -> None:
    (project / "pages").mkdir(parents=True)
    (project / "public").mkdir()
    (project / "pages" / "index.pyxl").write_text(page_source, encoding="utf-8")


def test_routes_json_is_the_only_thing_on_stdout() -> None:
    with runner.isolated_filesystem():
        _scaffold(Path("demo"))

        result = runner.invoke(app, ["routes", "demo", "--json"], catch_exceptions=False)

        assert result.exit_code == 0
        # Parses on its own — no "Routes for demo/" banner mixed in.
        assert isinstance(json.loads(result.stdout), list)


def test_routes_json_failure_leaves_stdout_empty() -> None:
    result = runner.invoke(
        app, ["routes", "no_such_project_dir_xyz", "--json"], catch_exceptions=False
    )

    assert result.exit_code == 1
    assert result.stdout == ""
    assert "does not exist" in result.stderr


def test_routes_human_table_still_uses_stdout() -> None:
    """Without ``--json`` the output is for a human to read, not to redirect —
    it stays on stdout so a plain ``pyxle routes`` still pipes to a pager."""
    with runner.isolated_filesystem():
        _scaffold(Path("demo"))

        result = runner.invoke(app, ["routes", "demo"], catch_exceptions=False)

        assert result.exit_code == 0
        assert "route(s) found" in result.stdout


def test_json_log_format_does_not_contaminate_the_document() -> None:
    """``--log-format json`` turns log lines into JSON objects — the shape most
    likely to be mistaken for payload. They must still leave by stderr, or a
    redirect captures log objects where the document should be and the file
    parses as neither. Checked on the failure path, where there is definitely
    something to log."""
    with runner.isolated_filesystem():
        _scaffold(Path("demo"))

        result = runner.invoke(
            app,
            ["--log-format", "json", "openapi", "no_such_dir_xyz"],
            catch_exceptions=False,
        )

        assert result.exit_code == 1
        assert result.stdout == ""
        assert json.loads(result.stderr.strip())["level"] == "error"


def test_json_log_format_success_line_stays_off_stdout() -> None:
    """The success path has a log line too, when ``--out`` is used."""
    with runner.isolated_filesystem():
        _scaffold(Path("demo"))

        result = runner.invoke(
            app,
            ["--log-format", "json", "openapi", "demo", "--out", "schema.json"],
            catch_exceptions=False,
        )

        assert result.exit_code == 0
        assert result.stdout == ""
        assert json.loads(result.stderr.strip())["level"] == "success"


def test_to_stderr_preserves_verbosity_and_formatter() -> None:
    """``--quiet`` and ``--log-format json`` have to survive the switch, or the
    error channel would start disagreeing with the flags the user passed."""
    captured: list[str] = []
    base = ConsoleLogger(
        secho=lambda message, **_: captured.append(str(message)),
        formatter=LogFormat.JSON,
        verbosity=Verbosity.QUIET,
    )

    redirected = base.to_stderr()

    assert redirected.formatter is LogFormat.JSON
    assert redirected.verbosity is Verbosity.QUIET
    # A distinct object, so pointing one command's logger at stderr cannot leak
    # into the shared logger every other command holds.
    assert redirected is not base
    base.error("still mine")
    assert len(captured) == 1
    assert json.loads(captured[0])["level"] == "error"


def test_to_stderr_writes_to_stderr(capsys) -> None:
    """The mechanism itself, independent of any command wiring."""
    ConsoleLogger().to_stderr().error("probe message")

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "probe message" in captured.err
