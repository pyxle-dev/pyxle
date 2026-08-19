"""Path conversion helpers for dev server routing."""

from __future__ import annotations

from pyxle.routing import (
    RoutePathSpec,
    route_path_from_relative,
    route_path_variants_from_relative,
)


def url_path_is_under(path: str, prefix: str) -> bool:
    """Return ``True`` when ``path`` *is* ``prefix`` or sits beneath it.

    URL namespaces are made of whole segments, so the comparison must be too.
    A bare ``path.startswith("/client")`` also matches ``/client-logo.svg``,
    which belongs to the app's ``public/`` directory — mistaking it for a
    framework bundle serves the user a silent 404.
    """

    return path == prefix or path.startswith(prefix + "/")


__all__ = [
    "RoutePathSpec",
    "route_path_from_relative",
    "route_path_variants_from_relative",
    "url_path_is_under",
]
