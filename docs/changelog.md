# Changelog

Release notes for Pyxle. While we're in beta (`0.x`), minor versions may include breaking changes — those are called out explicitly here. To upgrade, run `pip install --upgrade pyxle-framework`.

## 0.4.2

- **Live dev-server reconciliation.** Editing a `.pyxl` in `pyxle dev` now applies route-*shape* changes without a restart — rename/add/remove a `@server` loader or `@action`, add or delete a page, wrap a page in a layout, change the head. The route table is rebuilt and hot-swapped on every change (these previously needed `rm -rf .pyxle-build` + a restart). Editing `pyxle.config.json` prints a clear "restart to apply" warning instead of being silently ignored.
- **`pyxle check` works on a clean install.** The JSX checker's parser dependencies are bundled into a self-contained extractor (shipped via `pyxle-langkit`), so `pyxle check` runs after `pip install 'pyxle-framework[langkit]'` with no extra npm setup. A missing-dependency failure now names the exact packages instead of an opaque error.
- **Locale-independent SSR.** The Python↔Node SSR transport pins UTF-8, so emoji and other non-BMP characters in components or loader data no longer crash rendering under a non-UTF-8 locale (e.g. `LANG=C`).
- **Smoother first run.** `pyxle init` generates a gitignored `.env.local` with a random dev `PYXLE_SECRET_KEY` (CSRF HMAC on out of the box); the scaffold `requirements.txt` now declares `pyxle-framework` itself; and `pyxle install` warns about PEP 668 externally-managed environments with venv guidance instead of letting pip throw a raw wall.
- **Docs:** documented calling an `@action` endpoint directly for scripts and tests.

## 0.4.1

- **No more double loader run on first load.** The page you land on is now seeded into the client navigation cache from the server render, so the active nav link's prefetch (a `<Link>` pointing at the current route) resolves from cache instead of re-fetching. The loader runs **once**, not twice — halving origin work on cold loads and stopping non-idempotent loaders (view counters, analytics) from firing twice. Back/forward navigation to the page is instant for the same reason.
- **Per-route navigation-cache TTL.** A route's [`cache`](reference/configuration.md#edge-caching) TTL now also governs how long its prefetched/seeded data stays fresh in the client navigation cache, so client freshness matches how long a CDN would serve the page. Routes without a `cache` entry use a default of **2 minutes** (raised from 30s), tunable via the new [`navigation.defaultPrefetchTtl`](reference/configuration.md#navigation) option.

## 0.4.0

- **Edge caching.** Declare cacheable routes in [`pyxle.config.json::cache`](reference/configuration.md#edge-caching) and pages are served `Cache-Control: public, s-maxage=N` (plus `stale-while-revalidate`) so a CDN or reverse proxy can absorb traffic instead of your origin — Pyxle's config-driven take on per-route revalidation. The per-user CSRF cookie is automatically omitted from cacheable responses. See [Deployment → CDN and edge caching](guides/deployment.md#cdn-and-edge-caching).
- **Hardened production errors.** SSR render failures — including the SPA-navigation JSON path — are sanitized in the response (no exception type, message, or path leaks to the client) while the full error is written to the server log for operators.
- **Faster static serving.** The static-asset middleware indexes public/client paths up front, skipping a per-request filesystem `stat` + exception on every dynamic request.
- **Layout & template loaders.** A `layout.pyxl` or `template.pyxl` can declare its own `@server` loader; its result lands on the component's `data` prop, just like a page — so shared UI (nav bars, the signed-in user, the framework version) loads once per request without repeating the loader in every page. See [Layouts → Layout data loaders](core-concepts/layouts.md#layout-data-loaders).

## 0.3.0

- **First-class plugin system.** Compose apps via `pyxle.config.json::plugins` (Django-style `INSTALLED_APPS`) — see the [Plugins guide](guides/plugins.md) and [Plugins API reference](reference/plugins-api.md).
- **Django-style service access.** Resolve any plugin-registered service with `from pyxle.plugins import plugin` and `plugin("auth.service")`, or use a typed shortcut shipped by the plugin (e.g. `from pyxle_auth import get_auth_service`).
- **First-party plugins.** Two official plugins land: [`pyxle-db`](plugins/pyxle-db.md) (SQLite-first with migrations) and [`pyxle-auth`](plugins/pyxle-auth.md) (email+password sessions, argon2id, rate limits).
- **WebSocket endpoints** — `pages/api/*.py` can export `async def websocket(ws)` for live updates, chat, log streaming. See the [API Routes guide](guides/api-routes.md#websocket-endpoints).
- **Client navigation cache with TTL + invalidation.** Loader payloads are cached for 30s by default (tunable) so back/forward navigation is instant. Call [`invalidate(url)`](reference/client-api.md#invalidateurl) from the client or return [`invalidate_routes(response, ...)`](reference/runtime-api.md#invalidate_routesresponse-urls) from an `@action` to keep list views fresh after mutations — automatically honoured by `useAction` and `<Form>`.
- **`ActionError` is auto-imported** for any `.pyxl` with an `@action`. No more `NameError: name 'ActionError' is not defined` on first try.
- **`<Head>` coerces multi-part `<title>` children** into a single string, silencing React's "title element received an array" warning for the common `<title>{name} — Brand</title>` pattern.
- **SSR worker pins `LANG=en-US.UTF-8`** by default (override with `PYXLE_SSR_LOCALE`) so `toLocaleString()` and other Intl calls stop causing hydration mismatches.
- **Vite resolver prefers pinned versions.** `pyxle build` now runs `npm install` before falling back to `npx --yes vite`, so builds honour your `package.json` pin instead of fetching `vite@latest`.
