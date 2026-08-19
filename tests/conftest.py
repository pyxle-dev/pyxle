"""Shared pytest fixtures for the whole suite."""

from __future__ import annotations

import pytest


#: Prefixes of the ``sys.modules`` keys compiled pages and API modules are
#: imported under (see ``pyxle.devserver.registry._module_key``).
_COMPILED_MODULE_PREFIXES = ("pyxle.server.pages", "pyxle.server.api")


@pytest.fixture(autouse=True)
def _isolate_dev_module_generation():
    """Give each test a clean slate for compiled page/API modules.

    A compiled module is cached in ``sys.modules`` under a key derived from the
    page's path *relative to the project*, with no project identity in it. One
    server serves one project, so in production that key is unique. Tests break
    the assumption: every ``tmp_path`` project has its own ``pages/index.pyxl``,
    so a test can be handed the module a previous test compiled from entirely
    different source.

    Two import paths need isolating, and they cache differently:

    * ``pyxle dev`` (``debug=True``) re-imports once the module-reload
      generation advances (see :mod:`pyxle.ssr.module_cache`), so bumping the
      generation is enough — module-level globals still persist within a test,
      exactly as they do across requests in a running server.
    * ``pyxle serve`` / ``pyxle openapi`` (``debug=False``) reuse whatever is in
      ``sys.modules`` unconditionally, and never consult the generation. Only
      dropping the entries isolates those.

    Both are handled here. Without the second, the suite passes or fails
    depending on collection order — ``tests/cli`` happens to run before
    ``tests/devserver`` alphabetically, and running them the other way round
    made CLI tests assert against another test's page.
    """
    import sys

    from pyxle.ssr import module_cache

    module_cache.mark_rebuild()
    yield
    for name in [
        name
        for name in sys.modules
        if name.startswith(_COMPILED_MODULE_PREFIXES)
    ]:
        del sys.modules[name]
