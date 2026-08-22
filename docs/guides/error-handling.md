# Error Handling

Pyxle provides structured error handling through error exceptions, error boundaries, and not-found pages.

## LoaderError

Raise `LoaderError` from a `@server` function to trigger the nearest error boundary:

```python
from pyxle.runtime import LoaderError

@server
async def load_user(request):
    user = await db.get_user(request.path_params["id"])
    if user is None:
        raise LoaderError("User not found", status_code=404)
    return {"user": user}
```

### LoaderError parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `message` | `str` | (required) | Error message shown in the error boundary |
| `status_code` | `int` | `500` | HTTP status code for the response |
| `data` | `dict` | `{}` | Additional context passed to the error boundary |

## ActionError

Raise `ActionError` from an `@action` function to return a structured error to the client:

```python
from pyxle.runtime import ActionError

@action
async def update_profile(request):
    body = await request.json()
    if len(body.get("name", "")) < 2:
        raise ActionError("Name must be at least 2 characters", status_code=400)
    # ...
    return {"updated": True}
```

The client receives `{ "ok": false, "error": "Name must be at least 2 characters" }`.

### ActionError parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `message` | `str` | (required) | Error message sent to the client |
| `status_code` | `int` | `400` | HTTP status code |
| `data` | `dict` | `{}` | Additional data in the error response |

### When an action raises something else

An `ActionError` is your answer to the caller. Any *other* exception — a
`KeyError`, a driver error, a bug — is not an answer, and its message may carry
a file path, a row ID or a connection string. So it is split the same way a
page's is:

- **In production**, the caller receives `{"ok": false, "error": "An
  unexpected error occurred."}` with status `500`. The real exception never
  reaches the browser. It **is** written to the server log, once, with its
  full traceback and the name of the action that raised it — that log is your
  only record, so make sure you collect it.
- **In development**, the response carries the real message and the traceback
  is logged the same way, so the terminal and the caller agree.

Actions do not render `error.pyxl` (see [below](#what-reaches-errorpyxl)) — an
action answers with JSON, not a page. Handle the failure at the call site,
where `useAction` exposes it as `error`.

The log line is written for every `500` an action can produce, not just a raised
exception: a module that fails to import, an unset `request.state` attribute, and
an action that returns something other than a dict are all recorded before the
response is sent.

## Error boundaries (`error.pyxl`)

Create an `error.pyxl` file to catch errors from pages in the same directory and below:

```
pages/
  error.pyxl          # Catches errors from all pages
  index.pyxl
  dashboard/
    error.pyxl        # Catches errors from dashboard pages only
    index.pyxl
    settings.pyxl
```

An error boundary is a React component that receives the error context as props:

```jsx
// pages/error.pyxl
export default function ErrorPage({ error }) {
  return (
    <div>
      <h1>Something went wrong</h1>
      <p>{error.message}</p>
      <p>Status: {error.statusCode}</p>
      <a href="/">Go home</a>
    </div>
  );
}
```

### What reaches `error.pyxl`

Your error page is not only for `LoaderError`. **Any** exception that escapes a
`@server` loader renders it — a missing dict key, a `None` where an object was
expected, a database driver's timeout — with `error.statusCode === 500`. You do
not have to catch and re-raise anything to be covered:

```python
@server
async def load_dashboard(request):
    # Both of these render the nearest error.pyxl.
    raise LoaderError("Not your workspace", status_code=403)   # status 403
    return {"id": session["user_id"]}                          # KeyError -> status 500
```

Reaches `error.pyxl`:

| Failure | Status |
|---------|--------|
| `LoaderError` raised by a page loader | its `status_code` |
| Any other exception from a page loader's body | `500` |
| Any exception from a `layout.pyxl` loader | `500` |
| A `request.state.x` read with no plugin or middleware providing it | `500` |
| A loader Pyxle cannot run, or whose return value it rejects | `500` |
| A `HEAD` that fails to evaluate | `500` |
| A component that throws while rendering — on the server, or in the browser | `500` |

Does **not** reach `error.pyxl`:

- **A `.pyxl` file whose module-level Python fails** — a bad import, a
  `SyntaxError`, an exception at import time. The page is broken before it has
  a loader to run, and the boundary's own module is loaded the same way, so
  Pyxle's fallback document is served instead. In `pyxle dev` the overlay shows
  it; a build catches most of it before you deploy.
- **A fault in Pyxle's own render pipeline.** Handling a framework fault by
  running more of your code can compound the failure, so these deliberately
  serve the fallback document. If you see one, it is a bug — please report it.
- **`@action` failures.** An action answers the caller with JSON, not a page.
  Raise `ActionError` and handle it where you called it (`useAction`'s error).
  An action that raises anything else answers `500` and is logged — see
  [when an action raises something else](#when-an-action-raises-something-else).
- **API routes** (`pages/api/**.py`) and **middleware**, neither of which is a
  page render.
- **A request matching no route at all** — that is [`not-found.pyxl`](#not-found-pages-not-foundpyxl).
- **Errors in a browser event handler or an `await`** — see
  [client-side errors](#client-side-errors).

### What the visitor sees, and what you see

For an author-raised `LoaderError` (or `ActionError`), `error.message` is your
own copy and reaches the visitor verbatim in every environment — that is the
point of raising it.

For **any other** exception the message comes from your dependencies or the
framework and may carry a file path, a row ID, a connection string or a token.
So it is split:

- **In production**, `error.message` is `"An unexpected error occurred."` and
  `error.type` is `"ServerError"`. The real exception never reaches the browser
  (see [Security](security.md)). It **is** written to the server log, once, with
  its full traceback — that log is your only record, so make sure you collect it.
- **In development**, `error.message` carries the real detail (with obvious
  secrets redacted), and the error overlay shows the full traceback pointing at
  the line in your `.pyxl` file.

Design `error.pyxl` for the production wording. Rendering `{error.message}` is
fine, but do not build the page around it saying something specific.

This holds however the visitor arrived. A client-side navigation fetches the
page as JSON rather than HTML, and that payload is built from the same rule —
so clicking a link to a failing page and pasting its URL give the boundary the
same `message`, `type`, `statusCode` and `data`. (They did not always: the
navigation path carried its own copy of the rule and had lost the author-raised
exemption, so a clicked link reported `ServerError` for a `LoaderError` a pasted
URL reported correctly.)

#### The fallback document reads by status, in dev as well as production

When no `error.pyxl` answers, Pyxle serves its own fallback document, and what
it says is decided by the **status** — because a `LoaderError(status_code=404)`
is your code stating a fact about the request, not a fault. A sub-500 fallback
is headed with the same wording a visitor would get (*Not found*, *Sign in
required*, *Too many requests*), and says in as many words that the response
was one your code chose. Only a 5xx is headed *Server Render Failed*.

In `pyxle dev` that document still carries everything you need to find the
`raise` — the exception type, its message, and the file and line in your
`.pyxl` — so nothing is lost by it no longer calling a deliberate 404 a crash.

### An `error.pyxl` does not run a loader

Unlike `not-found.pyxl`, an error page has **no** `@server` loader: it receives
only the `error` prop (plus its layouts' data), and a `@server` function
declared in an `error.pyxl` is never called. Fetching data at the moment the
page is already failing is how one error becomes two, so the boundary does the
least work it can.

Everything it does still touch is protected the same way. If a layout loader
raises while the boundary renders, or the boundary's own `HEAD` fails to
evaluate, the failure is logged and the boundary renders without that piece
rather than being lost. If the boundary itself cannot render at all, Pyxle's
fallback document is served — the boundary is never retried, so a failing error
page cannot loop. Throughout, the error reported is the *original* one; a
failure while handling it never replaces it.

### Error props

The `error` prop contains:

| Property | Type | Description |
|----------|------|-------------|
| `message` | `string` | The error message |
| `statusCode` | `number` | HTTP status code |
| `type` | `string` | Exception class name |
| `data` | `object?` | Additional data (if provided via `LoaderError(data=...)`) |

### The error page's head

An `error.pyxl` is an ordinary page: it is wrapped in its ancestor layouts, and
its document head is merged from the same sources with the same precedence as
any other page — the layout chain's `<Head>` blocks and `HEAD` variable, the
boundary's own `HEAD` variable, and its `<Head>` blocks. Your stylesheet,
favicon and site metadata reach the error page, and a `<title>` in `error.pyxl`
overrides the layout's:

```jsx
// pages/error.pyxl
import { Head } from 'pyxle/client';

export default function ErrorPage({ error }) {
  return (
    <main>
      <Head>
        <title>Something went wrong</title>
        <meta name="robots" content="noindex" />
      </Head>
      <h1>Something went wrong</h1>
      <p>{error.message}</p>
    </main>
  );
}
```

A callable or otherwise computed `HEAD` in an `error.pyxl` receives the error
context rather than loader data. If it raises, the head falls back to the
elements Pyxle could extract statically and the boundary still renders — the
visitor never loses the page over its head.

### Boundary resolution

When an error occurs, Pyxle walks up the directory tree from the page that failed until it finds an `error.pyxl`:

- `pages/dashboard/settings.pyxl` throws -->
  1. Check `pages/dashboard/error.pyxl`
  2. Check `pages/error.pyxl`
  3. Use default error document

### Client-side errors

The same `error.pyxl` is also a **client-side React error boundary**. The server renders the nearest `error.pyxl` when a loader or the initial render fails; once the page is interactive, that boundary keeps working in the browser. If a component throws while re-rendering — after a state update, during a client-side navigation, or on a hydration fault — the boundary catches it and renders the nearest `error.pyxl` in place, instead of React unmounting the page to a blank screen.

It receives an `error` prop with the same keys on both sides (`message`, `statusCode`, `type`), so one `error.pyxl` renders consistently whether the fault happened on the server or in the browser. The *values* differ for a client-caught render fault: there is no HTTP status in the browser, so the client boundary always reports `statusCode: 500` and `type` = the JS error name, and never carries `data` (which is server-only). Keep `error.pyxl` tolerant of `statusCode` being `500` on the client. The boundary is transparent until something throws, so it never affects hydration, and it resets on the next navigation. In `pyxle dev` the [error overlay](#dev-mode-error-overlay) still surfaces the full stack on top; in production the boundary is the user-facing fallback.

This catches *render* faults. An error thrown in an event handler or an `await` (e.g. a failed `fetch`) is not a render error — handle those where they occur (a `try/catch`, or surfacing an `ActionError` from `useAction`).

## Not-found pages (`not-found.pyxl`)

With no `not-found.pyxl` anywhere in your project, an unmatched URL is answered
with Pyxle's built-in 404 — the same designed document the other status
fallbacks use. Under `pyxle dev` it also names the file that replaces it. That
hint is dev-only; a production visitor sees the page without it. Clients that
did not ask for HTML (a `fetch` call, an API consumer) get a plain
`text/plain` body instead.

The built-in page is a floor, not the intended experience: it has none of your
layout, your navigation, or a way onward. Ship your own.

Create a `not-found.pyxl` file to customise the 404 page:

```jsx
// pages/not-found.pyxl
export default function NotFoundPage() {
  return (
    <div>
      <h1>404 - Page Not Found</h1>
      <p>The page you are looking for does not exist.</p>
      <a href="/">Go home</a>
    </div>
  );
}
```

Like error boundaries, not-found pages follow directory scoping -- a `not-found.pyxl` in `pages/docs/` handles 404s within `/docs/*`.

> **`not-found.pyxl` vs a loader 404.** `not-found.pyxl` fires only for a request whose path matches **no** route. A `LoaderError(status_code=404)` raised from a real route does **not** render `not-found.pyxl` — it renders the nearest `error.pyxl` with `error.statusCode === 404`. Branch on the status inside `error.pyxl` if you want a 404-specific message there:
>
> ```jsx
> export default function ErrorPage({ error }) {
>   if (error.statusCode === 404) return <h1>Not found</h1>;
>   return <h1>Something went wrong</h1>;
> }
> ```

`not-found.pyxl` is a **normal page**, not an error boundary: it receives **no** `error` prop, and it may declare its own `@server` loader (it gets that loader's props). Use `error.pyxl` when you need the `error` context; use `not-found.pyxl` for an unmatched-route landing page.

> **One thing outranks it, in dev only.** A page whose source does not compile
> registers no route, so its URL would land here — and a designed "page not
> found" is a confident answer to the wrong question when the file is present
> and correctly named. Under `pyxle dev` such a URL answers `500` with the
> compile error instead (file, line, message), and only such a URL: everything
> else still reaches your `not-found.pyxl`. `pyxle serve` never sees this,
> because `pyxle build` refuses to build a project that does not compile. See
> [when a rebuild fails](../architecture/dev-server.md#when-a-rebuild-fails).

## Dev mode error overlay

During development (`pyxle dev`), a **fault** also appears in a browser overlay
with:

- The error message and stack trace
- Breadcrumbs showing which stage failed (loader, renderer, hydration)
- File path and line number

An error you raised on purpose does **not** open it. A `LoaderError` with a
status below 500 — a 404 for a post that does not exist, a 403 for a page the
visitor may not see — is a response you wrote, so the overlay stays out of the
way and your `error.pyxl` renders, which is the whole point of raising it. The
same rule the server log already follows: an intentional sub-500 is not a server
error.

The overlay belongs to the page that failed. It is shown only while you are on
the URL whose render failed, so a broken `/reports` never covers a working `/` —
it renders full-screen, and an overlay on the wrong page would swallow that
page's clicks. A failure that is not tied to one URL, such as a failed rebuild,
does break every page and still shows on all of them.

It communicates via WebSocket and updates in real time as you fix errors. It also
**survives a reload**: unresolved errors are replayed when the page reconnects,
so a fault raised while no tab was open — or one you reloaded past — still shows.
Navigating away from the broken page clears it.

## A page that will not compile

A syntax error is different from a runtime error: the page never builds, so there
is nothing to render an error boundary *into*. `pyxle dev` serves the compile
error at that URL instead — the file, the line and column, the message, and the
source around it — and reloads the page by itself once the rebuild succeeds.

```
❌ Rebuild failed: pages/about.pyxl:7:9: unexpected indent
```

Both halves of the file are checked. A syntax error in the React half is
reported the same way, against the `.pyxl` line you wrote — not the line of the
`.jsx` Pyxle generates from it:

```
❌ Rebuild failed: pages/about.pyxl:16: JSX syntax error: Unexpected token, expected "jsxTagEnd"
```

### Every line number is a line of your file

Some errors name a **second** line — the one where the trouble started, as
opposed to the one where the compiler noticed it:

```
❌ Rebuild failed: pages/about.pyxl:11:5: closing parenthesis ')' does not match opening parenthesis '[' on line 8
```

Both are lines of `pages/about.pyxl`. Line 11 is the `)` that does not fit; line
8 is the `[` it should have matched. The same holds for
`unterminated string literal (detected at line 14)` and for `pyxle check`'s
`redefinition of unused 'os' from line 7`.

This is worth stating because Pyxle hands each half of your file to a different
checker — CPython for the Python, Babel for the React — and each of those
numbers its findings from the start of the block it was given, not from the
start of your file. Pyxle translates both the position and any line the message
itself names, so you never have to work out which coordinate system you are
reading.

A number Pyxle cannot account for is left exactly as the checker wrote it,
never nudged to the nearest line it does know. That covers numbers that were
never line references in the first place: a name you wrote is quoted back to you
untouched, so `__all__ = ["ghost on line 999"]` reports
`undefined name 'ghost on line 999' in __all__` and not some line of your file.

Only the routes that depend on the broken file are affected. A broken
`pages/about.pyxl` takes down `/about`; a broken `pages/blog/layout.pyxl` takes
down `/blog/*`, because every page there is wrapped in it. Everything else keeps
rendering and keeps hot-reloading while you fix it.

`error.pyxl` is not involved — it cannot be: it catches exceptions raised while a
page runs, and a page that does not compile never runs. This is dev-only;
`pyxle build` refuses to produce a `dist/` from a project that does not compile.

## Next steps

- Add client-side components: [Client Components](client-components.md)
- Secure your application: [Security](security.md)
