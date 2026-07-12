"""Dev-mode module-reload generation.

In ``pyxle serve`` a page's ``@server`` loader module (and an ``@action``
module) is imported once and reused for every request, so module-level globals
persist across requests for the life of the process. ``pyxle dev`` used to
re-import the module on *every* request, which reset those globals each time and
diverged from production — a module-level counter, in-process cache, or lazily
built singleton behaved differently in dev than it would in serve.

This module makes dev match serve. A single process-wide **generation** counter
is advanced once per *material* rebuild (see :class:`~pyxle.devserver.watcher`).
The importers (:func:`pyxle.ssr.view._import_server_module` and
:func:`pyxle.devserver.starlette_app._import_module`) stamp each freshly
imported module with the generation it was built against and reuse it while the
generation is unchanged — so globals persist across requests — re-importing only
after the generation advances. A re-import re-executes the module top to bottom
(resetting its globals) *and* re-runs its ``import`` statements, so an edit to
the module or to a helper it imports both take effect on the next request. That
is the hot-reload contract; module-level state is not expected to survive an
edit (it never does across a process restart either).

Note that module-level mutable state is per-process: under ``pyxle serve
--workers N`` each worker has its own copy, and nothing survives a restart. Use
a database, cache, or per-worker resource for state that must be shared or
durable — see the data-loading guide.
"""

from __future__ import annotations

#: Attribute stamped onto an imported dev module recording the generation it was
#: built against. Reused across requests until the generation advances.
GENERATION_ATTRIBUTE = "__pyxle_build_generation__"

_generation = 0


def current_generation() -> int:
    """Return the current dev module-reload generation."""

    return _generation


def mark_rebuild() -> None:
    """Advance the generation so cached dev modules re-import on next use.

    Called once per material rebuild. Idempotent per rebuild — calling it again
    without a rebuild simply advances the counter and forces one more re-import.
    """

    global _generation
    _generation += 1


__all__ = ["GENERATION_ATTRIBUTE", "current_generation", "mark_rebuild"]
