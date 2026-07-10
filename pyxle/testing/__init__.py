"""Test helpers for Pyxle projects.

Unit-test your ``@server`` loaders and ``@action`` handlers without a running
server. A ``.pyxl`` file is not an importable Python module (it carries a JSX
component alongside the Python and compiles into a build directory), so you
can't ``import`` its loader directly. These helpers compile the file and hand
you back the plain ``async`` functions to call.

Example::

    import asyncio
    from types import SimpleNamespace
    from pyxle.testing import load_loader

    def test_home_loader():
        load_home = load_loader("pages/index.pyxl")
        data = asyncio.run(load_home(SimpleNamespace()))
        assert data["hello"] == "world"

For an ``@action`` (or any named function on the page), use :func:`load_page`
and read the attribute off the returned module::

    page = load_page("pages/contact.pyxl")
    result = asyncio.run(page.submit(request))
"""

from __future__ import annotations

import importlib.util
import itertools
import tempfile
from pathlib import Path
from types import ModuleType
from typing import Callable, Optional, Union

from pyxle.compiler import compile_file

__all__ = ["load_page", "load_loader"]

_PathLike = Union[str, Path]
_counter = itertools.count()


def load_page(
    source_path: _PathLike,
    *,
    build_root: Optional[_PathLike] = None,
) -> ModuleType:
    """Compile a ``.pyxl`` file and return its executed server module.

    The returned module exposes the page's ``@server`` loader and every
    ``@action`` as plain ``async`` functions — call them directly with a
    request object (a lightweight fake such as ``types.SimpleNamespace`` is
    fine for unit tests). The compiled page's metadata (including
    ``loader_name``) is attached as ``module.__pyxle_metadata__``.

    ``build_root`` is where the compiled artifacts are written; it defaults to
    a fresh temporary directory, so tests never touch your project's build.
    """

    source = Path(source_path).resolve()
    root = Path(build_root) if build_root is not None else Path(
        tempfile.mkdtemp(prefix="pyxle-test-")
    )

    result = compile_file(source, build_root=root)

    module_name = f"pyxle_test_{source.stem}_{next(_counter)}"
    spec = importlib.util.spec_from_file_location(module_name, result.server_output)
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        raise RuntimeError(f"Could not load the compiled module for {source}")

    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.__pyxle_metadata__ = result.metadata  # type: ignore[attr-defined]
    return module


def load_loader(
    source_path: _PathLike,
    *,
    build_root: Optional[_PathLike] = None,
) -> Callable:
    """Compile a ``.pyxl`` file and return its ``@server`` loader function.

    Raises :class:`ValueError` if the page declares no ``@server`` loader.
    """

    module = load_page(source_path, build_root=build_root)
    loader_name = module.__pyxle_metadata__.loader_name  # type: ignore[attr-defined]
    if loader_name is None:
        raise ValueError(f"{Path(source_path)} has no @server loader to test.")
    return getattr(module, loader_name)
