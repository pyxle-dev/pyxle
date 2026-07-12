# Overview — A request, end to end

This is the one doc to read first. We're going to follow a single HTTP
request all the way from `localhost:8000/` to your browser, naming every
component it touches and every transformation it goes through. By the end
of this page you'll have a working mental model of the entire framework.

To make it concrete, we'll trace the homepage of a fresh `pyxle init`
project — the one with a Pyxle logo and a *"You're ready to build with
Pyxle"* heading.

---

## The starting point: one `.pyxl` file

Here's the file we're going to render, simplified slightly:

```python
# pages/index.pyxl
from datetime import datetime, timezone

@server
async def load_home(request):
    return {
        "version": "0.1.7",
        "now": datetime.now(timezone.utc).strftime("%H:%M:%S UTC"),
    }


import React from 'react';
import { Head } from 'pyxle/client';

export default function Home({ data }) {
    return (
        <main className="p-8">
            <Head>
                <title>Pyxle App</title>
            </Head>
            <h1>You're ready to build with Pyxle.</h1>
            <p>Pyxle v{data.version} · {data.now}</p>
        </main>
    );
}
```

That's the whole file. Notice three things:

1. **There's no separator** between the Python and the JSX. Pyxle figures
   out where one ends and the other begins by *parsing* the file with
   Python's `ast` module — the parser is the most interesting subsystem in
   the framework, and it gets its own [deep-dive doc](parser.md).

2. **The decorators are tags, not wrappers.** `@server` doesn't transform
   `load_home` — it just sets `__pyxle_loader__ = True` on the function so
   the framework can find it later. You can read the decorator source in
   ten seconds: it's three lines. See [The runtime](runtime.md).

3. **`<Head>` is the recommended way to control the document head.**
   During SSR the compiler extracts the children of `<Head>` and merges
   them with head contributions from layouts and parent components. Pyxle
   also supports a legacy `HEAD` Python variable that the parser picks
   up at compile time, kept around for backward compatibility, but
   `<Head>` is the idiomatic choice for every real page. See
   [SSR § Stage 3: resolving the head](ssr.md#stage-3-resolving-the-head).

---

## Stage 0 — `pyxle dev` starts

Before any request can arrive, the dev server has to be running. When you
type `pyxle dev`, this is what happens:

1. **Load configuration.** The CLI looks for `pyxle.config.json`, parses
   it into a frozen `PyxleConfig` dataclass, applies environment-variable
   overrides (`PYXLE_*`), then applies CLI flag overrides
   (`--port`, `--host`, etc.). Precedence is **CLI > env > file > default**.
   Source: `cli/__init__.py`. Details: [The CLI](cli.md).

2. **Build the initial page set.** The compiler walks `pages/`, parses
   every `.pyxl` file, and writes three artifacts per file into
   `.pyxle-build/`:
   - `.pyxle-build/server/pages/index.py` — the Python loader, executable
   - `.pyxle-build/client/pages/index.jsx` — the React component, bundleable
   - `.pyxle-build/metadata/pages/index.json` — extracted route info
   Details: [The compiler](compiler.md).

3. **Start Vite.** Pyxle spawns Vite as a subprocess on port 5173, pointed
   at the just-generated `.pyxle-build/client/`. Vite handles JS/CSS
   bundling, hot module replacement, and React Refresh. Pyxle's dev
   server *proxies* asset requests to Vite — Vite never sees the user's
   HTTP requests directly. Details: [The dev server § Vite integration](dev-server.md#vite-integration).

4. **Start an SSR worker pool.** By default, Pyxle starts one persistent
   Node.js worker that stays alive for the life of the dev server. The
   worker speaks newline-delimited JSON over stdin/stdout. Each render
   round-trip is ~30-80ms; spawning a fresh subprocess per request would
   be 200-400ms. Details: [SSR § Worker pool mode](ssr.md#worker-pool-mode).

5. **Start the file watcher.** Pyxle watches `pages/` (plus any `dev.watch`
   directories and global stylesheets/scripts) for hot reload. When a watched
   file changes, the watcher debounces for 250ms (so saving twice in quick
   succession is one rebuild), then recompiles only the changed files and
   reloads the browser. Changes under `public/` are **not** rebuilt — static
   assets are served live from disk and picked up on the next refresh (like
   Next.js); the watcher only keeps `public/`'s file index current so a newly
   added asset resolves without a restart. Details:
   [The dev server § The watcher](dev-server.md#the-watcher).

6. **Start Starlette on port 8000.** This is the ASGI app that actually
   answers your browser. The router has separate branches for pages, API
   routes, action routes (`/api/__actions/<name>`), client assets,
   public assets, and a catch-all 404 handler. Details:
   [The dev server § The Starlette app](dev-server.md#the-starlette-app).

When all six are up, the console shows a curated startup summary:

```
✅ Pyxle dev server ready in 512 ms
ℹ️    Local:   http://127.0.0.1:8000
ℹ️    Vite:    http://127.0.0.1:5173
ℹ️    Routes:  1 page(s), 1 API route(s)
```

You're ready to take requests. The per-line Vite firehose and the step-by-step
internal build chatter are hidden by default — pass `pyxle dev --verbose` to
restore them. See [The dev server § lifecycle](dev-server.md#lifecycle-in-one-diagram).

---

## Stage 1 — The browser asks for `/`

You open `http://localhost:8000/` in Chrome. The browser sends:

```
GET / HTTP/1.1
Host: localhost:8000
Accept: text/html,application/xhtml+xml,...
```

Starlette's router receives this and dispatches to the handler that
`build_page_router()` registered for the `/` route — that handler is a
closure created by `_make_page_handler()` (`devserver/starlette_app.py:330`).

The handler resolves which `.pyxl` file owns this route by looking it up in
the **page registry** (`devserver/registry.py`). The registry was built
during the initial compile — it maps each route path to a `PageRoute`
dataclass containing every path the SSR pipeline needs:

```python
PageRoute(
    path="/",
    source_relative_path=Path("index.pyxl"),
    source_absolute_path=…/pages/index.pyxl,
    server_module_path=…/.pyxle-build/server/pages/index.py,
    client_module_path=…/.pyxle-build/client/pages/index.jsx,
    metadata_path=…/.pyxle-build/metadata/pages/index.json,
    module_key="pyxle.server.pages.index",
    loader_name="load_home",
    loader_line=10,
    head_elements=("<title>Pyxle App</title>",),
    head_is_dynamic=False,
)
```

That `PageRoute`, plus the request, plus the dev server settings, get
passed to `build_page_response()` in `ssr/view.py`. This is the SSR entry
point. Everything that follows lives inside it.

---

## Stage 2 — Run the loader

Pyxle imports the compiled server module
(`.pyxle-build/server/pages/index.py`). The module is imported once and
reused across requests — in production and, since it's reused between
rebuilds, in dev too — so a rebuild re-imports it and your `.pyxl` edits
are reflected immediately. (Reuse means module-level state persists across
requests; see [Server-side rendering](ssr.md).)

Once imported, Pyxle finds the function tagged `__pyxle_loader__ = True`
— that's `load_home` — and calls it with the Starlette `Request`:

```python
data = await load_home(request)
# data == {"version": "0.1.7", "now": "08:53:58 UTC"}
```

A few invariants matter here:

- **Loaders are always async.** The parser refuses to compile a sync
  `@server def`. This is enforced at compile time so you can never
  accidentally block the event loop.
- **Loaders take exactly one positional argument named `request`.** The
  parser checks this in `_detect_loader()` and emits a structured error
  if you violate it.
- **Loaders return a JSON-serializable dict.** If you return something
  that can't be serialized, the framework raises `ComponentRenderError`
  and shows a friendly overlay (in dev) or a generic 500 (in prod).

If the loader raises `LoaderError("Not found", status_code=404)`, Pyxle
walks up the directory tree from the current page looking for the
nearest `error.pyxl` file. If one exists, it renders that boundary with
the error context. If not, it falls back to the default error document.
Details: [SSR § Error handling](ssr.md#error-handling).

---

## Stage 3 — Resolve the head

While the loader's data is the **body** of the page, the **`<head>`** is
assembled from up to four sources, in order of increasing priority:

1. **Layout `<Head>` blocks.** If `pages/layout.pyxl` (or any ancestor
   layout) has a `<Head>` JSX block, its contents go in first.
2. **The page's `HEAD` Python variable.** Static or callable. If it's a
   callable, Pyxle invokes it with the loader data: `HEAD(data)`.
3. **The page's `<Head>` JSX blocks.** If the React component renders a
   `<Head>` block, that contributes too.
4. **Runtime registrations** (rare; used by some advanced helpers).

`merge_head_elements()` (`ssr/head_merger.py`) deduplicates these by tag
identity:

- `<title>` — only the highest-priority one wins.
- `<meta name="X">` — deduped by `name`.
- `<meta property="X">` — deduped by `property`.
- `<link rel="canonical">` — singleton.
- `<script src="X">` — deduped by `src`.
- Anything with `data-head-key="X"` — deduped manually.
- Tags without a clear identity (e.g., a second preconnect) are kept.

Each element is also **sanitized**: event handler attributes
(`onclick`, `onerror`) are stripped, `<` and `>` inside `<title>` are
escaped, and `javascript:` / `vbscript:` URLs are neutralized.

For our `index.pyxl`, the merged head ends up as:

```html
<title>Pyxle App</title>
```

Plus whatever Vite injects (the HMR client tag, the React Refresh
preamble) and Pyxle's own boilerplate (charset, viewport).

---

## Stage 4 — Render the React component

Pyxle now needs to render `<Home data={...}/>` to a string of HTML. This
happens in `ComponentRenderer.render()` (`ssr/renderer.py`).

In **worker pool mode** (the default since v0.1.7), the renderer sends a
JSON message to the next available SSR worker:

```json
{"id": "uuid-...", "componentPath": ".../index.jsx", "props": {"data": {...}}}
```

Inside the worker (a long-running Node.js process running `ssr_worker.mjs`),
each render does:

1. **Bundle the component with esbuild.** All imports get inlined into a
   single JS string. esbuild is *fast* — typically under 30ms per file
   even for non-trivial component trees. Cached across requests for the
   same file.
2. **Evaluate the bundle in a fresh context.** The default export is the
   `Home` function.
3. **Render with `react-dom/server.renderToString`.** This produces a
   plain HTML string for the component tree, including any `<Head>` JSX
   blocks (which the worker also returns separately so they can be
   hoisted into the document head).
4. **Reply with another newline-delimited JSON message** containing
   `{html, head_elements, inline_styles}`.

The worker pool is just round-robin over N workers, with automatic
respawn if a worker crashes. See [SSR § Worker pool mode](ssr.md#worker-pool-mode).

What comes back from `ComponentRenderer.render()` is a `RenderResult`
dataclass with the body HTML, any extracted head elements, and any
inline `<style>` blocks the component injected.

---

## Stage 5 — Assemble the document

`build_document_shell()` in `ssr/template.py` glues everything together
into a complete HTML document. It produces a **shell** — a `prefix` and
`suffix` of strings — so the body HTML can be slotted in for **streaming**.

Here's the rough structure of the shell:

```html
<!DOCTYPE html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />

    <!-- Dev only: Vite client + React Refresh preamble -->
    <script type="module" src="http://127.0.0.1:5173/@vite/client"></script>
    <script type="module">
      import RefreshRuntime from "http://127.0.0.1:5173/@react-refresh"
      RefreshRuntime.injectIntoGlobalHook(window)
      window.$RefreshReg$ = () => {}
      window.$RefreshSig$ = () => (type) => type
      window.__vite_plugin_react_preamble_installed__ = true
    </script>

    <!-- Merged head from all 4 sources -->
    <title>Pyxle App</title>
  </head>
  <body>
    <div id="root">
        <!-- ← body HTML gets streamed in here -->
    </div>
    <script id="__PYXLE_PROPS__" type="application/json">
      {"data": {"version": "0.1.7", "now": "08:53:58 UTC"}}
    </script>
    <script>window.__PYXLE_PAGE_PATH__ = "/pages/index.jsx";</script>
    <script type="module" src="http://127.0.0.1:5173/client-entry.js"></script>
  </body>
</html>
```

There are a few subtle things going on:

- **The props are inlined as JSON inside a `<script type="application/json">`
  tag.** When React hydrates on the client, it reads this script's text
  content, parses it, and uses it as the initial props. JSON inside a
  script tag is safe from XSS as long as `</` is escaped to `<\/` —
  which Pyxle does in `template.py`.
- **A CSP nonce is generated for every response.** It's a 24-byte
  URL-safe random token, attached to all `<script>` tags Pyxle emits.
  If you set a strict Content-Security-Policy header, the nonce flows
  through automatically.
- **The shell is split into prefix + suffix** so the response can be
  streamed: prefix → body HTML → suffix. The browser starts parsing the
  `<head>` before the loader's data has even been serialized.

---

## Stage 6 — Stream the response

Pyxle wraps everything in a `StreamingResponse` and sends it back to the
browser:

```python
async def _document_stream():
    yield shell.prefix.encode("utf-8")    # everything up to <div id="root">
    yield artifacts.body_html.encode("utf-8")  # the rendered React tree
    yield shell.suffix.encode("utf-8")    # hydration scripts + closing tags

return StreamingResponse(_document_stream(), status_code=200)
```

The browser sees the response start arriving immediately (the prefix is
already constructed when the loader hasn't even returned in some cases).
The `<head>` parses, stylesheets and scripts start downloading in
parallel, and the body fills in as the loader and renderer finish.

---

## Stage 7 — Hydration in the browser

The browser receives the HTML and starts parsing. Vite's client script
loads, then the React Refresh runtime, then `client-entry.js` (a bundled
version of `pyxle/client`'s hydration entry).

`client-entry.js` does:

1. Read `window.__PYXLE_PAGE_PATH__` to find which JS module to import.
2. Read the `<script id="__PYXLE_PROPS__">` JSON for initial props.
3. Dynamically import the page component (`/pages/index.jsx`).
4. Call `ReactDOM.hydrateRoot(document.getElementById("root"), <Page {...props} />)`.

React's hydration walks the existing server-rendered DOM, attaches event
listeners, and the page becomes interactive. This is the same hydration
flow as Next.js or Remix; Pyxle does nothing fancy here.

---

## Stage 8 — Client-side navigation

If the user clicks a `<Link href="/about">`, the client runtime
intercepts the click and asks Pyxle for the *next* page in JSON form
instead of HTML:

```
GET /about HTTP/1.1
x-pyxle-navigation: 1
```

Starlette sees the `x-pyxle-navigation` header and routes the request
to `build_page_navigation_response()` instead of `build_page_response()`.
That function:

1. Runs the new page's loader.
2. Resolves and merges its `HEAD` elements.
3. Returns JSON instead of HTML:

```json
{
  "ok": true,
  "routePath": "/about",
  "props": {"data": {…}},
  "headMarkup": "<title>About</title>"
}
```

The client runtime then:
1. Updates the document `<head>` with the new head markup.
2. Calls React's render with the new props and the new component.

No full page reload, no re-downloading the JS bundle. Same React tree,
new data and new component.

---

## What just happened?

Take a step back. In the time it took to read this doc, you've seen:

- The compiler process the source.
- The Starlette router dispatch the request.
- The page registry lookup.
- The loader run.
- The head pipeline merge from four sources.
- The Node.js worker pool render React on the server.
- The streaming HTML response stream out, with CSP nonces and JSON props
  inlined for hydration.
- The browser hydrate.
- Client-side navigation use a JSON endpoint instead of HTML.

That's the entire framework. Every other doc in this section is a closer
look at one of these stages.

Where to go next:

- Curious about **how Python and JSX get separated in a `.pyxl` file**?
  → [The .pyxl file format](pyxl-files.md), then [The parser](parser.md).

- Want to know **what the compiled `.py` and `.jsx` artifacts look like**?
  → [The compiler](compiler.md).

- Wondering **how dynamic routes like `[id].pyxl` work**?
  → [Routing](routing.md).

- Curious about **the SSR worker pool, head merging, and streaming**?
  → [Server-side rendering](ssr.md).

- Want to know **what changes when you `pyxle build` for production**?
  → [Build and serve](build-and-serve.md).

- Curious about **the dev server's file watcher and incremental rebuilds**?
  → [The dev server](dev-server.md).

Pick your favourite and keep going.
