# Caching

By default a page's `@server` loader runs on **every request** and the page is
rendered fresh each time. For pages whose content is the same for everyone and
changes rarely — a marketing page, a docs article, a blog post — that work is
wasted. Pyxle's **page cache** stores the rendered HTML and serves it back
without re-running the loader or the SSR render, then refreshes it in the
background when it goes stale.

> **The one rule: only cache pages that render no per-user data.** A cached
> render is shared byte-for-byte with every visitor. If a page embeds a logged-in
> user's name, a CSRF token, or anything request-specific, **do not cache it** —
> you would serve one user's page to another. Caching is always opt-in, exactly
> so this stays your deliberate choice.

## Making a page cacheable

Return a `{"data": ..., "revalidate": <seconds>}` envelope from your loader
instead of a plain dict. `data` is the props your component receives (exactly
as before); `revalidate` is how many seconds the cached render stays fresh.

```python
@server
async def load_post(request):
    post = await fetch_post(request.path_params["slug"])
    return {
        "data": {"post": post},
        "revalidate": 60,   # cache this render for 60 seconds
    }
```

```jsx
export default function Post({ data }) {
  return <article>{data.post.body}</article>;   // `data` is the inner dict
}
```

That's it. The first request renders and stores the page; requests within the
next 60 seconds are served from the cache — no loader, no Node render.

The envelope is recognised only in its exact two-key shape (`data` **and**
`revalidate`, nothing else). A normal loader that happens to return keys named
`data` or `revalidate` is never mistaken for a cache directive.

> **Testing this? Not under `pyxle dev`.** The page cache is deliberately
> **disabled in the dev server**, so a cached render can never hide an edit while
> you work. A page that looks uncached in `pyxle dev` is behaving correctly.
> Verify caching against a production server — `pyxle build` then `pyxle serve` —
> and watch the `x-pyxle-cache: MISS` / `HIT` response header. See
> [Where the cache lives](#where-the-cache-lives).

### Pages without a loader

A page with no `@server` loader can still opt into caching with a module-level
`CACHE` directive in its Python section:

```python
CACHE = {"revalidate": 3600}   # cache this static page for an hour
```

```jsx
export default function About() {
  return <main><h1>About Us</h1></main>;
}
```

`revalidate` is the freshness window in seconds, exactly as in the loader
envelope (`0` means "serve cached but re-render every request"). It is validated
at compile time: an invalid `CACHE` directive (non-numeric or negative value, or
a malformed shape) is reported as a **compile diagnostic** and the page is
treated as **uncached** — whether that diagnostic blocks the build depends on
your strict/non-strict diagnostic mode. If a page declares *both* a loader
envelope and a `CACHE` directive, the loader's `revalidate` wins.

## Incremental regeneration (stale-while-revalidate)

When a cached page passes its `revalidate` window, the **next** request still
gets the cached (stale) bytes immediately — no one waits for a re-render — and a
single background re-render refreshes the cache for everyone after it. This is
incremental static regeneration (ISR): fast responses, fresh-enough content, and
never a thundering herd (only one refresh runs per page at a time, even under
load).

`revalidate: 0` is valid and means "serve the cached copy but re-render on every
request" — useful when you want to absorb bursts without ever serving content
older than one render.

## Static pre-rendering (`pyxle build --static`)

Pages with no `@server` loader and no dynamic route parameters render the same
HTML for everyone, every time. `pyxle build --static` renders them **once at
build time** and stores the result, so the first request after a deploy is
already a cache hit — no cold SSR render, even for the very first visitor.

```bash
pyxle build --static
```

This pre-renders every loader-less, non-dynamic page into the build output's
`prerendered/` subdirectory (`dist/prerendered/` by default; `dist` here means
the configured build/output directory, i.e. `pyxle build -o/--out-dir`);
on startup `pyxle serve` warms its page cache from that directory. Pages with a
loader (or a `{param}` route) are skipped — they still render live and cache at
runtime as before.

A static page's **layout** loader still runs at build time — its result is baked
into the pre-rendered HTML, so a layout loader used by static pages should return
the same data for everyone (global status, navigation), not per-user state. Build-
time loaders run with the same plugin context a request has, so a layout (or page)
loader that reads from a plugin like `pyxle-db` pre-renders correctly — the static
builder opens your plugins' connections at build time, just as a server would.

Pre-rendered entries have no expiry until you
`cache.invalidate(...)` the route. With the default in-memory backend they are
re-warmed from the new `dist/prerendered/` on every restart; with a **shared
file/redis backend**, warmed copies persist in that store, so if a later deploy
stops passing `--static` (or adds a loader to a previously-static route), flush
the cache or invalidate those routes — replacing `dist/` does not clear them.

## Invalidating the cache

When the underlying data changes — you publish a post, edit a page — purge the
cached render so the next request re-renders immediately instead of waiting out
the `revalidate` window:

```python
from pyxle import cache

@action
async def publish_post(request):
    body = await request.json()
    await save_post(body)
    await cache.invalidate(f"/posts/{body['slug']}")   # drop that page's cache
    return {"ok": True}
```

- `await cache.invalidate(path)` purges one route's cached render. Returns
  `True` if something was cached, `False` otherwise — safe to call either way.
- `await cache.invalidate_all()` purges every cached render.

## How it interacts with the edge cache

A route listed in your `pyxle.config.json` [`cache`](../reference/configuration.md)
block (the **edge** cache, which sets `Cache-Control: public, s-maxage=…` for a
CDN) is *also* served from the server-side page cache automatically, using that
same TTL — you don't need to repeat yourself. When both apply, a loader's
`revalidate` wins over the edge `s-maxage`.

Cached page responses carry a strong `ETag`, so a conditional request
(`If-None-Match`) gets a `304 Not Modified`, and an `x-pyxle-cache` response
header reports `HIT`, `STALE`, or `MISS` for debugging.

## Client navigation cache

Separately from the server-side page cache, the browser keeps a per-URL
**navigation cache** of loader payloads so back/forward navigation is instant.
Its lifetime mirrors a page's real cacheability:

- A page with a `cache` entry (or `CACHE` directive) reuses that TTL.
- A **dynamic page** — a `@server` loader with no declared cache lifetime — is
  **not** navigation-cached by default (TTL `0`), so a fresh navigation always
  refetches and a just-made mutation is visible immediately rather than hidden
  behind a stale window. **Opt in** by giving the route a `cache` entry.
- A static, loader-less page uses the client default (2 minutes).

See [Navigation cache TTL](../reference/client-api.md#navigation-cache-ttl) for
the full client-side details.

## Where the cache lives

The cache is enabled automatically for production serves (`pyxle serve`) and
disabled in `pyxle dev` so a cached render never hides an edit while you work.
Choose where rendered HTML is stored with the `PYXLE_PAGE_CACHE_BACKEND`
environment variable:

| `PYXLE_PAGE_CACHE_BACKEND` | Stores in | Use when |
|---|---|---|
| `memory` *(default)* | bounded in-process memory (LRU) | single process, or any deploy where per-worker caches are fine |
| `file` | local disk (`PYXLE_PAGE_CACHE_DIR`) | one host, multiple workers sharing a cache that survives restarts |
| `redis` | shared Redis (`PYXLE_PAGE_CACHE_REDIS_URL`) | multiple hosts, or cross-worker invalidation |
| `off` | nothing — caching disabled | you want it off in production (`none` and `disabled` are accepted aliases for `off`) |

The in-memory backend is **bounded** by entry count (`PYXLE_PAGE_CACHE_MAX_ENTRIES`,
default 512) and total body bytes (`PYXLE_PAGE_CACHE_MAX_BYTES`, default 64 MiB)
with LRU eviction, so it never grows without limit. The Redis backend needs the
optional extra: `pip install 'pyxle-framework[redis]'`.

**Invalidation and workers.** With the in-memory or file backend under
`pyxle serve --workers N`, each worker keeps its own store, so
`cache.invalidate(...)` reaches the worker that handled the action; entries
still expire everywhere on their own via `revalidate`. The **Redis** backend is
shared across every worker and host, so an invalidation fans out to all of them
— choose it when you need cross-worker purges.

## When *not* to cache

- Pages that show the signed-in user's data, a per-user CSRF token, or anything
  that varies by request. Leave these as plain loaders (`return {...}`), and they
  are never cached.
- Pages that must always reflect the absolute latest data with zero staleness —
  use a plain loader, or `revalidate: 0` plus `cache.invalidate(...)` on every
  write.

> **Query strings:** the cache keys on the route **path** only, so a request
> that carries a query string (`/search?q=…`, `?page=2`) is always served live —
> never cached and never served a cached entry. A page whose content varies by
> query is therefore never collapsed onto one shared render.

### Don't put a `<Form>` on a shared-cached page

A cacheable response is stored once and served to many people, so it must not
carry anything belonging to one of them — including a CSRF token. On a page made
cacheable with `revalidate`, Pyxle therefore suppresses the per-user token, and
[`<Form>`](../reference/client-api.md#form) renders without its hidden
`_csrf_token` field. The client then reads the token from the cookie and adds
the field on its first render.

The visible effect is a React hydration warning on first load and one subtree
re-rendered on the client. **With JavaScript the form still works** — JS
submissions authenticate with the `x-csrf-token` header rather than the hidden
field. **Without JavaScript, on a shared-cached page, the submission is
rejected**, because there is no token to send. Embedding one would defeat the
suppression that makes the page safe to cache at all.

Keep the form on an uncached route: cache the page people read, leave the page
they submit from on a plain loader (or `revalidate: 0`). Static pre-rendering is
unaffected — `pyxle build --static` never runs the CSRF middleware, so a
prerendered page contains no token, stale or otherwise.

## Next steps

- Load data: [Data Loading](../core-concepts/data-loading.md)
- Mutate data: [Server Actions](../core-concepts/server-actions.md)
- Configure the edge cache: [Configuration](../reference/configuration.md)
