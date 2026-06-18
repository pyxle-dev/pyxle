"""Loading-state resolution for ``loading.pyxl`` pages.

Pyxle supports a file-convention loading state inspired by Next.js's
``loading.js``:

* ``pages/loading.pyxl``           — root loading fallback
* ``pages/dashboard/loading.pyxl`` — loading fallback for ``/dashboard/*``

During a streaming render the nearest ``loading.pyxl`` (closest ancestor of the
route) supplies the ``<Suspense>`` fallback shown as the shell while the page's
async boundaries resolve. Resolution walks **up** the directory tree from the
route; the closest ancestor wins — the same convention as ``error.pyxl``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, Optional, Sequence

from ._boundary import resolve_nearest


_LOADING_FILENAMES = frozenset({"loading.pyxl"})


def is_loading_file(relative_path_posix: str) -> bool:
    """Return True if the source file is a ``loading.pyxl``."""
    return PurePosixPath(relative_path_posix).name.lower() in _LOADING_FILENAMES


@dataclass(frozen=True, slots=True)
class LoadingBoundaryRegistry:
    """Maps directory segments to their compiled ``loading.pyxl`` page routes.

    Keys are directory paths relative to ``pages/`` in POSIX form. The root
    directory is represented as ``"."``.

    Example::

        loading_pages = {".": <root loading.pyxl route>, "dashboard": <...>}
    """

    loading_pages: dict[str, Any]

    @property
    def has_loading_pages(self) -> bool:
        return bool(self.loading_pages)

    def find_loading_boundary(self, route_path: str) -> Optional[Any]:
        """Find the nearest ``loading.pyxl`` for *route_path* by walking up."""
        return resolve_nearest(route_path, self.loading_pages)


def build_loading_boundary_registry(
    pages: Sequence[Any],
) -> LoadingBoundaryRegistry:
    """Build a :class:`LoadingBoundaryRegistry` from compiled loading pages.

    Takes the full list of compiled ``loading.pyxl`` page routes and indexes
    them by their parent directory.
    """

    loading_pages: dict[str, Any] = {}

    for page in pages:
        name = page.source_relative_path.name.lower()
        if name not in _LOADING_FILENAMES:
            continue
        parent = page.source_relative_path.parent.as_posix()
        # Normalise root directory to "."
        if not parent or parent == ".":
            parent = "."
        loading_pages[parent] = page

    return LoadingBoundaryRegistry(loading_pages=loading_pages)


__all__ = [
    "LoadingBoundaryRegistry",
    "build_loading_boundary_registry",
    "is_loading_file",
]
