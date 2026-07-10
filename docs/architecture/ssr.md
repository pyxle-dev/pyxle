# Server-side rendering

Server-side rendering (SSR) is the process of turning a `PageRoute` and
an HTTP request into an HTML response. It's the part of the framework
that runs on every page load — so it has to be fast, robust, and
predictable. It's also the part that touches both Python (the loader)
*and* Node.js (the React renderer), so a fair bit of the complexity
lives in the bridge between the two languages.

This doc walks through the SSR pipeline stage by stage. By the end
you'll know exactly what happens between "Starlette receives a GET" and
"the browser hydrates", including the worker pool protocol, the head
merge algorithm, and the streaming response strategy.

**Files (`pyxle/ssr/`):**

| File | Lines | What it does |
|---|---|---|
| `view.py` | 720 | Orchestrates loader, render, head, document assembly |
| `renderer.py` | 310 | Wraps Node.js component rendering (subprocess or worker pool) |
| `worker_pool.py` | 330 | Persistent SSR workers, NDJSON over stdin/stdout |
| `head_merger.py` | 420 | Merges head elements from 4 sources, deduplicates, sanitizes |
| `template.py` | 455 | HTML document assembly (dev + production modes) |
| `__init__.py` | 50 | Re-exports the public surface |

The interesting code lives in `view.py`, `renderer.py`, and
`head_merger.py`. The rest is supporting infrastructure.

---

## The pipeline at a glance

```
HTTP request
    │
    ▼
1. Page handler (Starlette closure)
    │
    ▼
2. build_page_response()  ← view.py
    │
    ├──▶ 3. Run @server loader
    │       (import compiled module, call function, await result)
    │
    ├──▶ 4. Resolve HEAD
    │       (HEAD variable, layout JSX, page JSX, runtime registrations)
    │       → merge_head_elements() in head_merger.py
    │
    ├──▶ 5. Render React component
    │       → ComponentRenderer.render() in renderer.py
    │       → SsrWorkerPool.render() (or per-request subprocess)
    │       → Node.js worker bundles with esbuild + React renderToString
    │
    ├──▶ 6. Build the document shell
    │       → build_document_shell() in template.py
    │       → prefix + suffix HTML, props serialized as JSON
    │
    └──▶ 7. Stream response
            → StreamingResponse(prefix → body → suffix)
                │
                ▼
              Browser
```

Source: `ssr/view.py:51-202`.

If anything goes wrong at any stage, error handling kicks in:

- A `LoaderError` triggers the nearest `error.pyxl` boundary.
- A `ComponentRenderError` (the React component crashes during SSR)
  also tries the error boundary.
- An unexpected exception falls through to `render_error_document()`,
  which renders the developer overlay (in dev) or a generic
  `Server Error` page (in production).

---

## Stage 1: importing the compiled server module

The page handler closure inside `build_page_router()` knows the
`PageRoute` for the current URL. The first thing it does is import
the **compiled Python module** at `PageRoute.server_module_path`:

```python
spec = importlib.util.spec_from_file_location(
    page.module_key, page.server_module_path,
)
module = importlib.util.module_from_spec(spec)
sys.modules[page.module_key] = module
spec.loader.exec_module(module)
```

In **dev mode**, before importing, Pyxle purges any cached version of
this module from `sys.modules`:

```python
if settings.debug:
    _purge_page_modules(settings.pages_dir)
```

This is the hot-reload mechanism. When you save a `.pyxl` file:

1. The watcher rebuilds the artifacts.
2. The `.py` file on disk has new content.
3. The next request purges the old cached module.
4. Python's import machinery reads the new file from disk.
5. The new code runs.

In **production mode** (`pyxle serve`), modules are imported once at
startup and never re-imported. This avoids the per-request import
overhead and is consistent with the immutable nature of a deployed
build.

Source: `ssr/view.py:614-660`.

---

## Stage 2: running the loader

Once the module is imported, Pyxle finds the function tagged with
`__pyxle_loader__ = True` (set by the `@server` decorator) and calls
it:

```python
loader_fn = getattr(module, page.loader_name)
data = await loader_fn(request)
```

Loaders are *always* async — the parser refuses to compile a sync
`@server def`. They take exactly one positional argument named
`request` (a Starlette `Request` object). They return a JSON-
serializable dict.

A few invariants hold:

- **The loader name comes from the metadata.** Pyxle doesn't `getattr`
  for "the function with `@server`" at runtime — it looked up the
  name once at compile time and stored it in
  `PageRoute.loader_name`. This means runtime startup doesn't have
  to walk the module looking for decorated functions.
- **The loader is awaited directly.** Pyxle does not wrap it in
  `asyncio.shield`, doesn't add timeouts, doesn't catch exceptions
  outside of the documented error types. The loader is your code,
  and it runs as your code.
- **The result must be JSON-serializable.** If you return a `set`, a
  `datetime`, or a custom class, the next stage (`json.dumps`) will
  raise `TypeError`. Pyxle catches that and routes it through the
  same error pipeline as any other render failure.

If the loader raises `LoaderError`, Pyxle catches it and looks for the
nearest `error.pyxl` boundary up the directory tree. If found, it
renders the error page with the error context as a prop. If not, it
renders the default error document.

If the loader raises any other exception, Pyxle treats it the same way
but logs it as an unexpected failure and includes the stack trace in
dev mode.

Source: `ssr/view.py:341-485`.

---

## Stage 3: resolving the head

The `<head>` of the HTML response is assembled from up to **four**
sources, in order of increasing priority:

1. **Layout `<Head>` JSX blocks** — collected at registry-build time
   from `layout.pyxl` (and `template.pyxl`) ancestors of the current
   page.
2. **The page's `HEAD` variable** — Python string, list of strings,
   or callable returning either. If it's a callable, Pyxle invokes
   it with the loader's data.
3. **The page's `<Head>` JSX blocks** — extracted from the page's
   JSX section by the parser at compile time.
4. **Runtime registrations** — rare; used by some advanced helpers
   that register head elements during SSR.

Each source contributes a list of HTML strings (e.g.
`['<title>About</title>', '<meta name="description" content="..." />']`).
The `merge_head_elements()` function (`ssr/head_merger.py:252`) merges
them with deduplication and sanitization.

### Deduplication

The merge identifies "the same" element by its **deduplication key**,
which depends on the tag:

| Tag | Dedupe key |
|---|---|
| `<title>` | tag name (so only one `<title>` survives) |
| `<meta charset>` | "charset" (singleton) |
| `<meta name="X">` | `name="X"` |
| `<meta property="X">` | `property="X"` |
| `<meta http-equiv="X">` | `http-equiv="X"` |
| `<link rel="canonical">` | "canonical" (singleton) |
| `<link rel="X" href="Y">` | `rel="X" + href="Y"` |
| `<script src="X">` | `src="X"` |
| `data-head-key="X"` | `X` (manual key) |
| Anything else | no key — always included |

If two elements share a key, the **higher-priority** one wins. So a
`<title>` in the page's JSX overrides a `<title>` in the layout's
JSX. A `<meta name="description">` in the page's `HEAD` variable
overrides one in the layout.

Within the same priority tier, *deeper nesting wins* (page over
parent layout, child layout over root layout). This matches the
behaviour of React Helmet and other head libraries — your innermost
layer is the most specific, so it should win.

Source: `ssr/head_merger.py:123-280`.

### Sanitization

Every element passes through `sanitize_head_element()`
(`head_merger.py:327`), which parses the element and rebuilds it
through a strict allowlist:

1. **Enforces a tag allowlist.** Only `<title>`, `<meta>`, `<link>`,
   `<script>`, and `<style>` may appear in the head; anything else
   (`<base>`, `<iframe>`, `<object>`, ...) is dropped entirely.
2. **HTML-escapes every attribute value.** Elements are rebuilt from
   their parsed attributes with values escaped, so a quote inside
   interpolated data can't break out of its attribute and inject
   markup. Trailing markup injected after a breakout is discarded.
3. **Escapes `<` and `>` inside `<title>...</title>`.** The HTML spec
   says title content shouldn't contain raw HTML, but if a user
   ships dynamic title content from a loader, an attacker could try
   to inject a closing `</title>` tag and break out of the title
   element. Pyxle escapes both characters defensively and discards
   anything after the closing `</title>`.
4. **Strips event handler attributes.** Any attribute starting with
   `on` (`onclick`, `onerror`, `onload`, etc.) is removed. Head
   elements don't run JavaScript, so these are pure XSS vectors.
5. **Neutralizes `javascript:`, `vbscript:`, and `data:` URLs in
   `href`, `src`, and `action` attributes.** A `<link
   rel="stylesheet" href="javascript:alert(1)">` would actually
   execute that script in some browsers; Pyxle replaces the URL with
   an empty string.
6. **Rejects `<meta http-equiv="refresh">`** — a data-controlled
   refresh target is an open-redirect vector.

Void tags (`<meta>`, `<link>`) are re-serialized self-closing
(`<meta ... />`), so output markup may differ cosmetically from the
input. The sanitization is **always on** — there's no opt-out.
Inline `<script>`/`<style>` content is the one deliberate
pass-through: it is treated as trusted author code and preserved
verbatim, so it must never contain interpolated user data.

---

## Stage 4: rendering the React component

This is the most complex part of the SSR pipeline. We need to take a
React component (which is JavaScript) and produce an HTML string from
it (also JavaScript), all from inside Python.

The mechanism is `ComponentRenderer` (`ssr/renderer.py:65`), which
delegates to either:

- **Per-request subprocess mode** (`--ssr-workers 0`) — spawn a fresh
  Node.js subprocess for every render
- **Worker pool mode** (default, `--ssr-workers >= 1`) — keep N
  persistent Node.js workers alive

Each mode has its own Node.js entry script: subprocess mode runs
`render_component.mjs` (one render per process), while the worker pool runs
the long-lived `ssr_worker.mjs` (a persistent render loop). Both share the
same esbuild + React rendering core.

### The render protocol

A render request is a JSON message:

```json
{
  "id": "uuid-...",
  "componentPath": "/abs/path/to/index.jsx",
  "props": {"data": {"version": "0.1.7"}}
}
```

The Node.js worker:

1. **Bundles the component with esbuild.** Imports get inlined into
   a single JavaScript string. esbuild is fast — typically under
   30ms even for non-trivial component trees. The bundle is cached
   per `componentPath` so repeat requests for the same page reuse
   the bundle.
2. **Evaluates the bundle in a fresh context.** The default export is
   the React component function.
3. **Calls `react-dom/server.renderToString`.** This produces a
   plain HTML string from the component tree. (A request flagged
   `stream: true` instead drives `renderToPipeableStream` and replies
   with a sequence of `chunk`/`end`/`error` frames sharing the request
   id — see [Streaming SSR](../guides/streaming.md). The buffered
   `renderToString` path described here remains the default for every
   non-streaming, cacheable, and statically generated render.)
4. **Collects head elements emitted by `<Head>` blocks.** When the
   component renders a `<Head>...</Head>` block, the runtime
   captures the children and returns them separately (so they can
   be hoisted into the document head).
5. **Replies with another JSON message.**

The reply:

```json
{
  "id": "uuid-...",
  "html": "<main>...</main>",
  "headElements": ["<title>About</title>"],
  "inlineStyles": [{"identifier": "...", "contents": "..."}]
}
```

The `id` field lets the worker pool match responses to in-flight
requests, since multiple requests may be in flight simultaneously
against a single worker.

### Subprocess mode

In subprocess mode, every render does:

```
1. Spawn `node render_component.mjs <component-path>` as a child process
2. Write the JSON request to its stdin
3. Read the JSON response from its stdout
4. Wait for the process to exit
```

Total cost: ~200-400ms per render, dominated by Node.js startup
(loading the V8 runtime, the React module graph, esbuild's
internals). This is **fine for one-off scripts** but unacceptable
for a dev server that re-renders on every navigation.

Subprocess mode exists as a fallback for environments where keeping
persistent processes alive is awkward — for example, some CI
pipelines or constrained sandboxes.

### Worker pool mode

`SsrWorkerPool` (`ssr/worker_pool.py:134`) keeps **N** Node.js
processes alive for the lifetime of the dev server. Each worker is
a long-running script that reads NDJSON requests from stdin and
writes NDJSON responses to stdout.

```
Pool startup:
  spawn N workers
  start a background reader task per worker

Render request:
  pick the next worker (round-robin)
  generate a request id
  register a future for that id
  send the request as one JSON line on stdin
  await the future

Background reader (per worker):
  read raw bytes from stdout
  split into newline-delimited messages
  for each message:
    parse JSON
    look up the future for the message's id
    set the result
```

Costs per render in worker pool mode: ~30-80ms (mostly esbuild
bundling). The Node.js startup is amortized across all renders the
worker handles in its lifetime.

Worker pool size:
- **Default: 1 worker.** Sufficient for solo development.
- **Recommended for production: number of CPU cores.** This lets
  the SSR pipeline saturate the CPU under load.
- **`--ssr-workers 0`** falls back to subprocess mode.

### Why NDJSON?

The newline-delimited JSON protocol has three nice properties:

1. **It's pipe-friendly.** stdin/stdout are streams; newline framing
   is the simplest way to split a stream into messages.
2. **It's debuggable.** You can run a worker by hand and pipe JSON
   lines into it to test it without involving Pyxle.
3. **It interleaves cleanly.** Multiple in-flight requests can share
   one worker because each response carries an `id` matching its
   request.

The downside is that messages have to fit in memory before sending —
no streaming a 100MB JSON blob through one message. In practice, SSR
messages are tiny (a few kilobytes) so this isn't a constraint.

### Worker crash recovery

If a worker process crashes mid-request, the pool:

1. Notices the EOF on stdout.
2. Marks the worker as dead.
3. Rejects all in-flight futures for that worker with a clear error.
4. Spawns a replacement worker to keep the pool size constant.

The replacement is silent — no log spam — unless replacements happen
in rapid succession (which would indicate a deeper problem).

Source: `ssr/worker_pool.py:134-330`.

### Why one renderer per request and a pool of workers, not the other way around?

Because rendering is **CPU-bound** (esbuild + React's
`renderToString`), not IO-bound. A single Python process can serve
many concurrent IO-bound requests using async/await, but it can't
parallelize CPU work without releasing the GIL. Offloading the CPU
work to N Node.js processes lets Pyxle saturate the CPU on
multi-core machines without the Python side getting in the way.

The Python side stays single-process and async; the Node.js side is
the parallelism boundary.

---

## Stage 5: assembling the document

`build_document_shell()` (`ssr/template.py:67`) takes the rendered
body HTML, the merged head, and the loader's data, and produces a
**shell** — a `prefix` and a `suffix` of HTML strings — designed for
streaming.

The shell looks like this (slightly abbreviated):

```html
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <!-- DEV ONLY: Vite client + React Refresh preamble -->
  <script type="module" src="http://127.0.0.1:5173/@vite/client" nonce="abc"></script>
  <script type="module" nonce="abc">
    import RefreshRuntime from "http://127.0.0.1:5173/@react-refresh"
    RefreshRuntime.injectIntoGlobalHook(window)
    window.$RefreshReg$ = () => {}
    window.$RefreshSig$ = () => (type) => type
    window.__vite_plugin_react_preamble_installed__ = true
  </script>
  <!-- Global stylesheets, inlined -->
  <style data-pyxle-style="...">...</style>
  <!-- Merged head elements from all 4 sources -->
  <title>My Page</title>
</head>
<body>
  <div id="root">                          ← prefix ends here
                                            (body HTML streams in here)
  </div>                                   ← suffix starts here
  <script id="__PYXLE_PROPS__" type="application/json">
    {"data": {"version": "0.1.7"}}
  </script>
  <script nonce="abc">window.__PYXLE_PAGE_PATH__ = "/pages/index.jsx";</script>
  <script type="module" src="http://127.0.0.1:5173/client-entry.js" nonce="abc"></script>
</body>
</html>
```

A few things worth noting:

- **The CSP nonce is generated per request.** It's a 24-byte
  URL-safe random token (`secrets.token_urlsafe(24)`), attached to
  every `<script>` and `<style>` Pyxle emits. If you set a strict
  CSP header, the nonce flows through automatically.
- **The props are inlined as JSON.** When React hydrates on the
  client, it reads `<script id="__PYXLE_PROPS__">.textContent`,
  parses it, and uses it as the initial props. This is faster than
  refetching the data on hydration and matches the HTML the user
  saw on first paint.
- **JSON is escaped to be safe inside `<script>`.** Specifically,
  `</` is escaped to `<\/` so a property value like `"<\/script>"`
  can't break out of the script tag and inject HTML. Source:
  `template.py:181`.
- **The `<div id="root">` is empty in the prefix.** The body HTML
  goes between the prefix and the suffix when the response is
  streamed.

### Dev mode vs production mode

The shell looks slightly different in production:

- **No Vite client tag.** Vite isn't running.
- **No React Refresh preamble.** Hot reload doesn't apply in prod.
- **Hashed asset paths from the manifest.** Instead of
  `http://127.0.0.1:5173/client-entry.js`, the production shell
  uses the entries from `dist/client/manifest.json`:
  `<link rel="stylesheet" href="/client/dist/assets/index-abc123.css" />`
  and `<script type="module" src="/client/dist/assets/index-def456.js" />`.
- **Optional preload hints** for the page's specific JS chunks.

The branching happens based on `settings.debug`. Source:
`template.py:88-200`.

---

## Stage 6: streaming the response

Once the shell and the body HTML are ready, Pyxle wraps them in a
`StreamingResponse`:

```python
async def _document_stream():
    yield shell.prefix.encode("utf-8")
    yield artifacts.body_html.encode("utf-8")
    yield shell.suffix.encode("utf-8")

return StreamingResponse(
    _document_stream(),
    status_code=artifacts.status_code,
    media_type="text/html; charset=utf-8",
)
```

The browser starts receiving bytes immediately. Practical effect:

- The browser parses the `<head>` and starts downloading the
  stylesheet and JS bundle in parallel **before** the body HTML has
  fully arrived.
- Time-to-first-byte is dominated by the loader and the renderer,
  but time-to-first-paint is much faster than a non-streaming
  response would be.

### What about React's streaming SSR?

React has its own streaming SSR via `renderToPipeableStream`,
which can interleave Suspense boundaries with the document. Pyxle
*does not* use that yet — the current renderer uses
`renderToString`, which is synchronous on the React side.

The reason is pragmatism: `renderToString` is simpler, doesn't
require coordinating async boundaries between Python and Node.js,
and produces output that's compatible with React's hydration in
both v18 and the upcoming v19. Adding `renderToPipeableStream` would
be a future improvement; the streaming we *do* do (prefix → body →
suffix) is enough to get the round-trip win for the common case.

---

## Stage 7: client-side navigation

When a user clicks a `<Link href="/about">`, the client runtime
intercepts the click and asks Pyxle for the **next page in JSON
form** instead of HTML:

```
GET /about
x-pyxle-navigation: 1
```

The Starlette page handler sees the header and routes to
`build_page_navigation_response()` (`ssr/view.py:204`) instead of
`build_page_response()`. The nav response:

1. Runs the new page's loader.
2. Resolves and merges the new page's HEAD elements.
3. Returns JSON:

```json
{
  "ok": true,
  "routePath": "/about",
  "props": {"data": {"page": "About", "subtitle": "Hello"}},
  "headMarkup": "<title>About — MyApp</title><meta name=\"description\" content=\"...\"/>",
  "scriptDeclarations": [],
  "imageDeclarations": []
}
```

The client runtime then:

1. Updates the document `<head>` by replacing the previous
   `headMarkup` with the new one (using small DOM patches, not a
   full innerHTML swap, to avoid flicker on `<link>` elements).
2. Imports the new page component dynamically (it's already in the
   bundle if Vite has prefetched it; otherwise the import triggers
   a fetch).
3. Calls React's render with the new component and props.

No full page reload, no JS bundle re-download, no CSS reflash. The
React tree updates in place.

---

## Error handling

Errors at each stage are routed to a specific handler:

| Stage | Exception | What happens |
|---|---|---|
| Loader | `LoaderError` | Render nearest `error.pyxl` with error context |
| Loader | Other exception | Render nearest `error.pyxl` with generic error context |
| HEAD | `HeadEvaluationError` | Render error boundary or default error doc |
| Render | `ComponentRenderError` | Render error boundary or default error doc |
| Anything | Other exception | Render `error_document` (dev) / generic 500 (prod) |

Every error path also notifies the WebSocket overlay (in dev mode)
with a structured event including the route, the error, and a
breadcrumb list describing which stage failed:

```json
{
  "type": "error",
  "route": "/dashboard",
  "error": {"type": "RuntimeError", "message": "..."},
  "breadcrumbs": [
    {"label": "Loader", "status": "failed", "detail": "..."},
    {"label": "Renderer", "status": "blocked"},
    {"label": "Hydration", "status": "blocked"}
  ]
}
```

The browser overlay parses this and renders an inline error UI with
the breadcrumbs as a visual stack trace.

In **production** (`pyxle serve`), error responses are intentionally
opaque: a generic `<h1>Server Error</h1>` page with no exception
type, no message, no route path, no Vite tag. The actual error
details are written to the server logs by the request handler. This
is a security boundary — production responses must not leak internal
state. Exception messages can contain database row IDs, internal
URLs, or file paths; a generic error page is the only safe default.

Source: `ssr/template.py:250-360` (the dev/prod branching in
`render_error_document`).

---

## Performance characteristics

For a typical page on a 2025-era laptop:

| Operation | Cost |
|---|---|
| Loader execution | Whatever your loader does (1-100ms typical) |
| Head merging | <1ms for typical pages (5-15 elements) |
| Worker pool render | 30-80ms (esbuild + React renderToString) |
| Subprocess render | 200-400ms (mostly Node.js startup) |
| Document assembly | <1ms |
| Streaming TTFB | Loader + render time |

The worker pool render time dominates the request lifecycle. To
optimize:

- **Use the worker pool**, not subprocess mode. (Default since
  v0.1.7.)
- **Set worker count to your CPU core count** for production.
  `pyxle dev --ssr-workers 4` on a quad-core gives you four-way
  rendering parallelism.
- **Cache the rendered page** if your data changes infrequently.
  Pyxle ships a built-in server-side page cache — return a
  `{"data": ..., "revalidate": N}` envelope from your loader (or a
  module-level `CACHE` directive on a loader-less page) to serve the
  stored HTML without re-running the loader or the Node render. A CDN
  or a Redis cache backend can sit in front for multi-host
  deployments. See the [Caching guide](../guides/caching.md).
- **Don't import heavy modules at module top level.** Each
  hot-reload re-imports the module; deferring expensive imports to
  inside the loader function avoids paying the cost on every save.

---

## Public API

The SSR module exports a small surface from `ssr/__init__.py`:

```python
from .renderer import ComponentRenderer
from .view import build_page_response, build_page_navigation_response
from .view import build_not_found_response
```

Most code that wants to render a page calls `build_page_response()`.
The dev server's page handler is a closure that does:

```python
async def page_handler(request):
    return await build_page_response(
        request=request,
        settings=settings,
        page=page_route,
        renderer=component_renderer,
        overlay=overlay_manager,
        error_boundaries=error_boundary_registry,
    )
```

Everything else flows from the `PageRoute` (which the dev server
built from compiled metadata) and the `request` (which Starlette
handed in).

---

## Where to read next

- **[Build and serve](build-and-serve.md)** — How `pyxle build`
  produces the same compiled artifacts that the dev server uses,
  but ahead of time, and how `pyxle serve` runs them in production
  without Vite.

- **[The runtime](runtime.md)** — The contract behind `@server`
  loaders and `@action` mutations, and why they intentionally have
  no runtime wrapping.

- **[The dev server](dev-server.md)** — How the SSR pipeline is
  wired into the Starlette app, including the file watcher that
  triggers module-cache invalidation when you save.
