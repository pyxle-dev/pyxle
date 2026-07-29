# Bug: `isinstance(..., int)` allows `bool` values, bypassing type checks

### Description
In Python, `bool` is a subclass of `int`. This means that `isinstance(True, int)` evaluates to `True`.
There are several places in the codebase where `isinstance(value, int)` is used without explicitly rejecting `bool` values. Since JSON can represent booleans (`true` / `false`), a malformed JSON payload containing a boolean where an integer is expected will pass the type check but may crash downstream logic that strictly expects integers, or behave unexpectedly.

In some parts of the codebase, this is handled correctly (e.g., `not isinstance(value, int) or isinstance(value, bool)` in `pyxle/config.py`). However, other modules miss this secondary check.

### Instances found
1. **`pyxle/devserver/registry.py`**:
   - `loader_line` parsing:
     ```python
     loader_line = payload.get("loader_line")
     if not isinstance(loader_line, int):  # Allows bool
         loader_line = None
     ```
   - `websocket_line` parsing:
     ```python
     websocket_line = payload.get("websocket_line")
     if not isinstance(websocket_line, int):  # Allows bool
         websocket_line = None
     ```
     *Fix:* `if not isinstance(..., int) or isinstance(..., bool):`

2. **`pyxle/compiler/jsx_parser.py`**:
   - `error_line` parsing:
     ```python
     error_line = payload.get("line")
     return JSXParseResult(
         ...
         error_line=error_line if isinstance(error_line, int) else None,  # Allows bool
     )
     ```
     *Fix:* `error_line=error_line if isinstance(error_line, int) and not isinstance(error_line, bool) else None`

3. **`pyxle/config.py`**:
   - `cors.maxAge` validation:
     ```python
     max_age = value.get("maxAge", 600)
     if not isinstance(max_age, int) or max_age < 0:  # Allows bool
         raise ConfigError(...)
     ```
     *Fix:* `if not isinstance(max_age, int) or isinstance(max_age, bool) or max_age < 0:`

4. **`pyxle/devserver/csrf.py`**:
   - `_extract_port` method:
     ```python
     return port if isinstance(port, int) else None # Allows bool
     ```
     *Fix:* `return port if isinstance(port, int) and not isinstance(port, bool) else None`

### Impact
This could result in configuration bugs, dev server registry loading failures, or incorrect line numbers in error handling when boolean values are inadvertently provided instead of integers.
