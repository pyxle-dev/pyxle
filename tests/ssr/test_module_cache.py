"""Tests for the dev module-reload generation counter."""

from __future__ import annotations

from pyxle.ssr import module_cache


def test_current_generation_advances_on_mark_rebuild() -> None:
    """Each ``mark_rebuild`` advances the generation by one; between calls it is
    stable (which is what lets importers reuse a module across requests)."""
    start = module_cache.current_generation()

    assert module_cache.current_generation() == start  # stable without a rebuild

    module_cache.mark_rebuild()
    assert module_cache.current_generation() == start + 1

    module_cache.mark_rebuild()
    assert module_cache.current_generation() == start + 2
