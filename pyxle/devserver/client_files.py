"""Utilities for writing client-side assets required by the dev server."""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

from .settings import DevServerSettings

CLIENT_ENTRY_FILENAME = "client-entry.js"
CLIENT_HTML_FILENAME = "index.html"
VITE_CONFIG_FILENAME = "vite.config.js"
TSCONFIG_FILENAME = "tsconfig.json"


def _write_text_if_changed(path: Path, contents: str) -> None:
    if path.exists():
        current = path.read_text(encoding="utf-8")
        if current == contents:
            return
    path.write_text(contents, encoding="utf-8")


def write_client_bootstrap_files(settings: DevServerSettings) -> None:
    client_root = settings.client_build_dir
    client_root.mkdir(parents=True, exist_ok=True)

    files = {
        CLIENT_HTML_FILENAME: _render_client_index(),
        CLIENT_ENTRY_FILENAME: _render_client_entry(settings),
        VITE_CONFIG_FILENAME: _render_vite_config(settings),
        TSCONFIG_FILENAME: _render_tsconfig(),
        "pyxle/index.js": _render_client_runtime_index(),
        "pyxle/slot.jsx": _render_slot_runtime(),
        "pyxle/script.jsx": _render_script_component(),
        "pyxle/image.jsx": _render_image_component(),
        "pyxle/head.jsx": _render_head_component(),
        "pyxle/client-only.jsx": _render_client_only_component(),
        "pyxle/use-action.jsx": _render_use_action_component(),
        "pyxle/use-pathname.jsx": _render_use_pathname_component(),
        "pyxle/use-auth.jsx": _render_use_auth_component(),
        "pyxle/use-websocket.jsx": _render_use_websocket_component(),
        "pyxle/form.jsx": _render_form_component(),
        "pyxle/client.js": _render_client_barrel(),
        "pyxle/index.d.ts": _render_client_runtime_index_types(),
        "pyxle/link.d.ts": _render_client_runtime_link_types(),
        "pyxle/slot.d.ts": _render_slot_runtime_types(),
        "pyxle/script.d.ts": _render_script_component_types(),
        "pyxle/image.d.ts": _render_image_component_types(),
        "pyxle/head.d.ts": _render_head_component_types(),
        "pyxle/client-only.d.ts": _render_client_only_component_types(),
        "pyxle/use-action.d.ts": _render_use_action_component_types(),
        "pyxle/use-pathname.d.ts": _render_use_pathname_component_types(),
        "pyxle/use-auth.d.ts": _render_use_auth_component_types(),
        "pyxle/use-websocket.d.ts": _render_use_websocket_component_types(),
        "pyxle/form.d.ts": _render_form_component_types(),
    }

    for relative_path, contents in files.items():
        target = client_root / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        _write_text_if_changed(target, contents)


def _render_client_index() -> str:
    return (
        dedent(
            """
            <!doctype html>
            <html lang="en">
              <head>
                <meta charset="utf-8" />
                <meta name="viewport" content="width=device-width, initial-scale=1" />
                <title>Pyxle App</title>
              </head>
              <body>
                <div id="root"></div>
                <script type="module" src="./client-entry.js"></script>
              </body>
            </html>
            """
        ).strip()
        + "\n"
    )


def _render_client_runtime_index() -> str:
    return (
        dedent(
            """
            import React from 'react';
            import {
              Slot,
              SlotProvider,
              useSlot,
              useSlots,
              mergeSlotLayers,
              normalizeSlots,
            } from './slot.jsx';

            const prefetchedHrefs = new Set();
            const viewportCallbacks = new Map();
            let viewportObserver = null;

            function getRouter() {
              if (typeof window === 'undefined') {
                return null;
              }
              return window.__PYXLE_ROUTER__ ?? null;
            }

            function getViewportObserver() {
              if (viewportObserver || typeof window === 'undefined' || !('IntersectionObserver' in window)) {
                return viewportObserver;
              }
              viewportObserver = new IntersectionObserver(
                (entries) => {
                  for (const entry of entries) {
                    if (!entry.isIntersecting) {
                      continue;
                    }
                    const callback = viewportCallbacks.get(entry.target);
                    if (callback) {
                      callback();
                    }
                  }
                },
                { rootMargin: '200px' },
              );
              return viewportObserver;
            }

            function unsubscribeFromViewport(node) {
              if (viewportCallbacks.has(node)) {
                viewportCallbacks.delete(node);
              }
              if (viewportObserver) {
                viewportObserver.unobserve(node);
              }
            }

            function triggerPrefetch(href) {
              if (!href || prefetchedHrefs.has(href)) {
                return;
              }
              prefetchedHrefs.add(href);
              const router = getRouter();
              router?.prefetch(href).catch(() => {});
            }

            function scheduleIdlePrefetch(href) {
              if (typeof window === 'undefined') {
                return;
              }
              if ('requestIdleCallback' in window) {
                window.requestIdleCallback(() => triggerPrefetch(href));
              } else {
                setTimeout(() => triggerPrefetch(href), 200);
              }
            }

            function shouldSkip(event) {
              if (event.metaKey || event.altKey || event.ctrlKey || event.shiftKey) {
                return true;
              }
              const target = event.currentTarget;
              if (!target) {
                return false;
              }
              const routerAttr = target.getAttribute('data-pyxle-router');
              return routerAttr && routerAttr.toLowerCase() === 'off';
            }

            function mergeRefs(ref, node) {
              if (typeof ref === 'function') {
                ref(node);
              } else if (ref && typeof ref === 'object') {
                ref.current = node;
              }
            }

            function normalizeHref(candidate) {
              if (candidate == null) {
                return null;
              }
              // Hash-only links (e.g. "#section") should scroll natively,
              // not trigger client-side navigation.
              if (typeof candidate === 'string' && candidate.startsWith('#')) {
                return null;
              }
              try {
                const url = new URL(candidate, window.location.origin);
                if (url.origin !== window.location.origin) {
                  return null;
                }
                // API routes and static files are not navigable pages.
                if (url.pathname.startsWith('/api/') || /[.][a-zA-Z0-9]+$/.test(url.pathname)) {
                  return null;
                }
                // Same-page hash change — let browser handle scroll.
                if (url.pathname === window.location.pathname
                    && url.search === window.location.search
                    && url.hash && url.hash !== window.location.hash) {
                  return null;
                }
                return url;
              } catch (error) {
                return null;
              }
            }

            export const Link = React.forwardRef(function PyxleLink(props, forwardedRef) {
              const {
                href,
                prefetch = true,
                replace = false,
                scroll,
                shallow,
                passHref,
                onClick,
                onMouseEnter,
                children,
                ...rest
              } = props ?? {};

              const internalRef = React.useRef(null);

              React.useEffect(() => {
                const node = internalRef.current;
                if (!node || !prefetch) {
                  return () => {};
                }
                const url = normalizeHref(href);
                if (!url) {
                  return () => {};
                }
                const observer = getViewportObserver();
                if (!observer) {
                  scheduleIdlePrefetch(url.href);
                  return () => {};
                }
                const handler = () => triggerPrefetch(url.href);
                viewportCallbacks.set(node, handler);
                observer.observe(node);
                return () => {
                  unsubscribeFromViewport(node);
                };
              }, [href, prefetch]);

              const handleMouseEnter = React.useCallback(
                (event) => {
                  if (typeof onMouseEnter === 'function') {
                    onMouseEnter(event);
                  }
                  if (event.defaultPrevented || !prefetch) {
                    return;
                  }
                  const url = normalizeHref(href);
                  if (!url) {
                    return;
                  }
                  triggerPrefetch(url.href);
                },
                [href, onMouseEnter, prefetch],
              );

              const handleClick = React.useCallback(
                async (event) => {
                  if (typeof onClick === 'function') {
                    onClick(event);
                  }
                  if (event.defaultPrevented || shouldSkip(event)) {
                    return;
                  }
                  const url = normalizeHref(href);
                  if (!url) {
                    return;
                  }
                  event.preventDefault();
                  const router = getRouter();
                  if (!router) {
                    window.location.assign(url.href);
                    return;
                  }
                  const didNavigate = await router.navigate(url.href, {
                    replace,
                    scroll,
                    shallow,
                  });
                  if (!didNavigate) {
                    window.location.assign(url.href);
                  }
                },
                [href, onClick, replace, scroll, shallow],
              );

              const renderedHref = typeof href === 'string'
                ? href
                : (typeof href === 'object' && href !== null && 'toString' in href)
                  ? String(href)
                  : (rest.href ?? '#');

              const elementProps = {
                ...rest,
                href: renderedHref,
                onClick: handleClick,
                onMouseEnter: handleMouseEnter,
                ref: (node) => {
                  internalRef.current = node;
                  mergeRefs(forwardedRef, node);
                },
              };

              if (passHref && href) {
                elementProps.href = typeof href === 'string' ? href : String(href);
              }

              return React.createElement('a', elementProps, children);
            });

            export function navigate(href, options = {}) {
              const url = normalizeHref(href);
              if (!url) {
                window.location.assign(href);
                return Promise.resolve(false);
              }
              const router = getRouter();
              if (!router) {
                window.location.assign(url.href);
                return Promise.resolve(false);
              }
              return router.navigate(url.href, options);
            }

            export function prefetch(href) {
              const url = normalizeHref(href);
              if (!url) {
                return Promise.resolve(false);
              }
              const router = getRouter();
              if (!router) {
                return Promise.resolve(false);
              }
              return router.prefetch(url.href);
            }

            export function refresh() {
              const router = getRouter();
              if (!router) {
                window.location.reload();
                return Promise.resolve(false);
              }
              return router.refresh();
            }

            // invalidate(url) — drop a specific URL from the client-side
            // navigation cache so the next navigation to that URL refetches
            // the loader payload instead of reusing the stored one. Use
            // after a mutation (create/update/delete) to keep list views
            // in sync without a full reload. Pass no argument to clear
            // every cached URL.
            export function invalidate(href) {
              const router = getRouter();
              if (!router) {
                return false;
              }
              if (typeof router.invalidate !== 'function') {
                return false;
              }
              if (href === undefined || href === null) {
                return router.invalidate();
              }
              const url = normalizeHref(href);
              if (!url) {
                return false;
              }
              return router.invalidate(url.href);
            }

            // Re-export framework primitives
            export { Script } from './script.jsx';
            export { Image } from './image.jsx';
            export { Head } from './head.jsx';
            export { default as ClientOnly } from './client-only.jsx';

            export { Slot, SlotProvider, useSlot, useSlots, mergeSlotLayers, normalizeSlots, getRouter };
            export default Link;
            """
        ).strip()
        + "\n"
    )


def _project_uses_tailwind(project_root: Path) -> bool:
    """Return ``True`` when the project depends on ``@tailwindcss/vite``.

    Tailwind v4 is wired into Vite via the ``@tailwindcss/vite`` plugin, so its
    presence in ``package.json`` (either dependency section) is the signal that
    the generated config should load the plugin. Reading ``package.json`` keeps
    detection robust whether Tailwind was scaffolded or added by hand later.
    """

    package_json = project_root / "package.json"
    try:
        import json  # noqa: PLC0415

        data = json.loads(package_json.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    for section in ("dependencies", "devDependencies"):
        deps = data.get(section)
        if isinstance(deps, dict) and "@tailwindcss/vite" in deps:
            return True
    return False


def _project_import_aliases(project_root: Path) -> list[tuple[str, str]]:
    """Return ``(prefix, target)`` import aliases declared in ``jsconfig.json``.

    Each ``"<prefix>/*": ["<target>/*"]`` entry becomes a Vite path alias so an
    import like ``@/lib/utils`` resolves the same way the editor and shadcn/ui
    resolve it. ``target`` is a project-root-relative POSIX path (``.`` for the
    default ``@/* -> ./*``); malformed configs are ignored.
    """

    jsconfig = project_root / "jsconfig.json"
    try:
        import json  # noqa: PLC0415

        data = json.loads(jsconfig.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []

    options = data.get("compilerOptions")
    paths = options.get("paths") if isinstance(options, dict) else None
    if not isinstance(paths, dict):
        return []

    aliases: list[tuple[str, str]] = []
    for key, value in paths.items():
        if not isinstance(key, str) or not key.endswith("/*"):
            continue
        if not isinstance(value, list) or not value or not isinstance(value[0], str):
            continue
        prefix = key[:-2]
        target_glob = value[0]
        target = target_glob[:-2] if target_glob.endswith("/*") else target_glob
        # Normalise ``./`` and empty targets to the project root marker ``.``.
        target = target.lstrip("./") or "."
        if not prefix or "/" in prefix or "'" in prefix or "\\" in prefix:
            continue
        aliases.append((prefix, target))
    return aliases


def _render_vite_config(settings: DevServerSettings) -> str:
    vite_host = settings.vite_host
    vite_port = settings.vite_port
    define_block = _build_public_env_defines()

    uses_tailwind = _project_uses_tailwind(settings.project_root)
    tailwind_import = (
        "\n            import tailwindcss from '@tailwindcss/vite';" if uses_tailwind else ""
    )
    plugin_calls = "react(), tailwindcss()" if uses_tailwind else "react()"

    alias_entries: list[str] = []
    for prefix, target in _project_import_aliases(settings.project_root):
        replacement = (
            "projectRoot"
            if target == "."
            else f"path.resolve(projectRoot, {target!r})"
        )
        alias_entries.append(
            f"                  {{ find: '{prefix}', replacement: {replacement} }},"
        )
    user_alias_block = ("\n" + "\n".join(alias_entries)) if alias_entries else ""

    return (
        dedent(
            f"""
            import {{ defineConfig }} from 'vite';
            import react from '@vitejs/plugin-react';{tailwind_import}
            import path from 'node:path';

            const clientRoot = __dirname;
            const projectRoot = path.resolve(clientRoot, '..', '..');
            const pyxleClientDir = path.resolve(clientRoot, 'pyxle');
            const base = process.env.PYXLE_VITE_BASE ?? '/';

            // Pyxle serves the HTML document from its own origin while the dev
            // assets come from Vite. Vite rewrites CSS `url(...)` references
            // (fonts, background images) to ROOT-RELATIVE paths like
            // `/@fs/...woff2`, which the browser resolves against the document's
            // origin (Pyxle) — not Vite — so they 404. Declaring Vite's public
            // `server.origin` makes it emit ABSOLUTE asset URLs against its own
            // origin instead. The bind host is normalised to a
            // browser-connectable host the same way `ssr/template.py` does for
            // the <script> origin, so assets and scripts always share one origin.
            const viteHost = '{vite_host}';
            const browserHost =
              viteHost === '0.0.0.0' || viteHost === '::' || viteHost === ''
                ? 'localhost'
                : viteHost;
            const vitePort = Number(process.env.PYXLE_VITE_PORT ?? {vite_port});

            __PYXLE_CSS_MODULE_HELPER__

            export default defineConfig({{
              base,
              root: clientRoot,
              publicDir: path.resolve(projectRoot, 'public'),
              plugins: [{plugin_calls}],{define_block}
              build: {{
                // esbuild minification + Rollup tree-shaking/code-splitting are
                // Vite's production defaults; these make the rest explicit.
                target: 'es2020',
                sourcemap: false,
                cssCodeSplit: true,
                // Pyxle ships its own `pyxle build --analyze`, so skip Vite's
                // slower gzip-size reporting to keep production builds fast.
                reportCompressedSize: false,
              }},
              css: {{
                // Deterministic CSS Module class names so the server-rendered
                // markup and the client bundle agree exactly — no React
                // hydration mismatch. The same algorithm runs in Pyxle's SSR
                // runtime (see ssr/render_component.mjs).
                modules: {{
                  generateScopedName: pyxleCssModuleClass,
                }},
              }},
              resolve: {{
                alias: [{user_alias_block}
                  {{ find: '/pages', replacement: path.resolve(clientRoot, 'pages') }},
                  {{ find: '/routes', replacement: path.resolve(clientRoot, 'routes') }},
                  {{ find: /^pyxle\\/client$/, replacement: path.resolve(pyxleClientDir, 'client.js') }},
                  {{ find: /^pyxle\\/client\\/(.+)$/, replacement: path.resolve(pyxleClientDir, '$1') }},
                ],
              }},
              server: {{
                host: viteHost,
                port: vitePort,
                strictPort: false,
                origin: `http://${{browserHost}}:${{vitePort}}`,
                fs: {{
                  allow: [projectRoot],
                }},
              }},
            }});
            """
        ).strip()
        + "\n"
    ).replace("__PYXLE_CSS_MODULE_HELPER__", CSS_MODULE_SCOPED_NAME_JS)


# Canonical CSS-Module class-name generator, shared verbatim between the Vite
# client build (as ``css.modules.generateScopedName``) and Pyxle's SSR runtimes
# (``ssr/render_component.mjs`` + ``ssr/ssr_worker.mjs``). Because the scoped
# name is derived only from the file's basename, the local class name, and the
# stylesheet contents — never an absolute path — it produces identical output
# in dev, build, and production serve, so server- and client-rendered markup
# always carry the same class names (no React hydration mismatch).
CSS_MODULE_SCOPED_NAME_JS = dedent(
    """
    function pyxleCssModuleClass(name, filename, css) {
      const file = String(filename).split(/[\\\\/]/).pop() || 'module';
      const base = file.replace(/\\.module\\.css$/i, '').replace(/[^a-zA-Z0-9_-]/g, '-');
      const seed = base + '|' + name + '|' + (css || '');
      let hash = 5381;
      for (let index = 0; index < seed.length; index += 1) {
        hash = ((hash << 5) + hash + seed.charCodeAt(index)) >>> 0;
      }
      return base + '_' + name + '_' + hash.toString(36).slice(0, 6);
    }
    """
).strip()


def _build_public_env_defines() -> str:
    """Build a Vite ``define`` block injecting ``PYXLE_PUBLIC_*`` env vars.

    Each variable is exposed as ``import.meta.env.PYXLE_PUBLIC_*`` in client code.

    .. note::

        Environment variables are snapshot at dev-server startup.
        Rotating a ``PYXLE_PUBLIC_*`` variable at runtime requires a
        server restart for the change to appear in client bundles.
    Keys are validated against :data:`SAFE_IDENTIFIER_RE` to prevent code
    injection via malformed environment variable names.
    """

    import json  # noqa: PLC0415
    import logging  # noqa: PLC0415
    import os  # noqa: PLC0415

    from pyxle.devserver._security import SAFE_IDENTIFIER_RE

    _logger = logging.getLogger(__name__)

    prefix = "PYXLE_PUBLIC_"
    public_vars = {k: v for k, v in sorted(os.environ.items()) if k.startswith(prefix)}
    if not public_vars:
        return ""

    entries: list[str] = []
    for key, value in public_vars.items():
        if not SAFE_IDENTIFIER_RE.match(key):
            _logger.warning("Skipping PYXLE_PUBLIC_ key with invalid name: %r", key)
            continue
        # Vite/esbuild treat `define` VALUES as raw expressions, so a string
        # constant must be emitted as a quoted JS string literal (JSON.stringify).
        # A bare value is silently dropped in dev and crashes `vite build`
        # ("Invalid define value") for any non-identifier value (URLs, 0x… keys).
        entries.append(
            f"    'import.meta.env.{key}': JSON.stringify({json.dumps(value)})"
        )

    if not entries:
        return ""

    define_content = ",\n".join(entries)
    return f"\n  define: {{\n{define_content},\n  }},"


def _render_slot_runtime() -> str:
    return (
        dedent(
            """
            import React, { createContext, useContext, useMemo } from 'react';

            const SlotContext = createContext(Object.freeze({}));

            export function normalizeSlots(candidate) {
              if (!candidate || typeof candidate !== 'object') {
                return {};
              }
              const normalized = {};
              for (const [name, factory] of Object.entries(candidate)) {
                if (typeof factory === 'function') {
                  normalized[name] = factory;
                }
              }
              return normalized;
            }

            function appendSlotFactory(registry, name, factory) {
              if (typeof factory !== 'function') {
                return;
              }
              if (!registry[name]) {
                registry[name] = [];
              }
              registry[name].push(factory);
            }

            export function mergeSlotLayers(layers, pageSlots = {}) {
              const registry = {};
              const list = Array.isArray(layers) ? layers : [];
              for (const layer of list) {
                if (!layer || !layer.slots) {
                  continue;
                }
                const slots = normalizeSlots(layer.slots);
                for (const [name, factory] of Object.entries(slots)) {
                  appendSlotFactory(registry, name, factory);
                }
              }
              const normalizedPageSlots = normalizeSlots(pageSlots);
              for (const [name, factory] of Object.entries(normalizedPageSlots)) {
                appendSlotFactory(registry, name, factory);
              }
              return registry;
            }

            export function SlotProvider({ slots, children }) {
              const value = useMemo(
                () => (slots && typeof slots === 'object' ? slots : {}),
                [slots],
              );
              return React.createElement(SlotContext.Provider, { value }, children);
            }

            export function useSlots() {
              return useContext(SlotContext);
            }

            export function useSlot(name) {
              const slots = useSlots();
              const entry = slots?.[name];
              if (!entry) {
                return null;
              }
              if (Array.isArray(entry)) {
                return entry.length ? entry : null;
              }
              if (typeof entry === 'function') {
                return [entry];
              }
              return null;
            }

            export function Slot({ name, props = {}, fallback = null }) {
              const slotFactories = useSlot(name);
              if (slotFactories && slotFactories.length) {
                return slotFactories.map((factory, index) => {
                  const rendered = factory(props);
                  if (rendered == null) {
                    return rendered;
                  }
                  return React.createElement(
                    React.Fragment,
                    { key: `slot-${name}-${index}` },
                    rendered,
                  );
                });
              }
              if (typeof fallback === 'function') {
                return fallback(props);
              }
              return fallback ?? null;
            }
            """
        ).strip()
        + "\n"
    )


def _render_client_runtime_index_types() -> str:
    return (
        dedent(
            """
            import type { LinkProps } from './link';
            import type { SlotDictionary } from './slot';

            export type NavigationTarget = string | URL | Location;

            export interface NavigationOptions {
              replace?: boolean;
              scroll?: boolean | 'preserve';
              shallow?: boolean;
              updateHistory?: boolean;
            }

            export interface PyxleRouter {
              navigate(href: NavigationTarget, options?: NavigationOptions): Promise<boolean>;
              prefetch(href: NavigationTarget): Promise<boolean>;
              refresh(): Promise<boolean>;
              /**
               * Evict a specific URL from the client-side navigation
               * cache (``undefined``/no arg clears every entry). The
               * next ``navigate`` to that URL will refetch the loader
               * payload instead of reusing the cached one. Use after
               * mutations (create/delete/update) so list views don't
               * show stale data.
               */
              invalidate(href?: NavigationTarget): boolean;
            }

            export declare function navigate(href: NavigationTarget, options?: NavigationOptions): Promise<boolean>;
            export declare function prefetch(href: NavigationTarget): Promise<boolean>;
            export declare function refresh(): Promise<boolean>;
            export declare function invalidate(href?: NavigationTarget): boolean;
            export declare function getRouter(): PyxleRouter | null;

            // Re-export framework primitives with types
            export { Script, type ScriptProps } from './script';
            export { Image, type ImageProps } from './image';
            export { Head, type HeadProps } from './head';
            export { default as ClientOnly, type ClientOnlyProps } from './client-only';

            export { Link, type LinkProps } from './link';
            export { Slot, SlotProvider, useSlot, useSlots, type SlotDictionary } from './slot';

            export default Link;
            """
        ).strip()
        + "\n"
    )


def _render_client_runtime_link_types() -> str:
    return (
        dedent(
            """
            import type React from 'react';

            export interface LinkProps extends React.AnchorHTMLAttributes<HTMLAnchorElement> {
              href: string;
              prefetch?: boolean;
              replace?: boolean;
              scroll?: boolean | 'preserve';
              shallow?: boolean;
              passHref?: boolean;
            }

            export declare const Link: React.ForwardRefExoticComponent<LinkProps & React.RefAttributes<HTMLAnchorElement>>;
            export default Link;
            """
        ).strip()
        + "\n"
    )


def _render_slot_runtime_types() -> str:
    return (
        dedent(
            """
            import type React from 'react';

            export type SlotFactory<TProps = any> = (props: TProps) => React.ReactNode;
            export type SlotDictionary = Record<string, SlotFactory<any>>;
            export type SlotRegistry = Record<string, SlotFactory<any>[]>;

            export interface SlotLayer {
              kind?: string;
              reset?: boolean;
              slots?: SlotDictionary | null | undefined;
            }

            export interface SlotProviderProps {
              slots?: SlotRegistry | null | undefined;
              children?: React.ReactNode;
            }

            export interface SlotProps<TProps = any> {
              name: string;
              props?: TProps;
              fallback?: React.ReactNode | SlotFactory<TProps> | null;
            }

            export declare function normalizeSlots(candidate: unknown): SlotDictionary;
            export declare function mergeSlotLayers(layers: SlotLayer[], pageSlots?: SlotDictionary): SlotRegistry;
            export declare function SlotProvider(props: SlotProviderProps): React.ReactElement;
            export declare function useSlots(): SlotRegistry;
            export declare function useSlot<TProps = any>(name: string): SlotFactory<TProps>[] | null;
            export declare function Slot<TProps = any>(props: SlotProps<TProps>): React.ReactElement | React.ReactElement[] | null;
            """
        ).strip()
        + "\n"
    )


def _render_client_entry(settings: DevServerSettings) -> str:
    content = (
      dedent(
        """
        __PYXLE_GLOBAL_SCRIPT_IMPORTS__
        import React from 'react';
        import ReactDOM from 'react-dom/client';
        __PYXLE_GLOBAL_STYLE_IMPORTS__

        const componentModules = {
              ...import.meta.glob('/pages/**/*.jsx'),
              ...import.meta.glob('/routes/**/*.jsx'),
            };
            __PYXLE_OVERLAY_BLOCK__
            const NAVIGATION_HEADER = 'x-pyxle-navigation';
            const HEAD_START_SELECTOR = 'meta[data-pyxle-head-start]';
            const HEAD_END_SELECTOR = 'meta[data-pyxle-head-end]';
            const PREFETCH_TRIGGER = 'hover';
            const STALE_STYLE_ATTR = 'data-pyxle-stale-style';
            const NEW_STYLE_ATTR = 'data-pyxle-new-style';
            const STYLESHEET_LOAD_TIMEOUT = 3000;

            let reactRoot = null;
            let currentPagePath = window.__PYXLE_PAGE_PATH__ || '';
            let navigationController = null;
            // Monotonic token: each navigateTo() claims the next value, so a
            // navigation that awaited an in-flight prefetch can detect it was
            // superseded by a newer click and bail out instead of rendering a
            // stale page (the abort controller can't cancel a shared prefetch).
            let navigationSequence = 0;

            // ---- Navigation cache with TTL ------------------------------
            //
            // Every cached payload carries a ``cachedAt`` timestamp and a
            // per-entry ``ttlMs`` lifetime, so entries older than their own
            // window count as misses. A page's lifetime mirrors its edge-cache
            // TTL from ``pyxle.config.json::cache``: the server tags each nav
            // payload (and the SSR seed) with ``navCacheTtlSeconds``, so the
            // client navigation cache stays fresh exactly as long as a CDN
            // would serve that page — cached enough to keep back/forward and
            // prefetched navigation instant, stale after the page's window.
            //
            // Pages with no ``cache`` entry fall back to ``DEFAULT_NAV_STALE_MS``
            // below — 2 minutes: enough to reuse prefetched/seeded data across a
            // quick read-then-navigate without holding dynamic data for long. The
            // default is overridable via ``__PYXLE_NAV_STALE_MS__``
            // (``pyxle.config.json::navigation.defaultPrefetchTtl``); ``0`` means
            // "never cache".
            const DEFAULT_NAV_STALE_MS = (() => {
              const configured = window.__PYXLE_NAV_STALE_MS__;
              if (typeof configured === 'number' && configured >= 0) {
                return configured;
              }
              return 120_000;
            })();

            // Resolve a payload's cache lifetime (ms). The server attaches the
            // page's configured edge-cache TTL as ``navCacheTtlSeconds`` (in
            // seconds) when one is set; otherwise fall back to the default.
            function navTtlFromPayload(payload) {
              const ttl = payload && payload.navCacheTtlSeconds;
              if (typeof ttl === 'number' && ttl >= 0) {
                return ttl * 1000;
              }
              return DEFAULT_NAV_STALE_MS;
            }

            const _navStorage = new Map();
            const navigationCache = {
              get(key) {
                const entry = _navStorage.get(key);
                if (!entry) return undefined;
                if (entry.ttlMs === 0 || Date.now() - entry.cachedAt > entry.ttlMs) {
                  _navStorage.delete(key);
                  return undefined;
                }
                return entry.payload;
              },
              set(key, payload, ttlMs) {
                const lifetime = typeof ttlMs === 'number' ? ttlMs : navTtlFromPayload(payload);
                _navStorage.set(key, { payload, cachedAt: Date.now(), ttlMs: lifetime });
              },
              has(key) {
                return this.get(key) !== undefined;
              },
              delete(key) {
                return _navStorage.delete(key);
              },
              clear() {
                _navStorage.clear();
              },
              get size() {
                return _navStorage.size;
              },
            };

            const navigationPromises = new Map();
            const moduleCache = new Map();

            const router = {
              navigate: (href, options = {}) => navigateTo(href, options),
              prefetch: (href) => prefetchNavigation(href),
              refresh: () => refreshCurrentPage(),
              // invalidate(href?) — evict a specific URL's cached nav
              // payload. Without an argument, clears every cached URL.
              // The next navigate(href) refetches the loader instead of
              // serving the stale payload. Essential after mutations.
              invalidate: (href) => {
                if (href === undefined || href === null) {
                  navigationCache.clear();
                  navigationPromises.clear();
                  failedPrefetches.clear();
                  return true;
                }
                try {
                  const target = new URL(href, window.location.origin);
                  const key = getCacheKey(target);
                  const removed =
                    navigationCache.delete(key) ||
                    navigationPromises.delete(key) ||
                    failedPrefetches.delete(key);
                  return removed;
                } catch {
                  return false;
                }
              },
            };

            window.__PYXLE_ROUTER__ = router;

            const availableModules = Object.keys(componentModules);
            if (!currentPagePath && availableModules.length > 0) {
              currentPagePath = availableModules[0];
            }

            function parseInitialProps() {
              try {
                const propsTag = document.getElementById('__PYXLE_PROPS__');
                const rawProps = propsTag?.textContent ?? '{}';
                return rawProps ? JSON.parse(rawProps) : {};
              } catch (error) {
                console.error('[Pyxle] Failed to parse initial props', error);
                return {};
              }
            }

            function serializeProps(props) {
              try {
                return JSON.stringify(props).replace(/</g, '\\u003C');
              } catch (error) {
                console.warn('[Pyxle] Failed to serialize props payload', error);
                return '{}';
              }
            }

            function updatePropsTag(props) {
              const propsTag = document.getElementById('__PYXLE_PROPS__');
              if (!propsTag) {
                return;
              }
              propsTag.textContent = serializeProps(props);
            }

            function updateHead(markup) {
              const head = document.head;
              if (!head) {
                return;
              }
              const start = head.querySelector(HEAD_START_SELECTOR);
              const end = head.querySelector(HEAD_END_SELECTOR);
              if (!start || !end) {
                return;
              }
              const fragmentHtml = (markup ?? '').trim();
              const existingNodes = [];
              const staleStylesheets = [];
              const newStylesheets = [];
              let node = start.nextSibling;
              while (node && node !== end) {
                existingNodes.push(node);
                node = node.nextSibling;
              }
              const processed = new Set();
              const signatureMap = new Map();
              for (const existing of existingNodes) {
                const signature = getNodeSignature(existing);
                if (!signatureMap.has(signature)) {
                  signatureMap.set(signature, []);
                }
                signatureMap.get(signature).push(existing);
              }

              const template = document.createElement('template');
              template.innerHTML = fragmentHtml;
              const nextNodes = Array.from(template.content.childNodes);
              for (const nextNode of nextNodes) {
                const signature = getNodeSignature(nextNode);
                const pool = signatureMap.get(signature);
                const candidate = pool?.shift?.();
                if (candidate) {
                  processed.add(candidate);
                  candidate.removeAttribute?.(STALE_STYLE_ATTR);
                  syncNodeContent(candidate, nextNode);
                  continue;
                }
                const nodeToInsert = nextNode;
                head.insertBefore(nodeToInsert, end);
                if (isStylesheetNode(nodeToInsert)) {
                  nodeToInsert.setAttribute(NEW_STYLE_ATTR, '1');
                  newStylesheets.push(nodeToInsert);
                }
              }

              for (const existing of existingNodes) {
                if (!processed.has(existing) && existing.parentNode === head) {
                  if (isStylesheetNode(existing)) {
                    existing.setAttribute(STALE_STYLE_ATTR, '1');
                    staleStylesheets.push(existing);
                  } else {
                    head.removeChild(existing);
                  }
                }
              }

              if (staleStylesheets.length) {
                const finalize = () => cleanupStaleStylesheets(staleStylesheets);
                if (newStylesheets.length) {
                  waitForStylesheets(newStylesheets).then(finalize);
                } else {
                  setTimeout(finalize, 0);
                }
              }
            }

            function getNodeSignature(node) {
              if (!node) {
                return '';
              }
              if (node.nodeType !== Node.ELEMENT_NODE) {
                return `text:${node.textContent ?? ''}`;
              }
              const element = node;
              const key = element.getAttribute?.('data-pyxle-head-key');
              if (key) {
                return `key:${key}`;
              }
              const tag = element.tagName?.toLowerCase?.() ?? '';
              if (tag === 'title') {
                return 'title';
              }
              if (tag === 'meta') {
                const name = element.getAttribute('name');
                const property = element.getAttribute('property');
                const content = element.getAttribute('content');
                return `meta:${name ?? property ?? ''}:${content ?? ''}`;
              }
              if (tag === 'link') {
                return `link:${element.getAttribute('rel') ?? ''}:${element.getAttribute('href') ?? ''}`;
              }
              if (tag === 'script') {
                return `script:${element.getAttribute('src') ?? ''}:${element.textContent ?? ''}`;
              }
              return element.outerHTML ?? '';
            }

            function syncNodeContent(target, source) {
              if (!target || !source) {
                return;
              }
              if (target.nodeType !== source.nodeType) {
                target.replaceWith(source);
                return;
              }
              if (target.nodeType === Node.TEXT_NODE) {
                if (target.textContent !== source.textContent) {
                  target.textContent = source.textContent;
                }
                return;
              }
              if (target.tagName?.toLowerCase?.() === 'title') {
                if (target.textContent !== source.textContent) {
                  target.textContent = source.textContent ?? '';
                }
                return;
              }
              const isMeta = target.tagName?.toLowerCase?.() === 'meta';
              const isLink = target.tagName?.toLowerCase?.() === 'link';
              if (isMeta || isLink) {
                const sourceAttrs = Array.from(source.attributes ?? []);
                const targetAttrs = new Set(Array.from(target.attributes ?? []).map((attr) => attr.name));
                for (const attr of sourceAttrs) {
                  target.setAttribute(attr.name, attr.value);
                  targetAttrs.delete(attr.name);
                }
                for (const attrName of targetAttrs) {
                  target.removeAttribute(attrName);
                }
                return;
              }
              target.replaceWith(source);
            }

            function isStylesheetNode(node) {
              if (!node || node.nodeType !== Node.ELEMENT_NODE) {
                return false;
              }
              if (node.tagName?.toLowerCase?.() !== 'link') {
                return false;
              }
              const rel = node.getAttribute('rel') ?? '';
              return rel.toLowerCase().includes('stylesheet');
            }

            function cleanupStaleStylesheets(nodes) {
              for (const node of nodes) {
                if (!node) {
                  continue;
                }
                node.removeAttribute(STALE_STYLE_ATTR);
                if (node.parentNode === document.head) {
                  document.head.removeChild(node);
                }
              }
            }

            function waitForStylesheets(nodes) {
              return Promise.all(
                nodes.map((node) => {
                  return new Promise((resolve) => {
                    if (!node) {
                      resolve();
                      return;
                    }
                    const cleanup = () => {
                      node.removeAttribute(NEW_STYLE_ATTR);
                      resolve();
                    };
                    let settled = false;
                    const onComplete = () => {
                      if (settled) {
                        return;
                      }
                      settled = true;
                      clearTimeout(timer);
                      cleanup();
                    };
                    const timer = setTimeout(onComplete, STYLESHEET_LOAD_TIMEOUT);
                    node.addEventListener('load', onComplete, { once: true });
                    node.addEventListener('error', onComplete, { once: true });
                    try {
                      if (node.sheet && node.sheet.cssRules !== null) {
                        onComplete();
                      }
                    } catch (error) {
                      if (String(error?.name).toLowerCase() === 'securityerror') {
                        // Ignore cross-origin access errors; rely on load/error events.
                      }
                    }
                  });
                }),
              );
            }

            async function loadPageModule(pagePath) {
              if (moduleCache.has(pagePath)) {
                return moduleCache.get(pagePath);
              }
              const loader = componentModules[pagePath];
              if (!loader) {
                throw new Error(`[Pyxle] No module found for ${pagePath}`);
              }
              const promise = loader()
                .then((mod) => {
                  moduleCache.set(pagePath, Promise.resolve(mod));
                  return mod;
                })
                .catch((error) => {
                  moduleCache.delete(pagePath);
                  throw error;
                });
              moduleCache.set(pagePath, promise);
              return promise;
            }

            // The error context handed to the nearest error.pyxl when the
            // client boundary catches a render fault. Mirrors the server's
            // _build_error_context shape (message / statusCode / type) and is
            // passed under the SAME `error` prop key the server uses
            // (props={"error": ...}), so one error.pyxl reads `props.error`
            // identically on both sides. The error originated in the browser,
            // so its message is the client's own — no server-secret leak.
            function buildClientErrorContext(error) {
              const message =
                error && error.message ? String(error.message) : String(error);
              return {
                message: message,
                statusCode: 500,
                type: (error && error.name) || 'Error',
              };
            }

            // A React error boundary for the client. It is a transparent
            // passthrough until a descendant throws (so it adds no DOM and never
            // perturbs hydration); on error it renders the nearest error.pyxl —
            // parity with the server, which already renders that error.pyxl when
            // a loader or the SSR render fails. renderPage keys it by pagePath so
            // a later navigation remounts it and clears the error state.
            class PyxleErrorBoundary extends React.Component {
              constructor(props) {
                super(props);
                this.state = { error: null };
              }
              static getDerivedStateFromError(error) {
                return { error: error };
              }
              componentDidCatch(error, info) {
                // The dev overlay hooks window.onerror/console separately, so
                // logging here is enough to surface the fault in both modes.
                console.error('[Pyxle] Unhandled error during client render:', error, info);
              }
              render() {
                if (this.state.error) {
                  const Fallback = this.props.fallbackComponent;
                  if (Fallback) {
                    return React.createElement(Fallback, {
                      error: buildClientErrorContext(this.state.error),
                    });
                  }
                  return React.createElement(
                    'div',
                    {
                      role: 'alert',
                      'data-pyxle-client-error': '',
                      style: {
                        padding: '2rem',
                        fontFamily: 'system-ui, sans-serif',
                        color: '#b91c1c',
                      },
                    },
                    'Something went wrong rendering this page.',
                  );
                }
                return this.props.children;
              }
            }

            async function renderPage(pagePath, props, loadingAssetPath, errorAssetPath) {
              const module = await loadPageModule(pagePath);
              const Page = module.default;
              if (!Page) {
                throw new Error(`[Pyxle] Page module ${pagePath} is missing a default export.`);
              }
              const container = document.getElementById('root');
              if (!container) {
                throw new Error("[Pyxle] Hydration container '#root' not found");
              }

              // A route with a loading.pyxl boundary is wrapped in the SAME
              // <Suspense fallback={<Loading/>}> the streaming server emitted, so
              // the hydration boundary structure matches. The fallback module is
              // loaded BEFORE hydrateRoot so the boundary is ready immediately.
              let element = React.createElement(Page, props);
              if (loadingAssetPath) {
                const fallbackModule = await loadPageModule(loadingAssetPath);
                const Fallback = fallbackModule.default;
                element = React.createElement(
                  React.Suspense,
                  { fallback: Fallback ? React.createElement(Fallback) : null },
                  React.createElement(Page, props),
                );
              }

              // Wrap the (possibly Suspense-wrapped) page in the client error
              // boundary. Its fallback is the nearest error.pyxl, pre-loaded here
              // so the boundary's synchronous render() can mount it on a fault.
              // The boundary is transparent until then, so hydration is unchanged.
              let errorFallbackComponent = null;
              if (errorAssetPath) {
                try {
                  const errorModule = await loadPageModule(errorAssetPath);
                  errorFallbackComponent = errorModule.default || null;
                } catch (loadError) {
                  // If error.pyxl itself fails to load, fall through to the
                  // boundary's built-in message rather than break page render.
                  console.error('[Pyxle] Failed to load error boundary module:', loadError);
                }
              }
              element = React.createElement(
                PyxleErrorBoundary,
                { key: pagePath, fallbackComponent: errorFallbackComponent },
                element,
              );

              if (!reactRoot) {
                const placeholder = container.firstElementChild;
                const shouldClientRender = placeholder?.hasAttribute('data-pyxle-component');
                if (shouldClientRender) {
                  container.innerHTML = '';
                  reactRoot = ReactDOM.createRoot(container);
                  reactRoot.render(element);
                } else {
                  reactRoot = ReactDOM.hydrateRoot(container, element);
                }
              } else {
                reactRoot.render(element);
              }

              currentPagePath = pagePath;
              window.__PYXLE_PAGE_PATH__ = pagePath;
              updatePropsTag(props);
            }

            function normalizeUrl(target) {
              if (target instanceof URL) {
                return target;
              }
              if (typeof target === 'string') {
                try {
                  return new URL(target, window.location.href);
                } catch (error) {
                  return null;
                }
              }
              if (target && typeof target.href === 'string') {
                try {
                  return new URL(target.href, window.location.href);
                } catch (error) {
                  return null;
                }
              }
              return null;
            }

            function getCacheKey(url) {
              return `${url.pathname}${url.search}`;
            }

            function shouldHandleClick(event) {
              if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.altKey || event.ctrlKey || event.shiftKey) {
                return null;
              }
              const anchor = event.target?.closest?.('a[href]');
              if (!anchor) {
                return null;
              }
              if (anchor.dataset.pyxleRouter === 'off' || anchor.hasAttribute('download')) {
                return null;
              }
              const rel = anchor.getAttribute('rel');
              if (rel && rel.toLowerCase().includes('external')) {
                return null;
              }
              const targetAttr = anchor.getAttribute('target');
              if (targetAttr && targetAttr.toLowerCase() !== '_self') {
                return null;
              }
              const url = normalizeUrl(anchor.href);
              if (!url || url.origin !== window.location.origin) {
                return null;
              }
              const href = anchor.getAttribute('href') || '';
              if (href.startsWith('#')) {
                return null;
              }
              if (url.pathname === window.location.pathname && url.search === window.location.search && url.hash && url.hash !== window.location.hash) {
                return null;
              }
              return { anchor, url };
            }

            function handleLinkClick(event) {
              if (!event.defaultPrevented && event.button === 0 && !event.metaKey && !event.altKey && !event.ctrlKey && !event.shiftKey) {
                const anchor = event.target?.closest?.('a[href]');
                if (anchor && anchor.dataset.pyxleRouter !== 'off') {
                  const rawHref = anchor.getAttribute('href') || '';
                  if (rawHref.startsWith('#')) {
                    event.preventDefault();
                    const id = rawHref.slice(1);
                    if (id) {
                      const el = document.getElementById(id);
                      if (el) el.scrollIntoView({ behavior: 'smooth', block: 'start' });
                    }
                    window.history.replaceState(window.history.state, '', rawHref);
                    return;
                  }
                }
              }
              const result = shouldHandleClick(event);
              if (!result) {
                return;
              }
              event.preventDefault();
              navigateTo(result.url).catch(() => {});
            }

            function handlePopState(event) {
              // Handle all popstate events via client navigation, including
              // entries without pyxle state (e.g. from native <a href="#...">
              // hash links). This avoids disruptive full-page reloads when
              // navigating back/forward through hash-only history entries.
              navigateTo(new URL(window.location.href), {
                updateHistory: false,
                scroll: 'preserve',
              }).catch(() => {});
            }

            // ── Navigation progress indicator ──────────────────────
            //
            // A fixed top-of-viewport horizontal bar that shows when
            // client-side navigation takes longer than SHOW_DELAY_MS
            // (150ms). Navigations served from the prefetch cache
            // complete before the delay fires and never render the
            // bar, so "instant" navs feel instant and "slow" navs
            // get a progress indicator — the same UX pattern used by
            // Turbo, Nuxt, Inertia, and every framework's nprogress
            // plugin.
            //
            // State machine is hidden inside an IIFE so no globals
            // leak. Integration: ``markNavigating(true/false)`` calls
            // ``navProgress.start()`` / ``navProgress.complete()``,
            // which are the only public surface.
            //
            // Opt-out: set ``window.__pyxle_disable_progress__ =
            // true`` before the runtime loads, or set
            // ``<html data-pyxle-progress="off">``.
            const navProgress = (function initNavProgress() {
              const SHOW_DELAY_MS = 150;
              const TICK_MS = 400;
              const TARGET_CAP = 0.9;
              const DECAY = 0.15;
              const ELEMENT_ID = '__pyxle_nav_progress__';
              const STYLE_ID = '__pyxle_nav_progress_style__';

              let pendingTimer = null;
              let tickTimer = null;
              let hideTimer = null;
              let element = null;
              let progress = 0;
              let activeCount = 0;
              let prefersReducedMotion = false;

              function isDisabled() {
                if (typeof window === 'undefined' || typeof document === 'undefined') {
                  return true;
                }
                if (window.__pyxle_disable_progress__ === true) {
                  return true;
                }
                const root = document.documentElement;
                if (root && root.getAttribute('data-pyxle-progress') === 'off') {
                  return true;
                }
                return false;
              }

              function ensureStyles() {
                if (document.getElementById(STYLE_ID)) {
                  return;
                }
                const style = document.createElement('style');
                style.id = STYLE_ID;
                // Max safe int z-index (same trick Turbo uses) keeps
                // the bar above fixed headers, modals, and toasts.
                // Customisable via CSS custom properties on <html>.
                style.textContent = [
                  ':root {',
                  '  --pyxle-nav-progress-height: 3px;',
                  '  --pyxle-nav-progress-color: linear-gradient(90deg, #10b981 0%, #06b6d4 100%);',
                  '  --pyxle-nav-progress-shadow: 0 0 10px rgba(16, 185, 129, 0.5), 0 0 6px rgba(6, 182, 212, 0.4);',
                  '}',
                  '#' + ELEMENT_ID + ' {',
                  '  position: fixed;',
                  '  top: 0;',
                  '  left: 0;',
                  '  right: 0;',
                  '  height: var(--pyxle-nav-progress-height);',
                  '  background: var(--pyxle-nav-progress-color);',
                  '  box-shadow: var(--pyxle-nav-progress-shadow);',
                  '  transform-origin: 0 50%;',
                  '  transform: scaleX(0);',
                  '  opacity: 0;',
                  '  pointer-events: none;',
                  '  z-index: 2147483647;',
                  '  transition: transform 200ms cubic-bezier(0.4, 0, 0.2, 1), opacity 300ms ease-out;',
                  '  will-change: transform, opacity;',
                  '}',
                  '@media (prefers-reduced-motion: reduce) {',
                  '  #' + ELEMENT_ID + ' {',
                  '    transition: opacity 150ms ease-out;',
                  '  }',
                  '}',
                ].join('\\n');
                (document.head || document.documentElement).appendChild(style);
              }

              function ensureElement() {
                if (element && element.isConnected) {
                  return element;
                }
                ensureStyles();
                const existing = document.getElementById(ELEMENT_ID);
                if (existing) {
                  element = existing;
                  return element;
                }
                element = document.createElement('div');
                element.id = ELEMENT_ID;
                element.setAttribute('role', 'progressbar');
                element.setAttribute('aria-label', 'Loading page');
                element.setAttribute('aria-valuemin', '0');
                element.setAttribute('aria-valuemax', '100');
                element.setAttribute('aria-valuenow', '0');
                element.setAttribute('aria-hidden', 'true');
                (document.body || document.documentElement).appendChild(element);
                // Check prefers-reduced-motion once per creation.
                if (typeof window.matchMedia === 'function') {
                  try {
                    prefersReducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
                  } catch (err) {
                    prefersReducedMotion = false;
                  }
                }
                return element;
              }

              function setProgress(value) {
                progress = Math.max(0, Math.min(1, value));
                if (!element) return;
                element.style.transform = 'scaleX(' + progress + ')';
                element.setAttribute('aria-valuenow', String(Math.round(progress * 100)));
              }

              function showBar() {
                const el = ensureElement();
                el.style.opacity = '1';
                el.setAttribute('aria-hidden', 'false');
                progress = 0;
                setProgress(prefersReducedMotion ? 0.3 : 0.08);
                // After the browser commits the initial frame, ramp
                // quickly to 30% so the first tick has meaningful
                // visual progress even on fast connections.
                if (!prefersReducedMotion) {
                  requestAnimationFrame(() => {
                    requestAnimationFrame(() => setProgress(0.3));
                  });
                }
                startTicker();
              }

              function startTicker() {
                stopTicker();
                if (prefersReducedMotion) {
                  // Under reduced motion we hold at 30% with no
                  // ticking — the bar appears static until completion.
                  return;
                }
                tickTimer = window.setInterval(function onTick() {
                  // Decay easing: always crawl toward TARGET_CAP (0.9)
                  // but never reach it, so completion can burst from
                  // wherever we are to 1.0 in the final animation.
                  const next = progress + (TARGET_CAP - progress) * DECAY;
                  setProgress(next);
                }, TICK_MS);
              }

              function stopTicker() {
                if (tickTimer !== null) {
                  window.clearInterval(tickTimer);
                  tickTimer = null;
                }
              }

              function complete() {
                if (pendingTimer !== null) {
                  window.clearTimeout(pendingTimer);
                  pendingTimer = null;
                }
                stopTicker();
                if (!element || element.style.opacity !== '1') {
                  // Bar was never shown (instant nav). Nothing to do.
                  return;
                }
                setProgress(1);
                if (hideTimer !== null) {
                  window.clearTimeout(hideTimer);
                }
                // Give the 200ms transform transition a moment to
                // finish, then fade out. Total completion animation
                // is ~500ms from complete() call to element removal.
                hideTimer = window.setTimeout(function onFadeOut() {
                  if (!element) return;
                  element.style.opacity = '0';
                  element.setAttribute('aria-hidden', 'true');
                  hideTimer = window.setTimeout(function onReset() {
                    if (element) {
                      element.style.transform = 'scaleX(0)';
                      element.setAttribute('aria-valuenow', '0');
                    }
                    hideTimer = null;
                  }, 300);
                }, 200);
              }

              function start() {
                if (isDisabled()) {
                  return;
                }
                activeCount += 1;
                if (activeCount > 1) {
                  // Overlapping nav — keep the existing bar in flight
                  // rather than resetting. The second nav completing
                  // alone will NOT hide the bar (complete() below).
                  return;
                }
                // Schedule the show AFTER the delay so prefetched/
                // cached navs that complete in <150ms never flash
                // the bar.
                if (pendingTimer !== null) {
                  window.clearTimeout(pendingTimer);
                }
                pendingTimer = window.setTimeout(function onShow() {
                  pendingTimer = null;
                  showBar();
                }, SHOW_DELAY_MS);
              }

              function finish() {
                if (activeCount === 0) {
                  return;
                }
                activeCount -= 1;
                if (activeCount > 0) {
                  // Other navigations still in flight — keep the bar.
                  return;
                }
                complete();
              }

              return { start: start, finish: finish };
            })();

            function markNavigating(active) {
              const root = document.documentElement;
              if (root) {
                if (active) {
                  root.setAttribute('data-pyxle-navigation', '1');
                } else {
                  root.removeAttribute('data-pyxle-navigation');
                }
              }
              if (active) {
                navProgress.start();
              } else {
                navProgress.finish();
              }
            }

            async function requestNavigationPayload(url, { useController = true } = {}) {
              const cacheKey = getCacheKey(url);
              if (navigationCache.has(cacheKey)) {
                return navigationCache.get(cacheKey);
              }
              const controller = new AbortController();
              if (useController) {
                if (navigationController) {
                  navigationController.abort();
                }
                navigationController = controller;
              }
              try {
                const response = await fetch(`${url.pathname}${url.search}`, {
                  method: 'GET',
                  credentials: 'same-origin',
                  headers: {
                    [NAVIGATION_HEADER]: '1',
                    'x-requested-with': 'pyxle',
                    accept: 'application/json',
                  },
                  signal: controller.signal,
                  cache: 'no-store',
                });
                const contentType = response.headers.get('content-type') || '';
                if (!contentType.includes('application/json')) {
                  return null;
                }
                const payload = await response.json().catch(() => null);
                if (!payload || payload.ok !== true) {
                  return null;
                }
                navigationCache.set(cacheKey, payload);
                return payload;
              } catch (error) {
                if (!(error instanceof DOMException && error.name === 'AbortError')) {
                  console.error('[Pyxle] Failed to fetch navigation payload', error);
                }
                throw error;
              } finally {
                if (useController && navigationController === controller) {
                  navigationController = null;
                }
              }
            }

            const failedPrefetches = new Set();

            function prefetchNavigation(target) {
              const url = normalizeUrl(target);
              if (!url || url.origin !== window.location.origin) {
                return Promise.resolve(false);
              }
              // Never prefetch the page we're already on. Its data came down
              // with the SSR render (and is seeded into the cache at bootstrap),
              // so a prefetch would just re-run the loader — and any side
              // effects — for no benefit. Guards the window after the seed's
              // TTL lapses, when the cache check below would otherwise miss.
              if (url.pathname === window.location.pathname && url.search === window.location.search) {
                return Promise.resolve(false);
              }
              const cacheKey = getCacheKey(url);
              if (navigationCache.has(cacheKey)) {
                return Promise.resolve(true);
              }
              if (failedPrefetches.has(cacheKey)) {
                return Promise.resolve(false);
              }
              if (navigationPromises.has(cacheKey)) {
                return navigationPromises.get(cacheKey);
              }
              const promise = requestNavigationPayload(url, { useController: false })
                .then(async (payload) => {
                  if (!payload) {
                    failedPrefetches.add(cacheKey);
                    return false;
                  }
                  const pagePath = payload.page?.clientAssetPath;
                  if (pagePath) {
                    await prefetchModule(pagePath);
                  }
                  return true;
                })
                .catch(() => false)
                .finally(() => {
                  navigationPromises.delete(cacheKey);
                });
              navigationPromises.set(cacheKey, promise);
              return promise;
            }

            async function prefetchModule(pagePath) {
              if (!pagePath || moduleCache.has(pagePath)) {
                return true;
              }
              try {
                await loadPageModule(pagePath);
                return true;
              } catch (error) {
                return false;
              }
            }

            // Cross-page hash navigation: the anchor target only exists once
            // the NEXT page's DOM commits, which can trail renderPage by a
            // frame. Poll on animation frames (bounded) so /guide#section
            // scrolls like a native load; an unknown anchor leaves the page
            // at the top, matching browser behaviour for full loads.
            function scrollToHashWhenReady(hash, attempt = 0) {
              let id = hash.startsWith('#') ? hash.slice(1) : hash;
              try { id = decodeURIComponent(id); } catch (error) {}
              if (!id) return;
              const el = document.getElementById(id) || document.getElementsByName(id)[0];
              if (el) {
                el.scrollIntoView({ block: 'start' });
                return;
              }
              if (attempt < 30) {
                requestAnimationFrame(() => scrollToHashWhenReady(hash, attempt + 1));
              }
            }

            async function navigateTo(target, options = {}) {
              const url = normalizeUrl(target);
              if (!url) {
                return false;
              }
              if (url.origin !== window.location.origin) {
                window.location.assign(url.href);
                return false;
              }

              const navToken = ++navigationSequence;
              markNavigating(true);
              try {
                const cacheKey = getCacheKey(url);
                let payload = navigationCache.get(cacheKey);
                if (!payload && navigationPromises.has(cacheKey)) {
                  // A hover/viewport prefetch for this exact URL is already in
                  // flight. Reuse it instead of racing a duplicate request —
                  // otherwise every hover-then-click runs the page's @server
                  // loader twice. The settled prefetch leaves its payload in
                  // navigationCache; on prefetch failure we fall through to a
                  // normal (abortable) fetch below.
                  await navigationPromises.get(cacheKey).catch(() => {});
                  if (navToken !== navigationSequence) {
                    // A newer navigation started while we waited — let it win.
                    return false;
                  }
                  payload = navigationCache.get(cacheKey);
                }
                if (!payload) {
                  payload = await requestNavigationPayload(url, { useController: true });
                }
                if (!payload) {
                  window.location.assign(url.href);
                  return false;
                }

                const nextPagePath = payload.page?.clientAssetPath ?? currentPagePath;
                const nextProps = payload.props ?? {};
                await prefetchModule(nextPagePath);
                updateHead(payload.headMarkup ?? '');
                await renderPage(nextPagePath, nextProps, payload.page?.loadingAssetPath ?? null, payload.page?.errorAssetPath ?? null);

                if (options.updateHistory === false) {
                  window.history.replaceState({ pyxle: true, pagePath: nextPagePath }, '', `${url.pathname}${url.search}${url.hash}`);
                } else {
                  const method = options.replace ? 'replaceState' : 'pushState';
                  window.history[method]({ pyxle: true, pagePath: nextPagePath }, '', `${url.pathname}${url.search}${url.hash}`);
                }

                window.dispatchEvent(new CustomEvent('pyxle:routechange'));

                if (options.scroll !== 'preserve') {
                  window.scrollTo(0, 0);
                  if (url.hash) {
                    scrollToHashWhenReady(url.hash);
                  }
                }

                return true;
              } catch (error) {
                if (!(error instanceof DOMException && error.name === 'AbortError')) {
                  console.error('[Pyxle] Client navigation failed; falling back to full reload', error);
                  window.location.assign(url.href);
                }
                return false;
              } finally {
                markNavigating(false);
              }
            }

            async function refreshCurrentPage() {
              const url = new URL(window.location.href);
              const cacheKey = getCacheKey(url);

              // Evict stale cache so we get a fresh server response.
              navigationCache.delete(cacheKey);

              markNavigating(true);
              try {
                const payload = await requestNavigationPayload(url, { useController: true });
                if (!payload) {
                  return false;
                }

                const nextPagePath = payload.page?.clientAssetPath ?? currentPagePath;
                const nextProps = payload.props ?? {};
                updateHead(payload.headMarkup ?? '');
                await renderPage(nextPagePath, nextProps, payload.page?.loadingAssetPath ?? null, payload.page?.errorAssetPath ?? null);

                // Replace current history entry with fresh state — no scroll change.
                window.history.replaceState(
                  { pyxle: true, pagePath: nextPagePath },
                  '',
                  `${url.pathname}${url.search}${url.hash}`,
                );
                window.dispatchEvent(new CustomEvent('pyxle:routechange'));
                return true;
              } catch (error) {
                if (!(error instanceof DOMException && error.name === 'AbortError')) {
                  console.error('[Pyxle] Refresh failed', error);
                }
                return false;
              } finally {
                markNavigating(false);
              }
            }

            function handleLinkHover(event) {
              if (PREFETCH_TRIGGER !== 'hover') {
                return;
              }
              const anchor = event.target?.closest?.('a[href]');
              if (!anchor || anchor.dataset.pyxleRouter === 'off' || anchor.dataset.pyxlePrefetch === 'off') {
                return;
              }
              const href = anchor.getAttribute('href');
              if (!href || href.startsWith('#')) {
                return;
              }
              const url = normalizeUrl(href);
              if (!url || url.origin !== window.location.origin) {
                return;
              }
              // Skip API routes and static files — only prefetch page routes.
              // (The dot is written [.], not backslash-escaped: this JS lives
              // in a non-raw Python string, where a backslash escape here is
              // a SyntaxWarning on every compile and an error in future Python.)
              const p = url.pathname;
              if (p.startsWith('/api/') || /[.][a-zA-Z0-9]+$/.test(p)) {
                return;
              }
              prefetchNavigation(url).catch(() => {});
            }

            // Read the ``__PYXLE_NAV_SEED__`` blob the server embeds alongside
            // the props: the page's head markup and its navigation-cache TTL.
            function parseNavSeed() {
              try {
                const tag = document.getElementById('__PYXLE_NAV_SEED__');
                return tag && tag.textContent ? JSON.parse(tag.textContent) : null;
              } catch (error) {
                return null;
              }
            }

            // Seed the navigation cache with the page the user landed on, built
            // from data the server already rendered (props + the seed blob). The
            // active self-link's prefetch and any back/forward navigation to this
            // page then resolve from cache, so the loader never re-runs for the
            // page already on screen. Best-effort: a miss just costs a refetch.
            function seedCurrentPage(initialProps) {
              try {
                const seed = parseNavSeed();
                const url = new URL(window.location.href);
                navigationCache.set(getCacheKey(url), {
                  ok: true,
                  routePath: url.pathname,
                  requestedPath: url.pathname,
                  statusCode: 200,
                  page: { clientAssetPath: currentPagePath },
                  props: initialProps,
                  headMarkup: (seed && seed.headMarkup) || '',
                  navCacheTtlSeconds: seed ? seed.navCacheTtlSeconds : null,
                });
              } catch (error) {
                /* ignore — seeding is an optimisation, not a requirement */
              }
            }

            async function bootstrap() {
              const initialProps = parseInitialProps();
              seedCurrentPage(initialProps);
              // The SSR document sets __PYXLE_LOADING_ASSET__ when this route
              // streamed wrapped in a loading.pyxl <Suspense> boundary; the
              // client wraps identically so hydration matches.
              // __PYXLE_ERROR_ASSET__ carries the nearest error.pyxl so the
              // client error boundary can render it on a render fault.
              await renderPage(currentPagePath, initialProps, window.__PYXLE_LOADING_ASSET__ || null, window.__PYXLE_ERROR_ASSET__ || null);
              if (!window.history.state || !window.history.state.pyxle) {
                window.history.replaceState({ pyxle: true, pagePath: currentPagePath }, '', window.location.href);
              }
              
              // Load scripts from metadata
              loadScripts();
            }

            function loadScripts() {
              const scripts = window.__PYXLE_SCRIPTS__ || [];
              const afterInteractiveScripts = [];
              const lazyOnloadScripts = [];
              
              for (const scriptMeta of scripts) {
                const strategy = scriptMeta.strategy || 'afterInteractive';
                if (strategy === 'afterInteractive') {
                  afterInteractiveScripts.push(scriptMeta);
                } else if (strategy === 'lazyOnload') {
                  lazyOnloadScripts.push(scriptMeta);
                }
              }
              
              // Load afterInteractive scripts immediately
              for (const scriptMeta of afterInteractiveScripts) {
                injectScript(scriptMeta);
              }
              
              // Load lazyOnload scripts after idle or on load
              if (lazyOnloadScripts.length > 0) {
                if (typeof requestIdleCallback !== 'undefined') {
                  requestIdleCallback(() => {
                    for (const scriptMeta of lazyOnloadScripts) {
                      injectScript(scriptMeta);
                    }
                  });
                } else {
                  setTimeout(() => {
                    for (const scriptMeta of lazyOnloadScripts) {
                      injectScript(scriptMeta);
                    }
                  }, 1);
                }
              }
            }
            
            function injectScript(scriptMeta) {
              const src = scriptMeta.src;
              if (!src) {
                return;
              }

              // Check if script already exists
              const existing = document.querySelector(`script[src="${src}"]`);
              if (existing) {
                return;
              }

              const script = document.createElement('script');
              script.src = src;

              if (scriptMeta.async) {
                script.async = true;
              }
              if (scriptMeta.defer) {
                script.defer = true;
              }
              if (scriptMeta.module) {
                script.type = 'module';
              } else if (scriptMeta.noModule) {
                script.setAttribute('nomodule', '');
              }

              // Mark load/failure state so the <Script> React component can
              // synchronise with bootstrap-loaded scripts.  Without this, a
              // component that renders the same src after bootstrap
              // finishes would attach load listeners to an already-loaded
              // tag and its onLoad callback would never fire.
              script.addEventListener(
                'load',
                function () { script.setAttribute('data-pyxle-script-loaded', 'true'); },
                { once: true },
              );
              script.addEventListener(
                'error',
                function () { script.setAttribute('data-pyxle-script-failed', 'true'); },
                { once: true },
              );

              document.head.appendChild(script);
            }

            bootstrap().catch(() => {});

            __PYXLE_OVERLAY_BOOTSTRAP__
            document.addEventListener('click', handleLinkClick);
            document.addEventListener('mouseenter', handleLinkHover, { capture: true });
            window.addEventListener('popstate', handlePopState);

            // BFCache restore handler. When the user backgrounds a tab and
            // comes back after a long time, the browser may restore the
            // page from its back-forward cache. The restored DOM is stale
            // (loader data may have changed) and — critically — if the
            // browser served a cached navigation-JSON response instead of
            // fresh HTML during restoration, the user sees raw JSON. A
            // refresh() call re-fetches the current page's loader data
            // from the server and re-renders the component, so the page
            // is always correct after a BFCache restore.
            window.addEventListener('pageshow', function onPageShow(event) {
              if (event.persisted) {
                router.refresh();
              }
            });
            """
        ).strip()
        + "\n"
    )


    overlay_block = dedent(
        """
        const OVERLAY_CONTAINER_ID = '__PYXLE_ERROR_OVERLAY__';
        const OVERLAY_RECONNECT_DELAY = 1000;

        function ensureOverlayRoot() {
          let container = document.getElementById(OVERLAY_CONTAINER_ID);
          if (!container) {
            container = document.createElement('div');
            container.id = OVERLAY_CONTAINER_ID;
            document.body.appendChild(container);
          }
          if (!container.__pyxle_overlay_root) {
            container.__pyxle_overlay_root = ReactDOM.createRoot(container);
          }
          return container.__pyxle_overlay_root;
        }

        function OverlayDocument({ event, stackLines, breadcrumbs }) {
          return React.createElement(
            'div',
            {
              style: {
                position: 'fixed',
                inset: 0,
                backgroundColor: 'rgba(15, 23, 42, 0.92)',
                color: '#f8fafc',
                fontFamily: 'system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif',
                padding: '2rem',
                overflowY: 'auto',
                zIndex: 2147483647,
              },
            },
            [
              React.createElement(
                'div',
                { key: 'header', style: { display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '1.5rem' } },
                [
                  React.createElement(
                    'div',
                    { key: 'title', style: { fontSize: '1.5rem', fontWeight: 700 } },
                    `⚠️ Loader/render error in ${event.routePath}`,
                  ),
                  React.createElement(
                    'div',
                    { key: 'actions', style: { display: 'flex', gap: '0.5rem' } },
                    [
                      React.createElement(
                        'button',
                        {
                          key: 'retry',
                          style: {
                            backgroundColor: '#22c55e',
                            color: '#0f172a',
                            padding: '0.5rem 0.9rem',
                            borderRadius: '0.5rem',
                            fontWeight: 600,
                            border: 'none',
                            cursor: 'pointer',
                          },
                          onClick: () => window.location.reload(),
                        },
                        'Retry',
                      ),
                      React.createElement(
                        'button',
                        {
                          key: 'dismiss',
                          style: {
                            backgroundColor: 'transparent',
                            color: '#f8fafc',
                            padding: '0.5rem 0.9rem',
                            borderRadius: '0.5rem',
                            fontWeight: 600,
                            border: '1px solid rgba(148, 163, 184, 0.6)',
                            cursor: 'pointer',
                          },
                          onClick: clearOverlay,
                        },
                        'Dismiss',
                      ),
                    ],
                  ),
                ],
              ),
              React.createElement(
                'div',
                { key: 'message', style: { marginBottom: '1rem', fontSize: '1.1rem' } },
                event.message,
              ),
              breadcrumbs.length
                ? React.createElement(
                    'div',
                    {
                      key: 'breadcrumbs',
                      style: {
                        display: 'flex',
                        flexDirection: 'column',
                        gap: '0.75rem',
                        marginBottom: '1rem',
                      },
                    },
                    breadcrumbs.map((crumb, index) =>
                      React.createElement(
                        'div',
                        {
                          key: `crumb-${index}`,
                          style: {
                            padding: '0.9rem 1rem',
                            borderRadius: '0.75rem',
                            backgroundColor: 'rgba(148, 163, 184, 0.08)',
                            border: '1px solid rgba(148, 163, 184, 0.2)',
                            display: 'flex',
                            flexDirection: 'column',
                            gap: '0.35rem',
                          },
                        },
                        [
                          React.createElement(
                            'div',
                            {
                              key: 'crumb-header',
                              style: {
                                display: 'flex',
                                justifyContent: 'space-between',
                                alignItems: 'center',
                                fontWeight: 600,
                              },
                            },
                            [
                              React.createElement('span', { key: 'label' }, crumb.label ?? `Stage ${index + 1}`),
                              React.createElement(
                                'span',
                                {
                                  key: 'status',
                                  style: {
                                    textTransform: 'uppercase',
                                    fontSize: '0.75rem',
                                    letterSpacing: '0.08em',
                                    padding: '0.1rem 0.5rem',
                                    borderRadius: '999px',
                                    border: '1px solid rgba(148, 163, 184, 0.6)',
                                  },
                                },
                                String(crumb.status ?? 'unknown').toUpperCase(),
                              ),
                            ],
                          ),
                          crumb.detail
                            ? React.createElement(
                                'p',
                                {
                                  key: 'detail',
                                  style: {
                                    margin: 0,
                                    color: 'rgba(226, 232, 240, 0.85)',
                                    fontSize: '0.9rem',
                                  },
                                },
                                crumb.detail,
                              )
                            : null,
                        ],
                      ),
                    ),
                  )
                : null,
              stackLines.length
                ? React.createElement(
                    'pre',
                    {
                      key: 'stack',
                      style: {
                        backgroundColor: 'rgba(15, 23, 42, 0.6)',
                        borderRadius: '0.75rem',
                        padding: '1rem',
                        fontSize: '0.85rem',
                        lineHeight: 1.5,
                        whiteSpace: 'pre-wrap',
                      },
                    },
                    stackLines.join('\\n'),
                  )
                : null,
            ],
          );
        }

        function renderOverlay(event) {
          const root = ensureOverlayRoot();
          const stackLines = (event.stack ?? '').split('\\n').filter(Boolean);
          const breadcrumbs = Array.isArray(event.breadcrumbs) ? event.breadcrumbs : [];
          root.render(
            React.createElement(OverlayDocument, { event, stackLines, breadcrumbs }),
          );
        }

        function clearOverlay() {
          const container = document.getElementById(OVERLAY_CONTAINER_ID);
          if (!container || !container.__pyxle_overlay_root) {
            return;
          }
          container.__pyxle_overlay_root.render(null);
        }

        function connectOverlayChannel() {
          const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
          const url = `${protocol}//${window.location.host}/__pyxle__/overlay`;
          const socket = new WebSocket(url);

          socket.onmessage = (event) => {
            try {
              const payload = JSON.parse(event.data);
              if (payload.type === 'error') {
                renderOverlay(payload.payload ?? {});
              } else if (payload.type === 'clear') {
                clearOverlay();
              } else if (payload.type === 'reload') {
                const changed = Array.isArray(payload.payload?.changedPaths)
                  ? payload.payload.changedPaths
                  : [];
                const reason = changed.length ? changed.join(', ') : 'server changes';
                console.info(`[Pyxle] Reloading due to ${reason}`);
                window.location.reload();
              } else if (payload.type === 'log') {
                // Dev-only: a server-side log record forwarded so it surfaces
                // in the browser devtools console. `level` names the console
                // method to call; fall back to `console.log` if unknown.
                const data = payload.payload ?? {};
                const level = data.level;
                const method = typeof console[level] === 'function' ? level : 'log';
                const source = data.logger
                  ? `[pyxle:server ${data.logger}]`
                  : '[pyxle:server]';
                console[method](source, data.message ?? '');
              }
            } catch (error) {
              console.error('[Pyxle] Failed to parse overlay message', error);
            }
          };

          socket.onclose = () => {
            setTimeout(connectOverlayChannel, OVERLAY_RECONNECT_DELAY);
          };

          socket.onerror = () => {
            socket.close();
          };
        }
        """
    ).strip()
    overlay_bootstrap_call = "connectOverlayChannel();\n"

    if settings.debug:
        overlay_injection = overlay_block + "\n\n" if overlay_block else ""
        content = content.replace("__PYXLE_OVERLAY_BLOCK__", overlay_injection, 1)
        content = content.replace("__PYXLE_OVERLAY_BOOTSTRAP__", overlay_bootstrap_call, 1)
    else:
        content = content.replace("__PYXLE_OVERLAY_BLOCK__", "", 1)
        content = content.replace("__PYXLE_OVERLAY_BOOTSTRAP__", "", 1)

    script_block = ""
    if settings.global_scripts:
      script_lines = [f"import '{script.import_specifier}';" for script in settings.global_scripts]
      script_block = "\n".join(script_lines) + "\n"
    content = content.replace("__PYXLE_GLOBAL_SCRIPT_IMPORTS__\n", script_block, 1)

    style_block = ""
    if settings.global_stylesheets:
      style_lines = [f"import '{sheet.import_specifier}';" for sheet in settings.global_stylesheets]
      style_block = "\n".join(style_lines) + "\n"
    content = content.replace("__PYXLE_GLOBAL_STYLE_IMPORTS__\n", style_block, 1)
    return content


def _render_tsconfig() -> str:
    return (
        dedent(
            """
            {
              "compilerOptions": {
                "target": "ESNext",
                "useDefineForClassFields": true,
                "module": "ESNext",
                "moduleResolution": "Bundler",
                "strict": true,
                "jsx": "react-jsx",
                "esModuleInterop": true,
                "allowJs": true,
                "allowSyntheticDefaultImports": true,
                "resolveJsonModule": true,
                "isolatedModules": true,
                "skipLibCheck": true,
                "paths": {
                  "/pages/*": ["./pages/*"],
                  "/routes/*": ["./routes/*"],
                  "pyxle/client": ["./pyxle/client"],
                  "pyxle/client/*": ["./pyxle/*"]
                },
                "types": ["vite/client"]
              },
              "include": [
                "./client-entry.js",
                "./pages/**/*.jsx",
                "./pyxle/**/*"
              ]
            }
            """
        ).strip()
        + "\n"
    )


def _render_script_component() -> str:
    return (
        dedent(
            """
            /**
             * Framework-owned Script component for Pyxle.
             *
             * Strategies
             *   beforeInteractive  Statically extracted + injected in SSR <head>.
             *                      A dynamically-rendered instance can't honour
             *                      that contract (page already interactive), so
             *                      we warn and degrade to afterInteractive.
             *   afterInteractive   Loads on mount after hydration (default).
             *   lazyOnload         Loads on idle (requestIdleCallback / setTimeout).
             *
             * All loads are deduplicated by src across component instances AND
             * the framework's bootstrap loader — exactly one request per URL.
             */

            import React from 'react';

            const LOADED_ATTR = 'data-pyxle-script-loaded';
            const FAILED_ATTR = 'data-pyxle-script-failed';
            const scriptPromises = new Map();

            function ensureScriptLoaded(src, options) {
              const cached = scriptPromises.get(src);
              if (cached) return cached;

              const escape = (typeof CSS !== 'undefined' && CSS.escape) || ((s) => s);
              const existing = document.querySelector('script[src="' + escape(src) + '"]');
              if (existing) {
                const promise = new Promise((resolve, reject) => {
                  if (existing.getAttribute(LOADED_ATTR) === 'true') {
                    resolve();
                  } else if (existing.getAttribute(FAILED_ATTR) === 'true') {
                    reject(new Error('Script previously failed to load: ' + src));
                  } else {
                    existing.addEventListener('load', () => resolve(), { once: true });
                    existing.addEventListener('error', () => reject(new Error('Failed to load script: ' + src)), { once: true });
                  }
                });
                scriptPromises.set(src, promise);
                return promise;
              }

              const script = document.createElement('script');
              script.src = src;
              if (options.async) script.async = true;
              if (options.defer) script.defer = true;
              if (options.module) script.type = 'module';
              if (options.noModule) script.setAttribute('nomodule', '');
              if (options.crossOrigin) script.crossOrigin = options.crossOrigin;
              if (options.integrity) script.integrity = options.integrity;
              if (options.referrerPolicy) script.referrerPolicy = options.referrerPolicy;

              const promise = new Promise((resolve, reject) => {
                script.addEventListener('load', () => {
                  script.setAttribute(LOADED_ATTR, 'true');
                  resolve();
                }, { once: true });
                script.addEventListener('error', () => {
                  script.setAttribute(FAILED_ATTR, 'true');
                  reject(new Error('Failed to load script: ' + src));
                }, { once: true });
              });

              document.head.appendChild(script);
              scriptPromises.set(src, promise);
              return promise;
            }

            export function Script({
              src,
              strategy = 'afterInteractive',
              async: asyncProp,
              defer,
              module,
              noModule,
              onLoad,
              onError,
              children,
              ...attrs
            }) {
              if (typeof window === 'undefined') return null;

              React.useEffect(() => {
                if (!src) {
                  if (typeof children !== 'string' || children.length === 0) return undefined;
                  const script = document.createElement('script');
                  script.textContent = children;
                  if (module) script.type = 'module';
                  document.head.appendChild(script);
                  if (onLoad) onLoad();
                  return () => {
                    if (script.parentNode) script.parentNode.removeChild(script);
                  };
                }

                let effectiveStrategy = strategy;
                if (effectiveStrategy === 'beforeInteractive') {
                  console.warn(
                    '[Pyxle Script] strategy="beforeInteractive" requires the ' +
                    '<Script> to be statically present in a .pyxl file at build ' +
                    'time. Falling back to "afterInteractive" for dynamically ' +
                    'rendered src: ' + src
                  );
                  effectiveStrategy = 'afterInteractive';
                }

                const load = () => {
                  ensureScriptLoaded(src, {
                    async: asyncProp,
                    defer,
                    module,
                    noModule,
                    crossOrigin: attrs.crossOrigin,
                    integrity: attrs.integrity,
                    referrerPolicy: attrs.referrerPolicy,
                  }).then(
                    () => { if (onLoad) onLoad(); },
                    (err) => { if (onError) onError(err); }
                  );
                };

                if (effectiveStrategy === 'lazyOnload') {
                  if (typeof requestIdleCallback === 'function') {
                    const handle = requestIdleCallback(load);
                    return () => {
                      if (typeof cancelIdleCallback === 'function') cancelIdleCallback(handle);
                    };
                  }
                  const handle = setTimeout(load, 200);
                  return () => clearTimeout(handle);
                }

                load();
                return undefined;
              }, [src, strategy, module, noModule]);

              return null;
            }

            export default Script;
            """
        ).strip()
        + "\n"
    )


def _render_image_component() -> str:
    return (
        dedent(
            """
            /**
             * <Image> — an optimized <img> on par with Next.js's Image, for the
             * parts that don't need a server-side optimizer:
             *   - Responsive `srcset`/`sizes` generated from a `loader` (a CDN
             *     such as Cloudinary/imgix, or a build plugin). Without a loader
             *     it stays a plain <img> — resizing needs a real backend, so we
             *     never emit a fake srcset that re-downloads the full image.
             *   - `fill` mode (image fills a positioned parent).
             *   - `priority` (eager + `fetchpriority="high"`) for the LCP image.
             *   - Layout-shift prevention: width/height give the intrinsic ratio.
             *   - Blur-up placeholder, `fallbackSrc`, native lazy-loading,
             *     onLoad/onError, and a `data-pyxle-image-state` attribute.
             *
             * Actual byte optimization (compression, WebP/AVIF) is opt-in via
             * `loader` — see the Image optimization guide.
             */

            import React from 'react';

            const STATE_LOADING = 'loading';
            const STATE_LOADED = 'loaded';
            const STATE_ERROR = 'error';

            // Responsive width ladder (mirrors Next.js deviceSizes + imageSizes).
            const DEVICE_SIZES = [640, 750, 828, 1080, 1200, 1920, 2048, 3840];
            const IMAGE_SIZES = [16, 32, 48, 64, 96, 128, 256, 384];
            const ALL_SIZES = [...IMAGE_SIZES, ...DEVICE_SIZES].sort((a, b) => a - b);

            function isPassthroughSrc(src) {
              return typeof src !== 'string' || /^data:/.test(src) || /^blob:/.test(src);
            }

            // Which widths to put in the srcset. With `sizes`/`fill` the rendered
            // size is unknown, so offer the full device ladder; for a fixed width,
            // 1x and 2x (retina).
            function candidateWidths(width, sizes, fill) {
              if (fill || sizes) return DEVICE_SIZES;
              const w = Number(width) || 0;
              if (!w) return DEVICE_SIZES;
              const atLeast = (target) =>
                ALL_SIZES.find((s) => s >= target) || ALL_SIZES[ALL_SIZES.length - 1];
              return Array.from(new Set([atLeast(w), atLeast(w * 2)]));
            }

            export const Image = React.forwardRef(function PyxleImage(
              {
                src,
                alt = '',
                width,
                height,
                fill = false,
                sizes,
                quality,
                loader,
                objectFit,
                priority = false,
                lazy = true,
                placeholder = 'empty',
                blurDataURL,
                placeholderColor = '#e5e5e5',
                fallbackSrc,
                onLoad,
                onError,
                className,
                style,
                ...props
              },
              forwardedRef
            ) {
              const [state, setState] = React.useState(STATE_LOADING);
              const [currentSrc, setCurrentSrc] = React.useState(src);
              const internalRef = React.useRef(null);
              const setRef = (node) => {
                internalRef.current = node;
                if (typeof forwardedRef === 'function') forwardedRef(node);
                else if (forwardedRef) forwardedRef.current = node;
              };

              React.useEffect(() => {
                setState(STATE_LOADING);
                setCurrentSrc(src);
              }, [src]);

              // Cached images skip the load event — detect via `.complete`
              // and sync state manually so onLoad still fires.  Symmetrically,
              // a broken SSR-rendered src may have finished its failed fetch
              // before React hydrated (so the native `error` event fired
              // without a synthetic listener attached): detect that via
              // `complete && naturalWidth === 0` and drive the fallback path.
              React.useEffect(() => {
                const el = internalRef.current;
                if (!el || !el.complete || state !== STATE_LOADING) return;
                if (el.naturalWidth > 0) {
                  setState(STATE_LOADED);
                  if (onLoad) onLoad({ nativeEvent: null, target: el, fromCache: true });
                } else {
                  if (fallbackSrc && currentSrc !== fallbackSrc) {
                    setCurrentSrc(fallbackSrc);
                  } else {
                    setState(STATE_ERROR);
                    if (onError) onError({ nativeEvent: null, target: el });
                  }
                }
                // eslint-disable-next-line react-hooks/exhaustive-deps
              }, [currentSrc]);

              const handleLoad = (event) => {
                setState(STATE_LOADED);
                if (onLoad) onLoad(event);
              };

              const handleError = (event) => {
                if (fallbackSrc && currentSrc !== fallbackSrc) {
                  setCurrentSrc(fallbackSrc);
                  return;
                }
                setState(STATE_ERROR);
                if (onError) onError(event);
              };

              // Resolve src + srcset through the loader, when one is configured.
              const usesLoader = typeof loader === 'function' && !isPassthroughSrc(currentSrc);
              const widths = usesLoader ? candidateWidths(width, sizes, fill) : null;
              const resolvedSrc = usesLoader
                ? loader({ src: currentSrc, width: widths[widths.length - 1], quality })
                : currentSrc;
              const srcSet = usesLoader
                ? widths
                    .map((w) => loader({ src: currentSrc, width: w, quality }) + ' ' + w + 'w')
                    .join(', ')
                : undefined;
              const resolvedSizes = sizes || (fill ? '100vw' : undefined);

              const showPlaceholder = placeholder === 'blur' && state === STATE_LOADING;
              const mergedStyle = {
                ...(showPlaceholder
                  ? {
                      backgroundColor: blurDataURL ? undefined : placeholderColor,
                      backgroundImage: blurDataURL ? `url("${blurDataURL}")` : undefined,
                      backgroundSize: 'cover',
                      backgroundPosition: 'center',
                      backgroundRepeat: 'no-repeat',
                      filter: blurDataURL ? 'blur(20px)' : undefined,
                    }
                  : {}),
                transition: placeholder === 'blur' ? 'filter 250ms ease-out' : undefined,
                ...(fill
                  ? {
                      position: 'absolute',
                      inset: 0,
                      width: '100%',
                      height: '100%',
                      objectFit: objectFit || 'cover',
                    }
                  : objectFit
                    ? { objectFit }
                    : {}),
                ...style,
              };

              return (
                <img
                  ref={setRef}
                  src={resolvedSrc}
                  srcSet={srcSet}
                  sizes={resolvedSizes}
                  alt={alt}
                  width={fill ? undefined : width}
                  height={fill ? undefined : height}
                  loading={priority ? 'eager' : lazy ? 'lazy' : 'eager'}
                  decoding={priority ? 'sync' : 'async'}
                  {...(priority ? { fetchpriority: 'high' } : {})}
                  onLoad={handleLoad}
                  onError={handleError}
                  className={className}
                  style={mergedStyle}
                  data-pyxle-image-state={state}
                  {...props}
                />
              );
            });

            Image.displayName = 'PyxleImage';
            export default Image;
            """
        ).strip()
        + "\n"
    )


def _render_head_component() -> str:
    return (
        dedent(
            """
            import React from 'react';
            import { renderToStaticMarkup } from 'react-dom/server';

            /**
             * <Head> — declare elements that belong in the document <head>.
             *
             *   • SSR    — registers children with the framework's head
             *              registry so they land in the initial HTML.
             *   • Client — adopts the equivalent SSR-rendered elements on
             *              mount (no duplication), applies fresh ones on
             *              state-driven updates, and cleans up on unmount
             *              (restoring the prior <title>).
             */

            const OWNER_ATTR = 'data-pyxle-head-client';
            const KEY_ATTRS = ['name', 'property', 'rel', 'href', 'src', 'charset', 'http-equiv'];

            function findEquivalentHeadElement(target) {
              const tag = target.tagName.toLowerCase();
              const keyAttr = KEY_ATTRS.find((a) => target.hasAttribute(a));
              if (!keyAttr) return null;
              const keyValue = target.getAttribute(keyAttr);
              const escape = (typeof CSS !== 'undefined' && CSS.escape) || ((s) => s);
              const selector = tag + '[' + keyAttr + '="' + escape(keyValue) + '"]:not([' + OWNER_ATTR + '])';
              try {
                return document.head.querySelector(selector);
              } catch (_err) {
                return null;
              }
            }

            function applyHeadMarkup(markup) {
              if (!markup) return { nodes: [], previousTitle: null };
              const template = document.createElement('template');
              template.innerHTML = markup;
              const parsed = Array.from(template.content.childNodes).filter(
                (n) => n.nodeType === 1
              );
              const nodes = [];
              let previousTitle = null;
              for (const declared of parsed) {
                if (declared.tagName === 'TITLE') {
                  if (previousTitle === null) previousTitle = document.title;
                  document.title = declared.textContent || '';
                  continue;
                }
                const existing = findEquivalentHeadElement(declared);
                if (existing) {
                  existing.setAttribute(OWNER_ATTR, '');
                  nodes.push(existing);
                } else {
                  declared.setAttribute(OWNER_ATTR, '');
                  document.head.appendChild(declared);
                  nodes.push(declared);
                }
              }
              return { nodes, previousTitle };
            }

            // Normalise the children tree before rendering to coerce the
            // common ``<title>{x} — Brand</title>`` pattern into a single
            // text node. JSX compiles that to multiple children, and
            // React then warns "A title element received an array with
            // more than 1 element". We fix it here so every Pyxle app
            // doesn't have to use template literals for titles.
            function coerceTitleChildren(children) {
              return React.Children.map(children, (child) => {
                if (!React.isValidElement(child)) return child;
                if (child.type !== 'title') return child;
                const kids = React.Children.toArray(child.props.children);
                if (kids.length <= 1) return child;
                if (!kids.every((k) => typeof k === 'string' || typeof k === 'number')) {
                  return child;
                }
                return React.cloneElement(child, {}, kids.join(''));
              });
            }

            export const Head = React.forwardRef(({ children }, ref) => {
              const normalised = coerceTitleChildren(children);

              // SSR: register with the framework registry; renders nothing.
              if (typeof window === 'undefined') {
                if (typeof globalThis.__PYXLE_HEAD_REGISTRY__ !== 'undefined') {
                  try {
                    const headMarkup = renderToStaticMarkup(
                      React.createElement(React.Fragment, null, normalised)
                    );
                    globalThis.__PYXLE_HEAD_REGISTRY__.register(headMarkup);
                  } catch (error) {
                    console.error('[Pyxle Head] SSR extraction failed:', error);
                  }
                }
                return null;
              }

              // Client: render children to a static string so the effect's
              // dependency is stable across renders with identical children.
              let markup = '';
              try {
                markup = renderToStaticMarkup(
                  React.createElement(React.Fragment, null, normalised)
                );
              } catch (error) {
                console.error('[Pyxle Head] client render failed:', error);
              }

              React.useEffect(() => {
                const { nodes, previousTitle } = applyHeadMarkup(markup);
                return () => {
                  for (const node of nodes) {
                    if (node.parentNode) node.parentNode.removeChild(node);
                  }
                  if (previousTitle !== null) {
                    document.title = previousTitle;
                  }
                };
              }, [markup]);

              return null;
            });

            Head.displayName = 'PyxleHead';
            export default Head;
            """
        ).strip()
        + "\n"
    )


def _render_client_only_component() -> str:
    return (
        dedent(
            """
            import React from 'react';

            const ClientOnly = React.forwardRef(({ children, fallback }, ref) => {
              const [isClient, setIsClient] = React.useState(false);

              React.useEffect(() => {
                setIsClient(true);
              }, []);

              if (!isClient) {
                return fallback ?? React.createElement('div');
              }

              return React.createElement(React.Fragment, null, children);
            });

            ClientOnly.displayName = 'ClientOnly';
            export default ClientOnly;
            """
        ).strip()
        + "\n"
    )


def _render_script_component_types() -> str:
    return (
        dedent(
            """
            import type React from 'react';

            export interface ScriptProps {
              /** URL of the external script. Omit to use `children` as inline code. */
              src?: string;
              /** When to load the script. Defaults to 'afterInteractive'. */
              strategy?: 'beforeInteractive' | 'afterInteractive' | 'lazyOnload';
              async?: boolean;
              defer?: boolean;
              module?: boolean;
              noModule?: boolean;
              crossOrigin?: 'anonymous' | 'use-credentials' | '';
              integrity?: string;
              referrerPolicy?: React.HTMLAttributeReferrerPolicy;
              /** Inline script source (used when `src` is omitted). */
              children?: string;
              /** Fires once the script finishes loading. */
              onLoad?: () => void;
              /** Fires if loading fails. */
              onError?: (error: Error) => void;
            }

            export declare const Script: React.FC<ScriptProps>;
            export default Script;
            """
        ).strip()
        + "\n"
    )


def _render_image_component_types() -> str:
    return (
        dedent(
            """
            import type React from 'react';

            export type PyxleImageState = 'loading' | 'loaded' | 'error';

            export interface PyxleImageLoadEvent {
              nativeEvent: Event | null;
              target: HTMLImageElement;
              fromCache: boolean;
            }

            /** Arguments passed to a custom image `loader`. */
            export interface ImageLoaderProps {
              src: string;
              width: number;
              quality?: number;
            }

            /** Builds an optimized URL for a given width — e.g. a CDN endpoint. */
            export type ImageLoader = (props: ImageLoaderProps) => string;

            export interface ImageProps extends Omit<React.ImgHTMLAttributes<HTMLImageElement>, 'onLoad' | 'onError' | 'placeholder' | 'loader'> {
              src: string;
              width?: number | string;
              height?: number | string;
              alt?: string;
              /** Fill the nearest positioned ancestor (omit width/height). */
              fill?: boolean;
              /** `sizes` attribute — drives which srcset candidate the browser picks. */
              sizes?: string;
              /** Quality (1-100) passed to the `loader`. */
              quality?: number;
              /**
               * Builds optimized URLs per width. With a loader, `<Image>` emits a
               * responsive `srcset`; without one it stays a plain <img> (resizing
               * needs a backend — a CDN or build plugin). See the guide.
               */
              loader?: ImageLoader;
              /** CSS `object-fit` (applied to the <img>; defaults to `cover` under `fill`). */
              objectFit?: React.CSSProperties['objectFit'];
              /** Load eagerly (`loading="eager"`, `decoding="sync"`, `fetchpriority="high"`) — use for the LCP image. */
              priority?: boolean;
              /** Explicit lazy-load. Ignored when `priority` is true. Default: true. */
              lazy?: boolean;
              /** `"blur"` renders a background placeholder until the image loads. */
              placeholder?: 'empty' | 'blur';
              /** Data URL (or any valid url()) used as the blur placeholder. */
              blurDataURL?: string;
              /** Solid colour used when `placeholder="blur"` but no blurDataURL is provided. */
              placeholderColor?: string;
              /** If set, replaces `src` transparently when the original fails. */
              fallbackSrc?: string;
              /** Fires once on load (including the synthetic cache-hit path). */
              onLoad?: (event: PyxleImageLoadEvent | React.SyntheticEvent<HTMLImageElement>) => void;
              /** Fires once on error (after `fallbackSrc` has been tried, if set). */
              onError?: (event: React.SyntheticEvent<HTMLImageElement>) => void;
            }

            export declare const Image: React.ForwardRefExoticComponent<ImageProps & React.RefAttributes<HTMLImageElement>>;
            export default Image;
            """
        ).strip()
        + "\n"
    )


def _render_head_component_types() -> str:
    return (
        dedent(
            """
            import type React from 'react';

            export interface HeadProps {
              children?: React.ReactNode;
            }

            export declare const Head: React.ForwardRefExoticComponent<HeadProps & React.RefAttributes<HTMLDivElement>>;
            export default Head;
            """
        ).strip()
        + "\n"
    )


def _render_client_only_component_types() -> str:
    return (
        dedent(
            """
            import type React from 'react';

            export interface ClientOnlyProps {
              children: React.ReactNode;
              fallback?: React.ReactNode;
            }

            export declare const ClientOnly: React.ForwardRefExoticComponent<ClientOnlyProps & React.RefAttributes<HTMLDivElement>>;
            export default ClientOnly;
            """
        ).strip()
        + "\n"
    )


def _render_use_action_component() -> str:
    return (
        dedent(
            """
            import { useState, useCallback, useRef } from 'react';
            import { invalidate } from './index.js';

            // Cookie / header names honour ``csrf.cookieName`` and
            // ``csrf.headerName`` from pyxle.config.json. The document
            // shell injects the effective names as window globals —
            // including the auto (port-namespaced) ``pyxle-csrf-<port>``
            // default, which the client cannot derive itself (the bind
            // port is invisible behind a reverse proxy). The bare
            // fallbacks below only apply when no global was injected
            // (e.g. a pinned default name).
            function csrfCookieName() {
              if (typeof globalThis !== 'undefined' && typeof globalThis.__PYXLE_CSRF_COOKIE__ === 'string' && globalThis.__PYXLE_CSRF_COOKIE__) {
                return globalThis.__PYXLE_CSRF_COOKIE__;
              }
              return 'pyxle-csrf';
            }

            function csrfHeaderName() {
              if (typeof globalThis !== 'undefined' && typeof globalThis.__PYXLE_CSRF_HEADER__ === 'string' && globalThis.__PYXLE_CSRF_HEADER__) {
                return globalThis.__PYXLE_CSRF_HEADER__;
              }
              return 'x-csrf-token';
            }

            // Resolve the active CSRF token. Three sources, in order:
            //   1. ``document.cookie`` — the live value on the client.
            //   2. ``globalThis.__PYXLE_CSRF_TOKEN__`` — set by the SSR
            //      pipeline from the request's CSRF middleware. Used
            //      during SSR (where ``document`` is undefined).
            //   3. ``""`` — CSRF disabled or token unknown.
            function getCsrfToken() {
              if (typeof document !== 'undefined') {
                const cookieName = csrfCookieName();
                for (const part of document.cookie.split(';')) {
                  const eq = part.indexOf('=');
                  if (eq === -1) continue;
                  if (part.slice(0, eq).trim() === cookieName) {
                    return decodeURIComponent(part.slice(eq + 1));
                  }
                }
              }
              if (typeof globalThis !== 'undefined' && typeof globalThis.__PYXLE_CSRF_TOKEN__ === 'string') {
                return globalThis.__PYXLE_CSRF_TOKEN__;
              }
              return '';
            }

            // Honour ``x-pyxle-invalidate: /path/a, /path/b`` on any
            // fetch response. Each listed URL is dropped from the
            // client nav cache so the next ``navigate()`` refetches
            // fresh loader data. No-ops if the header is absent or
            // malformed; errors are swallowed because this is
            // best-effort plumbing.
            function _applyInvalidationHeader(response) {
              try {
                const raw = response && response.headers
                  ? response.headers.get('x-pyxle-invalidate')
                  : null;
                if (!raw) return;
                const urls = raw.split(',').map((u) => u.trim()).filter(Boolean);
                for (const url of urls) {
                  try { invalidate(url); } catch { /* ignore */ }
                }
              } catch {
                /* ignore */
              }
            }

            function resolveActionUrl(actionName, pagePath) {
              let page = pagePath;
              if (!page) {
                if (typeof window !== 'undefined') {
                  page = window.location.pathname;
                } else if (typeof globalThis.__PYXLE_CURRENT_PATHNAME__ === 'string') {
                  // SSR: use the framework-injected request path so the action
                  // URL matches what the client will resolve at hydration.
                  page = globalThis.__PYXLE_CURRENT_PATHNAME__;
                } else {
                  page = '/';
                }
              }
              const segment = page.replace(/^\\//, '') || 'index';
              return `/api/__actions/${segment}/${actionName}`;
            }

            export function useAction(actionName, options = {}) {
              const { pagePath, onMutate } = options;
              const [pending, setPending] = useState(false);
              const [error, setError] = useState(null);
              const [fields, setFields] = useState(null);
              const [data, setData] = useState(null);
              const abortRef = useRef(null);

              const execute = useCallback(
                async (payload) => {
                  if (abortRef.current) {
                    abortRef.current.abort();
                  }
                  const controller = new AbortController();
                  abortRef.current = controller;

                  setError(null);
                  setFields(null);
                  setPending(true);

                  if (typeof onMutate === 'function') {
                    onMutate(payload);
                  }

                  try {
                    const url = resolveActionUrl(actionName, pagePath);
                    const csrfToken = getCsrfToken();
                    const headers = { 'Content-Type': 'application/json' };
                    if (csrfToken) headers[csrfHeaderName()] = csrfToken;
                    const response = await fetch(url, {
                      method: 'POST',
                      headers,
                      body: JSON.stringify(payload ?? {}),
                      signal: controller.signal,
                    });

                    // Honour ``x-pyxle-invalidate`` from the server so
                    // the action's cache invalidations take effect on
                    // the very next ``navigate()``, without the caller
                    // having to wire it up.
                    _applyInvalidationHeader(response);

                    const json = await response.json();

                    if (!response.ok || json.ok === false) {
                      const message = json.error ?? `Action failed with status ${response.status}`;
                      const fieldErrors = json.fields ?? null;
                      setError(message);
                      setFields(fieldErrors);
                      return { ok: false, error: message, fields: fieldErrors, data: json.data ?? null };
                    }

                    const { ok: _ok, error: _err, fields: _fields, ...rest } = json;
                    setData(rest);
                    return { ok: true, ...rest };
                  } catch (err) {
                    if (err.name === 'AbortError') {
                      return { ok: false, error: 'Request aborted', fields: null };
                    }
                    const message = err.message ?? 'Network error';
                    setError(message);
                    return { ok: false, error: message, fields: null };
                  } finally {
                    if (abortRef.current === controller) {
                      setPending(false);
                      abortRef.current = null;
                    }
                  }
                },
                [actionName, pagePath, onMutate],
              );

              execute.pending = pending;
              execute.error = error;
              execute.fields = fields;
              execute.data = data;

              return execute;
            }
            """
        ).strip()
        + "\n"
    )


def _render_use_action_component_types() -> str:
    return (
        dedent(
            """
            /** Per-field validation errors: field path -> messages. */
            export type ActionFieldErrors = Record<string, string[]>;

            export interface UseActionOptions {
              /**
               * The page the action belongs to (e.g. ``"/todos"``). Defaults to
               * the current request path, so you rarely need to set it.
               */
              pagePath?: string;
              /** Called with the payload the moment a submit starts (optimistic UI). */
              onMutate?(payload: unknown): void;
            }

            /** The resolved result of invoking an action. */
            export type ActionResult<TData = Record<string, unknown>> =
              | ({ ok: true } & TData)
              | {
                  ok: false;
                  /** Human-readable error message. */
                  error: string;
                  /**
                   * Field-level validation errors when the server rejected the
                   * request body (HTTP 422), or ``null`` for any other failure.
                   */
                  fields: ActionFieldErrors | null;
                  /** Structured error payload, when the action attached one. */
                  data?: unknown;
                };

            /**
             * A callable action invoker. Call it with the request body to run
             * the action; reactive status is exposed as properties on the
             * function itself.
             */
            export interface ActionInvoker<TData = Record<string, unknown>> {
              (payload?: unknown): Promise<ActionResult<TData>>;
              /** True while a request is in flight. */
              pending: boolean;
              /** The last error message, or null. */
              error: string | null;
              /**
               * Field-level validation errors from the last failed submit, or
               * null. Cleared at the start of every new submit.
               */
              fields: ActionFieldErrors | null;
              /** The last successful result payload, or null. */
              data: TData | null;
            }

            /**
             * Bind a typed invoker to a named ``@action`` on the current page.
             *
             * The returned value is a function you call with the request body;
             * it also carries ``pending``, ``error``, ``fields`` and ``data``
             * so a form can render inline validation messages without extra
             * state.
             */
            export declare function useAction<TData = Record<string, unknown>>(
              actionName: string,
              options?: UseActionOptions
            ): ActionInvoker<TData>;
            """
        ).strip()
        + "\n"
    )


def _render_form_component() -> str:
    return (
        dedent(
            """
            import React, { useRef, useState, useCallback } from 'react';
            import { invalidate } from './index.js';

            // Cookie / header names honour ``csrf.cookieName`` and
            // ``csrf.headerName`` from pyxle.config.json. The document
            // shell injects the effective names as window globals —
            // including the auto (port-namespaced) ``pyxle-csrf-<port>``
            // default, which the client cannot derive itself (the bind
            // port is invisible behind a reverse proxy). The bare
            // fallbacks below only apply when no global was injected
            // (e.g. a pinned default name).
            function csrfCookieName() {
              if (typeof globalThis !== 'undefined' && typeof globalThis.__PYXLE_CSRF_COOKIE__ === 'string' && globalThis.__PYXLE_CSRF_COOKIE__) {
                return globalThis.__PYXLE_CSRF_COOKIE__;
              }
              return 'pyxle-csrf';
            }

            function csrfHeaderName() {
              if (typeof globalThis !== 'undefined' && typeof globalThis.__PYXLE_CSRF_HEADER__ === 'string' && globalThis.__PYXLE_CSRF_HEADER__) {
                return globalThis.__PYXLE_CSRF_HEADER__;
              }
              return 'x-csrf-token';
            }

            // Resolve the active CSRF token. Three sources, in order:
            //   1. ``document.cookie`` — the live value on the client.
            //   2. ``globalThis.__PYXLE_CSRF_TOKEN__`` — set by the SSR
            //      pipeline. Available during SSR + first hydrate so
            //      the rendered ``<input type="hidden" name="_csrf_token">``
            //      matches what the browser will see.
            //   3. ``""`` — CSRF disabled or token unknown.
            function getCsrfToken() {
              if (typeof document !== 'undefined') {
                const cookieName = csrfCookieName();
                for (const part of document.cookie.split(';')) {
                  const eq = part.indexOf('=');
                  if (eq === -1) continue;
                  if (part.slice(0, eq).trim() === cookieName) {
                    return decodeURIComponent(part.slice(eq + 1));
                  }
                }
              }
              if (typeof globalThis !== 'undefined' && typeof globalThis.__PYXLE_CSRF_TOKEN__ === 'string') {
                return globalThis.__PYXLE_CSRF_TOKEN__;
              }
              return '';
            }

            // See useAction for the rationale — comma-split the header
            // and invalidate each listed URL.
            function _applyInvalidationHeader(response) {
              try {
                const raw = response && response.headers
                  ? response.headers.get('x-pyxle-invalidate')
                  : null;
                if (!raw) return;
                const urls = raw.split(',').map((u) => u.trim()).filter(Boolean);
                for (const url of urls) {
                  try { invalidate(url); } catch { /* ignore */ }
                }
              } catch {
                /* ignore */
              }
            }

            function resolveActionUrl(actionName, pagePath) {
              let page = pagePath;
              if (!page) {
                if (typeof window !== 'undefined') {
                  page = window.location.pathname;
                } else if (typeof globalThis.__PYXLE_CURRENT_PATHNAME__ === 'string') {
                  // SSR: use the framework-injected request path so the action
                  // URL matches what the client will resolve at hydration.
                  page = globalThis.__PYXLE_CURRENT_PATHNAME__;
                } else {
                  page = '/';
                }
              }
              const segment = page.replace(/^\\//, '') || 'index';
              return `/api/__actions/${segment}/${actionName}`;
            }

            export function Form({
              action,
              pagePath,
              onSuccess,
              onError,
              resetOnSuccess = true,
              children,
              ...rest
            }) {
              const [pending, setPending] = useState(false);
              const [error, setError] = useState(null);
              const formRef = useRef(null);

              const actionUrl = resolveActionUrl(action, pagePath);
              // Read once at render so SSR markup and the first hydrate
              // match. The hidden ``_csrf_token`` field is what makes a
              // no-JS form POST satisfy the CSRF middleware — JS
              // submissions still take the header path below.
              const csrfFieldValue = getCsrfToken();

              const handleSubmit = useCallback(
                async (event) => {
                  event.preventDefault();
                  if (pending) return;

                  const form = formRef.current;
                  if (!form) return;

                  const formData = new FormData(form);
                  const payload = Object.fromEntries(formData.entries());

                  setError(null);
                  setPending(true);

                  try {
                    const csrfToken = getCsrfToken();
                    const headers = { 'Content-Type': 'application/json' };
                    if (csrfToken) headers[csrfHeaderName()] = csrfToken;
                    const response = await fetch(actionUrl, {
                      method: 'POST',
                      headers,
                      body: JSON.stringify(payload),
                    });

                    // Honour ``x-pyxle-invalidate`` so the form's
                    // submission invalidates other cached routes (e.g.
                    // a list view) before the caller navigates.
                    _applyInvalidationHeader(response);

                    const json = await response.json();

                    if (!response.ok || json.ok === false) {
                      const message = json.error ?? `Action failed with status ${response.status}`;
                      const fieldErrors = json.fields ?? null;
                      setError(message);
                      if (typeof onError === 'function') {
                        onError(message, fieldErrors);
                      }
                      return;
                    }

                    const { ok: _ok, error: _err, fields: _fields, ...data } = json;

                    if (resetOnSuccess && form) {
                      form.reset();
                    }

                    if (typeof onSuccess === 'function') {
                      onSuccess(data);
                    }
                  } catch (err) {
                    const message = err.message ?? 'Network error';
                    setError(message);
                    if (typeof onError === 'function') {
                      onError(message, null);
                    }
                  } finally {
                    setPending(false);
                  }
                },
                [actionUrl, pending, onSuccess, onError, resetOnSuccess],
              );

              return (
                <form
                  ref={formRef}
                  method="POST"
                  action={actionUrl}
                  onSubmit={handleSubmit}
                  {...rest}
                >
                  {csrfFieldValue ? (
                    <input type="hidden" name="_csrf_token" value={csrfFieldValue} />
                  ) : null}
                  {children}
                  {error && (
                    <p role="alert" style={{ color: 'red', marginTop: '0.5rem' }}>
                      {error}
                    </p>
                  )}
                </form>
              );
            }
            """
        ).strip()
        + "\n"
    )


def _render_form_component_types() -> str:
    return (
        dedent(
            """
            import type * as React from 'react';
            import type { ActionFieldErrors } from './use-action';

            export interface FormProps
              extends Omit<React.FormHTMLAttributes<HTMLFormElement>, 'action' | 'onError' | 'onSubmit'> {
              /** Name of the ``@action`` to POST to. */
              action: string;
              /**
               * The page the action belongs to (e.g. ``"/todos"``). Defaults to
               * the current request path, so you rarely need to set it.
               */
              pagePath?: string;
              /** Called with the action's result payload after a successful submit. */
              onSuccess?(data: Record<string, unknown>): void;
              /**
               * Called when the submit fails. Receives the error message and, for
               * a 422 validation failure, the per-field errors (else ``null``).
               */
              onError?(message: string, fields: ActionFieldErrors | null): void;
              /** Reset the form's fields after a successful submit. Default true. */
              resetOnSuccess?: boolean;
              children?: React.ReactNode;
            }

            /**
             * A progressively-enhanced ``<form>`` that POSTs to a Pyxle
             * ``@action``. Works without JavaScript (native POST) and upgrades
             * to a fetch submission with CSRF and cache invalidation when
             * hydrated.
             */
            export declare function Form(props: FormProps): React.ReactElement;
            """
        ).strip()
        + "\n"
    )


def _render_use_auth_component() -> str:
    return (
        dedent(
            """
            import { useCallback, useEffect, useSyncExternalStore } from 'react';

            // --- CSRF resolution (mirrors use-action.jsx) -------------------
            // The auth endpoints (/login, /signup, /logout) are state-changing
            // POSTs guarded by the framework CSRF middleware, so each request
            // must echo the token the same way useAction does.
            function csrfCookieName() {
              if (typeof globalThis !== 'undefined' && typeof globalThis.__PYXLE_CSRF_COOKIE__ === 'string' && globalThis.__PYXLE_CSRF_COOKIE__) {
                return globalThis.__PYXLE_CSRF_COOKIE__;
              }
              return 'pyxle-csrf';
            }

            function csrfHeaderName() {
              if (typeof globalThis !== 'undefined' && typeof globalThis.__PYXLE_CSRF_HEADER__ === 'string' && globalThis.__PYXLE_CSRF_HEADER__) {
                return globalThis.__PYXLE_CSRF_HEADER__;
              }
              return 'x-csrf-token';
            }

            function getCsrfToken() {
              if (typeof document !== 'undefined') {
                const cookieName = csrfCookieName();
                for (const part of document.cookie.split(';')) {
                  const eq = part.indexOf('=');
                  if (eq === -1) continue;
                  if (part.slice(0, eq).trim() === cookieName) {
                    return decodeURIComponent(part.slice(eq + 1));
                  }
                }
              }
              if (typeof globalThis !== 'undefined' && typeof globalThis.__PYXLE_CSRF_TOKEN__ === 'string') {
                return globalThis.__PYXLE_CSRF_TOKEN__;
              }
              return '';
            }

            // --- Auth seed (window.__PYXLE_AUTH__) --------------------------
            // The session middleware publishes the current user plus the
            // endpoint map; the SSR document seeds it as window.__PYXLE_AUTH__.
            // Endpoints default to the conventional /auth/* paths when no seed
            // is present (e.g. the auth plugin isn't installed).
            const DEFAULT_ENDPOINTS = {
              me: '/auth/me',
              login: '/auth/login',
              signup: '/auth/signup',
              logout: '/auth/logout',
            };

            function readSeed() {
              if (typeof window === 'undefined') return null;
              const seed = window.__PYXLE_AUTH__;
              return seed && typeof seed === 'object' ? seed : null;
            }

            function resolveEndpoints() {
              const seed = readSeed();
              const ep = seed && seed.endpoints && typeof seed.endpoints === 'object' ? seed.endpoints : {};
              return {
                me: typeof ep.me === 'string' ? ep.me : DEFAULT_ENDPOINTS.me,
                login: typeof ep.login === 'string' ? ep.login : DEFAULT_ENDPOINTS.login,
                signup: typeof ep.signup === 'string' ? ep.signup : DEFAULT_ENDPOINTS.signup,
                logout: typeof ep.logout === 'string' ? ep.logout : DEFAULT_ENDPOINTS.logout,
              };
            }

            // --- Shared store -----------------------------------------------
            // Module-level so every useAuth() consumer stays in sync: a logout
            // in the navbar updates the user everywhere at once.
            const store = {
              user: undefined,   // undefined = unresolved; null = anonymous
              loading: true,
              error: null,
              endpoints: resolveEndpoints(),
              subscribers: new Set(),
            };

            function computeSnapshot() {
              const user = store.user === undefined ? null : store.user;
              return {
                user,
                isAuthenticated: user != null,
                loading: store.loading,
                error: store.error,
              };
            }

            // A constant server snapshot keeps the hydration render identical
            // on both sides (no mismatch); the client swaps to the real value
            // immediately after hydration via getSnapshot.
            const SERVER_SNAPSHOT = { user: null, isAuthenticated: false, loading: true, error: null };
            let clientSnapshot = computeSnapshot();

            function commit(partial) {
              Object.assign(store, partial);
              clientSnapshot = computeSnapshot();
              for (const cb of store.subscribers) cb();
            }

            function subscribe(cb) {
              store.subscribers.add(cb);
              return () => { store.subscribers.delete(cb); };
            }
            function getSnapshot() { return clientSnapshot; }
            function getServerSnapshot() { return SERVER_SNAPSHOT; }

            // Seed synchronously on the client so the first post-hydration
            // render shows the SSR-resolved user with no network round-trip.
            (function seedFromWindow() {
              const seed = readSeed();
              if (seed && 'user' in seed) {
                store.user = seed.user == null ? null : seed.user;
                store.loading = false;
                clientSnapshot = computeSnapshot();
              }
            })();

            // --- Network ----------------------------------------------------
            async function postJson(url, body) {
              const headers = { 'Content-Type': 'application/json', 'Accept': 'application/json' };
              const token = getCsrfToken();
              if (token) headers[csrfHeaderName()] = token;
              const res = await fetch(url, {
                method: 'POST',
                headers,
                body: JSON.stringify(body ?? {}),
                credentials: 'same-origin',
              });
              let json = null;
              try { json = await res.json(); } catch { /* tolerate empty/non-JSON */ }
              return { res, json };
            }

            let refreshInFlight = null;

            async function refresh() {
              if (refreshInFlight) return refreshInFlight;
              commit({ loading: true, error: null });
              refreshInFlight = (async () => {
                try {
                  const res = await fetch(store.endpoints.me, {
                    method: 'GET',
                    headers: { 'Accept': 'application/json' },
                    credentials: 'same-origin',
                  });
                  if (!res.ok) throw new Error(`Failed to load session (${res.status})`);
                  const json = await res.json();
                  const user = (json && json.user) || null;
                  commit({ user, loading: false, error: null });
                  return user;
                } catch (err) {
                  // Treat any failure as anonymous, but surface the message.
                  commit({ user: null, loading: false, error: err.message ?? 'Failed to load session' });
                  return null;
                } finally {
                  refreshInFlight = null;
                }
              })();
              return refreshInFlight;
            }

            async function submitCredentials(url, credentials) {
              commit({ loading: true, error: null });
              try {
                const { res, json } = await postJson(url, credentials);
                if (!res.ok || (json && json.ok === false)) {
                  const message = (json && json.error) || `Request failed (${res.status})`;
                  commit({ loading: false, error: message });
                  return { ok: false, error: message, code: json && json.code };
                }
                const user = (json && json.user) || null;
                commit({ user, loading: false, error: null });
                return { ok: true, user };
              } catch (err) {
                const message = err.message ?? 'Network error';
                commit({ loading: false, error: message });
                return { ok: false, error: message };
              }
            }

            function login(credentials) {
              return submitCredentials(store.endpoints.login, credentials);
            }

            function signup(credentials) {
              return submitCredentials(store.endpoints.signup, credentials);
            }

            async function logout() {
              commit({ loading: true, error: null });
              try {
                await postJson(store.endpoints.logout, {});
              } catch { /* drop the local session even if the request fails */ }
              commit({ user: null, loading: false, error: null });
            }

            /**
             * useAuth — read and mutate the signed-in user.
             *
             * State is shared across every component that calls the hook, and
             * is seeded from the server render (window.__PYXLE_AUTH__) so a
             * signed-in user appears on the first client frame without a
             * round-trip. When no seed is present the session is resolved once
             * on mount.
             */
            export function useAuth() {
              const snapshot = useSyncExternalStore(subscribe, getSnapshot, getServerSnapshot);

              useEffect(() => {
                if (store.user === undefined && refreshInFlight === null) {
                  refresh();
                }
              }, []);

              return {
                user: snapshot.user,
                isAuthenticated: snapshot.isAuthenticated,
                loading: snapshot.loading,
                error: snapshot.error,
                login,
                signup,
                logout,
                refresh,
              };
            }
            """
        ).strip()
        + "\n"
    )


def _render_use_auth_component_types() -> str:
    return (
        dedent(
            """
            export interface PyxleUser {
              id: string;
              email: string;
              emailVerified: boolean;
              plan: string;
              createdAt: string;
            }

            export interface AuthResult {
              ok: boolean;
              user?: PyxleUser | null;
              error?: string;
              code?: string;
            }

            export interface UseAuthResult {
              /** The signed-in user, or `null` when anonymous. */
              user: PyxleUser | null;
              /** `true` when a user is signed in. */
              isAuthenticated: boolean;
              /** `true` while a sign-in / sign-up / refresh is in flight. */
              loading: boolean;
              /** The last error message, or `null`. */
              error: string | null;
              /** Sign in with email + password against `POST {prefix}/login`. */
              login(credentials: { email: string; password: string }): Promise<AuthResult>;
              /** Create an account against `POST {prefix}/signup`. */
              signup(credentials: { email: string; password: string }): Promise<AuthResult>;
              /** Sign out against `POST {prefix}/logout` and clear local state. */
              logout(): Promise<void>;
              /** Re-fetch the current user from `GET {prefix}/me`. */
              refresh(): Promise<PyxleUser | null>;
            }

            /**
             * Read and mutate the signed-in user. State is shared across all
             * consumers and seeded from the server render.
             */
            export declare function useAuth(): UseAuthResult;
            """
        ).strip()
        + "\n"
    )


def _render_use_websocket_component() -> str:
    return (
        dedent(
            """
            import { useCallback, useEffect, useRef, useState } from 'react';

            // Resolve a same-origin WebSocket URL. An absolute ws://, wss://
            // URL passes through; a path is joined to the current origin with
            // the matching secure scheme (wss: on https:).
            function resolveWsUrl(path) {
              const lower = String(path).toLowerCase();
              if (lower.startsWith('ws://') || lower.startsWith('wss://')) {
                return path;
              }
              const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
              const base = proto + '//' + window.location.host;
              return path.startsWith('/') ? base + path : base + '/' + path;
            }

            /**
             * useWebSocket — connect to a Pyxle page's `async def websocket(ws)`
             * handler (or any WS endpoint) with auto-reconnect, JSON message
             * parsing, and connection state.
             *
             * Returns { status, send, lastMessage, error }:
             *   - status: 'connecting' | 'open' | 'closed'
             *   - send(data): send a string as-is, or JSON-encode anything else;
             *     returns false if the socket isn't open
             *   - lastMessage: the most recent received message (JSON-parsed when
             *     the frame is valid JSON, else the raw string)
             *   - error: the last error message, or null
             *
             * Never connects during SSR. Reconnects with exponential backoff
             * (capped at 30s, with jitter) unless `reconnect: false`.
             */
            export function useWebSocket(path, options = {}) {
              const { onMessage, protocols, reconnect = true, maxRetries = Infinity } = options;
              // A stable dependency key: re-run (reconnect) when protocols change
              // by VALUE, but not when an inline array literal changes identity
              // on every render. Avoids both stale subprotocols and reconnect storms.
              const protocolsKey = JSON.stringify(protocols ?? null);
              const [status, setStatus] = useState('connecting');
              const [lastMessage, setLastMessage] = useState(null);
              const [error, setError] = useState(null);
              const socketRef = useRef(null);
              const onMessageRef = useRef(onMessage);
              onMessageRef.current = onMessage;

              const send = useCallback((data) => {
                const sock = socketRef.current;
                if (!sock || sock.readyState !== WebSocket.OPEN) return false;
                sock.send(typeof data === 'string' ? data : JSON.stringify(data));
                return true;
              }, []);

              useEffect(() => {
                // Never open a socket during SSR — keeps the server render and
                // the first client render identical (no hydration mismatch).
                if (typeof window === 'undefined') return undefined;

                let cancelled = false;
                let retries = 0;
                let retryTimer = null;

                function scheduleReconnect() {
                  if (cancelled || !reconnect || retries >= maxRetries) return;
                  // Exponential backoff with jitter, capped — never a fixed-delay
                  // reconnect loop that would thundering-herd a restarting server.
                  const ceiling = Math.min(1000 * Math.pow(2, retries), 30000);
                  const delay = ceiling / 2 + Math.random() * (ceiling / 2);
                  retries += 1;
                  retryTimer = setTimeout(connect, delay);
                }

                function connect() {
                  if (cancelled) return;
                  setStatus('connecting');
                  let sock;
                  try {
                    sock = protocols
                      ? new WebSocket(resolveWsUrl(path), protocols)
                      : new WebSocket(resolveWsUrl(path));
                  } catch (err) {
                    setError((err && err.message) || 'WebSocket connection failed');
                    scheduleReconnect();
                    return;
                  }
                  socketRef.current = sock;

                  sock.onopen = () => {
                    if (cancelled) return;
                    retries = 0;
                    setError(null);
                    setStatus('open');
                  };
                  sock.onmessage = (event) => {
                    if (cancelled) return;
                    let data = event.data;
                    if (typeof data === 'string') {
                      try {
                        data = JSON.parse(data);
                      } catch {
                        // Not JSON — keep the raw string.
                      }
                    }
                    setLastMessage(data);
                    if (typeof onMessageRef.current === 'function') {
                      try {
                        onMessageRef.current(data, event);
                      } catch (err) {
                        setError((err && err.message) || 'onMessage handler error');
                      }
                    }
                  };
                  sock.onerror = () => {
                    if (!cancelled) setError('WebSocket error');
                  };
                  sock.onclose = () => {
                    if (cancelled) return;
                    setStatus('closed');
                    scheduleReconnect();
                  };
                }

                connect();

                return () => {
                  cancelled = true;
                  if (retryTimer) clearTimeout(retryTimer);
                  const sock = socketRef.current;
                  socketRef.current = null;
                  if (sock) {
                    try {
                      sock.close();
                    } catch {
                      // ignore close races
                    }
                  }
                };
                // `protocols` enters the deps via the stable protocolsKey above,
                // so a changed subprotocol reconnects with the new value.
                // eslint-disable-next-line react-hooks/exhaustive-deps
              }, [path, reconnect, maxRetries, protocolsKey]);

              return { status, send, lastMessage, error };
            }
            """
        ).strip()
        + "\n"
    )


def _render_use_websocket_component_types() -> str:
    return (
        dedent(
            """
            export type WebSocketStatus = 'connecting' | 'open' | 'closed';

            export interface UseWebSocketOptions {
              /** Called for each received message (JSON-parsed when possible). */
              onMessage?(data: unknown, event: MessageEvent): void;
              /** WebSocket subprotocol(s). */
              protocols?: string | string[];
              /** Auto-reconnect on close with exponential backoff. Default true. */
              reconnect?: boolean;
              /** Max reconnect attempts. Default Infinity. */
              maxRetries?: number;
            }

            export interface UseWebSocketResult {
              /** Connection state. */
              status: WebSocketStatus;
              /** Send a string as-is, or JSON-encode anything else. Returns false
               *  if the socket isn't open. */
              send(data: unknown): boolean;
              /** The most recent received message. */
              lastMessage: unknown;
              /** The last error message, or null. */
              error: string | null;
            }

            /**
             * Connect to a WebSocket endpoint (a page's `async def websocket(ws)`
             * or any ws path) with auto-reconnect and JSON parsing. Same-origin
             * paths are resolved against the current origin.
             */
            export declare function useWebSocket(
              path: string,
              options?: UseWebSocketOptions
            ): UseWebSocketResult;
            """
        ).strip()
        + "\n"
    )


def _render_use_pathname_component() -> str:
    return (
        dedent(
            """
            import { useState, useEffect } from 'react';

            /**
             * usePathname — reactively track the current URL pathname.
             *
             * During SSR it reads the request path from
             * globalThis.__PYXLE_CURRENT_PATHNAME__ (set by the SSR worker
             * before rendering) so the server and client agree on hydration.
             * Falls back to '/' only when the global is absent.
             *
             * Re-renders the component whenever Pyxle performs a client-side
             * navigation or the browser fires a popstate event.
             */
            function getInitialPathname() {
              if (typeof window !== 'undefined') {
                return window.location.pathname;
              }
              if (typeof globalThis.__PYXLE_CURRENT_PATHNAME__ === 'string') {
                return globalThis.__PYXLE_CURRENT_PATHNAME__;
              }
              return '/';
            }

            export function usePathname() {
              const [pathname, setPathname] = useState(getInitialPathname);

              useEffect(() => {
                // Sync on mount in case the SSR value differs.
                setPathname(window.location.pathname);

                function onRouteChange() {
                  setPathname(window.location.pathname);
                }

                window.addEventListener('pyxle:routechange', onRouteChange);
                window.addEventListener('popstate', onRouteChange);
                return () => {
                  window.removeEventListener('pyxle:routechange', onRouteChange);
                  window.removeEventListener('popstate', onRouteChange);
                };
              }, []);

              return pathname;
            }
            """
        ).strip()
        + "\n"
    )


def _render_use_pathname_component_types() -> str:
    return (
        dedent(
            """
            /**
             * Reactively track the current URL pathname.
             *
             * Re-renders the component on every client-side navigation.
             * Returns `'/'` during SSR.
             */
            export declare function usePathname(): string;
            """
        ).strip()
        + "\n"
    )


def _render_client_barrel() -> str:
    return (
        dedent(
            """
            export { Head } from './head.jsx';
            export { Script } from './script.jsx';
            export { Image } from './image.jsx';
            export { default as ClientOnly } from './client-only.jsx';
            export { useAction } from './use-action.jsx';
            export { usePathname } from './use-pathname.jsx';
            export { useAuth } from './use-auth.jsx';
            export { useWebSocket } from './use-websocket.jsx';
            export { Form } from './form.jsx';
            export { Link, navigate, prefetch, refresh, invalidate, Slot, SlotProvider, useSlot, useSlots } from './index.js';
            """
        ).strip()
        + "\n"
    )


__all__ = [
    "CLIENT_ENTRY_FILENAME",
    "CLIENT_HTML_FILENAME",
    "VITE_CONFIG_FILENAME",
    "TSCONFIG_FILENAME",
    "write_client_bootstrap_files",
    "_render_client_entry",
  "_render_client_runtime_index",
    "_render_client_runtime_index_types",
    "_render_client_runtime_link_types",
    "_render_client_index",
    "_render_slot_runtime",
    "_render_slot_runtime_types",
    "_render_tsconfig",
    "_render_vite_config",
    "_render_use_action_component",
    "_render_use_action_component_types",
    "_render_form_component",
    "_render_form_component_types",
    "_render_use_pathname_component",
    "_render_use_pathname_component_types",
    "_render_use_auth_component",
    "_render_use_auth_component_types",
    "_render_use_websocket_component",
    "_render_use_websocket_component_types",
    "_build_public_env_defines",
]
