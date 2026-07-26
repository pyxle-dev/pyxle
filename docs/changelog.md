# Changelog

Release notes for Pyxle. While we're in beta (`0.x`), minor versions may include breaking changes — those are called out explicitly. To upgrade, run `pip install --upgrade pyxle-framework`.

## 0.8.0

- **Pyxle Studio — a dashboard built into `pyxle dev`.** Served at `/__pyxle/studio`: every route with its loader, actions, cache posture, and boundaries; an interactive tester (loaders run in-process, actions go through their real endpoint — CSRF, validation, and auth hooks included); a live request feed; latency metrics; the effective config (secrets redacted); and in-browser `pyxle check`. Dev-only by construction, with a `Host`-header allowlist. [Pyxle Studio](guides/studio.md).
- **New `pyxle studio` command.** Runs the dev server and opens the browser on the dashboard, enabling it for that run even when the config opts out. [CLI](reference/cli.md#pyxle-studio).
- **Breakpoint debugging directly in `.pyxl` files.** Set a breakpoint on a line inside a `@server` loader *and* on a line inside the JSX below it — both bind. Compiled server modules are remapped to their `.pyxl` sources, so the Python debugger binds natively; dev source maps chain through Vite so the React half binds in the browser. debugpy ships with the framework — nothing extra to install. [Debugging .pyxl files](guides/debugging-pyxl.md).
- **One-key debugging from VS Code.** Pyxle Language Tools 0.3.0 contributes a `pyxle` debug type: press F5 to run your dev server under the debugger — one clean session with a real Stop button that tears the whole server down — and open your app. A separate "Debug Pyxle app (React browser)" configuration debugs the React side in a standalone browser session. Plus a "Pyxle: Open Studio" command. [Debugging .pyxl files](guides/debugging-pyxl.md#quick-start-vs-code).
- **`pyxle dev --inspect` for attach-style debugging.** Hosts a debugpy server bound to `127.0.0.1` so any DAP client — or a remote VS Code — can attach to a running dev server. The server writes `.pyxle-build/dev-server.json` (ports, debugpy endpoint) for editor tooling and removes it on shutdown. [Debugging .pyxl files](guides/debugging-pyxl.md).
- **Dev tracebacks now point at `.pyxl` sources.** A loader/action error names your `.pyxl` file and line instead of the compiled module under `.pyxle-build/`. [Debugging](guides/debugging.md).
- **Breaking: `/__pyxle` is now a reserved URL namespace.** The dev server's Vite asset proxy never forwards paths under it, so an app route under `/__pyxle` no longer resolves in `pyxle dev` (it still serves in production, where the namespace is unused). Move any such route before upgrading. [Pyxle Studio](guides/studio.md#the-reserved-__pyxle-namespace).

## 0.7.5

- **`pyxle dev` now persists module-level state across requests, like `pyxle serve`.** A `@server`/`@action` module is imported once and reused between rebuilds, so a module-level counter or in-memory cache no longer resets on every refresh in dev. Saving a file still re-imports it (resetting globals and applying your edits). State stays per-process — use a database or cache for anything shared or durable ([Loaders should be stateless](core-concepts/data-loading.md#loaders-should-be-stateless)).
- **`pyxle init` now requires an explicit target.** A bare `pyxle init` used to silently scaffold into the current directory; it now errors, pointing to `pyxle init my-app` (new directory) or `pyxle init .` (current directory).

## 0.7.4

- **Fix: `pyxle dev` no longer hot-reloads in an endless loop on Linux.** After a save, the rebuild's own file reads surfaced through `inotify` as events the watcher mistook for edits, re-triggering the rebuild forever (macOS `FSEvents` doesn't report reads, so it was Linux-only). The watcher now reacts only to genuine change events, not read-only opens.

## 0.7.3

- **Docs: an `@action`'s awaited result is the flat `{ ok, ...yourReturn }`** — read fields directly (`res.title`), never `res.data.title` (a successful result has no `.data`; `useAction().data` is a separate hook property). Clarified across the [Server Actions guide](core-concepts/server-actions.md), the [client API reference](reference/client-api.md), and the scaffold `AGENTS.md`.

## 0.7.2

- **Fix: `pyxle init` scaffolds an installable `requirements.txt` again.** The template's `starlette` pin conflicted with the framework's after 0.7.1's security bump; both now use `starlette>=1.3.1,<2.0`.
- **Config: a boolean `port` is now rejected instead of binding port 1.** `bool` being an `int` subclass slipped past the validator; it now raises a clear `ConfigError`.

## 0.7.1

- **Security: require Starlette ≥ 1.3.1**, fixing a `Host`-header auth bypass (PYSEC-2026-161), form-parsing DoS (PYSEC-2026-249, -1943), and a Windows `StaticFiles` SSRF (CVE-2026-48818). Pyxle's own API is unchanged; a new `pip-audit` CI job guards dependencies.
- **`pyxle serve` refuses to start in production without `PYXLE_SECRET_KEY`** — previously only a warning, leaving CSRF tokens and signed cookies forgeable. [Security](guides/security.md), [Deployment](guides/deployment.md).
- **`pyxle dev`/`build` check the Node.js version up front** — a clear "Node.js 20.19+ required" message instead of an opaque Vite crash. [Installation → Troubleshooting](getting-started/installation.md#troubleshooting).
- **A real testing story: `pyxle.testing` helpers and a [Testing guide](guides/testing.md).** `load_loader("pages/index.pyxl")` / `load_page(...)` compile a page and hand back its loader/module for unit tests.
- **New [Debugging guide](guides/debugging.md) and Deployment fixes** — Node 22 in the sample Dockerfile, the real metrics path (`/api/__pyxle/metrics`), a migrations section, and a blue-green CSRF gotcha.
- **Security docs now match what Pyxle sends** — it already sets `X-Content-Type-Options`/`X-Frame-Options`/`Referrer-Policy`, so the guide covers only CSP and HSTS; a [SECURITY.md](https://github.com/pyxle-dev/pyxle/blob/main/SECURITY.md) is published for every repo.
- **A fresh project now ships a human `README.md`** alongside the AI-oriented `AGENTS.md`.
- **The [comparison guide](guides/comparison.md) names current gaps** — TSX authoring, a first-class test client, automatic image/font optimization, i18n — with how to bridge each today.

## 0.7.0

- **Fix: `error.pyxl` renders even when an ancestor layout has a `@server` loader.** Boundary renders now run ancestor layout loaders like a normal page instead of silently falling back to the built-in error document.
- **Pre-release fixes.** `PYXLE_SSR_WORKER_CONCURRENCY` reaches the worker again, `SIGTERM` to `pyxle dev` tears down the whole child tree, a failed rebuild broadcasts to the overlay, and reverting to last-good content triggers the recovery rebuild.
- **Fix: CommonJS React libraries no longer crash SSR.** The SSR bundler resolves dependencies ESM-first (like Vite), so packages without an `exports` map (e.g. `lucide-react`) link cleanly; a genuinely CJS-only package now fails with an actionable error. [Third-party packages](guides/third-party-packages.md#commonjs-packages-and-ssr).
- **Docs: [the road to a fully-typed Pyxle](guides/fully-typed-pyxle.md)** — where the type story is heading (TSX authoring, Python types across the boundary), grounded in what works today.
- **Breaking: Node.js 20.19+ is now required** (Vite 7's floor; Node 18 is EOL). [Installation](getting-started/installation.md).
- **Modernized scaffold — React 19, Vite 7, and an interactive `pyxle init`.** Arrow-key prompts for Tailwind, shadcn/ui, and the import alias, with `--yes`/explicit flags for non-interactive use. [Quick Start](getting-started/quick-start.md).
- **Tailwind is now opt-in — Tailwind v4 wired into Vite when chosen.** No config files or standalone watcher; decline it and plain CSS / CSS Modules work out of the box. [Styling](guides/styling.md).
- **shadcn/ui support** — choose it at `pyxle init` and `npx shadcn@latest add …` works with no `shadcn init`; the `@` alias resolves in the client build and SSR. [Third-party packages](guides/third-party-packages.md#shadcnui).
- **Fix: streaming SSR no longer serializes across requests.** An SSR worker now renders many requests concurrently (isolated per-request with `AsyncLocalStorage`), and `pyxle serve` auto-sizes its worker pool. [Streaming](guides/streaming.md#concurrent-streams-dont-queue).
- **`pyxle init` renders the framework pin from the running version** instead of a stale `>=0.4.1` that downgraded fresh projects.
- **`pyxle typecheck` fails fast when TypeScript isn't installed**, with an actionable message instead of npm's placeholder `tsc`. [TypeScript](guides/typescript.md).
- **Scaffolded `AGENTS.md` corrected** — `LoaderError` (like `server`/`action`/`ActionError`/…) is compiler-injected, not imported.
- **`pyxle check` works out of the box — `pyxle-langkit` is now a default dependency**, so JSX checking passes on a fresh scaffold with no extra step.
- **Fix: the CSRF cookie no longer collides between apps on one host.** The default name is namespaced by bind port (`pyxle-csrf-8000`) and injected into the page shell. [Security → CSRF](guides/security.md#csrf-protection).
- **Fix: multipart forms (file uploads) now pass CSRF with a `_csrf_token` field.** The middleware stream-parses just far enough to read the token (capped at 1 MiB) and replays the body untouched. [Security → Form bodies and uploads](guides/security.md#form-bodies-and-uploads).
- **`pyxle dev` output is clean by default, `--verbose` for the firehose** — a curated startup summary and one-line rebuild notices instead of Vite's raw stdout. [CLI](reference/cli.md#pyxle-dev).
- **Server-side logs stream to the browser console in `pyxle dev`**, prefixed `[pyxle:server]`; dev-only and bounded. [CLI](reference/cli.md#pyxle-dev).
- **`public/` changes are picked up on refresh instead of triggering a rebuild (matching Next.js)** — assets serve live from disk, and a newly added or removed file refreshes the static index. [Architecture → the watcher](architecture/dev-server.md#the-watcher).
- **Watch extra directories with `dev.watch`, ignore paths with `dev.ignore`.** A shared module outside `pages/` can now trigger hot reload; `dev.ignore` is additive to the built-in ignores. [Configuration → Development](reference/configuration.md#development).

### Documentation

- **Fixed the flagship [WebSockets](guides/websockets.md) example** — it derived the socket path from `window` during SSR and 500'd; it now builds the path from loader data.
- **More doc fixes** — a broken `@action` example in the [pyxle-db docs](plugins/pyxle-db.md) (`await request.json()`), documented CSRF on the pyxle-auth endpoints and where pyxle-auth accounts live, an honest [`pyxle check`](reference/cli.md#pyxle-check) scope note, and prerequisites up front in the [Introduction](getting-started/introduction.md).
- **`Accept: text/markdown` negotiation now follows RFC 9110** (q-values honoured, exact type match); new `markdown_is_acceptable(accept)` helper. [AI accessibility](guides/llms.md#negotiation-rules).
- **`/llms.txt` now emits absolute URLs and links `.md` only where Markdown actually resolves.** [AI accessibility](guides/llms.md#the-llmstxt-index).
- **Breaking: converted Markdown rewrites internal links to `.md`** — `html_to_markdown()` rewrites by default (`rewrite_links=False` for the old behavior). [AI accessibility](guides/llms.md#autoconvert-the-lossy-fallback).
- **Fix: a burst of rapid saves can no longer kill the dev server** — builds are serialized, `meta.json` writes atomically, and the Vite subprocess is supervised with bounded backoff.
- **Fix: reading an unprovided `request.state.<name>`** (e.g. `request.state.db` without pyxle-db) now gives a structured, guided error instead of a bare `AttributeError`.
- **Actionable SSR error when a component touches a browser global** (`window`, `document`, …) — dev names your `.pyxl` file and the fix (`useEffect`/`<ClientOnly>`); production stays generic. [Client Components](guides/client-components.md).

## 0.6.1

A sharper `pyxle check` — the edit → check → fix loop now catches classes of mistake it used to wave through.

- **`pyxle check` gained a semantic layer** — pyflakes over the Python section flags undefined names, unused imports, and redefinitions (compiler-injected names are recognized).
- **Duplicate `export default` is now caught at check time**, at the real source line, instead of failing later in the build.
- **JSX error lines are now accurate**, and Babel's misleading "unterminated regex" carries a plain-language hint about the real cause.
- **`LoaderError` and `invalidate_routes` are now auto-injected**, matching `ActionError`/`ValidationActionError`. [Runtime API](reference/runtime-api.md).
- **`pyxle install --break-system-packages`** for externally-managed (PEP 668) environments. [CLI](reference/cli.md#pyxle-install).

## 0.6.0

- **AI accessibility — serve your app as Markdown, plus `llms.txt`.** Opt in with `"llms": true` and every page gains a `.md` rendition (and `Accept: text/markdown` support), an `/llms.txt` index, and discovery headers; Markdown resolves from a co-located `<page>.md`, a `to_markdown` handler, or an ancestor `llms.py`, with an `autoConvert` HTML→Markdown fallback. Off by default, and adds nothing to the page hot path. [AI accessibility](guides/llms.md).

## 0.5.0

The depth release: caching/SSG/ISR, streaming SSR, realtime (WebSockets + a cross-worker Redis broker), Pydantic-validated actions, observability, background work, image optimization, and multi-worker serving — built, documented, and dogfooded on pyxle.dev. The two behavior changes below are called out explicitly.

- **Cross-worker realtime — a Redis pub/sub broker.** WebSocket channels span worker processes and machines with `PYXLE_REALTIME_BROKER=redis`; the in-process broker stays the default. No code change. [WebSockets](guides/websockets.md#cross-worker-realtime-with-redis).
- **Fix: streaming SSR now survives production gzip.** A streaming-aware `StreamingGZipMiddleware` flushes the compressor per chunk so the shell reaches the browser first. [Streaming](guides/streaming.md#streaming-survives-gzip-in-production).
- **Fix: dynamic pages are no longer client-nav-cached with stale data.** The navigation-cache TTL now mirrors server cacheability, so a dynamic page always refetches on back/forward. [Caching](guides/caching.md#client-navigation-cache).
- **Fix: `pyxle build --static` pre-renders pages whose loaders use a plugin.** The static builder stands up the same plugin context a request sees, so a DB-backed loader runs at build time. [Caching](guides/caching.md#static-pre-rendering-pyxle-build-static).
- **Changed (behavior): stricter nested-config validation.** The `cors`/`csrf`/`observability`/… blocks now reject unknown keys at boot, so a typo'd security key fails loudly instead of silently no-opping. [Configuration](reference/configuration.md).
- **Fix: `error.pyxl` no longer leaks internal error details in production** — the boundary gets a generic message and sanitized type; author-raised `LoaderError`/`ActionError` messages still pass through. [Error Handling](guides/error-handling.md).
- **Fix: `import.meta.env.PYXLE_PUBLIC_*` is now substituted during SSR**, so a public env var no longer causes a hydration mismatch. [Environment variables](guides/environment-variables.md).
- **Fix: scaffold `.gitignore` no longer ignores `.env`**, matching the env-vars doc.
- **Fix: `<Image>` emits a lowercase `fetchpriority` attribute** (React 18.3.1 rejected the camelCase form).
- **Fix: production gzip no longer prints `I/O operation on closed file`** — the compressor closes deterministically on every path.
- **Fix: scaffolded `jsconfig.json` drops the deprecated `baseUrl`** in favor of tsconfig-relative `paths`.
- **Fix: `pyxle typecheck` works on current TypeScript** — `"bundler"` resolution, no `baseUrl`.
- **Startup warning when a `BaseHTTPMiddleware` is paired with streaming routes** (it buffers responses, breaking streaming SSR). [Middleware](guides/middleware.md).
- **Clear compile-time error for TypeScript syntax in a client block** (it's plain JSX), pointing at your `.pyxl` source line. [TypeScript](guides/typescript.md).
- **New guides: [TypeScript](guides/typescript.md) and [Migrating from Flask or Django](guides/migration-from-flask-django.md).**
- **`error.pyxl` is now a client-side error boundary too** — a render fault after hydration renders the nearest boundary instead of a blank screen, with the same `error` prop on both sides. [Error Handling](guides/error-handling.md#client-side-errors).
- **Built-in rate limiting — `pyxle.middleware.RateLimitMiddleware`.** A dependency-free token-bucket limiter configured from `pyxle.config.json`; per-process, so rate-limit at the proxy for one global cap. Off by default. [Middleware](guides/middleware.md#rate-limiting).
- **Route policies now apply to `@action` endpoints** via `routeMiddleware.actions`, closing a bypass where an auth policy wrapped pages but not actions. [Middleware](guides/middleware.md#route-level-hooks).
- **`pyxle serve --workers 0` auto-detects the core count.** The [Deployment guide](guides/deployment.md#multi-core-worker-processes) adds a per-worker-state table and rolling-deploy guidance.
- **Build optimization: responsive `<Image>`, modulepreload hints, and `--analyze`.** `<Image>` emits a responsive `srcset` via a `loader` and gains `fill`/`sizes`/`priority`; the SSR shell preloads entry chunks; `pyxle build --analyze` reports bundle sizes. [Build Optimization](guides/build-optimization.md).
- **Background tasks & deferred work.** `request.state.background.add_task(...)` (or a `{"background": [...]}` return) runs after the response; `pyxle.tasks.enqueue(...)` schedules fire-and-forget work on an in-process pool (per-process — hand off to Celery/ARQ/Dramatiq for durability). [Background Tasks](guides/background-tasks.md).
- **OpenTelemetry tracing (opt-in).** Request/SSR/loader/action spans via the `[observability-otel]` extra; fully off and zero-cost by default. [Observability](guides/observability.md#opentelemetry-tracing).
- **`pyxle dev --dashboard` — a live terminal observability panel** (throughput, error rate, latency, cache hit ratio); dependency-free and dev-only. [Observability](guides/observability.md#dev-dashboard).
- **Structured access logging** — one line per request (`method`/`path`/`status`/`duration_ms`/correlation id) in console or JSON via `observability.accessLog`. [Observability](guides/observability.md#structured-logging).
- **Observability: request IDs, timing, metrics, and richer health probes.** Every request gets a correlation id (`X-Request-Id`) and timing; opt-in Prometheus metrics; `/readyz` runs dependency checks. Metrics are per-worker. [Observability](guides/observability.md).
- **Typed `@action` request validation with Pydantic.** Annotate a `body: Model` parameter and Pyxle validates before the action runs, returning `422` with a `fields` map on failure; `pyxle openapi` generates an OpenAPI 3.1 document from your models. Optional `[pydantic]` extra. [Server Actions](core-concepts/server-actions.md#validating-request-bodies-with-pydantic).
- **Fix: a server module that fails to import is no longer cached as a broken empty module** — the real error re-raises on each request.
- **WebSockets — page handlers, a client hook, and pub/sub.** A page can export `async def websocket(ws)`; `pyxle.realtime` adds `channel`/`room.publish`, WS auth/origin helpers, and a `useWebSocket()` hook. The in-process broker is per-worker (a Redis broker drops in). [WebSockets](guides/websockets.md).
- **Streaming SSR for `<Suspense>` pages.** The shell flushes immediately and each boundary streams in as it resolves; opt-in, zero-config, dynamic pages only. [Streaming](guides/streaming.md).
- **`loading.pyxl` route-level loading states** wrap a route in `<Suspense>`, streamed as the shell and applied identically on the client. [Streaming](guides/streaming.md#route-level-loading-states-with-loadingpyxl).
- **Server-side page caching with incremental regeneration.** Return `{"data", "revalidate": N}` (or a `CACHE` directive) to cache rendered HTML; `pyxle build --static` warms it; stale-while-revalidate, strong `ETag`, and `cache.invalidate(...)`. In-memory/disk/Redis backends. [Caching](guides/caching.md).

## 0.4.5

- **Signed cookies & tokens — `sign_cookie` / `verify_cookie`.** Stdlib-only helpers that attach a tamper-proof HMAC-SHA256 signature to any string (session id, unsubscribe link, reset token) and verify it in constant time; a `salt=` namespaces signatures, and signing without a secret fails closed. [Security](guides/security.md#signed-cookies-and-tokens).
- **Fix: `PYXLE_PUBLIC_*` client env vars now work in `pyxle dev` and no longer break `pyxle build`** — values are emitted as quoted JS string literals, so a non-identifier value (API URL, Turnstile key) resolves in both. [Environment variables](guides/environment-variables.md).
- **Fix: web fonts and other CSS `url()` assets no longer 404 in `pyxle dev`** — Pyxle sets Vite's `server.origin` so dev assets load from Vite directly.

## 0.4.4

- **Fix: cross-page hash links scroll to their anchor.** Client-side navigation to `/page#section` now jumps to the top and scrolls to the anchor once the next page commits, matching native behaviour.

## 0.4.3

- **Security: hardened HEAD sanitisation (XSS).** Dynamic `HEAD` values are now parsed and rebuilt from a strict tag allowlist with every attribute HTML-escaped, `on*` handlers dropped, dangerous URLs neutralised, and non-head tags rejected — closing an injection via the dynamic-meta-tags recipe. Inline `<script>`/`<style>` stays supported as trusted author code.
- **Security: `csrf.exemptPaths` match on segment boundaries**, so exempting `/api/webhooks` no longer also exempts `/api/webhooks-admin`.
- **Security: oversized no-JS form POSTs fail loud** with a `413` (asking for the token via header) instead of being silently truncated.
- **Fix: `<Script>`/`<Image>` boolean attributes written as strings** (`defer="false"`, `priority="0"`) now coerce correctly instead of to `True`.
- **Fix: custom `csrf.cookieName`/`headerName` now reach the client runtime** — non-default names are injected into the shell so `useAction`/`<Form>` stop `403`-ing.
- **Fix: no double loader run on hover-then-click navigation** — the click reuses the in-flight prefetch, and a superseded prefetch is discarded.
- **Multi-core serving: `pyxle serve --workers N`.** N independent server processes on one port, each with its own SSR pool — throughput scales with cores, no load balancer or shared state. [Deployment](guides/deployment.md#multi-core-worker-processes).
- **Sync API endpoints.** A plain `def endpoint(request)` (and sync `HTTPEndpoint` methods) now runs in Starlette's threadpool, so blocking drivers no longer need manual `asyncio.to_thread`. [API Routes](guides/api-routes.md#sync-endpoints-and-blocking-calls).
- **In-memory static asset cache.** Small static files (≤1 MB, 32 MB/process) are served from memory with no filesystem I/O; conditional requests and cache headers behave as before.

## 0.4.2

- **Live dev-server reconciliation.** Editing a `.pyxl` now applies route-shape changes without a restart — rename/add/remove a loader or action, add/delete a page, wrap a page in a layout — by hot-swapping the route table; editing `pyxle.config.json` prints a "restart to apply" warning.
- **`pyxle check` works on a clean install** — the JSX checker's parser dependencies are bundled (via `pyxle-langkit`), so `check` runs after `pip install 'pyxle-framework[langkit]'` with no npm setup.
- **Locale-independent SSR** — the Python↔Node transport pins UTF-8, so non-BMP characters no longer crash rendering under `LANG=C`.
- **Smoother first run** — `pyxle init` writes a gitignored `.env.local` with a random dev secret, the scaffold `requirements.txt` declares `pyxle-framework`, and `pyxle install` gives PEP 668 guidance.
- **Docs:** documented calling an `@action` endpoint directly for scripts and tests.

## 0.4.1

- **No more double loader run on first load.** The landing page is seeded into the client navigation cache from the server render, so its prefetch resolves from cache — the loader runs once, not twice (also making back/forward instant).
- **Per-route navigation-cache TTL.** A route's [`cache`](reference/configuration.md#edge-caching) TTL now also governs client navigation-cache freshness; routes without one default to 2 minutes, tunable via [`navigation.defaultPrefetchTtl`](reference/configuration.md#navigation).

## 0.4.0

- **Edge caching.** Declare cacheable routes in [`pyxle.config.json::cache`](reference/configuration.md#edge-caching) and pages serve `Cache-Control: public, s-maxage=N` (+ `stale-while-revalidate`) so a CDN absorbs traffic; the per-user CSRF cookie is omitted from cacheable responses. [Deployment](guides/deployment.md#cdn-and-edge-caching).
- **Hardened production errors.** SSR render failures (including the SPA-navigation JSON path) are sanitized in the response and logged in full for operators.
- **Faster static serving.** The static-asset middleware indexes paths up front, skipping a per-request `stat` on every dynamic request.
- **Layout & template loaders.** A `layout.pyxl`/`template.pyxl` can declare its own `@server` loader, so shared UI loads once per request without repeating the loader in every page. [Layouts](core-concepts/layouts.md#layout-data-loaders).

## 0.3.0

- **First-class plugin system.** Compose apps via `pyxle.config.json::plugins` (Django-style `INSTALLED_APPS`). [Plugins guide](guides/plugins.md), [Plugins API](reference/plugins-api.md).
- **Django-style service access.** Resolve any plugin service with `plugin("auth.service")` or a typed shortcut (e.g. `from pyxle_auth import get_auth_service`).
- **First-party plugins.** [`pyxle-db`](plugins/pyxle-db.md) (SQLite-first with migrations) and [`pyxle-auth`](plugins/pyxle-auth.md) (email+password sessions, argon2id, rate limits).
- **WebSocket endpoints** — `pages/api/*.py` can export `async def websocket(ws)`. [API Routes](guides/api-routes.md#websocket-endpoints).
- **Client navigation cache with TTL + invalidation.** Loader payloads are cached (30s default) for instant back/forward; call [`invalidate(url)`](reference/client-api.md#invalidateurl) or return [`invalidate_routes(...)`](reference/runtime-api.md#invalidate_routesresponse-urls) from an action to keep lists fresh.
- **`ActionError` is auto-imported** for any `.pyxl` with an `@action`.
- **`<Head>` coerces multi-part `<title>` children** into a single string, silencing React's array-title warning.
- **SSR worker pins `LANG=en-US.UTF-8`** (override with `PYXLE_SSR_LOCALE`) to stop `Intl` hydration mismatches.
- **Vite resolver prefers pinned versions** — `pyxle build` runs `npm install` before falling back to `npx --yes vite`.
