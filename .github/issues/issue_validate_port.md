# Bug Report: `_validate_port` accepts booleans as valid integer ports

## Description
In `pyxle/config.py`, the `_validate_port` function checks if the port value is an integer using `isinstance(value, int)`. However, in Python, `bool` is a subclass of `int`. As a result, if a user configures a boolean value like `True` or `False` for a port (e.g., `starlette.port`), the check passes because `True` evaluates to `1` and `False` evaluates to `0`.

While `0` is caught by `value <= 0`, `True` bypasses the check entirely and evaluates to `1`. This means a port configuration like `{"port": True}` will silently start the server on port `1`, which requires root privileges and is definitely unintended behavior, bypassing the expected configuration error.

## Affected Code
```python
# pyxle/config.py
def _validate_port(value: Any, key: str) -> int:
    if not isinstance(value, int):
        raise ConfigError(f"Invalid value for '{key}': expected integer port value.")
    if value <= 0 or value > 65535:
        raise ConfigError(f"Port for '{key}' must be between 1 and 65535 (got {value}).")
    return value
```

## Recommended Fix
Add a check to explicitly reject boolean values. This is already done elsewhere in `config.py` for other integer fields.

```python
<<<<<<< SEARCH
    if not isinstance(value, int):
=======
    if not isinstance(value, int) or isinstance(value, bool):
>>>>>>> REPLACE
```
