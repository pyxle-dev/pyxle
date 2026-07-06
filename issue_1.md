# `pyxle check` fails on clean install due to missing site-packages path in `jsx_parser.py`

## Description
When installing Pyxle in a clean virtual environment (e.g., via `pip install pyxle-framework[langkit]`), running the `pyxle check` CLI command raises an error stating that the JSX checker is unavailable, even when `pyxle-langkit` is successfully installed.

**Error output:**
```
  error: [jsx] line 1: JSX syntax error: JSX checker unavailable: the language toolkit isn't installed. Install it with `pip install 'pyxle-framework[langkit]'` (it also needs @babel/parser and @babel/traverse available to Node).
```

## Root Cause
The Node.js extractor script location is hardcoded in `pyxle/compiler/jsx_parser.py`. The `_js_bases` tuple attempts to resolve the script from relative sibling locations (like a monorepo development structure), but it does not check standard `site-packages` directories where `pip` installs the `pyxle_langkit` module.

```python
    # _run_babel_parser in pyxle/compiler/jsx_parser.py
    _js_bases = (
        Path(__file__).parent.parent / "pyxle_langkit" / "js",
        Path(__file__).parent.parent.parent / "pyxle_langkit" / "js",
        Path(__file__).resolve().parent.parent.parent.parent / "pyxle-langkit" / "pyxle_langkit" / "js",
    )
```

## Solution
To correctly locate the toolkit when installed via pip, the resolver should use Python's `importlib` or standard module path resolution to find `pyxle_langkit`.

For example:
```python
    _js_bases = [ ... existing paths ... ]
    try:
        import pyxle_langkit
        _js_bases.append(Path(pyxle_langkit.__file__).parent / "js")
    except ImportError:
        pass
```

## Reproduction
1. Create a clean virtual environment.
2. Run `pip install -e .[dev,test]` and `pip install pyxle-framework[langkit]`
3. Run `pytest tests/cli/test_commands.py::test_check_command_succeeds_on_valid_project` or create a dummy project and run `pyxle check`.
4. The test fails or the CLI exits with the JSX parser unavailable error.
