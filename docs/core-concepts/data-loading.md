# Data Loading

Pyxle uses `@server` decorated functions to load data on the server before rendering a page. The loader runs on every request, fetches whatever the page needs, and passes the result as props to the React component.

## Basic loader

```pyxl
@server
async def load_page(request):
    return {"message": "Hello from the server!"}

export default function MyPage({ data }) {
  return <h1>{data.message}</h1>;
}
```

The `@server` function:

1. Receives a Starlette [`Request`](https://www.starlette.io/requests/) object. Its first parameter **must be named exactly `request`** (enforced at compile time)
2. Must be `async` (enforced at compile time) and declared at module scope
3. Must return a JSON-serializable `dict`
4. The return value is available as `props.data` in the React component
5. Only **one** `@server` function is allowed per `.pyxl` file

## Accessing request data

The `request` parameter is a full Starlette `Request`. You have access to:

```python
@server
async def load_page(request):
    # URL path parameters (from dynamic routes)
    user_id = request.path_params["id"]

    # Query string parameters
    page = request.query_params.get("page", "1")

    # Request headers
    auth = request.headers.get("authorization", "")

    # Cookies
    session = request.cookies.get("session_id", "")

    # The full URL
    url = str(request.url)

    return {"user_id": user_id, "page": int(page)}
```

## Returning status codes

Return a tuple of `(data, status_code)` to set the HTTP status:

```python
@server
async def load_page(request):
    item = await fetch_item(request.path_params["id"])
    if item is None:
        return {"error": "Not found"}, 404
    return {"item": item}
```

## Caching the render

For a page whose content is the same for every visitor and changes rarely,
return a `{"data": ..., "revalidate": <seconds>}` envelope instead of a plain
dict. Pyxle caches the rendered HTML and serves it back without re-running the
loader, refreshing it in the background once it goes stale:

```python
@server
async def load_post(request):
    post = await fetch_post(request.path_params["slug"])
    return {"data": {"post": post}, "revalidate": 60}
```

`data` is the props your component receives; `revalidate` is the freshness
window in seconds. **Only do this for pages that render no per-user data** — a
cached render is shared with every visitor. See [Caching](../guides/caching.md)
for invalidation, incremental regeneration, and the full contract.

The envelope is recognized only in its **exact two-key shape** (`{"data", "revalidate"}`) **and** when `data` is itself a dict/mapping. To cache a list, wrap it: `{"data": {"items": [...]}, "revalidate": N}` — a top-level list (`{"data": [...], "revalidate": N}`) is *not* treated as an envelope and is passed through as ordinary props. `revalidate` must be `None` or a non-negative number of seconds; a bool, a negative number, or a string raises a loader error.

## Error handling in loaders

Raise `LoaderError` to trigger the nearest error boundary:

```python
from pyxle.runtime import LoaderError

@server
async def load_page(request):
    user = await fetch_user(request.path_params["id"])
    if user is None:
        raise LoaderError("User not found", status_code=404)
    if not user["active"]:
        raise LoaderError("Account suspended", status_code=403)
    return {"user": user}
```

When `LoaderError` is raised:

1. Pyxle searches up the directory tree for the nearest `error.pyxl`
2. If found, it renders the error boundary, passing the error context on the **`error`** prop (not `data`)
3. If not found, a default error page is shown

The same three steps run for **any** exception your loader raises — a `KeyError`, a driver timeout — as a `500`. `LoaderError` is what lets you choose the status code and write the message the visitor reads; you do not need it to reach the boundary.

The boundary therefore destructures `error`, and the context keys are `error.message`, `error.statusCode` (camelCase), `error.type`, and optional `error.data` (present only when the `LoaderError` carried non-empty `data`):

```jsx
export default function Error({ error }) {
  return <h1>{error.statusCode} — {error.message}</h1>;
}
```

See [Error Handling](../guides/error-handling.md) for full details.

## Using external APIs

Loaders can call any Python code -- databases, APIs, file systems:

```pyxl
import httpx

@server
async def load_posts(request):
    async with httpx.AsyncClient() as client:
        resp = await client.get("https://jsonplaceholder.typicode.com/posts")
        resp.raise_for_status()
    return {"posts": resp.json()[:10]}

export default function PostsPage({ data }) {
  return (
    <ul>
      {data.posts.map(post => (
        <li key={post.id}>{post.title}</li>
      ))}
    </ul>
  );
}
```

## Calling blocking libraries

Loaders are async and run on the server's event loop. Calling a blocking
library directly — a sync database driver, `requests`, a sync SDK — stalls
every other request handled by that worker while the call runs. Wrap
blocking calls in `asyncio.to_thread()` so they execute on a worker thread:

```python
import asyncio

@server
async def load_data(request):
    rows = await asyncio.to_thread(blocking_query, "SELECT ...")
    return {"rows": rows}
```

If the blocking work is a database read, keep one persistent connection per
worker thread (for example in a `threading.local()`) instead of connecting
per request -- connection setup usually costs more than the query itself.

For API routes there is a second option: a plain `def endpoint(request)` is
dispatched through Starlette's threadpool automatically. See
[Sync endpoints and blocking calls](../guides/api-routes.md#sync-endpoints-and-blocking-calls).

## Loaders should be stateless

A loader runs on every request and should behave as a pure read: fetch what the
page needs and return it. Avoid keeping mutable state in a **module-level
global** — a counter, an in-memory cache, an accumulating list:

```python
count = 0  # module-level state — avoid

@server
async def load_home(request):
    global count
    count += 1              # not what you want in production
    return {"count": count}
```

Module-level globals persist across requests for the life of a process — in
`pyxle serve`, and (because the module is imported once and reused) in
`pyxle dev` too. But that state is **per process**, never shared or durable:

- Under `pyxle serve --workers N` each worker has its own copy, so `count`
  above is really N independent counters — a client sees whichever worker
  handled its request.
- Nothing survives a restart, a deploy, or — in dev — a hot-reload edit, which
  re-imports the module and resets its globals.

For state that must be shared across requests or workers, or that must survive
a restart, use an explicit store: a database (see the
[`pyxle-db`](../guides/api-routes.md) plugin), a cache such as Redis, or a
per-worker resource like a `threading.local()` connection (above). Keep
per-request state inside the loader, and mutate persistent state through an
[`@action`](server-actions.md).

## Pages without loaders

If a page has no `@server` function, `data` is an empty object:

```jsx
export default function StaticPage() {
  return <h1>This page has no loader</h1>;
}
```

## How it works

1. A request hits a page route (e.g., `/blog/hello-world`)
2. Pyxle imports the compiled Python module and runs the `@server` function
3. The return value is serialised to JSON
4. The React component is rendered on the server with `{ data: loaderResult }` as props
5. The full HTML is sent to the browser
6. React hydrates the page on the client, using the same props embedded in the HTML

By default the loader runs on **every request**. To cache the rendered page and skip the loader on later requests, return a `{"data": ..., "revalidate": N}` envelope -- see [Caching](../guides/caching.md).

## Next steps

- Mutate data with server actions: [Server Actions](server-actions.md)
- Handle errors gracefully: [Error Handling](../guides/error-handling.md)
