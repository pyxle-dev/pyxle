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
4. `@server`/`@action` return a **plain dict.** On the client, an action's result arrives
   **wrapped** as `{ ok: true, ...yourDict }` (or `{ ok: false, error }`). Always check `res.ok`.
5. To raise a handled error you **must import it**: `from pyxle.runtime import ActionError,
   LoaderError`. (The decorators are auto-injected; these exception classes are not — forgetting
   this is the #1 `NameError`.)
6. Secrets stay server-side. An env var is exposed to the browser **only** if it is prefixed
   `PYXLE_PUBLIC_`. Never return secrets/tokens from a loader or action (they're serialized to
   the client).

---

## A complete page — copy this shape

```python
# pages/index.pyxl

# ── Python (server) ─────────────────────────────────────────────
# @server / @action need no import. ActionError/LoaderError DO (see below).

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
- To fail with a status code: `from pyxle.runtime import LoaderError` then
  `raise LoaderError("Not found", status_code=404)`.
- A page **without** a `@server` loader is fine — it just renders statically (the component
  takes no `data`).

## Mutations — `@action`

```python
from pyxle.runtime import ActionError    # required to raise it

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
const create = useAction('create_post');           // returns a callable + .pending/.error/.data
const res = await create({ title });               // arg becomes request.json() on the server
if (res.ok) { /* use res.id, res.title */ } else { /* show res.error */ }
```

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

## The client toolkit — `import { … } from 'pyxle/client'`

- `useAction(name)` — bind to an `@action`; returns a callable with `.pending`, `.error`, `.fields`, `.data`.
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
  styles/          tailwind.css (imported from the JSX section)
public/            static files served at /
db.py              (you add this) — e.g. SQLite helpers; import with `from db import ...`
pyxle.config.json  project config
requirements.txt   Python deps · package.json — Node deps
```

## Commands

```bash
pyxle dev      # dev server + hot reload at http://localhost:8000  (use this to verify changes)
pyxle build    # production build -> dist/
pyxle serve    # serve the production build
pyxle install  # (re)install Python + Node deps
```

## Rules — DO / DON'T

- **DO** keep one `export default function` per page (the component). Name the loader anything.
- **DO** return JSON-safe dicts from `@server`/`@action`. **DON'T** add your own `ok` key to an
  action's return — the framework adds it.
- **DON'T** call your own actions with `fetch` or write a route for them — use `useAction`/`<Form>`.
- **DON'T** put emoji or other non-BMP characters in **server-rendered** JSX text — SSR can fail
  to encode them. Use them only in client-only paths, or stick to plain text.
- **DON'T** import `server`/`action` to use the decorators (they're injected). **DO** import
  `ActionError`/`LoaderError` before raising them.
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
