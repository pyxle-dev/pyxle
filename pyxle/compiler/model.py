"""Data models for the Pyxle compiler."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict


@dataclass(frozen=True)
class ScriptDeclaration:
    """Represents a <Script /> element declaration in a .pyxl file."""

    src: str
    strategy: str = "afterInteractive"  # beforeInteractive, afterInteractive, lazyOnload
    async_: bool = False
    defer: bool = False
    module: bool = False
    no_module: bool = False

    def to_json(self) -> Dict[str, Any]:
        return {
            "src": self.src,
            "strategy": self.strategy,
            "async": self.async_,
            "defer": self.defer,
            "module": self.module,
            "noModule": self.no_module,
        }


@dataclass(frozen=True)
class ImageDeclaration:
    """Represents an <Image /> element declaration in a .pyxl file."""

    src: str
    width: int | None = None
    height: int | None = None
    alt: str = ""
    priority: bool = False
    lazy: bool = True

    def to_json(self) -> Dict[str, Any]:
        return {
            "src": self.src,
            "width": self.width,
            "height": self.height,
            "alt": self.alt,
            "priority": self.priority,
            "lazy": self.lazy,
        }


@dataclass(frozen=True)
class ActionDeclaration:
    """Represents a single ``@action``-decorated function in a compiled page."""

    name: str
    line: int | None

    def to_json(self) -> Dict[str, Any]:
        return {"name": self.name, "line": self.line}


@dataclass(frozen=True)
class PageMetadata:
    """Metadata emitted for each compiled page."""

    route_path: str
    alternate_route_paths: tuple[str, ...]
    client_path: str
    server_path: str
    loader_name: str | None
    loader_line: int | None
    head_elements: tuple[str, ...]
    head_is_dynamic: bool
    scripts: tuple[ScriptDeclaration, ...] = ()
    images: tuple[ImageDeclaration, ...] = ()
    head_jsx_blocks: tuple[str, ...] = ()
    actions: tuple[ActionDeclaration, ...] = ()
    websocket_name: str | None = None
    websocket_line: int | None = None
    cache_revalidate: float | None = None
    uses_suspense: bool = False

    def to_json(self) -> Dict[str, Any]:
        return {
            "route_path": self.route_path,
            "alternate_route_paths": list(self.alternate_route_paths),
            "client_path": self.client_path,
            "server_path": self.server_path,
            "loader_name": self.loader_name,
            "loader_line": self.loader_line,
            "head": list(self.head_elements),
            "head_dynamic": self.head_is_dynamic,
            "scripts": [s.to_json() for s in self.scripts],
            "images": [i.to_json() for i in self.images],
            "head_jsx_blocks": list(self.head_jsx_blocks),
            "actions": [a.to_json() for a in self.actions],
            "websocket_name": self.websocket_name,
            "websocket_line": self.websocket_line,
            "cache_revalidate": self.cache_revalidate,
            "uses_suspense": self.uses_suspense,
        }

    @property
    def has_loader(self) -> bool:
        return self.loader_name is not None

    @property
    def has_actions(self) -> bool:
        return bool(self.actions)

    @property
    def has_websocket(self) -> bool:
        return self.websocket_name is not None


@dataclass(frozen=True)
class CompilationResult:
    """Represents the outcome of compiling a `.pyxl` file."""

    source_path: Path
    python_code: str
    jsx_code: str
    server_output: Path
    client_output: Path
    metadata_output: Path
    metadata: PageMetadata

    def __post_init__(self) -> None:
        if self.server_output.is_dir() or self.client_output.is_dir():
            raise ValueError("Output paths must point to files, not directories.")

    def summary(self) -> str:
        loader = self.metadata.loader_name or "<none>"
        return (
            f"Compiled {self.source_path.name} ({self.metadata.route_path}): "
            f"loader={loader} -> server={self.server_output} client={self.client_output}"
        )
