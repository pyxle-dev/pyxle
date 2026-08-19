"""Custom exceptions for compiler failures."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(eq=False)
class CompilationError(Exception):
    """Raised when a `.pyxl` file cannot be compiled.

    ``line_number`` and ``column`` are 1-indexed positions in the original
    ``.pyxl`` source, or ``None`` when the failure has no position (a
    whole-file problem such as a source path outside ``pages/``). They are
    carried structurally rather than baked into :meth:`__str__` so a caller
    that knows the file — the dev-server rebuild, which knows *which* source
    it was compiling — can render one ``path:line:column: message`` location
    the terminal and the editor both understand.
    """

    message: str
    line_number: int | None = None
    column: int | None = None

    def __str__(self) -> str:  # pragma: no cover - trivial
        if self.line_number is None:
            return self.message
        return f"Line {self.line_number}: {self.message}"
