# AGENTS.md

Guidance for AI coding agents (Claude Code, Cursor, Copilot, Aider, …) working in this
**Pyxle** project. Read this before writing or editing `.pyxl` files. Pyxle is not like other
frameworks — most mistakes come from not understanding the one-file Python+React model below.

---

## TL;DR — the rules that prevent ~90% of mistakes

1. A `.pyxl` file is **Python on top, React/JSX on the bottom — in one file.** The split is
   automatic: the JSX section begins at your **first JavaScript `import`**. Never add a
   separator comment (`# --- JSX ---` and the like do nothing and signal stale knowledge).
2. `@server` loads data; its returned **dict becomes the component's `data` prop**. `@action`
   performs a mutation. Both decorators are **available without importing them.**
3. The client calls an `@action` with **`useAction('name')`, not `fetch()`.** There is no API
   route to write — the `@action` *is* the endpoint.
4. `@server`/`@action` return a **plain dict.** On the client, `const res = await myAction(...)`
   gives you `{ ok: true, ...yourDict }` (or `{ ok: false, error }`) — your fields sit **directly
   on `res`** (`res.users`), **not** under `res.data`. Always check `res.ok`.
5. Raise a handled error with `raise LoaderError(...)` (in a loader) or `raise ActionError(...)`
   (in an action) — **no import needed.** Like the decorators, these runtime classes
   (`LoaderError`, `ActionError`, `ValidationActionError`, `invalidate_routes`) are auto-injected.
   `pyxle check` reports any name you genuinely left undefined, and any duplicate `export default`.
6. Secrets stay server-side. An env var is exposed to the browser **only** if it is prefixed
   `PYXLE_PUBLIC_`. Never return secrets/tokens from a loader or action (they're serialized to
   the client).

---

## A complete page — copy this shape

```python
# pages/index.pyxl

# ── Python (server) ─────────────────────────────────────────────
# @server / @action / LoaderError / ActionError all work without imports (auto-injected).

@server
async def load(request):
    # Runs on the server. The returned dict becomes `data` in the component.
    return {"count": 0, "name": request.query_params.get("name", "world")}

@action
async def increment(request):
    body = await request.json()              # the payload you passed on the client
    return {"count": body["count"] + 1}      # plain dict; framework wraps as { ok, ...this }


# ── JavaScript (client) — begins at the first JS import ─────────
import React, { useState } from 'react';
import { Head, useAction } from 'pyxle/client';

export default function Home({ data }) {        // exactly one `export default` component
    const [count, setCount] = useState(data.count);
    const increment = useAction('increment');   // bind to the @action above

    async function bump() {
        const res = await increment({ count }); // POSTs to the action; CSRF handled for you
        if (res.ok) setCount(res.count);        // res === { ok: true, ...actionReturn }
    }

    return (
        <>
            <Head><title>Hello {data.name}</title></Head>
            <h1>Count: {count}</h1>
            <button onClick={bump} disabled={increment.pending}>+1</button>
        </>
    );
}
```

---

## The Python ↔ JSX split

- **Top = Python**, runs on the server. **Bottom = JavaScript/JSX**, runs on the server (SSR)
  then hydrates on the client.
- The boundary is the first line that is **not valid Python** — in practice your first JS
  `import` (`import React …`, `import { Head } from 'pyxle/client'`, or `import './styles/…'`).
  The compiler finds it automatically.
- Put **Python imports** (`from app.db import ...`) in the Python section; **JS imports** in the
  JSX section. Don't interleave the two languages.

## Loading data — `@server`

```python
@server
async def load(request):              # any function name works; @server marks it the loader
    user_id = request.path_params["id"]            # dynamic route segment, e.g. pages/[id].pyxl
    page    = int(request.query_params.get("page", "1"))
    return {"user_id": user_id, "page": page}      # -> component receives { data }
```

- The returned dict is JSON-serialized and passed to the component as `data`. Return only
  JSON-safe values.
- `request` is a Starlette `Request`: `request.path_params`, `request.query_params`,
  `request.headers`, `request.cookies`, `request.url`.
- To fail with a status code: `raise LoaderError("Not found", status_code=404)` (auto-injected,
  no import needed).
- A page **without** a `@server` loader is fine — it just renders statically (the component
  takes no `data`).

## Mutations — `@action`

```python
@action
async def create_post(request):
    body = await request.json()
    title = (body.get("title") or "").strip()
    if not title:
        raise ActionError("Title is required.")    # -> client gets { ok: false, error: "..." }
    # ...persist...
    return {"id": 1, "title": title}               # -> client gets { ok: true, id, title }
```

Call it from React — **never `fetch`**:

```jsx
const create = useAction('create_post');           // a callable, plus create.pending / .error / .fields
const res = await create({ title });               // arg becomes request.json() on the server
if (res.ok) { /* fields are top-level: res.id, res.title — never res.data.id */ }
else        { /* show res.error */ }
```

The value you `await` **is** the flat `{ ok, ...yourReturn }` object — read `res.title`, never
`res.data.title` (a successful result has no `.data`). The hook *also* exposes `create.data`: the
**same** fields from the last successful call, handy for rendering away from the call site. Both
reach your data, but the value you `await` is never nested under `.data`.

Or use the `<Form>` helper for form submissions:

```jsx
import { Form } from 'pyxle/client';
<Form action="create_post" onSuccess={(d) => navigate('/')} onError={(msg) => setErr(msg)}>
  <input name="title" required />
  <button type="submit">Create</button>
</Form>
```

**Optional — validate the body with Pydantic.** Annotate a parameter with a Pydantic model and
Pyxle validates the request body before your action runs (install `pyxle-framework[pydantic]`):

```python
from pydantic import BaseModel

class NewPost(BaseModel):
    title: str

@action
async def create_post(request, body: NewPost):     # body is a validated NewPost
    return {"id": 1, "title": body.title}
```

On failure the client gets `{ ok: false, error, fields }` (HTTP 422). Read `res.fields` (or
`create.fields`) — `{ [field]: string[] }` — to show messages per input; `<Form>` passes them as
`onError(msg, fields)`. For hand-rolled checks raise `ValidationActionError(fields={...})` (also
auto-injected). Export an OpenAPI schema with `pyxle openapi`.

If a mutation should refresh data shown elsewhere, re-run the current loader with `refresh()`
(from `pyxle/client`); see the docs for invalidating other routes.

**Background work — don't make the client wait.** To run work *after* the response (send an
email, emit a webhook), use `request.state.background.add_task(fn, *args)` inside an `@action`,
or return the shorthand `{"background": [fn, *args]}`. For fire-and-forget work from anywhere
(loaders too), `from pyxle.tasks import enqueue; enqueue(fn, *args)`. Both run **in-process** —
for durable/cross-worker jobs, hand off to Celery/ARQ/Dramatiq (see the Background Tasks guide).

## The client toolkit — `import { … } from 'pyxle/client'`

- `useAction(name)` — bind to an `@action`; returns a callable with `.pending`, `.error`, `.fields`, and `.data` (the last successful return's fields). The value you `await` is the flat `{ ok, ...return }` — read fields off it directly (`res.x`), not off `res.data`.
- `<Form action="name" onSuccess onError>` — submit named inputs to an `@action`.
- `<Head>` — set per-page `<title>`/`<meta>`/`<link>` (deduped + merged with layouts).
- `<Link href="/path">` — client-side navigation; `navigate('/path')` to do it imperatively.
- `prefetch('/path')` · `refresh()` · `usePathname()`.
- `<Image>` · `<Script>` · `<ClientOnly>` — optimized image, third-party scripts, client-only render.

## Routing (file-based, under `pages/`)

| File | URL |
|---|---|
| `pages/index.pyxl` | `/` |
| `pages/about.pyxl` | `/about` |
| `pages/blog/[slug].pyxl` | `/blog/:slug` → `request.path_params["slug"]` |
| `pages/docs/[...slug].pyxl` | catch-all → `request.path_params["slug"]` |
| `pages/shop/[[...slug]].pyxl` | optional catch-all (also matches `/shop`) |
| `pages/(marketing)/...` | route **group** — folder doesn't appear in the URL |

Special files: `layout.pyxl` (wraps a folder), `not-found.pyxl` (404), `error.pyxl` (error
boundary). Plain HTTP endpoints (webhooks, JSON APIs) go in `pages/api/*.py` as Starlette
handlers — that's separate from `@action`.

## Project layout

```
pages/             routes (.pyxl) + pages/api/*.py endpoints
  index.pyxl       the home page
  layout.pyxl      root layout
  styles/          app.css (imported from the JSX section)
  components/       shared JSX + CSS Modules (only when Tailwind is off, by default)
public/            static files served at /
db.py              (you add this) — e.g. SQLite helpers; import with `from db import ...`
jsconfig.json      import alias (default `@/*`) + editor hints
vite.config.js     re-exports Pyxle's generated Vite config (for shadcn/editor tooling)
pyxle.config.json  project config
requirements.txt   Python deps · package.json — Node deps
```

## Styling — this project

The stack is **React 19 + Vite 7**. CSS goes through Vite: `import './styles/app.css'`
(plain CSS) and `import styles from './x.module.css'` (CSS Modules) both work in
dev and build. **Tailwind is opt-in** and may or may not be set up here:

- **No Tailwind (default):** `pages/styles/app.css` is plain CSS. Don't invent
  `className="bg-slate-50"` Tailwind utilities — they won't do anything. Add real
  CSS rules, or a CSS Module (`pages/components/Badge.module.css` is an example).
- **Tailwind enabled:** `pages/styles/app.css` contains `@import "tailwindcss";`
  and utility classes work. There is **no** `tailwind.config.js` or
  `postcss.config.js` (Tailwind v4 runs through `@tailwindcss/vite`) — don't add them.

Check `pages/styles/app.css` to see which mode you're in. Import project modules
with the alias, e.g. `import { cn } from '@/lib/utils'`.

## Commands

```bash
pyxle dev      # dev server + hot reload at http://localhost:8000  (use this to verify changes)
pyxle studio   # same dev server + the Studio dashboard (routes, loader/action tester, live requests)
pyxle build    # production build -> dist/
pyxle serve    # serve the production build
pyxle install  # (re)install Python + Node deps
```

`pyxle dev --inspect` adds a debugger port — breakpoints bind directly in `.pyxl` files.

## Rules — DO / DON'T

- **DO** keep one `export default function` per page (the component). Name the loader anything.
- **DO** return JSON-safe dicts from `@server`/`@action`. **DON'T** add your own `ok` key to an
  action's return — the framework adds it.
- **DON'T** read an awaited action result as `res.data.x`. A successful result is the flat
  `{ ok, ...yourReturn }`, so the field is `res.x` — your returned data is never nested under
  `.data`. (`.data` is the hook property `useAction(...).data`; on an awaited result it shows up
  only on a *failure*, as the optional error payload — never your returned fields.)
- **DON'T** call your own actions with `fetch` or write a route for them — use `useAction`/`<Form>`.
- **DON'T** put emoji or other non-BMP characters in **server-rendered** JSX text — SSR can fail
  to encode them. Use them only in client-only paths, or stick to plain text.
- **DON'T** import `server`/`action` to use the decorators, or `LoaderError`/`ActionError`/
  `ValidationActionError`/`invalidate_routes` to raise/call them — the compiler auto-injects each
  alongside the decorator that uses it (a `@server` loader injects `LoaderError`; an `@action`
  injects `ActionError`/`ValidationActionError`/`invalidate_routes`). An explicit import (or your
  own definition) of one of these names is harmless — it takes precedence over the injected one —
  just unnecessary.
- **DON'T** expose secrets: env vars reach the browser only with a `PYXLE_PUBLIC_` prefix, and
  loader/action return values are sent to the client.
- **DON'T** add a `# --- JSX ---` / `# --- client ---` marker — the split is automatic.
- **DON'T** add `export const slots` / `export const createSlots` to a page or layout unless it
  actually fills a **named** `<Slot>`. They're optional; an empty `slots = {}` is just noise.

## Verify your work

1. Run `pyxle dev` and open `http://localhost:8000`. Parse/render errors show in a full-screen
   overlay with the file and line — read it, fix, save (hot reload re-renders).
2. For a route, confirm the file path maps to the URL you expect (table above).
3. Before shipping: `pyxle build` must succeed.

## When you need more

Full documentation: **https://pyxle.dev/docs**. Try snippets with zero install in the
**playground**: https://pyxle.dev/playground. Keep this file updated as the project grows.
