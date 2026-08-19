# Streaming SSR

By default Pyxle renders a page to its complete HTML string and then sends it.
For a page with a slow part — a section that waits on a third-party API, a
heavy below-the-fold widget — the visitor stares at a blank screen until the
*whole* page is ready.

**Streaming SSR** sends the page in pieces. Pyxle flushes the fast part (the
"shell") immediately, so the browser can paint it, and streams the slow parts
in as they become ready. Time-to-first-byte drops to the time it takes to
render the shell instead of the whole page.

Streaming is built on React's `renderToPipeableStream` and is **opt-in**: a
page streams only when it uses a `<Suspense>` boundary. Every other page keeps
the buffered render, unchanged.

## Opting in with `<Suspense>`

Wrap the slow part of your page in a `<Suspense>` boundary and give it a
`fallback`. The fallback renders into the shell and streams immediately; the
boundary's real content streams in when it resolves.

```jsx
import React, { Suspense } from 'react';

// React.lazy makes ActivityFeed suspend until its module loads, so the
// boundary actually defers and streams in. A plain synchronous component
// inside <Suspense> never suspends — it renders straight into the shell.
const ActivityFeed = React.lazy(() => import('./ActivityFeed.jsx'));

export default function Dashboard({ data }) {
  return (
    <main>
      <h1>{data.title}</h1>          {/* shell — flushed immediately */}

      <Suspense fallback={<p>Loading activity…</p>}>
        <ActivityFeed />            {/* streams in when it resolves */}
      </Suspense>
    </main>
  );
}
```

The compiler detects the `<Suspense>` at build time and marks the page as
streamable — there's no configuration and no flag to set. A page with no
`<Suspense>` is never affected.

> **Import `Suspense` under that exact name.** The compiler keys streaming off
> the literal element name `Suspense` (or `React.Suspense`). A page that aliases
> the import — `import { Suspense as Boundary } from 'react'` and writes
> `<Boundary>` — compiles fine but is **not** marked streamable and renders
> buffered, with no warning.

`<Suspense>` works the way it does in any React app: a child suspends by
throwing a promise (via `React.lazy` or your own promise-throwing data source),
and React shows the `fallback` until it resolves. Pyxle's `@server` loader still
runs first and passes its result as `data` props, exactly as for a buffered page
— streaming governs how the **rendered** page is delivered, not how the loader
runs.

> Pyxle ships **React 19**, so the `use(promise)` hook is available if you prefer
> it. `React.lazy` remains the zero-effort way to make a subtree suspend and
> stream.

## Route-level loading states with `loading.pyxl`

A `loading.pyxl` file declares a **route-level** loading state — the React
component it exports becomes the fallback for the whole route, the way Next.js's
`loading.js` does:

```
pages/
├── loading.pyxl              # fallback for every route
└── dashboard/
    ├── loading.pyxl          # fallback for /dashboard and everything under it
    └── index.pyxl
```

```jsx
// pages/dashboard/loading.pyxl
import React from 'react';

export default function DashboardLoading() {
  return <p>Loading the dashboard…</p>;
}
```

Pyxle wraps the page in `<Suspense fallback={<DashboardLoading/>}>` — on the
server (so the loading state streams as the shell) **and** on the client (so
hydration matches). The **nearest** `loading.pyxl` wins, walking up the
directory tree exactly like [`error.pyxl`](error-handling.md). It composes with
a page's own inner `<Suspense>`: an inner boundary handles its own subtree, and
anything it doesn't catch bubbles up to the `loading.pyxl` shell. A
`loading.pyxl` is compiled but never routable on its own.

> **A `loading.pyxl` only shows if the render actually suspends.** Because the
> `@server` loader runs *before* the component renders, a page whose data comes
> entirely from the loader has its props ready by render time and **never
> suspends** — so the fallback never appears, and the full page streams at once.
> The loading state shows only when the render itself suspends: a child using
> `React.lazy` or a thrown promise. This is the honest
> consequence of Pyxle's loader-first model; if you want a loading state for slow
> *loader* data, that data isn't what `loading.pyxl` defers.

A `loading.pyxl` should not declare a `<Head>` — like any streamed fallback it
renders before the head is finalized, and on a client-side navigation the head
is updated before the fallback renders, so a fallback's `<Head>` is ignored.

## Hydration

Nothing changes about hydration. The browser hydrates the same component it
always did; React reconciles the streamed markup (including the parts that
arrived after the shell) natively. A streaming page is hydrated exactly like a
buffered one.

Hydration is **selective**: React hydrates the shell (and makes its interactive
parts live) without waiting for a `<Suspense>` boundary to finish — an
interactive control above the boundary responds to input while the boundary is
still resolving, and each boundary hydrates as its content arrives.

## Faster time-to-first-byte

The point of streaming is that the browser receives — and can paint — the shell
before the slow boundary is ready. For a page whose boundary takes, say, 600 ms
to resolve, the shell's first byte arrives in tens of milliseconds rather than
after the full ~600 ms a buffered render would wait. The slower the boundary,
the bigger the win; a page with no slow boundary gains nothing, which is exactly
why streaming is opt-in.

### Concurrent streams don't queue

Overlapping requests to a streaming page render **concurrently**, even against a
single SSR worker. A streaming render spends almost all of its wall-clock time
idle — awaiting your `@server` loader and each `<Suspense>` boundary — and the
worker interleaves those idle windows across requests. Four visitors hitting the
same slow streaming page all get their shell's first byte in tens of
milliseconds; none waits for the others' boundaries to resolve first.

Each render is fully isolated: its pathname, CSRF token, `<Head>` tags, and
styles are scoped to that request, so interleaving never bleeds one visitor's
page into another's. Adding SSR **processes** (`--ssr-workers`, auto-sized by
default under `pyxle serve`) adds CPU-parallel throughput on top; the in-worker
concurrency handles the idle-wait overlap. See
[Deployment → SSR workers](deployment.md#ssr-workers).

## When a page does **not** stream

Streaming is deliberately narrow. A page falls back to the buffered render when:

- **It has no `<Suspense>` boundary.** There's nothing to defer, so there's no
  shell to flush early.
- **It is publicly cacheable** — a loader `{"data", "revalidate"}` envelope, a
  `CACHE` directive, or an edge `cache` config entry (see [Caching](caching.md)).
  A cacheable render has to be materialised in full so it can be stored and
  given an `ETag`; streaming it would buy nothing, so cacheable routes always
  render buffered. Streaming helps the dynamic, per-request pages that *can't*
  be cached.
- **The server is running without an SSR worker pool.** Streaming needs the
  pool's multi-frame transport. `pyxle serve` auto-sizes the pool by default and
  `pyxle dev` runs a single worker, so streaming works out of the box in both.
  The only way to lose it is `pyxle dev --ssr-workers 0`, which selects the
  per-request subprocess renderer (no pool, no streaming). Under `pyxle serve`,
  `--ssr-workers 0` means *auto-size* (one worker per CPU, capped at 4), not
  subprocess mode — so it keeps streaming.

A streamed response is always `Cache-Control: private, no-cache` — it is a
per-request render and is never shared between visitors.

### Streaming survives gzip in production

Production builds enable gzip compression. Pyxle uses a streaming-aware gzip
middleware that flushes the compressor after **every** chunk, so the shell's
compressed bytes reach the browser immediately instead of being held back until
the whole response finishes. Streaming SSR therefore works shell-first behind
gzip under `pyxle serve` — no configuration needed, and no need to disable
compression to keep the streaming benefit.

## The head is static while streaming

Because the document `<head>` is flushed *before* the component renders (that's
what makes the shell fast), only the **static** head is available to a streamed
page: the `HEAD` variable (including a `HEAD` callable evaluated from loader
data), an ancestor layout's own `HEAD` variable, and `<Head>` blocks declared in
your JSX or layout.

A `<Head>` element registered *during* render — i.e. returned from a component
as it renders — arrives too late to reach the already-flushed head and is
omitted from a streamed page. Put meta tags a streamed page needs in the `HEAD`
variable, not in a runtime-rendered `<Head>`. This only affects pages that opt
into streaming; buffered pages merge runtime `<Head>` exactly as before.

### Static means *literal*, not just "declared in JSX"

`<Head>` blocks are read from your source at compile time, before any of it has
run, so an element that interpolates anything is not static — there is no
render to fill it in, and the braces are not markup. Those elements are
[dropped rather than emitted](head-management.md#expressions-are-evaluated-by-the-render),
because the alternative is a literal `{data.title}` in the browser tab and a
`href="{faviconUrl}"` the browser requests as a relative URL:

```jsx
<Head>
  <title>{data.title}</title>                       {/* dropped on a streamed page */}
  <meta name="description" content={data.excerpt} />{/* dropped on a streamed page */}
  <title>Acme Status</title>                        {/* kept — no expression */}
</Head>
```

So on a streamed page, a `<Head>` element that interpolates loader data
contributes **nothing**, and the page falls back to whatever a layout supplies —
or, failing that, to the [default title](head-management.md#default-title),
your app's name.
This is the same limitation as above wearing a different hat: anything a
streamed page's head genuinely needs from loader data belongs in a `HEAD`
callable, which runs on the server before the flush.

```python
def HEAD(data):
    return [
        f'<title>{data["title"]}</title>',
        f'<meta name="description" content="{data["excerpt"]}" />',
    ]
```

## Custom middleware and streaming

A custom middleware that subclasses Starlette's `BaseHTTPMiddleware` **buffers
the whole response** before passing it on, which is fundamentally incompatible
with a streamed render. When a `<Suspense>` boundary genuinely defers, the page
becomes a chunked `StreamingResponse`, and a `BaseHTTPMiddleware` in the stack
raises `RuntimeError: No response returned.` (This can be intermittent in dev —
a warm chunk cache resolves the boundary in a single flush — but it fails every
time a boundary defers on real async data, e.g. in production.)

If you run streaming routes, write custom middleware as **pure ASGI** instead of
subclassing `BaseHTTPMiddleware`. A pure-ASGI middleware passes streamed and
buffered responses through identically. See
[Middleware → Streaming-safe middleware](middleware.md) for the pattern.

## Errors

If the render fails before the first byte is sent — an error while producing
the shell — Pyxle does **not** emit a half-written document. It falls back to
the nearest [`error.pyxl`](error-handling.md) boundary (or the sanitized error
page), exactly like a buffered render. Once the shell has flushed, an error
inside a `<Suspense>` boundary is handled by React: it streams the boundary's
fallback and recovers on the client.
