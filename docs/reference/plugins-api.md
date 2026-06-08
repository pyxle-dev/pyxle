# Plugins API Reference

Every public symbol in `pyxle.plugins`, including the access helpers introduced alongside the plugin system.

Importing:

```python
from pyxle.plugins import (
    PyxlePlugin,
    PluginContext,
    PluginSpec,
    plugin,
    active_context,
    set_active_context,
    load_plugins,
    run_startup,
    run_shutdown,
    PluginError,
    PluginResolutionError,
    PluginServiceError,
)
```

For the user-facing guide, see [Plugins](../guides/plugins.md). This page is the reference.

---

## `PyxlePlugin`

Abstract base class plugins subclass to become discoverable. Nothing is abstract on the Python level — every hook has a no-op default, so a minimal subclass is:

```python
class Plugin(PyxlePlugin):
    name = "pyxle-hello"
    version = "0.1.0"

    async def on_startup(self, ctx: PluginContext) -> None:
        ctx.register("hello.service", HelloService())
```

### Class attributes

| Attribute | Type | Default | Description |
|---|---|---|---|
| `name` | `str` | `"unnamed"` | User-facing plugin name. Used in error messages and for service namespacing. |
| `version` | `str` | `"0.0.0"` | Informational. Pyxle doesn't enforce versioning today. |

### Instance attributes

| Attribute | Type | Description |
|---|---|---|
| `self.settings` | `Mapping[str, Any]` | Populated by the loader after construction. Matches the `settings` object from the entry in `pyxle.config.json::plugins`. Empty dict when the user didn't provide one. |

### Methods

#### `async on_startup(ctx: PluginContext) -> None`

Called once at ASGI lifespan startup. Typical work: open long-lived connections, run migrations, register services. Raising here aborts ASGI boot (Starlette refuses to serve traffic), which is the right posture for "pyxle-db can't reach the database" scenarios.

```python
async def on_startup(self, ctx: PluginContext) -> None:
    self._client = await create_http_client()
    ctx.register("telemetry.client", self._client)
```

#### `async on_shutdown(ctx: PluginContext) -> None`

Called once at graceful shutdown. Runs in *reverse* plugin-declaration order so earlier plugins' services are still available when later plugins tear down. Raising here is logged but doesn't abort shutdown of remaining plugins — best-effort by design.

#### `middleware() -> Sequence[tuple[str, Mapping[str, Any]]]`

Returns middleware specs to append to the host app's stack. Each entry is `(import_string, options)`:

```python
def middleware(self) -> Sequence[tuple[str, Mapping[str, Any]]]:
    return [
        ("pyxle_requestid.middleware:RequestIdMiddleware", {"header": "x-request-id"}),
    ]
```

Default: empty tuple.

See the [Middleware contribution](../guides/plugins.md#middleware-contribution) section of the guide for stack-position details.

---

## `PluginContext`

Per-process lifecycle context shared across plugins. Plugins register named services here; loaders / actions retrieve them via `request.app.state.pyxle_plugins` or the module-level [`plugin(name)`](#pluginname-defaultmissing) helper.

### Constructor

```python
PluginContext(*, settings: Any = None)
```

`settings` is typically the host app's resolved `DevServerSettings`. Exposed on [`self.settings`](#ctx-settings) so plugins that need the host's project root, debug flag, etc. can read it.

### Methods

#### `register(name: str, service: Any) -> None`

Register a service under `name`. Raises `PluginServiceError` if the name is already registered or empty. Use [`replace`](#replacename-str-service-any-none) to deliberately overwrite.

```python
ctx.register("db.database", database)
```

#### `replace(name: str, service: Any) -> None`

Overwrite an existing registration (or register a new one). Never raises on an existing key. Use for host apps that want to substitute plugin services in tests or for plugins that supersede each other intentionally.

#### `get(name: str, default: Any = None) -> Any`

Return the service or `default` if absent. Non-raising.

```python
telemetry = ctx.get("telemetry.client")  # None if no telemetry plugin installed
```

#### `require(name: str) -> Any`

Return the service or raise `PluginServiceError`. The error message lists every registered name so typos are obvious from the traceback alone.

```python
auth = ctx.require("auth.service")
```

#### `has(name: str) -> bool`

Cheap membership check without side effects.

#### `names() -> tuple[str, ...]`

Snapshot of registered service keys, sorted. Useful in logs and admin pages.

### Properties

<a id="ctx-settings"></a>

#### `settings`

The host app's `DevServerSettings` (or whatever was passed to the constructor). Plugins access `ctx.settings.project_root`, `ctx.settings.debug`, etc.

---

## `plugin(name, default=MISSING)`

Django-style short form for `active_context().require(name)` / `active_context().get(name, default)`.

```python
from pyxle.plugins import plugin

@server
async def load(request):
    auth = plugin("auth.service")          # raises if missing
    telemetry = plugin("telemetry", None)  # returns None if absent
```

Resolves via the module-level active context that the devserver installs at ASGI startup. For use **inside** an ASGI request (loader, action, API endpoint). For use **outside** an ASGI context (tests, scripts), install a context manually via [`set_active_context`](#set_active_contextctx-plugincontext-none-none).

Raises `PluginServiceError` if:
- No active context has been installed (typical cause: calling from outside an ASGI request).
- The service `name` is not registered and no `default` was provided.

Prefer [plugin-provided helpers](../guides/plugins.md#option-1-plugin-provided-import-helpers-preferred) (like `get_auth_service()`) when they exist — they type-annotate their return values, which the generic helper cannot.

---

## `active_context() -> PluginContext`

Return the currently-installed module-level context. Raises `PluginServiceError` if none has been installed.

Most app code uses [`plugin(name)`](#pluginname-defaultmissing) instead of touching the context directly. Reach for `active_context()` when you need the full `PluginContext` object (e.g. to iterate `names()` for a diagnostics page).

---

## `set_active_context(ctx: PluginContext | None) -> None`

Install or clear the module-level context. Pyxle's devserver calls this inside the ASGI lifespan; library code shouldn't normally need to. Tests use it to stand up a clean registry outside the devserver:

```python
from pyxle.plugins import PluginContext, set_active_context

ctx = PluginContext()
ctx.register("my.service", FakeService())
set_active_context(ctx)
try:
    # ... code under test that calls plugin(...) or a plugin-provided helper
finally:
    set_active_context(None)
```

Passing `None` clears the active context, returning subsequent `plugin(...)` calls to the "no active context" error state.

Kept as an explicit function (not a module-level variable) so a future migration to `contextvars.ContextVar` is a drop-in replacement.

---

## `PluginSpec`

Resolved plugin entry from `pyxle.config.json`. Frozen dataclass.

### Fields

| Field | Type | Default | Description |
|---|---|---|---|
| `name` | `str` | — | User-facing name (e.g. `"pyxle-auth"`). |
| `module` | `str` | derived | Python module containing the plugin — default derived from `name` by replacing `-` with `_` and appending `.plugin`. |
| `attribute` | `str` | `"plugin"` | Attribute on `module` that holds the `PyxlePlugin` class or instance. |
| `settings` | `Mapping[str, Any]` | `{}` | Per-app configuration dict forwarded to the plugin. |

### Class methods

#### `PluginSpec.from_config_entry(entry, *, source="pyxle.config.json") -> PluginSpec`

Parse one item from a `plugins` config list. Accepts either a bare string or an object:

```python
PluginSpec.from_config_entry("pyxle-auth")
# PluginSpec(name='pyxle-auth', module='pyxle_auth.plugin', attribute='plugin', settings={})

PluginSpec.from_config_entry({
    "name": "pyxle-auth",
    "settings": {"cookieDomain": ".pyxle.app"},
})
```

Raises `PluginError` for malformed entries (empty string, missing `name`, wrong types).

---

## `load_plugins(specs) -> tuple[PyxlePlugin, ...]`

Resolve each `PluginSpec` to a `PyxlePlugin` instance. The host app's settings are propagated onto `instance.settings` so the plugin's hooks can read them.

```python
specs = [PluginSpec.from_config_entry(entry) for entry in config.plugins]
plugins = load_plugins(specs)
```

Failure modes:

- `PluginResolutionError` — module import failed, attribute missing, or attribute is a class that doesn't subclass `PyxlePlugin`.

---

## `async run_startup(plugins, ctx) -> None`

Call `on_startup` on each plugin in declaration order.

```python
await run_startup(plugins, ctx)
```

Failures propagate wrapped in `PluginError` with the underlying cause chained — ASGI startup aborts cleanly.

---

## `async run_shutdown(plugins, ctx) -> None`

Call `on_shutdown` on each plugin in **reverse** declaration order. Best-effort: failures are logged but don't interrupt the rest of the teardown.

```python
await run_shutdown(plugins, ctx)
```

---

## Errors

### `PluginError`

Base class for every plugin-system failure. Catch this in generic error boundaries; catch the more specific subclasses below when you want to branch on the failure mode.

### `PluginResolutionError`

Subclass of `PluginError`. Raised by `load_plugins` when:

- The declared module can't be imported (package missing, import-time crash).
- The declared attribute is missing.
- The declared attribute is a class that doesn't subclass `PyxlePlugin`.
- The declared attribute is neither a `PyxlePlugin` subclass nor an instance.

### `PluginServiceError`

Subclass of `PluginError`. Raised by:

- `PluginContext.register` on empty or duplicate names.
- `PluginContext.require` on missing names.
- `plugin(name)` when no active context is installed, or when the name is missing and no default was provided.

---

## See also

- [Plugins guide](../guides/plugins.md) — authoring walkthrough + consuming patterns.
- [Configuration reference](configuration.md) — `pyxle.config.json::plugins` schema.
- [pyxle-db](../plugins/pyxle-db.md) — first-party SQLite plugin.
- [pyxle-auth](../plugins/pyxle-auth.md) — first-party auth plugin.
