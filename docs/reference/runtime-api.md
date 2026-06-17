# Runtime API Reference

The `pyxle.runtime` module provides decorators and error classes used in `.pyxl` files. These are automatically available in the Python section of every `.pyxl` file -- you do not need to import them (the compiler injects the import).

## `@server`

Marks an async function as a page data loader.

```python
@server
async def load_page(request):
    return {"key": "value"}
```

**Behaviour:**

- Attaches `__pyxle_loader__ = True` to the function (no wrapping)
- The function must be `async` (enforced at compile time)
- Receives a Starlette [`Request`](https://www.starlette.io/requests/) object
- Must return a JSON-serializable `dict`
- Can return a `(dict, int)` tuple to set the HTTP status code
- Only one `@server` function is allowed per `.pyxl` file

**Request object properties:**

| Property | Type | Description |
|----------|------|-------------|
| `request.path_params` | `dict` | URL path parameters from dynamic routes |
| `request.query_params` | `QueryParams` | URL query string parameters |
| `request.headers` | `Headers` | HTTP request headers |
| `request.cookies` | `dict` | Request cookies |
| `request.url` | `URL` | Full request URL |
| `request.method` | `str` | HTTP method |
| `request.state` | `State` | Mutable state for middleware to attach data |

## `@action`

Marks an async function as a server action callable from React components.

```python
@action
async def create_item(request):
    body = await request.json()
    return {"id": 1, "name": body["name"]}
```

**Behaviour:**

- Attaches `__pyxle_action__ = True` to the function (no wrapping)
- The function must be `async`
- Receives a Starlette `Request` object
- Must return a JSON-serializable `dict`
- Multiple `@action` functions are allowed per `.pyxl` file
- Accessible via `POST /api/__actions/{page_path}/{action_name}`
- Protected by CSRF middleware by default

**Validated body parameter (optional):**

Annotate a parameter (after `request`) with a [Pydantic](https://docs.pydantic.dev/) `BaseModel` and Pyxle parses and validates the request body before the action runs, passing the typed model in:

```python
from pydantic import BaseModel

class NewItem(BaseModel):
    name: str

@action
async def create_item(request, body: NewItem):
    return {"id": 1, "name": body.name}
```

Requires the `[pydantic]` extra (`pip install "pyxle-framework[pydantic]"`). On a validation failure the action is not called and the client gets a `422` with a `fields` map. See [Validating request bodies](../core-concepts/server-actions.md#validating-request-bodies-with-pydantic) for the full pattern and the [`pyxle openapi`](cli.md#pyxle-openapi) command.

**Client response format:**

```json
// Success
{ "ok": true, "id": 1, "name": "Item" }

// Error (from ActionError)
{ "ok": false, "error": "Error message" }

// Validation error (422, from a Pydantic body or ValidationActionError)
{ "ok": false, "error": "Validation failed", "fields": { "name": ["Field required"] } }
```

## `LoaderError`

Exception class for structured loader errors. Triggers the nearest `error.pyxl` boundary.

```python
from pyxle.runtime import LoaderError

@server
async def load_page(request):
    raise LoaderError("Not found", status_code=404, data={"id": 42})
```

**Constructor:**

```python
LoaderError(message: str, status_code: int = 500, data: dict | None = None)
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `message` | `str` | (required) | Error message displayed in the error boundary |
| `status_code` | `int` | `500` | HTTP response status code |
| `data` | `dict \| None` | `None` | Additional JSON-serializable context |

**Properties:**

| Property | Type | Description |
|----------|------|-------------|
| `.message` | `str` | The error message |
| `.status_code` | `int` | HTTP status code |
| `.data` | `dict` | Additional context (empty dict if None was passed) |

## `ActionError`

Exception class for structured action errors. Returns a JSON error response to the client.

```python
@action
async def update_item(request):
    raise ActionError("Validation failed", status_code=400, data={"field": "name"})
```

> **Auto-imported since 0.3.0.** Any `.pyxl` file that declares at least one `@action` gets `from pyxle.runtime import ActionError` injected by the compiler, so `raise ActionError(...)` works without you remembering to import it. A user-defined `ActionError` class (or an existing import) is respected and takes precedence.

**Constructor:**

```python
ActionError(message: str, status_code: int = 400, data: dict | None = None, *, fields: dict[str, list[str]] | None = None)
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `message` | `str` | (required) | Error message sent to the client |
| `status_code` | `int` | `400` | HTTP response status code |
| `data` | `dict \| None` | `None` | Additional JSON-serializable payload |
| `fields` | `dict[str, list[str]] \| None` | `None` | Per-field validation messages (field path → messages), surfaced to the client as `fields`. Keyword-only |

**Properties:**

| Property | Type | Description |
|----------|------|-------------|
| `.message` | `str` | The error message |
| `.status_code` | `int` | HTTP status code |
| `.data` | `dict` | Additional data (empty dict if None was passed) |
| `.fields` | `dict[str, list[str]]` | Per-field messages (empty dict if None was passed) |

## `ValidationActionError`

A subclass of [`ActionError`](#actionerror) for request-body validation failures. Defaults to HTTP `422` and always carries `fields`. Pyxle raises it automatically when a Pydantic-typed `@action` body fails validation; raise it yourself for checks Pydantic can't express (uniqueness, cross-field rules):

```python
from pyxle.runtime import ValidationActionError

@action
async def register(request, body: Signup):
    if await db.email_taken(body.email):
        raise ValidationActionError(fields={"email": ["That email is already registered."]})
    ...
```

> **Auto-imported.** Like `ActionError`, any `.pyxl` file that declares at least one `@action` gets `ValidationActionError` injected by the compiler. A user-defined class or existing import takes precedence.

**Constructor:**

```python
ValidationActionError(message: str = "Validation failed", *, fields: dict[str, list[str]], status_code: int = 422, data: dict | None = None)
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `message` | `str` | `"Validation failed"` | Top-level error message |
| `fields` | `dict[str, list[str]]` | (required) | Field path → list of messages. Keyword-only |
| `status_code` | `int` | `422` | HTTP response status code |
| `data` | `dict \| None` | `None` | Additional JSON-serializable payload |

The client receives `{ "ok": false, "error": ..., "fields": { ... } }`. The `useAction` hook exposes the map as `result.fields`; `<Form>` passes it as the second argument to `onError`. See [Validating request bodies](../core-concepts/server-actions.md#validating-request-bodies-with-pydantic).

## `invalidate_routes(response, *urls)`

Tell the client router to evict its cached navigation payloads for `urls` before the caller's next navigation. Used by `@action` handlers that mutate data the client has already cached elsewhere.

```python
from pyxle.runtime import invalidate_routes

@action
async def delete_post(request):
    ...
    # Drop the client's cached /posts so its next navigate() refetches.
    return invalidate_routes({"ok": True}, "/posts", "/dashboard")
```

The helper accepts either a plain dict (the usual `@action` return shape) or a Starlette `Response` / `JSONResponse`. In both cases Pyxle attaches an `x-pyxle-invalidate: /posts, /dashboard` header to the outgoing response. `useAction` and `<Form>` read this header automatically and call the client-side [`invalidate(url)`](client-api.md#invalidateurl) for each listed URL — so mutation-driven cache invalidation works end-to-end with no extra wiring on the caller.

Parameters:

| Parameter | Type | Description |
|-----------|------|-------------|
| `response` | `dict \| Response` | The action's return value. Modified in place and returned. |
| `*urls` | `str` | URLs to invalidate. Empty / duplicate values are filtered. |

Returns the `response` argument (possibly with a header or sentinel key added) so you can `return invalidate_routes(...)` in one line.

## WebSockets — `pyxle.realtime`

A page that exports a module-scope `async def websocket(ws)` also serves a
WebSocket at its path. `pyxle.realtime` provides the pub/sub and auth helpers
those handlers use. See the [WebSockets guide](../guides/websockets.md) for the
full walkthrough.

### `channel(ws, name, *, broker=None)`

An async context manager that subscribes `ws` to a named channel for the life of
the block (unsubscribing on exit, including disconnect) and yields a handle with
`.publish(message)`:

```python
from pyxle.realtime import channel

async def websocket(ws):
    await ws.accept()
    async with channel(ws, f"room:{ws.path_params['room']}") as room:
        async for message in ws.iter_text():
            await room.publish(message)   # reaches every subscriber
```

A published `dict`/`list` is sent as a JSON frame, `str` as text, `bytes` as
binary. The broker defaults to the app-scoped `InProcessBroker` on
`app.state.pyxle_broker` (one per process — see the guide's **multi-worker
caveat**). Implement the `Broker` protocol (`subscribe` / `unsubscribe` /
`publish`) for a Redis/NATS backend.

### `authenticate_websocket(ws)`

Resolve the signed-in [`pyxle-auth`](../plugins/pyxle-auth.md) user for a
WebSocket upgrade, or `None`. The auth middleware never runs for WebSocket scope,
so a handler that needs the user must call this (it reads the session cookie off
the handshake). Returns `None` — zero database work — when the plugin isn't
installed or no cookie is present.

```python
from pyxle.realtime import authenticate_websocket

async def websocket(ws):
    user = await authenticate_websocket(ws)
    if user is None:
        await ws.close(code=4401)
        return
    await ws.accept()
    ...
```

### `origin_allowed(ws, allowed_origins)`

Whether the upgrade's `Origin` is permitted. CSRF doesn't apply to a WebSocket,
so checking the origin is the equivalent guard. An empty `allowed_origins`
allows all; a missing `Origin` header (same-origin / non-browser) is allowed.

## Document `<head>` elements

Pyxle offers two ways to contribute elements to the document `<head>`: the `<Head>` component (**recommended**) and the `HEAD` Python variable (lower-level alternative). Both are merged with automatic deduplication.

### `<Head>` component (recommended)

Imported from `pyxle/client`, usable anywhere in your JSX tree:

```jsx
import { Head } from 'pyxle/client';

export default function Page({ data }) {
  return (
    <>
      <Head>
        <title>{data.title}</title>
        <meta name="description" content={data.description} />
        <link rel="canonical" href={data.canonicalUrl} />
      </Head>
      {/* ... */}
    </>
  );
}
```

See [Head Management](../guides/head-management.md) for the full pattern (layouts, deduplication, reusable SEO components, etc.).

### `HEAD` variable (lower-level alternative)

Not part of `pyxle.runtime` but available in every `.pyxl` file. The `HEAD` variable is extracted at compile time by the parser; use it for fully-static head metadata that doesn't need React context, or for pages without a JSX component.

**Static form:**

```python
# String
HEAD = '<title>Page Title</title>'

# List of strings
HEAD = ['<title>Page Title</title>', '<meta name="description" content="..." />']
```

**Dynamic form (callable):**

```python
def HEAD(data):
    return f'<title>{data["title"]}</title>'
```

The callable receives the loader's return value. Must return a string or list of strings synchronously. For most dynamic head content, the `<Head>` component with normal JSX interpolation is simpler and more flexible.

## Explicit imports

While the compiler auto-injects `@server` and `@action`, you can also import explicitly:

```python
from pyxle.runtime import server, action, LoaderError, ActionError, ValidationActionError
```

This is useful for type checking and IDE support.
