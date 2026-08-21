"""Metadata registry assembly for the Pyxle development server."""

from __future__ import annotations

import hashlib
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
    websocket_name: Optional[str] = None
    websocket_line: Optional[int] = None
    cache_revalidate: float | None = None
    uses_suspense: bool = False

    @property
    def has_loader(self) -> bool:
        return self.loader_name is not None

    @property
    def has_actions(self) -> bool:
        return bool(self.actions)

    @property
    def has_websocket(self) -> bool:
        return self.websocket_name is not None


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
                    "websocket_name": entry.websocket_name,
                    "websocket_line": entry.websocket_line,
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
        websocket_name=metadata.websocket_name,
        websocket_line=metadata.websocket_line,
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

    # WebSocket handler metadata. Absent in builds produced before 2.5 — the
    # defensive defaults keep `pyxle serve --skip-build` working against an old
    # dist/ without a recompile.
    websocket_name = payload.get("websocket_name")
    if websocket_name is not None and not isinstance(websocket_name, str):
        websocket_name = None

    websocket_line = payload.get("websocket_line")
    if not isinstance(websocket_line, int):
        websocket_line = None

    cache_revalidate_payload = payload.get("cache_revalidate")
    cache_revalidate: float | None
    if isinstance(cache_revalidate_payload, (int, float)) and not isinstance(
        cache_revalidate_payload, bool
    ):
        cache_revalidate = float(cache_revalidate_payload)
    else:
        cache_revalidate = None

    uses_suspense = payload.get("uses_suspense") is True
    standalone = payload.get("standalone") is True

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
        standalone=standalone,
        actions=actions,
        websocket_name=websocket_name,
        websocket_line=websocket_line,
        cache_revalidate=cache_revalidate,
        uses_suspense=uses_suspense,
    )


def _resolve_client_module_path(client_root: Path, client_asset_path: str) -> Path:
    relative = client_asset_path.lstrip("/")
    return client_root / relative


def _module_key(relative_path: Path, *, prefix: str, drop_leading: str | None = None) -> str:
    """``sys.modules`` key for a compiled page or API module.

    The key has to be **unique per source file**. Compiled modules are cached in
    ``sys.modules`` under this key and reused without re-checking which file
    they came from, so two pages sharing a key means one silently serves the
    other's loader and component — at two different URLs, with a `200` and no
    error anywhere.

    Readability wants the route syntax stripped (``[id]`` reading as ``id``),
    but stripping is lossy and collides: ``[id]``/``id``, ``[[...slug]]``/
    ``[slug]``, ``(marketing)``/``marketing``, ``my-page``/``my_page`` and
    ``embed.js``/``embed_js`` all cleaned to one name. Route groups make this
    ordinary rather than exotic — their whole purpose is a directory that does
    not appear in the URL, so ``(marketing)/pricing`` and ``marketing/pricing``
    are two legitimate pages at two URLs.

    So a segment the cleaning *altered* carries a short digest of its original
    text. Segments that survive cleaning untouched — the overwhelming majority —
    keep exactly the key they had, and any two distinct source segments now
    produce distinct keys.
    """
    parts = [segment for segment in prefix.split(".") if segment]
    segments = list(relative_path.with_suffix("").parts)
    if drop_leading and segments and segments[0] == drop_leading:
        segments = segments[1:]

    for segment in segments:
        parts.append(_module_key_segment(segment))
    return ".".join(parts)


def _module_key_segment(segment: str) -> str:
    """Clean one path segment for :func:`_module_key`, keeping it collision-free."""
    cleaned = segment.replace("[", "").replace("]", "")
    cleaned = cleaned.replace("(", "").replace(")", "")
    cleaned = cleaned.replace("...", "")
    cleaned = cleaned.replace("-", "_").replace(" ", "_")
    cleaned = cleaned.replace(".", "_")
    if not cleaned:
        cleaned = "_"
    if cleaned[0].isdigit():
        cleaned = "_" + cleaned
    if cleaned == segment:
        return cleaned
    # Lossy: another segment could clean to the same name. Tie the key back to
    # the text it came from so the two cannot share a ``sys.modules`` entry.
    digest = hashlib.blake2s(segment.encode("utf-8"), digest_size=3).hexdigest()
    return f"{cleaned}_{digest}"


@dataclass(frozen=True, slots=True)
class LayoutHeadSource:
    """One layout/template's ``HEAD`` variable, located in the wrapping chain.

    ``static_elements`` holds the entries the compiler could read straight off
    the module's AST — a literal string, a literal list. When ``is_dynamic`` is
    set it could not: the value is an f-string, a concatenation, a
    comprehension, a ``json.dumps(...)`` call or a ``def HEAD(data)`` callable,
    and the only way to learn what it produces is to import the module and
    evaluate it. The module coordinates travel with the source precisely so the
    caller can do that — see :func:`pyxle.ssr.view._resolve_layout_head_elements`.

    A layout that reported only its literals is how site-wide JSON-LD used to
    vanish from every page below it, with no warning and no log line.
    """

    relative_path: Path
    server_module_path: Path
    module_key: str
    static_elements: tuple[str, ...] = ()
    is_dynamic: bool = False


@dataclass(frozen=True, slots=True)
class LayoutHeadContribution:
    """The head a page inherits from its layout/template chain.

    Two channels, deliberately kept apart, because they are two different kinds
    of thing and only one of them may be filtered downstream:

    * ``jsx_blocks`` — raw JSX **source** sliced out of ``<Head>`` blocks before
      any of it has run, so a tag may still hold an unevaluated ``{expression}``.
    * ``head_sources`` — one entry per layout that declares a ``HEAD`` variable,
      in wrapping order (closest ancestor first). Each is either finished HTML
      strings, where a brace is content (a JSON-LD payload, a CSS rule), or a
      pointer to a module whose ``HEAD`` still has to be evaluated.

    Merging them into one collection is what made a root layout's JSON-LD
    disappear from every page: the unevaluated-expression filter that
    ``jsx_blocks`` needs judged the already-finished ``HEAD`` strings by the
    same rule and deleted them.

    There is deliberately **no** literals-only accessor here. Offering one is
    what let the layout path quietly serve a partial answer while a page's
    identical ``HEAD`` rendered in full.
    """

    jsx_blocks: tuple[str, ...] = ()
    head_sources: tuple[LayoutHeadSource, ...] = ()


def _layout_chain_ancestors(page_relative_path: Path) -> list[Path]:
    """Directories that may hold a layout wrapping *page_relative_path*.

    Ordered closest ancestor first, project root last — the order a page's
    wrappers apply in. Shared by :func:`find_layout_head_contributions` and
    :func:`find_layout_loaders` so a layout's head and its loader can never
    disagree about which files wrap a page.
    """
    parts = list(page_relative_path.parent.parts)
    ancestors: List[Path] = []

    # The page's own directory (absent for a page sitting at the root).
    if page_relative_path.parent.name:
        ancestors.append(page_relative_path.parent)

    for index in range(len(parts) - 1, 0, -1):
        ancestors.append(Path(*parts[:index]))

    ancestors.append(Path("."))
    return ancestors


def _layout_relative_path(ancestor_dir: Path, filename: str) -> Path:
    """Project-relative path of *filename* inside *ancestor_dir*."""
    return Path(filename) if ancestor_dir == Path(".") else ancestor_dir / filename


def _layout_module_key(relative: Path) -> str:
    """``sys.modules`` key for a compiled layout/template module.

    A layout's loader and its ``HEAD`` must resolve to the *same* module
    object, so both call sites derive the key here — otherwise the module runs
    twice and module-level state (a client handle, a cached template) exists in
    two copies that disagree.
    """
    return relative.with_suffix("").as_posix().replace("/", ".")


def find_layout_head_contributions(
    settings: DevServerSettings,
    page_relative_path: Path,
) -> LayoutHeadContribution:
    """Find and load head contributions from layout/template files that wrap the page.

    Searches ancestor directories from the page's location for layout.pyxl and
    template.pyxl files, loading their compiled metadata. ``<Head>`` JSX blocks
    and ``HEAD`` variable sources are returned in **separate** channels (see
    :class:`LayoutHeadContribution`), each in directory precedence order
    (closest ancestor first).

    A layout whose ``HEAD`` the compiler could not read statically is reported
    as a :class:`LayoutHeadSource` with ``is_dynamic`` set, not dropped.
    """
    layout_jsx_blocks: List[str] = []
    head_sources: List[LayoutHeadSource] = []

    # Search for layout and template files in ancestor directories
    for ancestor_dir in _layout_chain_ancestors(page_relative_path):
        stop_here = False
        for filename in ("layout.pyxl", "template.pyxl"):
            relative = _layout_relative_path(ancestor_dir, filename)
            metadata_path = settings.metadata_build_dir / "pages" / relative.with_suffix(".json")

            metadata = _load_page_metadata(metadata_path)
            if metadata is not None:
                # A STANDALONE layout is the root of its own chain, so an
                # ancestor's <Head> does not belong on these pages either --
                # otherwise a section that opted out of the app shell still
                # inherits its analytics snippet and its stylesheet link.
                if metadata.standalone:
                    stop_here = True
                # Two channels, never one list: <Head> JSX is unevaluated source
                # and is filtered downstream; the HEAD variable is finished HTML
                # and must reach the document verbatim.
                if metadata.head_jsx_blocks:
                    layout_jsx_blocks.extend(metadata.head_jsx_blocks)
                if metadata.head_elements or metadata.head_is_dynamic:
                    head_sources.append(
                        LayoutHeadSource(
                            relative_path=relative,
                            server_module_path=(
                                settings.server_build_dir / "pages" / relative.with_suffix(".py")
                            ),
                            module_key=_layout_module_key(relative),
                            static_elements=metadata.head_elements,
                            is_dynamic=metadata.head_is_dynamic,
                        )
                    )

        if stop_here:
            break

    return LayoutHeadContribution(
        jsx_blocks=tuple(layout_jsx_blocks),
        head_sources=tuple(head_sources),
    )


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
    used by :func:`find_layout_head_contributions`.
    """

    loaders: List[LayoutLoaderInfo] = []

    for ancestor_dir in _layout_chain_ancestors(page_relative_path):
        stop_here = False
        for filename in ("layout.pyxl", "template.pyxl"):
            relative = _layout_relative_path(ancestor_dir, filename)
            metadata_path = settings.metadata_build_dir / "pages" / relative.with_suffix(".json")

            metadata = _load_page_metadata(metadata_path)
            if metadata is None:
                continue

            # A layout declaring STANDALONE is the root of its chain: nothing
            # above it wraps these pages, so nothing above it should run a
            # loader for them either. Without this the wrapper would be gone
            # and the query still charged — the outer layout's loader firing on
            # every request to a section that does not use it.
            if metadata.standalone:
                stop_here = True

            if not metadata.loader_name:
                continue

            loaders.append(LayoutLoaderInfo(
                relative_path=relative,
                server_module_path=(
                    settings.server_build_dir / "pages" / relative.with_suffix(".py")
                ),
                module_key=_layout_module_key(relative),
                loader_name=metadata.loader_name,
            ))

        if stop_here:
            break

    return tuple(loaders)
