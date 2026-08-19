"""Memoised path canonicalisation for the SSR render path.

``Path.resolve()`` is a blocking ``realpath(3)`` walk: one ``lstat`` per path
component, no yielding. Measured on this project's own build output (a
10-component compiled component path, warm dentry cache) it costs **~18.7us
and is 100% on-CPU** — it is not I/O wait that the event loop can overlap, it
is the loop's thread held busy.

That makes calling it from an ``async`` SSR handler a violation of CLAUDE.md
rule 8 (never block the event loop) with a cost that rule 15 asks us to judge
at 100 concurrent requests rather than at one. Measured with an event-loop lag
probe at concurrency 100, *each* per-request ``resolve()`` added ~1.9ms of
head-of-line delay to every other in-flight render; the buffered path made two
of them, for ~4ms of stall on top of a ~3ms render.

The resolved *value* still has to be correct, so the call is memoised rather
than dropped. These paths are handed to a Node subprocess as the module it must
import, and a deployment whose build directory sits under a symlink needs the
canonical form — swapping in ``os.path.abspath`` would be ~59x faster but would
stop following symlinks and break SSR in exactly that environment.

Resolution is pinned for the life of the process. That is not a new constraint:
:class:`~pyxle.devserver.settings.DevServerSettings` already resolves
``client_build_dir`` and ``project_root`` once at construction, so the symlinked
prefix of every component path is fixed at startup regardless. The dev server
clears this cache on rebuild alongside the renderer's bundle cache.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

__all__ = ["clear_resolved_paths", "resolve_component_path"]

#: Eviction policy (CLAUDE.md rule 17): least-recently-used, hard cap.
#: The key space is an application's compiled component paths — one per page
#: plus error and loading boundaries — which is fixed at build time, so a real
#: app settles into a few dozen entries and never evicts. The cap is what keeps
#: a caller that renders many distinct paths from growing this without bound.
RESOLVED_PATH_CACHE_MAXSIZE = 1024


@lru_cache(maxsize=RESOLVED_PATH_CACHE_MAXSIZE)
def resolve_component_path(path: Path) -> Path:
    """Return the canonical, symlink-free form of *path*, memoised per process.

    Semantically identical to ``path.resolve()`` — same symlink normalisation,
    same behaviour for a path that does not exist — but a repeat lookup for a
    path already seen costs a dict probe instead of a ``realpath`` walk.
    """

    return path.resolve()


def clear_resolved_paths() -> None:
    """Drop every memoised resolution.

    Called on a dev-server rebuild so a build that moves or re-links output is
    never served from a stale canonical path.
    """

    resolve_component_path.cache_clear()
