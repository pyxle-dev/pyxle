"""Shared pytest fixtures for the whole suite."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolate_dev_module_generation():
    """Advance the dev module-reload generation before each test.

    ``pyxle dev`` imports a page/action module once and reuses it across
    requests (so module-level globals persist like production), re-importing
    only after a rebuild advances the generation (see
    :mod:`pyxle.ssr.module_cache`). Tests, however, reuse the same ``module_key``
    for different temporary files, so without isolation one test would receive
    the module a previous test cached. Bumping the generation before each test
    forces the first import in a test to re-execute from that test's own file —
    exactly as a fresh process (or a rebuild) would.
    """
    from pyxle.ssr import module_cache

    module_cache.mark_rebuild()
    yield
