# Plugins

Pyxle plugins are reusable pieces of functionality — database layers, auth systems, storage adapters — that a host app composes via `pyxle.config.json` rather than hand-wiring in its entry point. The design is deliberately Django-inspired: declare what you want, and the framework handles discovery, startup order, and lifecycle.

> **Status (Phase A — stable since 0.5.0).** This guide covers the Phase A plugin surface: lifecycle hooks, named services, and middleware contribution. Page contribution (plugins shipping their own `.pyxl` pages that get merged into the host's route tree — the "pyxle-auth ships a default `/sign-in` page" use case) is a Phase B follow-up, still in [RFC](../plugins/rfc-plugin-pages.md). Everything documented here is stable; the additions in Phase B will be strictly additive.

## TL;DR

Declare plugins in `pyxle.config.json`:

```json
{
  "plugins": [
    "pyxle-db",
    {"name": "pyxle-auth", "settings": {"cookieDomain": ".example.com"}}
  ]
}
```

Pyxle imports each plugin, calls its `on_startup` hook inside the ASGI lifespan, and gives your app three ways to reach the services plugins register. Pick whichever reads best — they all resolve to the same object.

```python
# 1. Plugin-provided helper (recommended — typed return)
from pyxle_auth import get_auth_service

@server
async def load(request):
    auth = get_auth_service()
    user = await auth.resolve_session(cookie_value=request.cookies.get("pyxle_session", ""))
    return {"user": user}
```

```python
# 2. Generic shortcut — works for any service the registry holds
from pyxle.plugins import plugin

@server
async def load(request):
    auth = plugin("auth.service")
    telemetry = plugin("telemetry.client", None)  # optional; returns None if absent
```

```python
# 3. Long form — useful in middleware that already has the request
@server
async def load(request):
    auth = request.app.state.pyxle_plugins.require("auth.service")
```

All three coexist. The plugin-provided helpers (option 1) are preferred for app code because they type-annotate the return value; option 2 is there for ad-hoc access; option 3 is the fundamental mechanism.

## Config schema

Each entry in `plugins` is either a **string** or an **object**:

```json
{
  "plugins": [
    "pyxle-db",
    {
      "name": "pyxle-auth",
      "module": "pyxle_auth.plugin",
      "attribute": "plugin",
      "settings": {"cookieDomain": ".pyxle.app"}
    }
  ]
}
```

| Field | Required | Default | Description |
|---|---|---|---|
| `name` | ✅ | — | User-facing name. Used for service-namespace identification and error messages. |
| `module` | — | `{name.replace('-', '_')}.plugin` | Python module where the plugin object lives. |
| `attribute` | — | `"plugin"` | Attribute on `module` to fetch. A `PyxlePlugin` subclass is instantiated; an instance is used as-is. |
| `settings` | — | `{}` | Per-app config dict forwarded to the plugin instance via `self.settings`. |

The string form is sugar for `{"name": "...", "module": "..._plugin.plugin"}`. Both sugar and object form work side-by-side.

## Authoring a plugin

### Minimum viable plugin

A plugin is a regular Python package that exports a `PyxlePlugin` subclass (or instance) named `plugin`:

```python
# pyxle_hello/plugin.py
from pyxle.plugins import PyxlePlugin, PluginContext


class HelloService:
    def greet(self, name: str) -> str:
        return f"Hello, {name}!"


class Plugin(PyxlePlugin):
    name = "pyxle-hello"
    version = "0.1.0"

    async def on_startup(self, ctx: PluginContext) -> None:
        # Register services under a short namespace so consumers can
        # ``plugin("hello.service")``.
        ctx.register("hello.service", HelloService())

    async def on_shutdown(self, ctx: PluginContext) -> None:
        # Flush, close connections, stop background tasks.
        pass


# Either export the class (Pyxle instantiates it) or an instance:
plugin = Plugin
```

### Ship a typed import helper

Every plugin should ship a one-liner helper so consumers can import instead of reaching into the registry:

```python
# pyxle_hello/__init__.py
from pyxle_hello.plugin import HelloService


def get_hello() -> HelloService:
    """Return the active pyxle-hello service.

    Requires ``pyxle-hello`` to be listed in ``pyxle.config.json::plugins``.
    """
    from pyxle.plugins import plugin as _plugin
    return _plugin("hello.service")
```

Consumer code then writes `from pyxle_hello import get_hello` — typed, short, idiomatic. See the `pyxle-db` and `pyxle-auth` helpers for real-world examples.

### Handling plugin settings

Settings arrive as a dict from the host app's `pyxle.config.json`. The framework doesn't validate the shape — your plugin should. A simple pattern:

```python
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class HelloConfig:
    prefix: str = "Hello"
    shout: bool = False

    @classmethod
    def from_user_settings(cls, raw: dict) -> "HelloConfig":
        known = {"prefix", "shout"}
        unknown = set(raw) - known
        if unknown:
            raise ValueError(
                f"pyxle-hello: unknown settings keys: {sorted(unknown)}. "
                f"Supported: {sorted(known)}."
            )
        return cls(
            prefix=str(raw.get("prefix", "Hello")),
            shout=bool(raw.get("shout", False)),
        )


class Plugin(PyxlePlugin):
    name = "pyxle-hello"

    async def on_startup(self, ctx: PluginContext) -> None:
        config = HelloConfig.from_user_settings(dict(self.settings or {}))
        ctx.register("hello.service", HelloService(config))
```

The "unknown keys" check is important — a typo like `"prefxi": "Hi"` would otherwise silently fall through to the default and leave the user wondering why their setting isn't taking effect. Fail loud at startup, not at runtime.

### Declaring a dependency on another plugin

Plugins declare dependencies in two places:

1. **In `pyproject.toml`** — so pip installs the dep alongside your plugin.
2. **In `on_startup` at service-lookup time** — plus a clear error if the required service isn't present (which means the host app forgot to list the dependency in `plugins`, or listed them in the wrong order).

```python
from pyxle.plugins import PluginServiceError


class Plugin(PyxlePlugin):
    name = "pyxle-my-cache"

    async def on_startup(self, ctx: PluginContext) -> None:
        try:
            db = ctx.require("db.database")
        except PluginServiceError as exc:
            raise PluginServiceError(
                "pyxle-my-cache requires 'db.database' from pyxle-db — "
                "list \"pyxle-db\" BEFORE \"pyxle-my-cache\" in "
                "pyxle.config.json::plugins."
            ) from exc
        ctx.register("cache.store", MyCacheStore(db))
```

Phase B will introduce declarative dependencies so the framework can topo-sort plugins for you. For now, order is manual.

### Environment variables

Plugins that read environment variables should document the contract clearly in their README. There's no framework-level env-var declaration yet — Phase B adds `env_vars: Sequence[EnvVarSpec]` for `pyxle doctor`-style validation.

### Testing a plugin

The cleanest pattern mirrors the framework's own tests: drive the plugin through its real public API (`PluginSpec.from_config_entry` → `load_plugins` → `run_startup`) rather than unit-testing internals. Tests look the same whether you're writing a plugin or consuming one.

```python
# tests/test_plugin.py
import pytest
from pyxle.plugins import (
    PluginContext, PluginSpec, load_plugins, run_startup, run_shutdown,
)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_service_is_registered_on_startup() -> None:
    spec = PluginSpec.from_config_entry("pyxle-hello")
    plugins = load_plugins([spec])
    ctx = PluginContext()
    await run_startup(plugins, ctx)
    try:
        svc = ctx.require("hello.service")
        assert svc.greet("Alice") == "Hello, Alice!"
    finally:
        await run_shutdown(plugins, ctx)


@pytest.mark.anyio
async def test_import_helper_resolves_via_active_context() -> None:
    # The module-level ``plugin(name)`` / helper path requires an
    # installed active context. ``set_active_context`` is the public
    # hook for tests.
    from pyxle.plugins import set_active_context
    from pyxle_hello import get_hello

    spec = PluginSpec.from_config_entry("pyxle-hello")
    plugins = load_plugins([spec])
    ctx = PluginContext()
    await run_startup(plugins, ctx)
    set_active_context(ctx)
    try:
        assert get_hello() is ctx.require("hello.service")
    finally:
        set_active_context(None)
        await run_shutdown(plugins, ctx)
```

### Packaging + publishing

Ship your plugin as a normal Python package:

```toml
# pyproject.toml
[project]
name = "pyxle-hello"
version = "0.1.0"
dependencies = ["pyxle-framework>=0.3.0"]

[tool.hatch.build.targets.wheel]
packages = ["pyxle_hello"]
```

`pip install pyxle-hello` and list `"pyxle-hello"` in a host app's `pyxle.config.json::plugins` — that's the whole distribution story. No entry points or registration dance.

## Lifecycle

```
startup order:  A → B → C   (declaration order from pyxle.config.json)
shutdown order: C → B → A   (reverse — so earlier plugins' services
                             are still available during later teardown)
```

Startup failures abort the ASGI app immediately — a plugin that can't reach its database should raise, and Pyxle propagates it so Starlette refuses to serve traffic. Shutdown failures are logged but don't abort teardown of the remaining plugins.

## Consuming services from your app

### Option 1: plugin-provided import helpers (preferred)

Official plugins expose a typed helper function that reaches into the registry for you:

```python
from pyxle_db import get_database
from pyxle_auth import get_auth_service, get_auth_settings

@server
async def load(request):
    db = get_database()                 # -> Database
    auth = get_auth_service()           # -> AuthService
    settings = get_auth_settings()      # -> AuthSettings
    ...
```

This is the closest Pyxle gets to Django's `from django.contrib.auth import authenticate` pattern. Third-party plugins should ship similar helpers — it's one extra line of code for a much nicer consumer experience.

### Option 2: generic `plugin(name)` shortcut

For services that don't have a typed helper (or ad-hoc access to the registry):

```python
from pyxle.plugins import plugin

@server
async def load(request):
    auth = plugin("auth.service")
    telemetry = plugin("telemetry.client", None)  # default avoids raising
```

`plugin(name)` raises `PluginServiceError` when the name isn't registered and no default is provided. Error messages list every registered name, so typos are obvious at first request rather than silently returning `None`.

### Option 3: long form via the request

Useful inside middleware that already has the `request` and wants to stay context-pure (no module-level state reads):

```python
@server
async def load(request):
    ctx = request.app.state.pyxle_plugins
    db = ctx.require("db.database")
    rows = await db.fetchall("SELECT * FROM posts LIMIT 20")
    return {"posts": [dict(r) for r in rows]}
```

### When to use which

- **App loaders and actions** → option 1 (or 2 if no helper exists). Short and typed.
- **Middleware, route hooks, request-scoped helpers** → option 3. Avoids the module-level state read, which makes the middleware easier to test in isolation.
- **CLI scripts or tests calling plugin code outside an ASGI context** → install the context manually with `pyxle.plugins.set_active_context(ctx)` and then any of the three forms work.

## Middleware contribution

A plugin can declare ASGI middleware it wants appended to the host app's stack:

```python
class Plugin(PyxlePlugin):
    name = "pyxle-requestid"

    def middleware(self):
        return [
            ("pyxle_requestid.middleware:RequestIdMiddleware", {"header": "x-request-id"}),
        ]
```

Each entry is `(import_string, options)`:

- **Import string** — either `"package.module:Class"` (Starlette-convention form) or `"package.module.Class"` (plain dotted form). Both resolve the same way.
- **Options** — a dict of keyword arguments passed through as `Middleware(cls, **options)`, matching the shape of `pyxle.config.json::middleware`.

### Stack position

Plugin-contributed middleware sits **between** the host app's `custom_middleware` (from `pyxle.config.json::middleware`) and the Vite proxy. In order from outermost to innermost the stack looks like:

```
security headers (prod only)
GZip (prod only)
CORS
CSRF
static files
host app custom middleware
▶ plugin middleware (in plugin declaration order)
Vite proxy (dev only)
Pyxle app
```

A plugin that adds request-id middleware therefore sees requests *after* CSRF has validated them but *before* the Pyxle router dispatches, which is usually what you want. Plugins that genuinely need to run before CSRF (e.g. a plugin handling webhooks with a different validation scheme) should register an exempt path via `pyxle.config.json::csrf.exemptPaths` rather than trying to reorder middleware.

### When to use middleware vs. route hooks

- **Middleware** — cross-cutting request transforms (request IDs, rate-limit headers, metrics). Runs on *every* request.
- **Route hooks** — per-route decoration (auth check on a subset of routes). Declared in `pyxle.config.json::routeMiddleware` by host apps, not plugins today.

A plugin like `pyxle-auth` deliberately *doesn't* install middleware — it ships a service the host app consumes inside its own loaders. That way apps stay in control of *which* routes require auth, and the plugin doesn't gate traffic the app owner didn't opt into.

## Service naming conventions

| Namespace | Owner |
|---|---|
| `db.*` | `pyxle-db` |
| `auth.*` | `pyxle-auth` |
| `<plugin-short-name>.*` | Each official plugin gets its own prefix. |
| `app.*` | Reserved for the host app's own ad-hoc registrations. |

Plugins should document every name they register so consumers know what to ask for.

## Error handling

| Condition | Exception |
|---|---|
| Bad config entry (missing name, non-list, etc.) | `ConfigError` at load time |
| Module can't be imported | `PluginResolutionError` at startup |
| Attribute is missing / wrong type | `PluginResolutionError` at startup |
| `on_startup` raises | `PluginError` wrapping the original — ASGI startup aborts |
| `require()` misses | `PluginServiceError` at request time |

## Troubleshooting

### `PluginResolutionError: Plugin 'pyxle-foo' could not import module 'pyxle_foo.plugin'`

The package isn't installed, the module path is wrong, or the plugin author forgot to create `pyxle_foo/plugin.py`.

Checklist:
1. `pip show pyxle-foo` — is the package actually installed in this interpreter?
2. `python -c "import pyxle_foo.plugin"` — does the module import cleanly outside the devserver?
3. If the package uses a non-standard module layout, set `module` explicitly on the config entry:
   ```json
   {"name": "pyxle-foo", "module": "pyxle_foo.contrib.plugin"}
   ```

### `PluginResolutionError: attribute 'plugin' is a class but doesn't subclass PyxlePlugin`

The package exports a `plugin` attribute that isn't a `PyxlePlugin` subclass. Either the plugin author made a typo or you're pointing at the wrong attribute. Override `attribute` on the config entry if the plugin uses a non-standard export name:

```json
{"name": "pyxle-foo", "attribute": "FooPlugin"}
```

### `PluginServiceError: Service 'xxx' not registered. Available: ['...', '...']`

The listed available names are the full registry snapshot. Common causes:

- **Typo.** Compare letter-by-letter with the available list.
- **Wrong order.** `pyxle-auth` requires `pyxle-db` before it in the config list.
- **Plugin not installed.** The helper import succeeded but the plugin isn't in `plugins`.

### Plugin startup raises `PluginError: Plugin 'foo' on_startup failed: <original>`

Look at the wrapped `<original>` — that's the plugin's real failure (DB not reachable, API key invalid, schema drift, etc). Fix that; the `PluginError` layer is just framing.

### `plugin(name)` raises "No active plugin context" in a test

You're calling the helper outside an ASGI request. In tests, install a context manually:

```python
from pyxle.plugins import PluginContext, set_active_context

ctx = PluginContext()
ctx.register("my.service", FakeService())
set_active_context(ctx)
try:
    # ... code under test that calls plugin("my.service") or a helper
finally:
    set_active_context(None)
```

### Changing `pyxle.config.json::plugins` doesn't take effect

Pyxle resolves the plugins list at devserver startup, not per-request. Restart `pyxle dev` (or your production server) after editing the plugins list.

## Limitations (Phase A)

- Plugins cannot contribute their own `.pyxl` pages yet. To ship a default sign-in page today, the plugin author provides the source string and the host app imports it — a Phase B feature will let plugins contribute a `pages_dir()` directly.
- Plugin migrations aren't auto-discovered. A plugin that needs schema changes should register a "run migrations" method the host app calls from its own startup code. Phase B adds `migrations_dir()` + auto-application.
- Plugins cannot depend on each other by declaring it explicitly. Ordering is manual via the config list — put `pyxle-db` before `pyxle-auth` because auth depends on a database.

Phase B will address all three. Phase A ships now because the lifecycle + service registry alone already removes most of the hand-wiring in host apps.

## See also

- [Plugins API reference](../reference/plugins-api.md) — full signatures for `PyxlePlugin`, `PluginContext`, `plugin(name)`, and error types.
- [Configuration reference](../reference/configuration.md) — `pyxle.config.json::plugins` schema.
- [pyxle-db](../plugins/pyxle-db.md) — first-party SQLite plugin.
- [pyxle-auth](../plugins/pyxle-auth.md) — first-party auth plugin.
- [Middleware](middleware.md) — app-level middleware, for the `pyxle.config.json::middleware` array.
- [Runtime API](../reference/runtime-api.md) — `@server` / `@action` reference for consumers.
