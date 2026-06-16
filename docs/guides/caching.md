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

## Where the cache lives

The default backend is an in-memory store, **bounded** by entry count and total
bytes with LRU eviction, so it never grows without limit. The cache is enabled
automatically for production serves (`pyxle serve`) and disabled in `pyxle dev`
so a cached render never hides an edit while you're working.

Under `pyxle serve --workers N` each worker process keeps its own in-memory
cache, so `cache.invalidate(...)` reaches the worker that handled the action but
not the others; the entry still expires on its own everywhere via `revalidate`.
A shared backend (so invalidation fans out across every worker and host) is on
the roadmap.

## When *not* to cache

- Pages that show the signed-in user's data, a per-user CSRF token, or anything
  that varies by request. Leave these as plain loaders (`return {...}`), and they
  are never cached.
- Pages that must always reflect the absolute latest data with zero staleness —
  use a plain loader, or `revalidate: 0` plus `cache.invalidate(...)` on every
  write.

## Next steps

- Load data: [Data Loading](../core-concepts/data-loading.md)
- Mutate data: [Server Actions](../core-concepts/server-actions.md)
- Configure the edge cache: [Configuration](../reference/configuration.md)
