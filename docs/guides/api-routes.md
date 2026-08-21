# API Routes

Files under `pages/api/` are API endpoints. They are plain Python files (not `.pyxl`) that handle HTTP requests and return JSON or other responses.

## Basic API route

Create `pages/api/hello.py`. An API module exports an **`endpoint`** callable that receives the Starlette `Request` and returns a response:

```python
from starlette.requests import Request
from starlette.responses import JSONResponse

async def endpoint(request: Request) -> JSONResponse:
    return JSONResponse({"message": "Hello, world!"})
```

`endpoint` handles every HTTP method bound to the route. This responds to `GET /api/hello`:

```bash
curl http://localhost:8000/api/hello
# {"message": "Hello, world!"}
```

## HTTP methods

`endpoint` receives every method bound to the route. Branch on `request.method` to handle more than one:

```python
from starlette.requests import Request
from starlette.responses import JSONResponse

async def endpoint(request: Request) -> JSONResponse:
    if request.method == "GET":
        users = await fetch_all_users()
        return JSONResponse({"users": users})

    if request.method == "POST":
        body = await request.json()
        user = await create_user(body["name"], body["email"])
        return JSONResponse({"user": user}, status_code=201)

    return JSONResponse({"error": "Method not allowed"}, status_code=405)
```

For multi-method endpoints with automatic `405 Method Not Allowed` handling, use an `HTTPEndpoint` class (below) — Starlette dispatches each request to the matching `get`/`post`/… method and rejects the rest.

## Using HTTPEndpoint classes

For more structure, use Starlette's `HTTPEndpoint`:

```python
from starlette.endpoints import HTTPEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse

class Users(HTTPEndpoint):
    async def get(self, request: Request) -> JSONResponse:
        return JSONResponse({"users": []})

    async def post(self, request: Request) -> JSONResponse:
        body = await request.json()
        return JSONResponse({"created": True}, status_code=201)
```

## Sync endpoints and blocking calls

`endpoint` can also be a plain synchronous function. Pyxle dispatches sync
endpoints through Starlette's threadpool, so a blocking body — a database
driver, a sync SDK — occupies a worker thread instead of freezing the event
loop:

```python
import sqlite3
import threading

from starlette.requests import Request
from starlette.responses import JSONResponse

_local = threading.local()

def _db() -> sqlite3.Connection:
    # One persistent connection per worker thread: avoids paying the
    # connect/teardown cost (and SQLite WAL churn) on every request.
    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = _local.conn = sqlite3.connect("app.db")
        conn.row_factory = sqlite3.Row
    return conn

def endpoint(request: Request) -> JSONResponse:
    row = _db().execute("SELECT * FROM items WHERE id = ?", (1,)).fetchone()
    return JSONResponse(dict(row) if row else {"error": "not found"})
```

The same applies to sync `get`/`post`/… methods on `HTTPEndpoint` classes —
Starlette threadpools those natively.

Inside an `async def` endpoint, never call blocking libraries directly — that
stalls every request on the worker's event loop. Either make the endpoint
sync (above) or wrap the call:

```python
import asyncio

async def endpoint(request: Request) -> JSONResponse:
    rows = await asyncio.to_thread(blocking_query, "SELECT ...")
    return JSONResponse({"rows": rows})
```

For sub-millisecond calls the sync-endpoint form is usually faster — one
threadpool hop per request instead of a hop per wrapped call.

Note: route hooks (and the default API policies) wrap function endpoints.
`HTTPEndpoint` classes are dispatched natively by Starlette and bypass route
hooks — the same rationale as WebSocket routes.

## WebSocket endpoints

Since 0.3.0, an API module can export `async def websocket(ws)` to register a WebSocket handler at the same path. The file can export both `endpoint` (HTTP) and `websocket` — they bind to the same URL and Pyxle dispatches based on the protocol of the incoming request.

```python
# pages/api/chat.py
from starlette.websockets import WebSocket

async def websocket(ws: WebSocket) -> None:
    await ws.accept()
    try:
        while True:
            message = await ws.receive_text()
            await ws.send_text(f"echo: {message}")
    except Exception:
        # Client disconnected or socket closed; nothing to clean up.
        pass
```

Client side:

```jsx
const socket = new WebSocket(`ws://${location.host}/api/chat`);
socket.onmessage = (event) => console.log(event.data);
socket.onopen = () => socket.send('hello');
```

You can also export a Starlette `WebSocketEndpoint` subclass for multi-method dispatch:

```python
from starlette.endpoints import WebSocketEndpoint

class websocket(WebSocketEndpoint):
    encoding = "text"

    async def on_connect(self, ws): await ws.accept()
    async def on_receive(self, ws, data): await ws.send_text(f"echo: {data}")
    async def on_disconnect(self, ws, close_code): pass
```

Notes:

- WebSocket handlers run outside the HTTP route-hooks pipeline — hooks wrap request-to-response callables and the WS lifecycle doesn't match that shape. Authenticate, rate-limit, and log inside the handler body.
- CSRF doesn't apply to WebSocket upgrades. Enforce your own origin / session checks in `on_connect` before `await ws.accept()`.

## Dynamic API routes

Use the same bracket syntax as page routes:

```
pages/api/users/[id].py  -->  /api/users/:id
```

## Where API routes can live

An `api` directory can sit anywhere under `pages/`, not only at the top. A
`.py` file is an endpoint whenever the URL it maps to has an `api` segment:

```
pages/api/health.py                     -->  /api/health
pages/s/[slug]/api/v2/summary.json.py   -->  /s/:slug/api/v2/summary.json
```

The second form is what a compatibility API needs — a shape another vendor's
clients already expect, served per tenant. The path is derived from the file
path in full, so dynamic segments and a file extension in the URL both work.

Everywhere else, `.py` files are ignored by routing. That is deliberate: it
lets you colocate helpers with the pages that use them without publishing them
by accident.

```
pages/s/[slug]/queries.py   -->  not a route, importable by neighbours
```

### An `api` directory holds no pages

A directory named `api` is **server ground**, all the way through. One rule
decides everything about it, so you can predict all of it from the name:

| In an `api` directory | What happens |
|---|---|
| `.py` files | Endpoints — they serve URLs |
| `.jsx`, `.css`, `.json`, … | Never copied into the client build |
| `.pyxl` files | Refused: an `api` directory holds no pages |
| Links to its URLs | Left to the browser, never client-side navigations |

So a `.pyxl` page inside one is reported when your project is scanned, by
`pyxle dev` and `pyxle build` alike:

```
A directory named 'api' holds endpoints, not pages, but this page sits inside one:
  pages/docs/api/overview.pyxl
An 'api' directory is server ground throughout: its .py files serve URLs, its
client assets (.jsx, .css, .json) are never shipped to the browser, and links to
its URLs are never client-side navigations — so a page there loads without the
components beside it. Rename the directory (for example 'reference/' or
'api-docs/'), or move the page out of it.
```

Rename the directory — `pages/docs/reference/overview.pyxl` — and the page and
the components beside it work as they do anywhere else.

Only directories count, as everywhere else in this rule: `pages/api.pyxl` is an
ordinary page serving `/api`.

#### A page URL may still contain `api`

The reserved thing is the directory, not the URL shape. A dynamic route fills
its segments in from the request, so `pages/docs/[...slug].pyxl` serves
`/docs/api/config` and `pages/s/[slug]/index.pyxl` serves `/s/api` — both are
ordinary pages and both render normally.

What such a page gives up is the client router. It reads the rule off the URL,
where a page is indistinguishable from an endpoint, and resolves the ambiguity
towards safety: a link to an `api` path is never prefetched on hover — a
prefetch would issue a `GET` at what may be your endpoint, from a mouse
movement — and a click performs an ordinary navigation rather than a
client-side one. The page loads the way it would with JavaScript disabled. The
same rule governs the `.md` renditions in [AI accessibility](llms.md): links to
`api` paths are left pointing at the endpoint.

### Private modules inside an `api` directory

Inside an `api` directory the same colocation is available, marked the way
Python already marks it: **a leading underscore means private**. A file or
directory whose name starts with `_` is never a route.

```
pages/api/orders.py            -->  /api/orders
pages/api/_shared.py           -->  not a route
pages/api/__init__.py          -->  not a route
pages/api/_internal/db.py      -->  not a route (the whole directory is private)
```

A private module is an ordinary Python module — import it from the endpoints
beside it exactly as you would any other module in your project:

```python
# pages/api/_shared.py
DEFAULT_LIMIT = 50

def serialise(order):
    return {"id": order.id, "total": order.total}
```

```python
# pages/api/orders.py
from starlette.responses import JSONResponse

from pages.api._shared import DEFAULT_LIMIT, serialise

async def endpoint(request):
    orders = await fetch_orders(limit=DEFAULT_LIMIT)
    return JSONResponse({"orders": [serialise(order) for order in orders]})
```

Only the segments at or below the `api` directory are read this way. Above it
the path is a URL, where an underscore is just a character:
`pages/_admin/api/health.py` still serves `/_admin/api/health`.

Private modules are not routes, so nothing compiles them — `pyxle build` copies
them into `dist/` instead, and they deploy with the endpoints that import them
([what to ship](deployment.md#what-to-ship)).

Under `pyxle dev` they still hot-reload. Saving one prints

```
✅ Reloaded pages/api/_shared.py in 9 ms
```

— "reloaded" rather than "rebuilt", because nothing was compiled: the module is
dropped from Python's import cache and every endpoint that imports it is
re-imported, so the next request runs your new code. If the endpoint cannot be
imported afterwards (the helper has a syntax error, or the name it exports was
renamed), the previous route table keeps serving and the terminal says so; fix
the file and save again.

The rule applies to `.py` modules in an `api` directory, not to pages: a
`.pyxl` file named with a leading underscore is a normal route.

A route may end in an extension the browser reads as an asset — `.js`, `.css`,
`.json` — and it is still your endpoint. `pages/api/embed.js.py` serves
`/api/embed.js`, which is how an embeddable widget is usually shipped:

```python
# pages/api/embed.js.py
from starlette.responses import Response

async def endpoint(request):
    return Response(
        "console.log('hello from your app');",
        media_type="application/javascript; charset=utf-8",
        headers={"access-control-allow-origin": "*"},
    )
```

```python
from starlette.requests import Request
from starlette.responses import JSONResponse

async def endpoint(request: Request) -> JSONResponse:
    user_id = request.path_params["id"]
    user = await fetch_user(user_id)
    if user is None:
        return JSONResponse({"error": "Not found"}, status_code=404)
    return JSONResponse({"user": user})
```

## Reading request bodies

```python
async def endpoint(request: Request) -> JSONResponse:
    # JSON body
    body = await request.json()

    # Form data
    form = await request.form()

    # Raw body
    raw = await request.body()

    return JSONResponse({"received": True})
```

## Error responses

Return appropriate HTTP status codes:

```python
async def endpoint(request: Request) -> JSONResponse:
    api_key = request.headers.get("x-api-key")
    if not api_key:
        return JSONResponse({"error": "Missing API key"}, status_code=401)

    data = await fetch_data(api_key)
    if data is None:
        return JSONResponse({"error": "Not found"}, status_code=404)

    return JSONResponse({"data": data})
```

## API routes vs server actions

| Feature | API routes | Server actions |
|---------|-----------|----------------|
| File location | `pages/api/*.py` | Inside `.pyxl` files |
| HTTP methods | Any (GET, POST, PUT, etc.) | POST only |
| Response format | Any Starlette Response | JSON dict |
| Called from | Anywhere (curl, fetch, etc.) | `<Form>` or `useAction` |
| CSRF protection | On for POST/PUT/PATCH/DELETE¹ | Enabled by default |
| Use case | Public APIs, webhooks, integrations | Form submissions, mutations |

¹ CSRF runs app-wide, so a state-changing API request (POST/PUT/PATCH/DELETE) must carry the double-submit token by default — same as any other route. A public webhook or third-party integration that can't send the token must list its path prefix in [`csrf.exemptPaths`](security.md). Safe methods (GET/HEAD/OPTIONS) are never checked.

## Next steps

- Add middleware to your routes: [Middleware](middleware.md)
- Protect routes with CSRF: [Security](security.md)
