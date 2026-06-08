"""Template loading utilities used by the scaffolding commands."""

from __future__ import annotations

from dataclasses import dataclass
from importlib import resources
try:  # ``importlib.resources.abc`` exists on Python 3.11+; 3.10 keeps it on ``importlib.abc``.
    from importlib.resources.abc import Traversable
except ModuleNotFoundError:  # Python 3.10
    from importlib.abc import Traversable
from pathlib import Path
from string import Template
from typing import Any, Mapping

_TEMPLATE_PACKAGE = "pyxle.templates.scaffold"


@dataclass(frozen=True)
class ScaffoldingTemplate:
    """Represents a template file stored within the package resources."""

    resource_path: str
    binary: bool = False

    def _resource(self) -> Traversable:
        return resources.files(_TEMPLATE_PACKAGE).joinpath(self.resource_path)

    def render(self, context: Mapping[str, Any] | None = None) -> bytes:
        """Render the template using ``context`` and return bytes."""

        context = context or {}
        resource = self._resource()
        if self.binary:
            return resource.read_bytes()
        raw = resource.read_text(encoding="utf-8")
        return Template(raw).safe_substitute(**context).encode("utf-8")


class TemplateRegistry:
    """Registry that stores the mapping of output paths to templates."""

    def __init__(self) -> None:
        self._entries: dict[Path, ScaffoldingTemplate] = {}

    def register(self, output_path: str | Path, template: ScaffoldingTemplate) -> None:
        path = Path(output_path)
        if path in self._entries:
            raise ValueError(f"Template already registered for '{path}'")
        self._entries[path] = template

    def items(self) -> list[tuple[Path, ScaffoldingTemplate]]:
        return list(self._entries.items())
