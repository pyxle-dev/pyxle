# Streaming SSR

By default Pyxle renders a page to its complete HTML string and then sends it.
For a page with a slow part — a section that waits on a third-party API, a
heavy below-the-fold widget — the visitor stares at a blank screen until the
*whole* page is ready.

**Streaming SSR** sends the page in pieces. Pyxle flushes the fast part (the
"shell") immediately, so the browser can paint it, and streams the slow parts
in as they become ready. Time-to-first-byte drops to the time it takes to
render the shell instead of the whole page.

Streaming is built on React 18's `renderToPipeableStream` and is **opt-in**: a
page streams only when it uses a `<Suspense>` boundary. Every other page keeps
the buffered render, unchanged.

## Opting in with `<Suspense>`

Wrap the slow part of your page in a `<Suspense>` boundary and give it a
`fallback`. The fallback renders into the shell and streams immediately; the
boundary's real content streams in when it resolves.

```jsx
import React, { Suspense } from 'react';

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

`<Suspense>` works the way it does in any React 18 app: a child suspends by
throwing a promise (via `React.lazy`, a `use(promise)` call, or your own
promise-throwing data source), and React shows the `fallback` until it
resolves. Pyxle's `@server` loader still runs first and passes its result as
`data` props, exactly as for a buffered page — streaming governs how the
**rendered** page is delivered, not how the loader runs.

## Hydration

Nothing changes about hydration. The browser hydrates the same component it
always did; React 18 reconciles the streamed markup (including the parts that
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
  pool's multi-frame transport. `pyxle serve` runs the pool by default.

A streamed response is always `Cache-Control: private, no-cache` — it is a
per-request render and is never shared between visitors.

## The head is static while streaming

Because the document `<head>` is flushed *before* the component renders (that's
what makes the shell fast), only the **static** head is available to a streamed
page: the `HEAD` variable (including a `HEAD` callable evaluated from loader
data) and `<Head>` blocks declared in your JSX or layout.

A `<Head>` element registered *during* render — i.e. returned from a component
as it renders — arrives too late to reach the already-flushed head and is
omitted from a streamed page. Put meta tags a streamed page needs in the `HEAD`
variable, not in a runtime-rendered `<Head>`. This only affects pages that opt
into streaming; buffered pages merge runtime `<Head>` exactly as before.

## Errors

If the render fails before the first byte is sent — an error while producing
the shell — Pyxle does **not** emit a half-written document. It falls back to
the nearest [`error.pyxl`](error-handling.md) boundary (or the sanitized error
page), exactly like a buffered render. Once the shell has flushed, an error
inside a `<Suspense>` boundary is handled by React: it streams the boundary's
fallback and recovers on the client.
