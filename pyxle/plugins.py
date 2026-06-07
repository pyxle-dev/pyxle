"""First-class plugin system for Pyxle apps.

Django-inspired. Instead of every app hand-wiring DB connections, auth
services, and middleware in its entry-point, plugins declare themselves
in ``pyxle.config.json``::

    {
      "plugins": [
        "pyxle-db",
        {"name": "pyxle-auth", "settings": {"cookieDomain": ".pyxle.app"}}
      ]
    }

Pyxle resolves each entry to a plugin instance, calls its ``on_startup``
hook at ASGI boot, and exposes a shared :class:`PluginContext` that the
host app's ``@server`` loaders and ``@action`` handlers can use to look
up plugin-provided services by name::

    @server
    async def load(request):
        auth = request.app.state.pyxle_plugins.require("auth.service")
        user = await auth.resolve_session(...)
        return {"user": user}

Design notes:

* **No import magic.** Plugin discovery is explicit via the config list
  — we never scan installed packages for plugins. This keeps surprises
  out of ``pyxle init`` and makes the loaded plugin set reproducible
  across environments.
* **Services over monkey-patching.** A plugin exposes capabilities by
  registering named services on the context. Host code asks for them
  by name. No global singletons, no patch-the-framework tricks.
* **Lifecycle is async-first.** ``on_startup`` / ``on_shutdown`` both
  run inside Starlette's lifespan events; they can ``await`` database
  connections, external APIs, etc.
* **Additive.** Apps without a ``plugins`` key behave exactly as before.
  Every change here is backward-compatible with 0.2.x projects.
"""

from __future__ import annotations

import importlib
from abc import ABC
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence


class PluginError(Exception):
    """Base class for plugin-system failures.

    Separate from the wider framework errors so callers that only care
    about plugin-config problems (e.g. an IDE surfacing issues from
    ``pyxle.config.json``) can catch narrowly.
    """


class PluginResolutionError(PluginError):
    """Raised when a plugin declared in config cannot be imported /
    instantiated. Message includes the offending name and the module
    path we tried so the failure is actionable from a stack trace alone.
    """


class PluginServiceError(PluginError):
    """Raised when a service-registry operation fails (duplicate
    registration, missing required lookup, etc.).
    """


# ---------------------------------------------------------------------------
# Config-level representation
#
# PluginSpec mirrors what a user writes in ``pyxle.config.json`` after
# normalising the sugar forms. Kept immutable so the framework can hand
# it to plugin code without worrying about mutation.


@dataclass(frozen=True, slots=True)
class PluginSpec:
    """Resolved plugin entry from ``pyxle.config.json``.

    Attributes:
        name: User-facing name. Matches the PyPI package minus any
            ``pyxle-`` prefix is convention but not enforced — any
            string is fine, it's only used for service-registry
            namespacing and error messages.
        module: Python module where the plugin instance lives. Default
            derived from ``name`` — ``pyxle-auth`` → ``pyxle_auth.plugin``.
            Users can override for non-conventional layouts.
        attribute: Attribute on ``module`` to fetch. Default ``plugin``.
            If the attribute is a subclass of :class:`PyxlePlugin` it's
            instantiated with no args; otherwise it's used as-is.
        settings: Free-form dict forwarded to the plugin instance via
            :attr:`PyxlePlugin.settings`. Plugins are responsible for
            validating their own shape.
    """

    name: str
    module: str
    attribute: str = "plugin"
    settings: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def from_config_entry(
        cls, entry: "str | Mapping[str, Any]", *, source: str = "pyxle.config.json"
    ) -> "PluginSpec":
        """Build a spec from one item of the ``plugins`` config list.

        Accepts either a bare string (``"pyxle-auth"``) or an object
        (``{"name": "pyxle-auth", "module": "...", "settings": {...}}``).
        """
        if isinstance(entry, str):
            name = entry.strip()
            if not name:
                raise PluginError(f"Empty plugin name in {source}")
            return cls(name=name, module=_default_module_for(name))

        if not isinstance(entry, Mapping):
            raise PluginError(
                f"Plugin entry in {source} must be a string or object, "
                f"got {type(entry).__name__}"
            )

        name = entry.get("name")
        if not isinstance(name, str) or not name.strip():
            raise PluginError(
                f"Plugin entry in {source} is missing a non-empty 'name'"
            )
        name = name.strip()

        module = entry.get("module")
        if module is None:
            module = _default_module_for(name)
        elif not isinstance(module, str) or not module.strip():
            raise PluginError(
                f"Plugin '{name}' has an invalid 'module' value in {source}"
            )
        module = module.strip()

        attribute = entry.get("attribute", "plugin")
        if not isinstance(attribute, str) or not attribute.strip():
            raise PluginError(
                f"Plugin '{name}' has an invalid 'attribute' value in {source}"
            )
        attribute = attribute.strip()

        settings = entry.get("settings", {})
        if not isinstance(settings, Mapping):
            raise PluginError(
                f"Plugin '{name}' 'settings' must be an object, "
                f"got {type(settings).__name__}"
            )

        return cls(
            name=name,
            module=module,
            attribute=attribute,
            settings=dict(settings),
        )


def _default_module_for(plugin_name: str) -> str:
    """``pyxle-auth`` → ``pyxle_auth.plugin``.

    Hyphens become underscores (PEP 503 normalisation in reverse),
    and ``.plugin`` is appended. Matches the convention used by every
    Pyxle-branded plugin today. Users can override via the ``module``
    field on a plugin entry.
    """
    module_base = plugin_name.replace("-", "_")
    return f"{module_base}.plugin"


# ---------------------------------------------------------------------------
# Plugin base class


class PyxlePlugin(ABC):
    """Base class plugins inherit to be discoverable by Pyxle.

    Subclasses override the hooks they need. Every hook has a reasonable
    no-op default so a minimal plugin is just::

        class Plugin(PyxlePlugin):
            name = "pyxle-hello"
            version = "0.1.0"

            async def on_startup(self, ctx):
                ctx.register("hello.service", HelloService())

    Hook return values:

    * :meth:`on_startup` / :meth:`on_shutdown` — ``None``. Raise to abort.
    * :meth:`middleware` — iterable of ``(import_string, options_dict)``
      pairs, appended to the app's middleware stack in declaration order.

    Instances receive their :class:`PluginSpec` after construction so
    they can read per-app config from ``self.settings``.
    """

    name: str = "unnamed"
    version: str = "0.0.0"

    # Populated by the loader after construction. Plugins can read
    # ``self.settings`` to get the config-provided dict.
    settings: Mapping[str, Any] = {}

    async def on_startup(self, ctx: "PluginContext") -> None:
        """Called once when the ASGI app boots.

        Typical work here: open long-lived connections, run migrations,
        register services other plugins / the host app will consume.
        Raises here abort startup — the error propagates and Starlette
        refuses to serve requests, which is the right posture for
        "pyxle-db can't reach the database" kinds of failures.
        """

    async def on_shutdown(self, ctx: "PluginContext") -> None:
        """Called once at graceful shutdown.

        Mirror of :meth:`on_startup`. Runs in reverse registration
        order so plugins can depend on each other's services at
        startup and tear them down in a safe sequence.
        """

    def middleware(self) -> Sequence[tuple[str, Mapping[str, Any]]]:
        """Middleware specs to append to the host app's stack.

        Each entry is ``(import_string, options)`` — the same shape
        accepted in ``pyxle.config.json::middleware``. Empty by default.
        """
        return ()


# ---------------------------------------------------------------------------
# Service registry / lifecycle context


class PluginContext:
    """Per-process lifecycle context shared across plugins.

    Plugins register named services here; the host app's loaders and
    actions retrieve them via ``request.app.state.pyxle_plugins``.

    Naming convention for service keys:

    * ``<plugin-shortname>.<capability>`` — e.g. ``db.database``,
      ``auth.service``, ``auth.settings``.
    * Plugins own their namespace; host apps should prefix with
      ``app.`` to avoid collisions.

    The registry enforces one-writer semantics: re-registering the
    same key raises. If a host app wants to override a plugin's
    service it can call :meth:`replace` explicitly.
    """

    def __init__(self, *, settings: Any = None) -> None:
        self._services: dict[str, Any] = {}
        self._settings = settings

    # ---- service registry ------------------------------------------------------

    def register(self, name: str, service: Any) -> None:
        """Register a service under ``name``. Raises if already present."""
        if not isinstance(name, str) or not name:
            raise PluginServiceError("Service name must be a non-empty string")
        if name in self._services:
            raise PluginServiceError(
                f"Service '{name}' is already registered. "
                "Use replace() to override deliberately."
            )
        self._services[name] = service

    def replace(self, name: str, service: Any) -> None:
        """Overwrite an existing service (or register a new one)."""
        if not isinstance(name, str) or not name:
            raise PluginServiceError("Service name must be a non-empty string")
        self._services[name] = service

    def get(self, name: str, default: Any = None) -> Any:
        """Return the service or ``default`` if absent. Non-raising."""
        return self._services.get(name, default)

    def require(self, name: str) -> Any:
        """Return the service or raise :class:`PluginServiceError`.

        Use this when the caller genuinely cannot continue without the
        service — the error message lists every registered name so the
        developer can see the typo from the traceback.
        """
        if name not in self._services:
            available = sorted(self._services)
            raise PluginServiceError(
                f"Service '{name}' not registered. "
                f"Available: {available or '(none)'}"
            )
        return self._services[name]

    def has(self, name: str) -> bool:
        return name in self._services

    def names(self) -> tuple[str, ...]:
        """Snapshot of registered service keys, sorted."""
        return tuple(sorted(self._services))

    # ---- accessors -------------------------------------------------------------

    @property
    def settings(self) -> Any:
        """The host app's resolved :class:`DevServerSettings`."""
        return self._settings


# ---------------------------------------------------------------------------
# Loader


def load_plugins(specs: Sequence[PluginSpec]) -> tuple[PyxlePlugin, ...]:
    """Resolve each :class:`PluginSpec` to a concrete plugin instance.

    Errors out with a clear message if the import fails, the attribute
    is missing, or the resolved object is neither a :class:`PyxlePlugin`
    instance nor a subclass we can instantiate.
    """
    plugins: list[PyxlePlugin] = []
    for spec in specs:
        try:
            module = importlib.import_module(spec.module)
        except ImportError as exc:
            raise PluginResolutionError(
                f"Plugin '{spec.name}' could not import module "
                f"'{spec.module}': {exc}"
            ) from exc

        try:
            target = getattr(module, spec.attribute)
        except AttributeError as exc:
            raise PluginResolutionError(
                f"Plugin '{spec.name}' module '{spec.module}' has no "
                f"attribute '{spec.attribute}'"
            ) from exc

        if isinstance(target, type):
            if not issubclass(target, PyxlePlugin):
                raise PluginResolutionError(
                    f"Plugin '{spec.name}' attribute '{spec.attribute}' "
                    f"is a class but doesn't subclass PyxlePlugin"
                )
            instance = target()
        elif isinstance(target, PyxlePlugin):
            instance = target
        else:
            raise PluginResolutionError(
                f"Plugin '{spec.name}' attribute '{spec.attribute}' must "
                "be a PyxlePlugin subclass or instance; got "
                f"{type(target).__name__}"
            )

        # Hand the per-app settings dict through so the plugin can read
        # its own config. Done after construction to keep the subclass
        # __init__ signature free of framework coupling.
        # Using object.__setattr__ so plugins that declare ``settings``
        # at class level (the default) can still have it overridden
        # per-instance without frozen-dataclass surprises.
        try:
            instance.settings = dict(spec.settings)
        except AttributeError:
            # A plugin author deliberately marked `settings` read-only —
            # respect that and skip assignment.
            pass

        plugins.append(instance)
    return tuple(plugins)


async def run_startup(
    plugins: Sequence[PyxlePlugin], ctx: PluginContext
) -> None:
    """Call :meth:`on_startup` on every plugin in order.

    Raises propagate — an ASGI lifespan startup failure tells Starlette
    to abort, which is the right posture for "pyxle-db couldn't connect".
    """
    for plugin in plugins:
        try:
            await plugin.on_startup(ctx)
        except Exception as exc:  # pragma: no cover - surfaced in logs
            raise PluginError(
                f"Plugin '{plugin.name}' on_startup failed: {exc}"
            ) from exc


async def run_shutdown(
    plugins: Sequence[PyxlePlugin], ctx: PluginContext
) -> None:
    """Call :meth:`on_shutdown` on every plugin in REVERSE order.

    Plugins set up in order A → B → C typically tear down C → B → A so
    the later plugins' services are still available to the earlier
    ones' shutdown code. Failures are logged but don't abort shutdown
    of the remaining plugins — shutdown is best-effort by design.
    """
    import logging as _logging

    logger = _logging.getLogger("pyxle.plugins")
    for plugin in reversed(plugins):
        try:
            await plugin.on_shutdown(ctx)
        except Exception:  # pragma: no cover - logged, not re-raised
            logger.exception(
                "Plugin '%s' on_shutdown failed; continuing with teardown.",
                plugin.name,
            )


# ---------------------------------------------------------------------------
# Module-level shortcut: ``plugin(name)``
#
# Django users reach into the registry via plain imports — they don't
# write ``request.app.state.installed_apps.get_app_config('auth')`` on
# every view. Pyxle mirrors that with a tiny helper the devserver
# wires up once at lifespan startup::
#
#     from pyxle.plugins import plugin
#     auth = plugin("auth.service")
#
# which resolves to the same :class:`PluginContext.require` call the
# verbose ``request.app.state.pyxle_plugins.require("auth.service")``
# form makes. For multi-app testing or libraries that want to stay
# context-pure, :func:`set_active_context` is the supported override.

_active_context: "PluginContext | None" = None
_MISSING: Any = object()


def set_active_context(ctx: "PluginContext | None") -> None:
    """Install (or clear) the module-level plugin context.

    Called by the devserver from inside the ASGI lifespan. Tests that
    want a clean slate or a one-off context can call this directly —
    pass ``None`` to clear. Kept as an explicit function (rather than
    exposing the variable) so a future contextvar migration doesn't
    break callers.
    """
    global _active_context
    _active_context = ctx


def active_context() -> "PluginContext":
    """Return the active module-level plugin context.

    Raises :class:`PluginServiceError` if no context has been installed.
    In production this is only possible before ASGI startup has run;
    in tests it's the common "I forgot to call set_active_context" case.
    """
    if _active_context is None:
        raise PluginServiceError(
            "No active plugin context. ``plugin(...)`` can only be used "
            "from within an ASGI request (loader, action, API endpoint) "
            "or after ``pyxle.plugins.set_active_context(ctx)`` in a test."
        )
    return _active_context


def plugin(name: str, default: Any = _MISSING) -> Any:
    """Retrieve a service registered by a plugin.

    Thin sugar over ``PluginContext.require(name)`` / ``.get(name)``:

        from pyxle.plugins import plugin

        auth = plugin("auth.service")          # raises if missing
        telemetry = plugin("telemetry", None)  # returns None if absent

    Prefer plugin-provided helper functions (e.g. ``get_auth_service()``
    from ``pyxle_auth``) where they exist — they type-annotate their
    return values, which this generic helper cannot.
    """
    ctx = active_context()
    if default is _MISSING:
        return ctx.require(name)
    return ctx.get(name, default)


__all__ = [
    "PluginContext",
    "PluginError",
    "PluginResolutionError",
    "PluginServiceError",
    "PluginSpec",
    "PyxlePlugin",
    "active_context",
    "load_plugins",
    "plugin",
    "run_shutdown",
    "run_startup",
    "set_active_context",
]
