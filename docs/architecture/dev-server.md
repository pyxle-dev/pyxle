# The dev server

`pyxle dev` is the command you'll spend the most time with. It runs a
Starlette ASGI app on port 8000, a Vite subprocess on port 5173, an
SSR worker pool, an incremental compiler, a file watcher, and a
WebSocket error overlay — all coordinated as a single async program.

This doc explains how those pieces fit together. By the end you'll
understand what every line in the startup banner means, what happens
when you save a file, and how to read the dev server's source code.

**Files (`pyxle/devserver/`):**

| File | What it does |
|---|---|
| `__init__.py` (~290) | The `DevServer` class and the top-level lifecycle |
| `starlette_app.py` (~820) | Creates the Starlette ASGI app and routers |
| `settings.py` (~150) | The frozen `DevServerSettings` config object |
| `scanner.py` (~100) | Walks `pages/` and computes file content hashes |
| `builder.py` (~165) | Orchestrates one incremental build pass |
| `watcher.py` (~350) | Watches the filesystem and debounces events |
| `vite.py` (~370) | Spawns and supervises the Vite subprocess |
| `proxy.py` (~155) | Forwards Vite-served URLs to Vite's port |
| `registry.py` (~380) | Loads compiled metadata into a `RouteTable` |
| `routes.py` (~280) | The `PageRoute` / `ApiRoute` / `ActionRoute` dataclasses |
| `layouts.py` (~295) | Generates layout-wrapped client modules |
| `overlay.py` (~105) | WebSocket overlay for error notifications |
| `build_errors.py` (~280) | Which sources failed to compile, and the page served instead |
| `error_pages.py` (~140) | Discovers `error.pyxl` and `not-found.pyxl` boundaries |
| `route_hooks.py` (~225) | Per-route middleware policies |
| `middleware.py` (~75) | Loads custom user middleware modules |
| `tailwind.py` (~300) | Legacy standalone Tailwind v3 watcher (Tailwind v4 runs through the `@tailwindcss/vite` plugin instead) |
| `csrf.py` (~160) | CSRF protection middleware |
| `client_files.py` (~2170) | Bundled client runtime sources |
| `scripts.py`, `styles.py` | Global script and stylesheet resolution |

That's a lot. Most of it doesn't matter for understanding how the
dev server works at a high level. The key pieces are: the
**Starlette app**, the **builder**, the **watcher**, **Vite**, and
the **registry**. Everything else is supporting infrastructure
around those five.

---

## Lifecycle in one diagram

```
$ pyxle dev
   │
   ▼
1. Load config
   - Read pyxle.config.json
   - Apply env vars
   - Apply CLI flags
   - Build a frozen DevServerSettings
   │
   ▼
2. Initial compile (builder.py)
   - Scan pages/ for .pyxl and .py files
   - Compile every file via PyxParser + ArtifactWriter
   - Write .pyxle-build/{server,client,metadata}/ artifacts
   - Compose layouts
   - Build the metadata registry → RouteTable
   │
   ▼
3. Start Vite (vite.py)
   - Spawn `vite dev --port 5173`
   - Wait for TCP readiness on port 5173
   - Auto-restart if Vite crashes
   │
   ▼
4. Start the SSR worker pool (ssr/worker_pool.py)
   - Spawn N persistent Node.js workers (default: 1)
   - Each worker speaks NDJSON on stdin/stdout
   │
   ▼
5. Build the Starlette app (starlette_app.py)
   - Register page, API, action routes
   - Add middleware (CORS, CSRF, static, custom, Vite proxy)
   - Add health endpoints (/healthz, /readyz)
   - Add WebSocket route for the overlay
   │
   ▼
6. Start the file watcher (watcher.py)
   - Rebuild watch: pages/, dev.watch dirs, global stylesheets/scripts
   - Index-only watch: public/ (served live — never rebuilds/reloads)
   - Debounce events for 250ms
   - On a rebuild-watch change: rebuild via builder.py and reload registry
   │
   ▼
7. Start uvicorn on port 8000
   - The Starlette app is now serving requests
```

When all seven steps are done, the console shows a curated startup summary:

```
✅ Pyxle dev server ready in 512 ms
ℹ️    Local:   http://127.0.0.1:8000
ℹ️    Vite:    http://127.0.0.1:5173
ℹ️    Routes:  13 page(s), 1 API route(s)
ℹ️    Studio:  http://127.0.0.1:8000/__pyxle/studio
```

By default the per-line Vite firehose and the internal step-by-step lifecycle
chatter (`Preparing …`, `Launching Vite …`, `Discovered … route(s)`, the raw
`[vite] …` output) are hidden so the console stays readable. Each incremental
rebuild then prints a single concise line (`✅ Rebuilt … in X ms`). Run
`pyxle dev --verbose` (or `pyxle -v dev`) to restore the full firehose and the
debug-level internals — every one of those hidden lines is emitted at debug
level, so verbose mode surfaces the entire lifecycle for troubleshooting.

Genuine signal — errors, warnings, the URLs, and rebuild success/failure — is
always shown regardless of verbosity.

---

## The Starlette app

`create_starlette_app()` (`devserver/starlette_app.py:506`) is the
factory function that builds the entire ASGI application. It returns
a `Starlette` instance with:

### Routes

- **Page routes** (`build_page_router()`, line 291) — one Starlette
  `Route` per `PageRoute` in the route table. Each route has a
  closure handler that knows which page to render.
- **API routes** (`build_api_router()`, line 187) — one Starlette
  endpoint per `pages/api/*.py` file. Functions named after HTTP
  methods (`get`, `post`, etc.) get registered for those methods;
  a function named `handle` gets all methods.
- **Action routes** (`build_action_router()`, line 363) — POST-only
  endpoints under `/api/__actions/{name}` for every `@action`
  decorated function.
- **Static asset mount** (`build_client_assets_mount()`, line 498)
  — serves `/client/*` and `/dist/*` directly from disk.
- **Public files mount** (`build_static_files_mount()`, line 485)
  — serves whatever's in `public/`.
- **Health endpoints** — `/healthz` and `/readyz` for orchestration.
- **Catch-all 404** — walks up the request path looking for the
  nearest `not-found.pyxl` boundary.
- **WebSocket route** at `/__pyxle__/overlay` — used by the dev
  overlay client.

### Middleware stack

Listed from outermost to innermost (outermost runs first on request,
last on response):

1. **GZip** — production only
2. **CORS** — if `cors` is configured in `pyxle.config.json`
3. **CSRF** — if `csrf.enabled` is true in config
4. **`StaticAssetsMiddleware`** — short-circuits requests for
   `/client/*` and public assets so they don't reach the page router
5. **Custom user middleware** — anything declared in
   `pyxle.config.json` `middleware: ["mymodule:MyMiddleware"]`
6. **Vite proxy** — dev only, forwards JS/CSS/HMR requests to Vite

The middleware stack is built in `create_starlette_app()` line
~668. Each middleware is added with `Middleware(...)` and Starlette
chains them in order.

### Lifespan hooks

Starlette has a `lifespan` callback that runs on startup and
shutdown. Pyxle uses it to:

- **Start the SSR worker pool** on startup
- **Stop the worker pool gracefully** on shutdown (give workers 5
  seconds to exit cleanly, then kill any holdouts)

This means workers are alive for the lifetime of the dev server,
not per-request.

---

## The incremental builder

`build_once()` (`devserver/builder.py:50`) is the function that
runs **one build pass** — initial compile, or rebuild after a file
change. It:

1. **Scans `pages/`** with `scanner.scan_source_tree()` to find
   every `.pyxl`, `pages/api/*.py`, and client asset file. For each
   file, it computes the SHA256 hash of the contents.
2. **Compares hashes against the previous build's metadata** to
   find which files actually changed since last time.
3. **Compiles only the changed `.pyxl` files** by calling
   `compile_file()` for each. Unchanged files are left alone.
4. **Copies API modules** (`pages/api/*.py`) to their build location.
5. **Composes layouts** for any pages whose ancestor `layout.pyxl`
   files have changed (`layouts.compose_layout_templates()`).
6. **Syncs global stylesheets and scripts** declared in the config.
7. **Removes orphaned artifacts** for source files that have been
   deleted since the last build.
8. **Returns a `BuildSummary`** dataclass with counts: pages
   compiled, APIs copied, etc.

The hash-based diffing is the key to performance. A typical 50-page
project has *thousands* of unchanged files at any moment; running
the parser on every one of them on every save would be wasteful.
With hash diffing, a single-file edit triggers exactly one
`compile_file()` call.

Source: `devserver/builder.py:50-160`.

### When does a layout-only change trigger a page rebuild?

Layouts are tricky: when you edit `pages/dashboard/layout.pyxl`,
**every page under `pages/dashboard/`** needs its composed route
module regenerated. The composed module is what bundles the layout
with the page, so it needs to be re-emitted whenever the layout's
identity changes.

The builder handles this by recompiling layouts first, then
re-running the layout composition pass for any page whose ancestor
layout was rebuilt. This is invisible to you — you save the layout,
and a moment later the affected pages reload in the browser.

---

## The watcher

`ProjectWatcher` (`devserver/watcher.py:101`) wraps Python's
`watchdog` library to observe filesystem events. It's structured as:

1. A **watchdog observer** running in a background thread, posting
   raw events to a queue.
2. A **debounce buffer** that aggregates events for 250ms (the
   default; configurable). Saving a file twice in quick succession
   only triggers one rebuild.
3. A **dispatch callback** that the dev server registers; the
   watcher calls it with the set of changed paths after the debounce
   window expires.

The dispatch callback runs `build_once()` and then refreshes the
metadata registry.

### When a rebuild fails

A build pass does not stop at the first file it cannot compile. It
builds every other source, records the ones it could not, and raises
`BuildFailed` carrying both halves — the partial `BuildSummary` and a
`BuildFailure` per broken file (path, line, column, message, and a code
frame captured at failure time). Stopping at the first failure used to
mean that while one page was unparseable, an edit to any file scanned
after it never reached the browser.

Three things then happen, and they are deliberately independent:

1. **The terminal** prints one line per broken file, located:
   `Rebuild failed: pages/about.pyxl:7:9: unexpected indent`. Files that
   *did* rebuild are still reported on their own success line.
2. **The overlay** receives the failure (route label `(rebuild)`) and
   keeps it as the current error until it is retracted, so a browser
   that connects afterwards — which is what a page reload produces — is
   told about it too.
3. **The failed set** is published to `app.state.pyxle_build_failures`
   (a `BuildFailureRegistry`, dev-only). Every page handler consults it
   before rendering: a route whose own source failed, or whose
   `layout.pyxl` / `template.pyxl` chain failed, answers `500` with the
   compile error instead of the artifacts the previous pass left on
   disk. Without this the route serves the last version that compiled —
   a page that looks completely healthy while the file is broken.

Scope is exactly the failing source and the pages a broken wrapper
wraps: a broken `pages/about.pyxl` leaves `/`, `/blog` and every API
route untouched, and a broken `pages/blog/layout.pyxl` takes down
`/blog/*` and nothing above it.

**A page that has never compiled reaches the 404 path, not a page
handler.** No successful pass ever registered a route for it, so the
request falls through to whatever answers unmatched URLs — the built-in
404 document, or the project's own `not-found.pyxl`. Both describe a
missing address, which is the wrong problem: the file is in `pages/`
and named correctly, and the compiler error explaining why nothing
serves it is already in the registry.

So the 404 path consults the registry too, before either answer. Each
`BuildFailure` records what the source *would* have served —
`url_paths` for parameterless URLs, `url_patterns` for dynamic ones
(`/posts/{slug}`, compiled with Starlette's own `compile_path` so
matching cannot drift from routing) — and a request matching one gets
the same `500` compile-error document a stale route serves, with the
closing hint changed to say there is no route for the address yet. The
most specific claim wins: a static URL over a pattern, a concrete
pattern over a catch-all, a deeper catch-all over a shallower one.

Two boundaries keep that from over-claiming. Patterns are matched
**only** where the router matched nothing (Starlette merges `endpoint`
into the scope on a match, and its absence is the test), so a live
route always outranks a pattern that was never registered — an endpoint
raising `HTTPException(404, "User not found")` still says exactly that.
And a URL no broken source claims is an ordinary 404, unchanged.

**Both halves of a `.pyxl` are checked, for the price of one.** The
Python half is checked by `ast.parse`. The JSX half is checked by the
Babel extractor pass that already runs on every compile to read
`<Head>`, `<Script>`, `<Image>` and `<Suspense>` — it has to parse the
section to find them, so a parse failure is a judgement it has already
made. It used to discard that judgement and return empty metadata,
which is why a JSX typo logged a success line, silently dropped the
page's `<Head>`, and only surfaced later from the bundler against the
generated `.jsx`. Reporting it adds no Node subprocess: measured at one
spawn per compile either way, and it removes the second spawn `pyxle
check` used to make to rediscover the same error.

Two constraints shape it. The line is mapped from the JSX section back
to the `.pyxl` (`_map_jsx_line`), because every JSX-side tool numbers
lines from the start of the section it was handed. And it is gated on
`settings.debug` (`compile_file(..., report_jsx_syntax=...)`), so
`pyxle build` and `pyxle serve` keep their existing behaviour and a
disagreement between Babel and esbuild can never newly break a release
that builds today. The extractor also distinguishes "this code does not
parse" from "the checker could not run"
(`JSXParseResult.toolchain_available`); a missing Node install degrades
silently, exactly as before, instead of failing every file.

The compile-error page is the same dark document the SSR failure path
serves (`pyxle/devserver/build_errors.py`), plus the source frame and a
socket connection that reloads the page when the next rebuild succeeds.
It is never cached (`Cache-Control: no-store`), and the registry is
never created outside `debug` mode — `pyxle build` still refuses to
produce a `dist/` from a project that does not compile.

A source that has *never* compiled has no route of its own, so its URL
would otherwise be answered by whatever dynamic or catch-all page
matches it. Each failure records the parameterless URL it would serve,
and a handler reached at that URL reports the failure rather than
rendering. A page that has never compiled, is dynamic (`[slug].pyxl`),
*and* has no route is not covered — with no catch-all in the project
that URL simply 404s.

### Module cache invalidation

When a `.pyxl` file's *Python half* changes, the dev server's existing
imported version of the compiled `.py` is stale. Python's import
system caches modules in `sys.modules` — re-importing the same module
key returns the cached version.

So that module-level globals persist across requests exactly like
`pyxle serve`, the dev server imports a page/action module once and
reuses it, re-importing only after a rebuild. A rebuild advances a
process-wide *reload generation* (`ssr/module_cache.py`); each imported
module is stamped with the generation it was built against, and the
importer re-imports from disk when the generation has advanced. Changed
`.py` helper modules are additionally dropped from `sys.modules`
(`_invalidate_python_modules`), so a re-imported page picks up an edited
helper too.

This is why Python edits show up immediately without restarting the dev
server. The hot-reload story is: write file → watcher fires → incremental
build → reload generation advances → next request re-imports the new code
(module-level state resets, as it would on any restart). Between rebuilds
the module is reused, so a module-level counter or cache persists across
requests — but only per process (see [SSR → module reuse](ssr.md)).

**A pass can change running code without changing a build artifact.** A
private helper (`pages/api/_shared.py`, anything under `dev.watch`) is not
a source the scanner builds — it serves no URL, so nothing compiles or
copies it — and editing one produces a `BuildSummary` with no changes at
all. It is still code the server runs, and `WatcherStatistics` says so on
its own field: `purged_modules`, the modules dropped from `sys.modules`
this pass. Both the watcher (deciding whether to advance the reload
generation) and the dev server's rebuild listener (deciding whether to
refresh the route table) gate on *artifact changes **or** purged modules*,
and the two must not disagree. When the listener gated on the summary
alone, a helper edit was read as "nothing happened": the route table was
never refreshed, so endpoint modules were never re-imported and the
endpoint served the helper's old values indefinitely — no rebuild line, no
error, nothing to restart, until an unrelated file was edited.

Refreshing the route table re-imports endpoint modules, so it is also
where a broken helper surfaces. If the import fails, the routes are not
swapped: the previous table keeps serving and the terminal says so. The
next successful change applies the new one — no restart.

### What's watched

By default, the watcher observes:

- `pages/` — recursive, on the **rebuild** watch (a change runs
  `build_once()` and reloads the browser)
- Any directory listed in [`dev.watch`](../reference/configuration.md#development)
  — also on the rebuild watch, so a shared Python module imported from
  outside `pages/` (e.g. `lib/`) hot-reloads
- Any file referenced in `globalStyles` or `globalScripts` config
- `public/` — recursive, but on a **lightweight index-only** watch that
  never rebuilds or reloads (see below)
- The `pyxle.config.json` itself (changing the config triggers a
  full restart, not a hot-reload)

On the rebuild watch, generated build output is ignored so a rebuild's
own writes don't trigger another rebuild: `.pyxle-build/` and
`__pycache__/` trees, `*.pyc` bytecode, and `*.db`/`*.db-wal`/`*.db-shm`
journals. A [`dev.ignore`](../reference/configuration.md#development) list
adds extra glob patterns on top of these built-ins — it can add ignores
but never clear them. The watcher does not watch `node_modules/` or
`dist/`.

The watcher also reacts only to genuine **change** events — create,
modify, delete, rename, and writable-close — and ignores read-only file
open/close events. This matters on Linux: a rebuild *reads* every source
file to hash and recompile it, and those reads surface through `inotify`
as open/close events. Treating a read as an edit would let the rebuild's
own reads re-trigger it in an endless loop; filtering to real changes
prevents that. (macOS `FSEvents` never reports reads, so the loop only
ever appeared on Linux.)

### `public/` is served live, not rebuilt

Changes under `public/` **do not** rebuild or reload the page — matching
Next.js, which never rebuilds on `public/` changes. Public assets are
served straight from disk (dev serves them with a revalidating
`no-cache` header), so an **edited** file is reflected on the next
request/refresh with no watcher action.

The watcher still keeps a lightweight watch on `public/` for one job: a
**newly created or deleted** file changes which URLs resolve, so those
structural events refresh the static-file index the static middleware
uses for its O(1) membership check. That's how a freshly added public
asset becomes reachable without restarting `pyxle dev`. This index
watch never calls `build_once()` and never touches the browser-reload
channel. (`pyxle serve` builds the index once and runs no watcher — the
production tree is immutable.)

---

## Vite integration

`ViteProcess` (`devserver/vite.py`) supervises the Vite dev
server subprocess. The Vite process is responsible for:

- Bundling JSX for the browser
- Serving static assets from `.pyxle-build/client/`

Vite's React Refresh runtime is present in the page, but Pyxle's own
watcher reloads the browser on every successful rebuild
(`_maybe_schedule_reload`), so a source edit lands as a full page
reload rather than a hot update — see [For AI agents](../guides/for-ai-agents.md).

Pyxle's dev server **does not** serve JS/CSS to the browser
directly. Instead, it **proxies** asset requests to Vite's port.
This sounds inefficient but it isn't: the proxy just forwards bytes,
and Vite is the JS expert.

### Spawning Vite

`ViteProcess` launches plain `vite`, resolved off `PATH`. Only when
that spawn fails with "no such executable" does it fall back, in
order:

1. `node node_modules/vite/bin/vite.js` — the project's own install,
   run through the `node` on `PATH`
2. `node_modules/.bin/vite` (or `vite.cmd` on Windows) — the same
   install, via its shim
3. `npm install` — run once, if the project has a `package.json` and
   the install hasn't already been tried, then 1 and 2 are retried
4. `npx --yes vite` — last resort

Candidates 1 and 2 are picked by checking that the file exists; the
resolved command is then committed to, and whether it worked is
decided by the readiness probe below rather than a separate
`--version` call. Step 3 is why a fresh `pyxle init` works on the
second `pyxle dev`: the first attempt notices the missing
dependencies and installs them.

Source: `devserver/vite.py` (`_recover_missing_vite`).

### Readiness probing

After spawning Vite, Pyxle polls Vite's TCP port (default 5173)
every 100ms until it accepts connections. Once it does, Vite is
"ready" and Pyxle starts serving page requests. The whole readiness
window is usually under a second.

If Vite takes longer than 10 seconds to come up, Pyxle reports a
timeout and shuts down — usually a sign that something else is
holding port 5173.

### Auto-restart

If the Vite subprocess exits unexpectedly (crashes, OOMs, gets
killed), Pyxle's monitor coroutine catches the exit and schedules a
restart after 0.5 seconds. The restart probes for readiness again
and re-attaches to the proxy.

This is invisible during normal use but essential for long dev
sessions: it keeps Vite running across edits to its config, plugin
errors, and Node version mismatches without requiring you to
restart the dev server.

Source: `devserver/vite.py` (`_restart_after_exit`).

### The proxy

`ViteProxy` (`devserver/proxy.py:40`) is a small ASGI middleware
that forwards specific URLs to Vite. It matches:

- Anything ending in `.js`, `.jsx`, `.ts`, `.tsx`, `.mjs`, `.css`,
  or `.map`
- Anything starting with `/@vite/` (Vite's internal endpoints)
- `/@react-refresh` (the HMR endpoint)

For matching requests, it uses `httpx.AsyncClient.stream()` to
forward chunks without buffering, so a 5MB CSS file doesn't get
loaded into Pyxle's memory before being sent to the browser. Headers
are filtered to drop hop-by-hop fields.

For non-matching requests, the middleware passes through to the
next layer (the page/API router).

### Which browsers the dev server trusts

`pyxle dev` runs two HTTP servers. Pyxle serves the document; Vite
serves the JavaScript modules that document loads. Same machine,
different port is still a **different origin**, so every
`<script type="module">` on the page is a cross-origin request, and
Vite answers it only for an origin on its CORS allow-list — which by
default is loopback only.

That default is the whole reason this needs a policy. Run
`pyxle dev --host 0.0.0.0`, open the page from a phone, and the
document arrives complete and correct while every module request is
refused: a page that renders and never becomes interactive. Nothing
reports it. Vite answered `200`, so it logs nothing; a refused module
is not a JavaScript error, so no `window.onerror` handler and no
overlay hears about it.

`devserver/dev_origins.py` is the single definition of "an origin this
dev server may serve", and every place that has to answer the question
reads it: the generated `vite.config.js`, Pyxle's own dev CORS
middleware, the `<script src>` host, the overlay WebSocket, and the
startup banner. A dev server that answers off-box allows the addresses
it can itself be reached at — loopback, and the private-network ranges
on its own ports. Never `cors: true`: that would let any page the
developer happens to have open read the source of the project they are
working on.

**The generated config describes the running server.** Every command
that builds — `pyxle routes`, `pyxle check`, `pyxle build` — regenerates
`.pyxle-build/client/vite.config.js`, and only `pyxle dev` was told the
addresses. Vite watches its own config and restarts onto whatever it
finds there, so a regeneration from the config file's loopback defaults
would silently narrow a running server's allow-list and kill every
remote browser attached to it. So the addresses come from the one
process that knows them: `pyxle dev` records them in
`.pyxle-build/dev-server.json`, and any command that regenerates the
config while that server is alive keeps them
(`dev_origins.active_dev_session`). A record whose process is gone — a
crashed or `kill -9`'d server — is ignored.

**When it does go wrong, something says so.** Two lines exist because
this failure otherwise has no symptom at all:

- The server warns when it serves a document to an origin its own
  allow-list does not cover ("this page … will render and never become
  interactive"), once per origin.
- The dev document installs a capturing `error` listener that names any
  module from Vite's origin that failed to load, in the browser console.

The overlay WebSocket uses the same allow-list, so a browser the dev
server invited with its `Network:` URL gets hot reload and the error
overlay too — and a refused socket is logged rather than silently
closed.

---

## The metadata registry

`MetadataRegistry` (`devserver/registry.py`) is the in-memory map
from route paths to `PageRoute` / `ApiRoute` / `ActionRoute`
objects.

`build_metadata_registry()` (line 118) walks
`.pyxle-build/metadata/` and reads each `.json` file. For every
page, it constructs a `PageRoute` containing:

- The route path (primary)
- Any alias paths (from optional catch-all routes)
- Paths to the server module, client module, and metadata
- The Python module key for `importlib`
- Loader name and line number
- Static head metadata
- Action metadata

The dev server then iterates the registry to register Starlette
routes. After every rebuild, the registry is **rebuilt from scratch**
— Pyxle never tries to incrementally patch the registry, because
the cost of a full rebuild is small (millisecond range for typical
projects) and the correctness is much easier to reason about.

### Layout head discovery

A layout's `<Head>` JSX block and its Python `HEAD` variable both
contribute to every page below it. `find_layout_head_contributions()`
walks ancestor directories of each page looking for `layout.pyxl`
(and `template.pyxl`) metadata and returns a
`LayoutHeadContribution` holding the two **in separate channels**:

- `jsx_blocks` — raw JSX source from `<Head>`, which may still hold
  unevaluated `{expressions}` and is filtered for them when the head
  is merged.
- `head_variable` — the layout's `HEAD` list, which is finished HTML
  and is passed through verbatim (braces in it are content: JSON-LD,
  a CSS rule).

Keeping them apart is load-bearing, not cosmetic: filtering the
`HEAD` channel deletes a layout's JSON-LD from every page under it,
and nothing re-supplies it, because a Python `HEAD` variable is never
rendered by React. The SSR pipeline reads both at request time
without re-parsing.

Source: `devserver/registry.py:337`.

---

## The error overlay

`OverlayManager` (`devserver/overlay.py:24`) maintains a set of
WebSocket connections from browser tabs. When the dev server has
something to tell the browser — a build error, a runtime error, a
successful rebuild — it broadcasts a JSON message to every
connected client.

Event types:

- `"error"` — sent when a build fails or a runtime error occurs.
  Includes the error message, stack, and "breadcrumbs" describing
  which stage of the request pipeline failed (loader, render, head
  evaluation, etc.).
- `"clear"` — sent when a previously-failing route succeeds. The
  client uses this to dismiss any visible error overlay.
- `"reload"` — sent after a successful rebuild — including a rebuild
  that failed on *another* file, for the half that did compile. The
  client triggers a soft reload of the current page.

An error is not a one-off broadcast: the manager keeps the current
error for each route (keyed by route path, plus `(rebuild)` for build
failures) and replays the most recent one to every client that connects
afterwards. A reload closes the socket that was told about the error and
opens a new one, so without the replay a reload made the error vanish
while the fault remained. `"clear"` for a route drops that route's entry
and only that one, so a healthy render of `/` never silences a build
failure in `pages/about.pyxl`.
The socket accepts the origins in
[the dev server's allow-list](#which-browsers-the-dev-server-trusts) —
the same ones Vite is told to serve modules to, so a browser that can
load the page can also receive its reloads and errors. Any other origin
is closed with code `4003` and named in the terminal; it is deliberately
not "any origin", because these messages carry source paths, stack
traces and forwarded server logs.

- `"log"` — a server-side `logging` record forwarded to the browser
  devtools console (dev only). The payload carries the target
  `console` method (`info`/`warn`/`error`/`debug`), the formatted
  message, and a display name for the source; the client prints it
  prefixed `[pyxle:server <source>]`. A bounded `logging.Handler`
  (`devserver/log_forwarding.py`) is attached to the root logger while
  the dev server runs and detached on shutdown. It never blocks the
  event loop, drops records when no client is connected or the send
  fails, guards against re-entrancy, and throttles bursts. By default
  only `INFO`+ from your own loggers is forwarded; `--verbose` also
  forwards `DEBUG` and the framework's internal loggers. To make those
  records reachable (Python's root logger defaults to `WARNING`, which
  drops `INFO` before any handler sees it), the handler lowers the root
  logger level to `INFO` (`DEBUG` under `--verbose`) for the lifetime of
  the dev session and restores the previous level on shutdown. This is
  dev-only; `pyxle serve` never touches your logging configuration.

  Two details make the canonical `log = logging.getLogger(__name__)`
  behave the way a Python developer expects.

  **Compiled pages are user code, not internals.** Inside a `.pyxl`,
  `__name__` is the synthetic module key the dev server imports the page
  under — `pyxle.server.pages.about` (see `registry._module_key`). The
  namespace filter that keeps `uvicorn`, `watchfiles` and Pyxle's own
  `pyxle.*` loggers out of the console therefore has an explicit
  carve-out for `pyxle.server.*`: there is no such package, and every
  logger beneath it belongs to the developer. The console prefix shows
  the `.pyxl` file instead of that key, resolved from the record's own
  `pathname` — compiled pages execute with their source as
  `co_filename`, so the record already knows. A record carrying a
  generated path instead (an API module runs from its copy under
  `.pyxle-build/`) keeps the module key rather than naming a file nobody
  edits.

  **The terminal keeps what the browser gets.** Attaching a handler to
  the root logger ends `logging.lastResort`, so the dev server installs
  an equivalent stderr sink when nothing else is listening. That sink is
  gated by a filter rather than a level: `WARNING`+ from anything
  (exactly what `lastResort` printed) *plus* whatever is being forwarded
  to the browser. Pinned at `WARNING` it would send the `INFO` records
  the lowered root level exists to capture *only* to a devtools console
  that may not be open.

The browser-side overlay client lives in `pyxle/client/` and is
included in the default scaffold.

---

## How a request flows through the dev server

Putting everything together, here's what happens when the browser
asks for a page in dev mode:

```
GET /dashboard
   │
   ▼
ASGI app (Starlette)
   │
   ▼
1. Static asset middleware
   "Is /dashboard a file in /client/ or /public/?"
   No → pass through
   │
   ▼
2. CORS / CSRF middleware (if enabled)
   "Is the request allowed?"
   Yes → pass through
   │
   ▼
3. Custom user middleware (if any)
   "Anything to do here?"
   No → pass through
   │
   ▼
4. Vite proxy
   "Does /dashboard look like a Vite asset?"
   No → pass through
   │
   ▼
5. Page router
   "Is /dashboard in the route table?"
   Yes → invoke the page handler closure
   │
   ▼
6. Page handler
   - Look up the PageRoute
   - In dev mode, purge stale modules from sys.modules
   - Call the SSR pipeline (build_page_response)
   │
   ▼
7. SSR pipeline (see ssr.md)
   - Run loader
   - Resolve head
   - Render component on a worker
   - Assemble document
   - Stream response
   │
   ▼
HTML response → browser
```

The browser then loads `/client/...` URLs for the JS bundle, which
hit the static asset middleware and get forwarded to Vite via the
proxy. Vite serves them, and React hydrates.

---

## What the dev server is *not*

Let me list a few things the dev server explicitly does **not** do,
because the absences are part of the design:

- **It doesn't bundle JS itself.** Vite does. Pyxle is a Python
  framework that proxies a JavaScript bundler — it doesn't try to
  out-Vite Vite.
- **It doesn't watch your `node_modules/`.** Adding a dependency
  requires `pip install` (Python) or `npm install` (JS) followed by
  a manual `pyxle dev` restart. We could watch them, but it would
  triple the watcher's event volume for marginal value.
- **It doesn't have its own caching layer.** The render cache lives
  inside the SSR worker (esbuild caches its bundles). The metadata
  cache is the registry. There's no application-level cache.
- **It doesn't guess about routes.** Every route comes from a real
  file on disk. There is no `routes.py` or `urls.py` you can
  manipulate at runtime.
- **It doesn't have a "production mode" toggle in dev.** `pyxle dev`
  is dev. `pyxle serve` is production. They are different commands
  for different lifecycles, and trying to make one mode mimic the
  other usually papers over real differences.

These absences are deliberate. The dev server is meant to be small
enough that you can read it cover to cover in an afternoon and
understand exactly what it does.

---

## Where to read next

- **[Server-side rendering](ssr.md)** — What happens *inside* a
  page handler: loader execution, head merging, component rendering
  on a worker, document assembly, streaming, and client-side
  navigation.

- **[Build and serve](build-and-serve.md)** — How `pyxle build`
  takes the same compiled artifacts that `pyxle dev` produces and
  packages them for production, and how `pyxle serve` runs without
  Vite or the file watcher.

- **[The CLI](cli.md)** — How `pyxle dev` parses its flags and
  config and bridges them to `DevServerSettings`.
