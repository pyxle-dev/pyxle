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
