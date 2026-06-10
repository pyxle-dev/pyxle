# Data Loading

Pyxle uses `@server` decorated functions to load data on the server before rendering a page. The loader runs on every request, fetches whatever the page needs, and passes the result as props to the React component.

## Basic loader

```python
@server
async def load_page(request):
    return {"message": "Hello from the server!"}
```

```jsx
export default function MyPage({ data }) {
  return <h1>{data.message}</h1>;
}
```

The `@server` function:

1. Receives a Starlette [`Request`](https://www.starlette.io/requests/) object
2. Must be `async` (enforced at compile time)
3. Must return a JSON-serializable `dict`
4. The return value is available as `props.data` in the React component

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
2. If found, it renders the error boundary with the error context as props
3. If not found, a default error page is shown

See [Error Handling](../guides/error-handling.md) for full details.

## Using external APIs

Loaders can call any Python code -- databases, APIs, file systems:

```python
import httpx

@server
async def load_posts(request):
    async with httpx.AsyncClient() as client:
        resp = await client.get("https://jsonplaceholder.typicode.com/posts")
        resp.raise_for_status()
    return {"posts": resp.json()[:10]}
```

```jsx
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
[Sync endpoints and blocking calls](../guides/api-routes.md).

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

The loader runs on **every request**. There is no built-in caching -- use your own caching strategy in the loader if needed.

## Next steps

- Mutate data with server actions: [Server Actions](server-actions.md)
- Handle errors gracefully: [Error Handling](../guides/error-handling.md)
