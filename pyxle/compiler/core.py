"""Core compilation routines for `.pyxl` files."""

from __future__ import annotations

from pathlib import Path

from pyxle.routing import route_path_variants_from_relative

from .exceptions import CompilationError
from .model import CompilationResult
from .parser import PyxParser
from .writers import ArtifactWriter


def compile_file(
    source_path: Path,
    *,
    build_root: Path,
    client_root: Path | None = None,
    server_root: Path | None = None,
    report_jsx_syntax: bool = False,
) -> CompilationResult:
    """Compile a single `.pyxl` file into client/server artifacts.

    ``report_jsx_syntax`` makes a JSX section Babel cannot parse a compile
    error rather than silently-empty JSX metadata. The dev server turns it on;
    the production build path leaves it off, so its behaviour is unchanged.
    """

    if source_path.suffix != ".pyxl":
        raise CompilationError("Only `.pyxl` files can be compiled", None)

    source_path = source_path.resolve()
    build_root = build_root.resolve()
    client_root = (client_root or build_root / "client").resolve()
    server_root = (server_root or build_root / "server").resolve()
    metadata_root = (build_root / "metadata").resolve()

    build_root.mkdir(parents=True, exist_ok=True)
    client_root.mkdir(parents=True, exist_ok=True)
    server_root.mkdir(parents=True, exist_ok=True)
    metadata_root.mkdir(parents=True, exist_ok=True)

    page_relative = _relative_page_path(source_path)
    route_spec = route_path_variants_from_relative(page_relative)

    parser = PyxParser()
    parse_result = parser.parse(source_path, report_jsx_syntax=report_jsx_syntax)

    writer = ArtifactWriter(
        build_root=build_root,
        client_root=client_root,
        server_root=server_root,
        metadata_root=metadata_root,
    )

    return writer.write(
        source_path=source_path,
        page_relative_path=page_relative,
        route_path=route_spec.primary,
        alternate_route_paths=route_spec.aliases,
        parse_result=parse_result,
    )


def _relative_page_path(source_path: Path) -> Path:
    parts = source_path.parts
    for index in range(len(parts) - 1, -1, -1):
        if parts[index] == "pages":
            if index == len(parts) - 1:
                raise CompilationError("Expected file path inside `pages/`", None)
            return Path(*parts[index + 1 :])
    raise CompilationError("`pages/` directory not found in source path", None)

