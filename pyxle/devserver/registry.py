"""Metadata registry assembly for the Pyxle development server."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

from pyxle.compiler.model import PageMetadata

from .build import BuildMetadata, load_build_metadata
from .path_utils import route_path_variants_from_relative
from .scanner import SourceKind
from .settings import DevServerSettings


@dataclass(frozen=True, slots=True)
class PageRegistryEntry:
    """Description of a compiled page available to the dev server."""

    route_path: str
    alternate_route_paths: tuple[str, ...]
    source_relative_path: Path
    source_absolute_path: Path
    server_module_path: Path
    client_module_path: Path
    metadata_path: Path
    client_asset_path: str
    server_asset_path: str
    module_key: str
    content_hash: str
    loader_name: Optional[str]
    loader_line: Optional[int]
    head_elements: tuple[str, ...]
    head_is_dynamic: bool
    scripts: tuple[dict, ...] = ()
    images: tuple[dict, ...] = ()
    head_jsx_blocks: tuple[str, ...] = ()
    actions: tuple[dict, ...] = ()
    cache_revalidate: float | None = None
    uses_suspense: bool = False

    @property
    def has_loader(self) -> bool:
        return self.loader_name is not None

    @property
    def has_actions(self) -> bool:
        return bool(self.actions)


@dataclass(frozen=True, slots=True)
class ApiRegistryEntry:
    """Description of a compiled API endpoint."""

    route_path: str
    alternate_route_paths: tuple[str, ...]
    source_relative_path: Path
    source_absolute_path: Path
    server_module_path: Path
    module_key: str
    content_hash: str


@dataclass(frozen=True, slots=True)
class MetadataRegistry:
    """Aggregated view of pages and APIs for routing purposes."""

    pages: List[PageRegistryEntry]
    apis: List[ApiRegistryEntry]

    def find_page(self, route_path: str) -> Optional[PageRegistryEntry]:
        for entry in self.pages:
            if entry.route_path == route_path or route_path in entry.alternate_route_paths:
                return entry
        return None

    def find_api(self, route_path: str) -> Optional[ApiRegistryEntry]:
        for entry in self.apis:
            if entry.route_path == route_path or route_path in entry.alternate_route_paths:
                return entry
        return None

    def to_dict(self) -> Dict[str, object]:
        return {
            "pages": [
                {
                    "route_path": entry.route_path,
                    "alternate_route_paths": list(entry.alternate_route_paths),
                    "source": entry.source_relative_path.as_posix(),
                    "client_asset_path": entry.client_asset_path,
                    "server_asset_path": entry.server_asset_path,
                    "module_key": entry.module_key,
                    "content_hash": entry.content_hash,
                    "loader_name": entry.loader_name,
                    "loader_line": entry.loader_line,
                    "head": list(entry.head_elements),
                    "head_dynamic": entry.head_is_dynamic,
                    "scripts": list(entry.scripts),
                    "images": list(entry.images),
                    "head_jsx_blocks": list(entry.head_jsx_blocks),
                    "actions": list(entry.actions),
                    "cache_revalidate": entry.cache_revalidate,
                    "uses_suspense": entry.uses_suspense,
                }
                for entry in self.pages
            ],
            "apis": [
                {
                    "route_path": entry.route_path,
                    "alternate_route_paths": list(entry.alternate_route_paths),
                    "source": entry.source_relative_path.as_posix(),
                    "module_key": entry.module_key,
                    "content_hash": entry.content_hash,
                }
                for entry in self.apis
            ],
        }


def build_metadata_registry(
    settings: DevServerSettings,
    metadata: BuildMetadata | None = None,
) -> MetadataRegistry:
    """Derive routing metadata for pages and APIs."""

    metadata = metadata or load_build_metadata(settings.build_root)

    pages: List[PageRegistryEntry] = []
    apis: List[ApiRegistryEntry] = []

    for relative_key, record in sorted(metadata.sources.items()):
        relative_path = Path(relative_key)
        if record.kind == SourceKind.PAGE.value:
            page_entry = _build_page_entry(settings, relative_path, record.content_hash)
            if page_entry:
                pages.append(page_entry)
        elif record.kind == SourceKind.API.value:
            api_entry = _build_api_entry(settings, relative_path, record.content_hash)
            if api_entry:
                apis.append(api_entry)

    pages.sort(key=lambda entry: entry.route_path)
    apis.sort(key=lambda entry: entry.route_path)

    return MetadataRegistry(pages=pages, apis=apis)


def load_metadata_registry(settings: DevServerSettings) -> MetadataRegistry:
    """Convenience wrapper that loads metadata from disk and assembles the registry."""

    return build_metadata_registry(settings, load_build_metadata(settings.build_root))


def _build_page_entry(
    settings: DevServerSettings,
    relative_path: Path,
    content_hash: str,
) -> Optional[PageRegistryEntry]:
    filename = relative_path.name.lower()
    if filename in {"layout.pyxl", "template.pyxl"}:
        return None

    metadata_path = settings.metadata_build_dir / "pages" / relative_path.with_suffix(".json")
    metadata = _load_page_metadata(metadata_path)
    if metadata is None:
        return None

    source_absolute = settings.pages_dir / relative_path
    server_module = settings.server_build_dir / "pages" / relative_path.with_suffix(".py")
    client_module = _resolve_client_module_path(settings.client_build_dir, metadata.client_path)

    if not server_module.exists() or not client_module.exists():
        return None

    return PageRegistryEntry(
        route_path=metadata.route_path,
        alternate_route_paths=metadata.alternate_route_paths,
        source_relative_path=relative_path,
        source_absolute_path=source_absolute,
        server_module_path=server_module,
        client_module_path=client_module,
        metadata_path=metadata_path,
        client_asset_path=metadata.client_path,
        server_asset_path=metadata.server_path,
        module_key=_module_key(relative_path, prefix="pyxle.server.pages"),
        content_hash=content_hash,
        loader_name=metadata.loader_name,
        loader_line=metadata.loader_line,
        head_elements=metadata.head_elements,
        head_is_dynamic=metadata.head_is_dynamic,
        scripts=metadata.scripts,
        images=metadata.images,
        head_jsx_blocks=metadata.head_jsx_blocks,
        actions=metadata.actions,
        cache_revalidate=metadata.cache_revalidate,
        uses_suspense=metadata.uses_suspense,
    )


def _build_api_entry(
    settings: DevServerSettings,
    relative_path: Path,
    content_hash: str,
) -> Optional[ApiRegistryEntry]:
    server_module = settings.server_build_dir / relative_path
    if not server_module.exists():
        return None

    source_absolute = settings.pages_dir / relative_path

    route_spec = route_path_variants_from_relative(relative_path)

    return ApiRegistryEntry(
        route_path=route_spec.primary,
        alternate_route_paths=route_spec.aliases,
        source_relative_path=relative_path,
        source_absolute_path=source_absolute,
        server_module_path=server_module,
        module_key=_module_key(
            relative_path,
            prefix="pyxle.server.api",
            drop_leading="api",
        ),
        content_hash=content_hash,
    )


def _load_page_metadata(path: Path) -> Optional[PageMetadata]:
    try:
        with path.open("r", encoding="utf-8") as file:
            payload = json.load(file)
    except (OSError, json.JSONDecodeError):
        return None

    if not isinstance(payload, dict):
        return None

    route_path = payload.get("route_path")
    client_path = payload.get("client_path")
    server_path = payload.get("server_path")

    if not all(isinstance(value, str) for value in (route_path, client_path, server_path)):
        return None

    loader_name = payload.get("loader_name")
    if loader_name is not None and not isinstance(loader_name, str):
        loader_name = None

    loader_line = payload.get("loader_line")
    if not isinstance(loader_line, int):
        loader_line = None

    alternate_paths_payload = payload.get("alternate_route_paths", [])
    alternate_route_paths: tuple[str, ...]
    if isinstance(alternate_paths_payload, list) and all(isinstance(item, str) for item in alternate_paths_payload):
        alternate_route_paths = tuple(alternate_paths_payload)
    else:
        alternate_route_paths = tuple()

    head_payload = payload.get("head")
    head_elements: tuple[str, ...]
    if head_payload is None:
        head_elements = tuple()
    elif isinstance(head_payload, list) and all(isinstance(item, str) for item in head_payload):
        head_elements = tuple(head_payload)
    else:
        return None

    head_dynamic_payload = payload.get("head_dynamic", False)
    head_is_dynamic = head_dynamic_payload if isinstance(head_dynamic_payload, bool) else False

    scripts_payload = payload.get("scripts", [])
    scripts: tuple[dict, ...]
    if isinstance(scripts_payload, list) and all(isinstance(item, dict) for item in scripts_payload):
        scripts = tuple(scripts_payload)
    else:
        scripts = tuple()

    images_payload = payload.get("images", [])
    images: tuple[dict, ...]
    if isinstance(images_payload, list) and all(isinstance(item, dict) for item in images_payload):
        images = tuple(images_payload)
    else:
        images = tuple()

    head_jsx_blocks_payload = payload.get("head_jsx_blocks", [])
    head_jsx_blocks: tuple[str, ...]
    if isinstance(head_jsx_blocks_payload, list) and all(isinstance(item, str) for item in head_jsx_blocks_payload):
        head_jsx_blocks = tuple(head_jsx_blocks_payload)
    else:
        head_jsx_blocks = tuple()

    actions_payload = payload.get("actions", [])
    actions: tuple[dict, ...]
    if isinstance(actions_payload, list) and all(isinstance(item, dict) for item in actions_payload):
        actions = tuple(actions_payload)
    else:
        actions = tuple()

    cache_revalidate_payload = payload.get("cache_revalidate")
    cache_revalidate: float | None
    if isinstance(cache_revalidate_payload, (int, float)) and not isinstance(
        cache_revalidate_payload, bool
    ):
        cache_revalidate = float(cache_revalidate_payload)
    else:
        cache_revalidate = None

    uses_suspense = payload.get("uses_suspense") is True

    return PageMetadata(
        route_path=route_path,
        alternate_route_paths=alternate_route_paths,
        client_path=client_path,
        server_path=server_path,
        loader_name=loader_name,
        loader_line=loader_line,
        head_elements=head_elements,
        head_is_dynamic=head_is_dynamic,
        scripts=scripts,
        images=images,
        head_jsx_blocks=head_jsx_blocks,
        actions=actions,
        cache_revalidate=cache_revalidate,
        uses_suspense=uses_suspense,
    )


def _resolve_client_module_path(client_root: Path, client_asset_path: str) -> Path:
    relative = client_asset_path.lstrip("/")
    return client_root / relative


def _module_key(relative_path: Path, *, prefix: str, drop_leading: str | None = None) -> str:
    parts = [segment for segment in prefix.split(".") if segment]
    segments = list(relative_path.with_suffix("").parts)
    if drop_leading and segments and segments[0] == drop_leading:
        segments = segments[1:]

    for segment in segments:
        cleaned = segment.replace("[", "").replace("]", "")
        cleaned = cleaned.replace("(", "").replace(")", "")
        cleaned = cleaned.replace("...", "")
        cleaned = cleaned.replace("-", "_").replace(" ", "_")
        cleaned = cleaned.replace(".", "_")
        if not cleaned:
            cleaned = "_"
        if cleaned[0].isdigit():
            cleaned = "_" + cleaned
        parts.append(cleaned)
    return ".".join(parts)


def find_layout_head_jsx_blocks(
    settings: DevServerSettings,
    page_relative_path: Path,
) -> tuple[str, ...]:
    """Find and load head blocks from layout/template files that wrap the page.
    
    Searches ancestor directories from the page's location for layout.pyxl and template.pyxl
    files, loading their compiled metadata to extract both head_jsx_blocks (from <Head>)
    and head_elements (from legacy HEAD variables). Returns the combined blocks in 
    directory precedence order (closest ancestor first).
    """
    # Compute ancestor directories in reverse order (closest to root)
    parts = list(page_relative_path.parent.parts)
    ancestors: List[Path] = []
    
    # Start with the page's immediate directory
    if page_relative_path.parent.name:
        ancestors.append(page_relative_path.parent)
    
    # Add each parent directory
    for index in range(len(parts) - 1, 0, -1):
        ancestors.append(Path(*parts[:index]))
    
    # Add root
    ancestors.append(Path("."))
    
    layout_head_blocks: List[str] = []
    
    # Search for layout and template files in ancestor directories
    for ancestor_dir in ancestors:
        for filename in ("layout.pyxl", "template.pyxl"):
            metadata_path = settings.metadata_build_dir / "pages" / ancestor_dir / Path(filename).with_suffix(".json")
            # Handle root directory case
            if ancestor_dir == Path("."):
                metadata_path = settings.metadata_build_dir / "pages" / Path(filename).with_suffix(".json")
            
            metadata = _load_page_metadata(metadata_path)
            if metadata is not None:
                # Include both JSX Head blocks and legacy HEAD variable elements
                if metadata.head_jsx_blocks:
                    layout_head_blocks.extend(metadata.head_jsx_blocks)
                if metadata.head_elements:
                    layout_head_blocks.extend(metadata.head_elements)
    
    return tuple(layout_head_blocks)


@dataclass(frozen=True, slots=True)
class LayoutLoaderInfo:
    """Metadata needed to execute a layout's ``@server`` loader."""

    relative_path: Path
    server_module_path: Path
    module_key: str
    loader_name: str


def find_layout_loaders(
    settings: DevServerSettings,
    page_relative_path: Path,
) -> tuple[LayoutLoaderInfo, ...]:
    """Discover layout/template files with ``@server`` loaders that wrap *page_relative_path*.

    Walks ancestor directories from the page's location (closest first, root last)
    and returns a :class:`LayoutLoaderInfo` for each layout or template whose
    compiled metadata declares a loader.  The order matches the wrapping order
    used by :func:`find_layout_head_jsx_blocks`.
    """

    parts = list(page_relative_path.parent.parts)
    ancestors: List[Path] = []

    if page_relative_path.parent.name:
        ancestors.append(page_relative_path.parent)

    for index in range(len(parts) - 1, 0, -1):
        ancestors.append(Path(*parts[:index]))

    ancestors.append(Path("."))

    loaders: List[LayoutLoaderInfo] = []

    for ancestor_dir in ancestors:
        for filename in ("layout.pyxl", "template.pyxl"):
            relative = ancestor_dir / filename if ancestor_dir != Path(".") else Path(filename)
            metadata_path = settings.metadata_build_dir / "pages" / relative.with_suffix(".json")
            if ancestor_dir == Path("."):
                metadata_path = settings.metadata_build_dir / "pages" / Path(filename).with_suffix(".json")

            metadata = _load_page_metadata(metadata_path)
            if metadata is None or not metadata.loader_name:
                continue

            server_module = settings.server_build_dir / "pages" / relative.with_suffix(".py")
            module_key = relative.with_suffix("").as_posix().replace("/", ".")

            loaders.append(LayoutLoaderInfo(
                relative_path=relative,
                server_module_path=server_module,
                module_key=module_key,
                loader_name=metadata.loader_name,
            ))

    return tuple(loaders)
