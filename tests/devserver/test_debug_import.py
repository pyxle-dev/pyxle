"""Integration tests for debug-mode module imports (the Pyxle debugger).

Builds a real temporary project, then imports the generated server modules
through the dev server's importer with ``debug=True`` and asserts that code
objects, tracebacks, and fallbacks all reference the original ``.pyxl``
sources — the contract that makes native ``.pyxl`` breakpoints and tracebacks
work. Node is never spawned: the compile pipeline is Node-free by default.
"""

from __future__ import annotations

import sys
import traceback
from pathlib import Path

import pytest

from pyxle.devserver.builder import build_once
from pyxle.devserver.settings import DevServerSettings
from pyxle.devserver.starlette_app import _import_module

LOADER_PAGE = """\


@server
async def load_home(request):
    return {"message": "hi"}

def boom():
    raise ValueError("kaboom")

import React from 'react';

export default function Home({ data }) {
    return <div>{data.message}</div>;
}
"""

SELF_SUFFICIENT_PAGE = """\
from pyxle.runtime import LoaderError, server


@server
async def load_page(request):
    return {"ok": True}

import React from 'react';

export default function Page({ data }) {
    return <div>{String(data.ok)}</div>;
}
"""

MULTI_SEGMENT_PAGE = """\
@server
async def load_stats(request):
    return {"n": helper()}

import React from 'react';

export default function Stats({ data }) {
    return <div>{data.n}</div>;
}

def helper():
    return 41 + 1
"""

STATIC_PAGE = """\
import React from 'react';

export default function About() {
    return <p>About</p>;
}
"""


@pytest.fixture
def project(tmp_path: Path) -> DevServerSettings:
    root = tmp_path / "project"
    (root / "pages").mkdir(parents=True)
    (root / "public").mkdir()
    settings = DevServerSettings.from_project_root(root)

    write_file(settings.pages_dir / "index.pyxl", LOADER_PAGE)
    write_file(settings.pages_dir / "self.pyxl", SELF_SUFFICIENT_PAGE)
    write_file(settings.pages_dir / "stats.pyxl", MULTI_SEGMENT_PAGE)
    write_file(settings.pages_dir / "about.pyxl", STATIC_PAGE)

    build_once(settings)
    return settings


def write_file(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


@pytest.fixture
def module_keys():
    """Track imported module keys and evict them from ``sys.modules`` after."""
    keys: list[str] = []
    yield keys
    for key in keys:
        sys.modules.pop(key, None)


def _import(settings: DevServerSettings, keys: list[str], page: str, *, debug: bool):
    key = f"pyxle_debug_import_test_{page}_{'debug' if debug else 'plain'}"
    keys.append(key)
    module_path = settings.server_build_dir / "pages" / f"{page}.py"
    return _import_module(key, module_path, debug=debug)


def _pyxl_line(settings: DevServerSettings, page: str, needle: str) -> int:
    source = (settings.pages_dir / f"{page}.pyxl").read_text(encoding="utf-8")
    return source.splitlines().index(needle) + 1


def test_debug_import_binds_loader_to_pyxl_source(project, module_keys) -> None:
    module = _import(project, module_keys, "index", debug=True)

    pyxl_path = (project.pages_dir / "index.pyxl").resolve()
    code = module.load_home.__code__
    assert code.co_filename == str(pyxl_path)
    # A decorated function's code object starts at the decorator line — which
    # must be the decorator's line in the .pyxl, not in the generated .py
    # (where injected imports shift everything down).
    assert code.co_firstlineno == _pyxl_line(project, "index", "@server")


def test_debug_import_tracebacks_reference_the_pyxl(project, module_keys) -> None:
    module = _import(project, module_keys, "index", debug=True)

    pyxl_path = (project.pages_dir / "index.pyxl").resolve()
    with pytest.raises(ValueError, match="kaboom") as excinfo:
        module.boom()

    frames = traceback.extract_tb(excinfo.tb)
    boom_frame = next(frame for frame in frames if frame.name == "boom")
    assert boom_frame.filename == str(pyxl_path)
    assert boom_frame.lineno == _pyxl_line(
        project, "index", '    raise ValueError("kaboom")'
    )


def test_production_import_also_binds_to_pyxl_source(project, module_keys) -> None:
    """Remapping is NOT a debug-only affordance.

    This assertion used to be the opposite — production kept the generated
    ``dist/server/pages/index.py`` path. That was wrong in the place it costs
    most: production sanitises its error responses, so the server log is the
    only record of a failure, and it pointed the reader at a generated artifact
    that may not exist on their machine, at a line they did not write.
    """
    module = _import(project, module_keys, "index", debug=False)

    pyxl_path = (project.pages_dir / "index.pyxl").resolve()
    assert module.load_home.__code__.co_filename == str(pyxl_path)


def test_production_import_falls_back_when_pyxl_is_not_deployed(
    project, module_keys
) -> None:
    """A dist-only deploy ships no ``.pyxl``, and must behave exactly as before.

    This is the guarantee that makes the change above safe to ship: with no
    source on disk there is nothing to remap to, so the import degrades to the
    generated module rather than failing or naming a path that does not exist.
    """
    (project.pages_dir / "index.pyxl").unlink()

    module = _import(project, module_keys, "index", debug=False)

    generated = project.server_build_dir / "pages" / "index.py"
    assert module.load_home.__code__.co_filename == str(generated)


def test_production_traceback_names_the_authors_line(project, module_keys) -> None:
    """The symptom P2-24 was filed for: the frame an on-call reader actually sees.

    Asserts the traceback, not just ``co_filename``, because the frame is what
    ends up in the log.
    """
    import traceback

    from pyxle.ssr.view import _import_server_module

    key = "pyxle_debug_import_test_prod_tb_index"
    module_keys.append(key)
    module_path = project.server_build_dir / "pages" / "index.py"
    module = _import_server_module(key, module_path, debug=False)

    try:
        module.boom()
    except ValueError as exc:
        frames = traceback.extract_tb(exc.__traceback__)
    else:  # pragma: no cover - the fixture page raises
        raise AssertionError("boom() was expected to raise")

    pyxl_path = (project.pages_dir / "index.pyxl").resolve()
    boom_frame = next(frame for frame in frames if frame.name == "boom")
    assert boom_frame.filename == str(pyxl_path)
    assert boom_frame.lineno == _pyxl_line(
        project, "index", '    raise ValueError("kaboom")'
    )


def test_debug_import_maps_page_with_user_owned_runtime_imports(
    project, module_keys
) -> None:
    """A page that imports its runtime names itself gets zero injected lines —
    the map must still hold, with every line at its exact .pyxl position."""
    module = _import(project, module_keys, "self", debug=True)

    pyxl_path = (project.pages_dir / "self.pyxl").resolve()
    code = module.load_page.__code__
    assert code.co_filename == str(pyxl_path)
    assert code.co_firstlineno == _pyxl_line(project, "self", "@server")


def test_debug_import_maps_both_python_segments(project, module_keys) -> None:
    """A python|jsx|python page concatenates two Python segments; a function
    in the second segment must resolve to its true .pyxl line, past the JSX."""
    module = _import(project, module_keys, "stats", debug=True)

    pyxl_path = (project.pages_dir / "stats.pyxl").resolve()
    loader_code = module.load_stats.__code__
    helper_code = module.helper.__code__
    assert loader_code.co_filename == str(pyxl_path)
    assert helper_code.co_filename == str(pyxl_path)
    assert loader_code.co_firstlineno == _pyxl_line(project, "stats", "@server")
    assert helper_code.co_firstlineno == _pyxl_line(project, "stats", "def helper():")
    assert module.helper() == 42


def test_debug_import_static_stub_needs_no_remap(project, module_keys) -> None:
    """A JSX-only page compiles to a footer-less static stub — debug mode
    imports it through the stock loader path unchanged."""
    module = _import(project, module_keys, "about", debug=True)

    assert module.__file__ == str(project.server_build_dir / "pages" / "about.py")
    # No footer means the module never carries the map dunders at runtime.
    assert not hasattr(module, "__pyxle_source__")
    assert not hasattr(module, "__pyxle_line_map__")


def test_debug_import_falls_back_when_pyxl_deleted(project, module_keys) -> None:
    """Deleting the .pyxl after a build (e.g. a stale artifact) degrades to
    importing the generated module under its own path — never a hard failure."""
    (project.pages_dir / "index.pyxl").unlink()

    module = _import(project, module_keys, "index", debug=True)

    generated = project.server_build_dir / "pages" / "index.py"
    assert module.load_home.__code__.co_filename == str(generated)


def test_ssr_view_importer_remaps_in_debug_mode(project, module_keys) -> None:
    """The SSR view's importer shares the debug-loader wiring — loaders
    resolved for page rendering must also bind to the .pyxl."""
    from pyxle.ssr.view import _import_server_module

    key = "pyxle_debug_import_test_view_index"
    module_keys.append(key)
    module_path = project.server_build_dir / "pages" / "index.py"
    module = _import_server_module(key, module_path, debug=True)

    pyxl_path = (project.pages_dir / "index.pyxl").resolve()
    assert module.load_home.__code__.co_filename == str(pyxl_path)
