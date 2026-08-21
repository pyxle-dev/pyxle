# Pyxle Documentation

Pyxle is a Python-first full-stack web framework that brings the Next.js developer experience to the Python ecosystem. Write server logic in Python, UI in React, and ship them together in `.pyxl` files.

**Status:** beta (`0.x`) — see the [changelog](changelog.md) for the current release.

## What's new in 0.8.0

The **inside view** — **Pyxle Studio**, a dashboard built into `pyxle dev` at `/__pyxle/studio`: every route with its loader and actions, an interactive tester that runs them with real inputs, a live request feed, latency metrics, the effective config, and in-browser `pyxle check`. Plus **breakpoint debugging inside a single `.pyxl` file** — a Python loader breakpoint *and* a React component breakpoint in the same file — from VS Code's **F5** or `pyxle dev --inspect` for any DAP client. A new `pyxle studio` command opens the dashboard, dev tracebacks now name your `.pyxl` file and line instead of the compiled module, and `/__pyxle` becomes a reserved URL namespace. See [Pyxle Studio](guides/studio.md) and [Debugging](guides/debugging.md).

## What's new in 0.7.0

The **hardened run** — a QA and robustness pass across the framework: **React 19 + Vite 7** with an interactive `pyxle init`, concurrent streaming SSR that no longer serializes across requests, dev-server supervision and build-cache safety, correct `error.pyxl` rendering under layout loaders, and ESM-first SSR resolution for CommonJS packages. `0.7.1` follows with a Starlette **security** bump, a production `PYXLE_SECRET_KEY` guard, up-front Node-version checks, and a real testing story (`pyxle.testing`). See the [Changelog](changelog.md).

## What's new in 0.6.0

**Your app, in Markdown.** Every page can be served as clean Markdown — append `.md` to any URL or send `Accept: text/markdown` — with an `/llms.txt` index and discovery headers, so AI assistants and coding agents read your app as text. Off by default; nothing on the page hot path. See [AI accessibility](guides/llms.md).

## What's new in 0.5.0

The **depth release** — going deep on the production path.

- **Server-side caching, SSG & ISR.** A `@server` loader can cache its rendered HTML (`{"data", "revalidate": N}`), `pyxle build --static` pre-renders loader-less pages, and stale pages revalidate in the background. See [Caching](guides/caching.md).
- **Streaming SSR.** `<Suspense>` pages flush the shell first, then stream boundaries as they resolve — and it survives production gzip. See [Streaming SSR](guides/streaming.md).
- **Realtime WebSockets.** Page `websocket` handlers, a `useWebSocket` hook, and `pyxle.realtime` pub/sub, with a **cross-worker Redis broker** for multi-process serving. See [WebSockets](guides/websockets.md).
- **Pydantic-validated actions.** Annotate an `@action` body with a Pydantic model for automatic parse/validate/`422`, plus `pyxle openapi`. See [Server Actions](core-concepts/server-actions.md).
- **Observability.** Request IDs, timing, Prometheus metrics, structured logging, a dev dashboard, and opt-in OpenTelemetry. See [Observability](guides/observability.md).
- **Background tasks, image optimization, and multi-worker serving** (`pyxle serve --workers N`). See the [Changelog](changelog.md) for the full list.

## What's new in 0.4.0

- **Edge caching.** Declare cacheable routes in [`pyxle.config.json::cache`](reference/configuration.md#edge-caching) and pages are served `Cache-Control: public, s-maxage=N` (plus `stale-while-revalidate`) so a CDN or reverse proxy can absorb traffic instead of your origin — Pyxle's config-driven take on per-route revalidation. The per-user CSRF cookie is automatically omitted from cacheable responses. See [Deployment → CDN and edge caching](guides/deployment.md#cdn-and-edge-caching).
- **Hardened production errors.** SSR render failures — including the SPA-navigation JSON path — are sanitized in the response (no exception type, message, or path leaks to the client) while the full error is written to the server log for operators.
- **Faster static serving.** The static-asset middleware indexes public/client paths up front, skipping a per-request filesystem `stat` + exception on every dynamic request.
- **Layout & template loaders.** A `layout.pyxl` or `template.pyxl` can declare its own `@server` loader; its result lands on the component's `data` prop, just like a page — so shared UI (nav bars, the signed-in user, the framework version) loads once per request without repeating the loader in every page. See [Layouts → Layout data loaders](core-concepts/layouts.md#layout-data-loaders).

## What's new in 0.3.0

- **First-class plugin system.** Compose apps via `pyxle.config.json::plugins` (Django-style `INSTALLED_APPS`) — see the [Plugins guide](guides/plugins.md) and [Plugins API reference](reference/plugins-api.md).
- **Django-style service access.** Resolve any plugin-registered service with `from pyxle.plugins import plugin` and `plugin("auth.service")`, or use a typed shortcut shipped by the plugin (e.g. `from pyxle_auth import get_auth_service`).
- **First-party plugins.** Two official plugins land: [`pyxle-db`](plugins/pyxle-db.md) (SQLite-first with migrations) and [`pyxle-auth`](plugins/pyxle-auth.md) (email+password sessions, argon2id, rate limits).
- **WebSocket endpoints** — `pages/api/*.py` can export `async def websocket(ws)` for live updates, chat, log streaming. See the [API Routes guide](guides/api-routes.md#websocket-endpoints).
- **Client navigation cache with TTL + invalidation.** Loader payloads are cached for 30s by default (tunable) so back/forward navigation is instant. Call [`invalidate(url)`](reference/client-api.md#invalidateurl) from the client or return [`invalidate_routes(response, ...)`](reference/runtime-api.md#invalidate_routesresponse-urls) from an `@action` to keep list views fresh after mutations — automatically honoured by `useAction` and `<Form>`.
- **`ActionError` is auto-imported** for any `.pyxl` with an `@action`. No more `NameError: name 'ActionError' is not defined` on first try.
- **`<Head>` coerces multi-part `<title>` children** into a single string, silencing React's "title element received an array" warning for the common `<title>{name} — Brand</title>` pattern.
- **SSR worker pins `LANG=en-US.UTF-8`** by default (override with `PYXLE_SSR_LOCALE`) so `toLocaleString()` and other Intl calls stop causing hydration mismatches.
- **Vite resolver prefers pinned versions.** `pyxle build` now runs `npm install` before falling back to `npx --yes vite`, so builds honour your `package.json` pin instead of fetching `vite@latest`.

---

## Getting Started

New to Pyxle? Start here.

- [Installation](getting-started/installation.md) -- Prerequisites and install steps
- [Quick Start](getting-started/quick-start.md) -- Create your first Pyxle app in 5 minutes
- [Project Structure](getting-started/project-structure.md) -- What each file and folder does

## Core Concepts

The fundamentals of how Pyxle works.

- [`.pyxl` Files](core-concepts/pyxl-files.md) -- How Python and React coexist in one file
- [Routing](core-concepts/routing.md) -- File-based routing, dynamic segments, catch-all routes
- [Data Loading](core-concepts/data-loading.md) -- `@server` loaders and passing props to components
- [Server Actions](core-concepts/server-actions.md) -- `@action` mutations, `<Form>`, and `useAction`
- [Layouts](core-concepts/layouts.md) -- Shared layouts, templates, and page composition

## Guides

Practical guides for common tasks.

- [Comparison](guides/comparison.md) -- Pyxle vs. Reflex, Django, NiceGUI, Streamlit, and Next.js + FastAPI
- [Styling](guides/styling.md) -- Plain CSS, CSS Modules, opt-in Tailwind v4, and global stylesheets
- [Third-party packages](guides/third-party-packages.md) -- Adding npm/pip packages, the import alias, and shadcn/ui
- [Head Management](guides/head-management.md) -- `<Head>` component, the `HEAD` variable, and dynamic meta tags
- [Caching](guides/caching.md) -- Server-side page caching, `revalidate`, the `CACHE` directive, and static generation
- [Streaming SSR](guides/streaming.md) -- Streaming `<Suspense>` pages with `renderToPipeableStream` for faster time-to-first-byte
- [API Routes](guides/api-routes.md) -- Building JSON APIs under `pages/api/`
- [Middleware](guides/middleware.md) -- Application-level and route-level middleware
- [Plugins](guides/plugins.md) -- Composing apps via `pyxle.config.json::plugins` (Django-style)
- [Environment Variables](guides/environment-variables.md) -- `.env` files, `PYXLE_PUBLIC_` prefix, and config overrides
- [Error Handling](guides/error-handling.md) -- `LoaderError`, `ActionError`, `error.pyxl`, and `not-found.pyxl`
- [Client Components](guides/client-components.md) -- `<Script>`, `<Image>`, `<ClientOnly>`, and `<Link>`
- [TypeScript](guides/typescript.md) -- Typed editor support, generated `.d.ts`, and the `pyxle typecheck` gate
- [The road to a fully-typed Pyxle](guides/fully-typed-pyxle.md) -- Roadmap: authoring in TypeScript and forwarding Python types to the client
- [Security](guides/security.md) -- CSRF protection, CORS, and HEAD sanitisation
- [Deployment](guides/deployment.md) -- `pyxle build`, `pyxle serve`, and hosting in production
- [AI accessibility (llms.txt & .md)](guides/llms.md) -- Serve every page as Markdown for AI agents: .md URLs, llms.txt, and conversion hooks
- [Editor setup](guides/editor-setup.md) -- VS Code extension, the Pyxle language server, and CI linting
- [Pyxle Studio](guides/studio.md) -- The dev dashboard: routes, an interactive loader/action tester, live requests, metrics, config, and checks
- [Debugging](guides/debugging.md) -- Breakpoints directly in .pyxl source with VS Code F5, `pyxle dev --inspect`, or any DAP client
- [Background Tasks](guides/background-tasks.md) -- request.state.background and the in-process task queue
- [Build Optimization](guides/build-optimization.md) -- pyxle build --analyze, modulepreload hints, and the <Image> component
- [Migrating from Flask/Django](guides/migration-from-flask-django.md) -- Route, template, and ORM equivalents in Pyxle
- [Migrating .pyx to .pyxl](guides/migration-pyx-to-pyxl.md) -- The legacy extension migration
- [Pyxle for AI coding agents](guides/for-ai-agents.md) -- Why Pyxle is the framework most optimised for pairing with Claude, Cursor, Copilot, and other AI coding agents

## Reference

Complete API and configuration reference.

- [CLI Commands](reference/cli.md) -- Every command, flag, and option
- [Configuration](reference/configuration.md) -- Full `pyxle.config.json` schema
- [Runtime API](reference/runtime-api.md) -- `@server`, `@action`, `LoaderError`, `ActionError`
- [Client API](reference/client-api.md) -- All client-side components and hooks
- [Plugins API](reference/plugins-api.md) -- `PyxlePlugin`, `PluginContext`, `plugin(name)`

## First-party plugins

Official plugins maintained alongside the framework.

- [pyxle-db](plugins/pyxle-db.md) -- SQLite-first database with migrations
- [pyxle-auth](plugins/pyxle-auth.md) -- Email+password session authentication
- [pyxle-mail](plugins/pyxle-mail.md) -- Transactional email over SMTP, Resend, or the console

## Architecture

The complete architecture handbook -- a guided tour of how Pyxle is built on the inside, written for everyone from "I just installed Pyxle yesterday" to "I want to send a PR that touches the SSR worker pool."

- [Architecture overview](architecture/README.md) -- Start here. Index of every architecture doc.
- [Request lifecycle](architecture/overview.md) -- One HTTP request, end to end, in one read.
- [The .pyxl file format](architecture/pyxl-files.md) -- Why Pyxle invented a new file extension.
- [The parser](architecture/parser.md) -- How `.pyxl` files get split into Python and JSX using only `ast.parse`. The most sensitive code in the framework.
- [The compiler](architecture/compiler.md) -- How parsed pages become `.py` + `.jsx` + `.json` artifacts.
- [Routing](architecture/routing.md) -- File-based routing, dynamic segments, catch-all routes, layouts, error boundaries.
- [The dev server](architecture/dev-server.md) -- Starlette + Vite + the file watcher + the incremental builder + the WebSocket overlay.
- [Server-side rendering](architecture/ssr.md) -- Loader execution, the Node.js worker pool, head merging, document assembly, streaming.
- [Build and serve](architecture/build-and-serve.md) -- What `pyxle build` and `pyxle serve` do for production.
- [The runtime](architecture/runtime.md) -- The `@server` and `@action` decorators and the *zero-magic* contract.
- [The CLI](architecture/cli.md) -- `pyxle init`, `dev`, `build`, `serve`, `check`. Config precedence and tolerant-mode validation.

## Advanced

For framework contributors and power users.

- [SSR Pipeline](advanced/ssr-pipeline.md) -- High-level SSR overview (see [architecture/ssr.md](architecture/ssr.md) for the full deep-dive)
- [Compiler Internals](advanced/compiler-internals.md) -- High-level compiler overview (see [architecture/compiler.md](architecture/compiler.md) and [architecture/parser.md](architecture/parser.md) for the full deep-dive)

## Examples

Complete, runnable apps in the [`examples/`](../examples) directory of the repo.
Each is written up in [Example applications](examples.md), which explains what
each one is meant to prove and what to look at first.

- [Charts](../examples/charts) -- A [Recharts](https://recharts.org) chart rendered from data a Python `@server` loader aggregated, in one `.pyxl` file. Server-rendered SVG, live after hydration, and a written-down account of what it takes to make a DOM-measuring library hydrate cleanly.
- [Chat](../examples/chat) -- Realtime chat: a page that serves both HTML and a WebSocket at the same path, using `pyxle.realtime` and the `useWebSocket()` hook.

## FAQ

- [Frequently Asked Questions](faq.md)

---

## Links

- GitHub: [github.com/pyxle-dev/pyxle](https://github.com/pyxle-dev/pyxle)
- Issues: [github.com/pyxle-dev/pyxle/issues](https://github.com/pyxle-dev/pyxle/issues)
- Install: `pip install pyxle-framework`
- PyPI: [pypi.org/project/pyxle-framework](https://pypi.org/project/pyxle-framework/)
