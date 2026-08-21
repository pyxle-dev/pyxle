"""Console logger helper ensuring consistent CLI output formatting."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import Enum
from typing import Callable

import typer

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")


def _strip_ansi(text: str) -> str:
    """Remove ANSI escape sequences from *text*."""
    return _ANSI_RE.sub("", text)


_LogFunction = Callable[..., None]


def _secho_stderr(message: str, **kwargs: object) -> None:
    """:func:`typer.secho`, writing to stderr instead of stdout."""
    typer.secho(message, err=True, **kwargs)  # type: ignore[arg-type]


class LogFormat(str, Enum):
    """Output format for CLI logs."""

    CONSOLE = "console"
    JSON = "json"


class Verbosity(str, Enum):
    """Verbosity level for CLI output."""

    QUIET = "quiet"
    NORMAL = "normal"
    VERBOSE = "verbose"


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class ConsoleLogger:
    """Simple console logger using Typer styling for consistent output.

    Parameters
    ----------
    secho:
        Callable that mirrors :func:`typer.secho`. This indirection allows tests
        to capture output without touching global state.
    formatter:
        Output format for log lines, either ``"console"`` (default) or ``"json"``.
    timestamp_factory:
        Callable returning ISO8601 timestamps for JSON log entries.
    """

    secho: _LogFunction = typer.secho
    formatter: LogFormat = LogFormat.CONSOLE
    verbosity: Verbosity = Verbosity.NORMAL
    timestamp_factory: Callable[[], str] = _utc_timestamp

    def set_formatter(self, formatter: LogFormat) -> None:
        """Switch the log formatter used by the console logger."""

        self.formatter = formatter

    def set_verbosity(self, verbosity: Verbosity) -> None:
        """Switch the verbosity level."""

        self.verbosity = verbosity

    def to_stderr(self) -> "ConsoleLogger":
        """Return a copy of this logger that writes to stderr.

        For a command whose **stdout is a data channel** — a document meant to
        be piped or redirected, as in ``pyxle openapi > openapi.json`` — every
        message has to leave by the other door. Logging to stdout there writes
        the message *into the artifact*: the redirect captures it, the file is
        corrupt, and the terminal stays silent, so a failure looks like a
        successful run that produced a strange file.

        Verbosity and formatter are carried over, so ``--quiet`` and
        ``--log-format json`` behave the same on the error channel.
        """

        return replace(self, secho=_secho_stderr)

    # Console emitters -------------------------------------------------

    def _emit_console(self, message: str, style: str, bold: bool = False) -> None:
        self.secho(message, fg=style, bold=bold)

    def _emit_json(self, level: str, message: str, extra: dict[str, object] | None = None) -> None:
        payload: dict[str, object] = {
            "level": level,
            "message": _strip_ansi(message),
            "timestamp": self.timestamp_factory(),
        }
        if extra:
            payload.update(extra)
        self.secho(json.dumps(payload, ensure_ascii=False))

    def _emit(self, *, level: str, console_message: str, style: str, bold: bool = False, extra: dict[str, object] | None = None) -> None:
        if self.formatter == LogFormat.JSON:
            self._emit_json(level, console_message, extra)
            return
        self._emit_console(console_message, style, bold=bold)

    def debug(self, message: str) -> None:
        """Emit a debug message (only shown in verbose mode)."""

        if self.verbosity != Verbosity.VERBOSE:
            return
        self._emit(level="debug", console_message=f"🔍 {message}", style="white")

    def info(self, message: str) -> None:
        """Emit an informational message (suppressed in quiet mode).

        An empty message is a deliberate blank spacer — commands like
        ``pyxle routes`` use one to separate sections — so it is emitted as a
        genuinely blank line rather than a bare ``ℹ️`` prefix floating in the
        output. Decorating a separator with an icon made our own documented
        sample output wrong.
        """

        if self.verbosity == Verbosity.QUIET:
            return
        console_message = f"ℹ️  {message}" if message.strip() else ""
        self._emit(level="info", console_message=console_message, style="cyan")

    def success(self, message: str) -> None:
        """Emit a success message."""

        self._emit(level="success", console_message=f"✅ {message}", style="green", bold=True)

    def warning(self, message: str) -> None:
        """Emit a warning message."""

        self._emit(level="warning", console_message=f"⚠️  {message}", style="yellow")

    def error(self, message: str) -> None:
        """Emit an error message."""

        self._emit(level="error", console_message=f"❌ {message}", style="red", bold=True)

    def diagnostic(
        self,
        message: str,
        *,
        file: str | None = None,
        line: int | None = None,
        column: int | None = None,
        hint: str | None = None,
        severity: str = "error",
    ) -> None:
        """Emit a structured diagnostic with optional file location and hint.

        Parameters
        ----------
        message:
            The primary error or warning message.
        file:
            Source file path (displayed as location context).
        line:
            1-based line number in the source file.
        column:
            1-based column number.
        hint:
            Suggestion for how to fix the problem.
        severity:
            Either ``"error"`` or ``"warning"``.
        """

        location = ""
        if file:
            location = file
            if line is not None:
                location += f":{line}"
                if column is not None:
                    location += f":{column}"

        if self.formatter == LogFormat.JSON:
            payload: dict[str, object] = {
                "level": severity,
                "message": message,
                "timestamp": self.timestamp_factory(),
            }
            if location:
                payload["location"] = location
            if hint:
                payload["hint"] = hint
            self.secho(json.dumps(payload, ensure_ascii=False))
            return

        # Console output: structured multi-line diagnostic
        style = "red" if severity == "error" else "yellow"
        marker = "error" if severity == "error" else "warning"

        if location:
            self.secho(f"  {marker}: {message}", fg=style, bold=True)
            self.secho(f"    --> {location}", fg="cyan")
        else:
            self.secho(f"  {marker}: {message}", fg=style, bold=True)

        if hint:
            self.secho(f"    hint: {hint}", fg="green")

    def step(self, label: str, detail: str | None = None) -> None:
        """Emit a step headline with optional detail (suppressed in quiet mode)."""

        if self.verbosity == Verbosity.QUIET:
            return
        suffix = f" — {detail}" if detail else ""
        message = f"▶️  {label}{suffix}"
        extra: dict[str, object] | None = None
        if detail is not None:
            extra = {"label": label, "detail": detail}
        else:
            extra = {"label": label}
        self._emit(level="step", console_message=message, style="magenta", extra=extra)


__all__ = ["ConsoleLogger", "LogFormat", "Verbosity"]
