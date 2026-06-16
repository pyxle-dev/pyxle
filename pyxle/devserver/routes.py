"""Route manifest builders for Starlette integration."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Optional

from .error_pages import is_error_boundary_file
from .path_utils import route_path_from_relative
from .registry import ApiRegistryEntry, MetadataRegistry, PageRegistryEntry


@dataclass(frozen=True, slots=True)
class PageRoute:
    """Descriptor for a Starlette page route."""

    path: str
    source_relative_path: Path
    source_absolute_path: Path
    server_module_path: Path
    client_module_path: Path
    metadata_path: Path
    module_key: str
    client_asset_path: str
    server_asset_path: str
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
class ActionRoute:
    """Descriptor for an auto-generated ``@action`` endpoint.

    Action endpoints accept ``POST /api/__actions/<page_path>/<action_name>``
    and dispatch to the corresponding ``@action``-decorated function in the
    page's server module.

    For pages with catch-all or dynamic route parameters (e.g.
    ``[[...slug]].pyxl``), an additional catch-all action route is registered
    so that URLs constructed from the full browser path still resolve.
    These routes set ``is_catchall=True`` and extract the action name from
    the last segment of the captured path at dispatch time.
    """

    path: str
    page_path: str
    action_name: str
    server_module_path: Path
    module_key: str
    is_catchall: bool = False


@dataclass(frozen=True, slots=True)
class ApiRoute:
    """Descriptor for a Starlette API route."""

    path: str
    source_relative_path: Path
    source_absolute_path: Path
    server_module_path: Path
    module_key: str
    content_hash: str


@dataclass(frozen=True, slots=True)
class RouteTable:
    """Aggregated routing descriptors for Starlette registration."""

    pages: List[PageRoute]
    apis: List[ApiRoute]
    actions: List[ActionRoute] = ()  # type: ignore[assignment]
    error_boundary_pages: List[PageRoute] = ()  # type: ignore[assignment]

    def find_page(self, path: str) -> Optional[PageRoute]:
        for entry in self.pages:
            if entry.path == path:
                return entry
        return None

    def find_api(self, path: str) -> Optional[ApiRoute]:
        for entry in self.apis:
            if entry.path == path:
                return entry
        return None

    def find_action(self, path: str) -> Optional[ActionRoute]:
        for entry in self.actions:
            if entry.path == path:
                return entry
        return None


def build_route_table(registry: MetadataRegistry) -> RouteTable:
    """Construct Starlette-friendly route descriptors from registry metadata."""

    page_routes: List[PageRoute] = []
    error_boundary_routes: List[PageRoute] = []

    for entry in registry.pages:
        posix = entry.source_relative_path.as_posix()
        if is_error_boundary_file(posix):
            # Error/not-found pages are compiled but not routed normally.
            error_boundary_routes.extend(_page_routes(entry))
        else:
            page_routes.extend(_page_routes(entry))

    api_routes: List[ApiRoute] = []
    for entry in registry.apis:
        api_routes.extend(_api_routes(entry))

    action_routes: List[ActionRoute] = []
    for entry in registry.pages:
        if not is_error_boundary_file(entry.source_relative_path.as_posix()):
            action_routes.extend(_action_routes(entry))

    page_routes.sort(key=lambda route: route.path)
    api_routes.sort(key=lambda route: route.path)
    action_routes.sort(key=lambda route: route.path)
    error_boundary_routes.sort(key=lambda route: route.path)

    return RouteTable(
        pages=page_routes,
        apis=api_routes,
        actions=action_routes,
        error_boundary_pages=error_boundary_routes,
    )


def _page_routes(entry: PageRegistryEntry) -> List[PageRoute]:
    candidates = [entry.route_path, *entry.alternate_route_paths]
    routes: List[PageRoute] = []
    for index, candidate in enumerate(candidates):
        routes.append(_page_route(entry, candidate, allow_inferred_fallback=index == 0))
    return routes


def _page_route(
    entry: PageRegistryEntry,
    candidate_path: str,
    *,
    allow_inferred_fallback: bool,
) -> PageRoute:
    normalized_path = candidate_path
    if allow_inferred_fallback:
        inferred = route_path_from_relative(entry.source_relative_path)
        if entry.route_path != inferred:
            normalized_path = inferred

    return PageRoute(
        path=normalized_path,
        source_relative_path=entry.source_relative_path,
        source_absolute_path=entry.source_absolute_path,
        server_module_path=entry.server_module_path,
        client_module_path=entry.client_module_path,
        metadata_path=entry.metadata_path,
        module_key=entry.module_key,
        client_asset_path=entry.client_asset_path,
        server_asset_path=entry.server_asset_path,
        content_hash=entry.content_hash,
        loader_name=entry.loader_name,
        loader_line=entry.loader_line,
        head_elements=entry.head_elements,
        head_is_dynamic=entry.head_is_dynamic,
        scripts=entry.scripts,
        images=entry.images,
        head_jsx_blocks=entry.head_jsx_blocks,
        actions=entry.actions,
        cache_revalidate=entry.cache_revalidate,
        uses_suspense=entry.uses_suspense,
    )


def select_static_pages(pages: Iterable[PageRoute]) -> list[PageRoute]:
    """Pages that can be pre-rendered at build time (``pyxle build --static``).

    A page is statically renderable when it has no ``@server`` loader -- so its
    render is a deterministic function of no per-request data -- and no dynamic
    route parameters (a ``{param}`` segment would need a value to render).
    """

    return [page for page in pages if not page.has_loader and "{" not in page.path]


def _api_routes(entry: ApiRegistryEntry) -> List[ApiRoute]:
    candidates = [entry.route_path, *entry.alternate_route_paths]
    routes: List[ApiRoute] = []
    for index, candidate in enumerate(candidates):
        routes.append(_api_route(entry, candidate, allow_inferred_fallback=index == 0))
    return routes


def _api_route(
    entry: ApiRegistryEntry,
    candidate_path: str,
    *,
    allow_inferred_fallback: bool,
) -> ApiRoute:
    normalized_path = candidate_path
    if allow_inferred_fallback:
        inferred = route_path_from_relative(entry.source_relative_path)
        if entry.route_path != inferred:
            normalized_path = inferred

    return ApiRoute(
        path=normalized_path,
        source_relative_path=entry.source_relative_path,
        source_absolute_path=entry.source_absolute_path,
        server_module_path=entry.server_module_path,
        module_key=entry.module_key,
        content_hash=entry.content_hash,
    )


def _action_routes(entry: PageRegistryEntry) -> List[ActionRoute]:
    """Build ``ActionRoute`` descriptors for each ``@action`` in a page entry.

    For pages with path-parameterised alternate routes (catch-all or dynamic
    segments), an additional catch-all action route is appended so that the
    client's ``useAction`` hook — which constructs the URL from the current
    browser path — resolves correctly regardless of which sub-path is active.
    """
    routes: List[ActionRoute] = []
    page_path = entry.route_path.rstrip("/") or "/"

    # Derive a URL-safe page segment from the route path.
    # e.g. "/" -> "index", "/dashboard/settings" -> "dashboard/settings"
    page_segment = page_path.lstrip("/") or "index"

    has_actions = False
    for action_info in entry.actions:
        if not isinstance(action_info, dict):
            continue
        action_name = action_info.get("name")
        if not isinstance(action_name, str) or not action_name:
            continue

        has_actions = True
        action_http_path = f"/api/__actions/{page_segment}/{action_name}"
        routes.append(
            ActionRoute(
                path=action_http_path,
                page_path=page_path,
                action_name=action_name,
                server_module_path=entry.server_module_path,
                module_key=entry.module_key,
            )
        )

    # For pages with path-parameterised alternate routes (e.g. catch-all
    # ``[[...slug]]``), the client builds action URLs using the full browser
    # path.  Register a catch-all action route that captures the trailing
    # segments and extracts the action name from the last one.
    if has_actions and any(
        "{" in alt for alt in entry.alternate_route_paths
    ):
        catchall_path = f"/api/__actions/{page_segment}/{{_pyxle_action_path:path}}"
        routes.append(
            ActionRoute(
                path=catchall_path,
                page_path=page_path,
                action_name="",
                server_module_path=entry.server_module_path,
                module_key=entry.module_key,
                is_catchall=True,
            )
        )

    return routes


__all__ = [
    "ActionRoute",
    "ApiRoute",
    "PageRoute",
    "RouteTable",
    "build_route_table",
]
