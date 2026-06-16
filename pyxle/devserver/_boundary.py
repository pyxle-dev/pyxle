"""Shared directory walk-up for file-convention boundary resolution.

Both the error/not-found boundary resolver (``error_pages``) and the
``loading.pyxl`` resolver (``loading_pages``) map directory segments to a
compiled page and pick the **closest ancestor** of a route. This module holds
the single walk-up implementation they share.
"""

from __future__ import annotations

from typing import Any, Optional


def resolve_nearest(route_path: str, registry: dict[str, Any]) -> Optional[Any]:
    """Return the registry entry for the nearest ancestor directory of *route_path*.

    Keys in *registry* are directory paths relative to ``pages/`` in POSIX
    form, with the root represented as ``"."``. The walk goes deepest-first and
    falls back to the root. For ``/dashboard/settings/profile`` the lookup order
    is:

        1. ``"dashboard/settings/profile"``
        2. ``"dashboard/settings"``
        3. ``"dashboard"``
        4. ``"."``  (root)
    """

    stripped = route_path.strip("/")
    if not stripped:
        return registry.get(".")

    parts = stripped.split("/")
    for end in range(len(parts), 0, -1):
        candidate = "/".join(parts[:end])
        if candidate in registry:
            return registry[candidate]

    return registry.get(".")


__all__ = ["resolve_nearest"]
