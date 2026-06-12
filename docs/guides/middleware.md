# Middleware

Pyxle supports two levels of middleware: **application-level middleware** that wraps every request, and **route-level hooks** that run only for specific route types.

## Application-level middleware

Add Starlette-compatible middleware classes to your config:

```json
{
  "middleware": [
    "myapp.middleware:LoggingMiddleware",
    "myapp.middleware:TimingMiddleware"
  ]
}
```

Each entry is a string in `module.path:ClassName` format. The class must be a standard Starlette middleware or `BaseHTTPMiddleware` subclass.

### Writing a middleware

```python
# myapp/middleware.py
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

class TimingMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        import time
        start = time.perf_counter()
        response = await call_next(request)
        duration = time.perf_counter() - start
        response.headers["X-Response-Time"] = f"{duration:.3f}s"
        return response
```

Middleware is applied in the order listed in config. The first middleware in the list is the outermost wrapper.

## Route-level hooks

Route hooks run before and after specific route handlers. Configure them per route type:

```json
{
  "routeMiddleware": {
    "pages": ["myapp.hooks:require_auth"],
    "apis": ["myapp.hooks:rate_limit"]
  }
}
```

> **Note:** Route hooks wrap function endpoints (`endpoint` in `pages/api/`) and page handlers. [`HTTPEndpoint` classes](api-routes.md#using-httpendpoint-classes) are dispatched natively by Starlette and bypass route hooks — as do WebSocket handlers — so auth or rate-limiting for class-based endpoints must live in the endpoint methods themselves or in application-level middleware.

### Writing a route hook (function style)

```python
# myapp/hooks.py
from starlette.requests import Request
from starlette.responses import Response, JSONResponse

async def require_auth(context, request: Request, call_next):
    token = request.cookies.get("session")
    if not token:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    return await call_next(request)
```

The function receives three arguments:

1. `context` -- a `RouteContext` with metadata about the matched route
2. `request` -- the Starlette `Request`
3. `call_next` -- an async callable that invokes the next handler

### Writing a route hook (class style)

```python
from pyxle.devserver.route_hooks import RouteHook

class AuditHook(RouteHook):
    async def on_pre_call(self, request, context):
        # Runs before the handler
        print(f"Request to {context.path}")

    async def on_post_call(self, request, response, context):
        # Runs after the handler
        print(f"Response status: {response.status_code}")

    async def on_error(self, request, context, exc):
        # Runs when the handler raises
        print(f"Error: {exc}")
```

### RouteContext

The `context` object provides metadata about the matched route:

| Property | Type | Description |
|----------|------|-------------|
| `target` | `"page" \| "api"` | Route type |
| `path` | `str` | URL path pattern |
| `source_relative_path` | `Path` | File path relative to project root |
| `source_absolute_path` | `Path` | Absolute file path on disk |
| `module_key` | `str` | Python import key |
| `content_hash` | `str` | Hash of the compiled route module — changes when the source changes, stable across reloads |
| `has_loader` | `bool` | Whether the page has a `@server` loader |
| `head_elements` | `tuple[str, ...]` | Rendered `<head>` markup registered by the page/layout (SSR only) |
| `allowed_methods` | `tuple[str, ...]` | HTTP methods the route handles |

`RouteContext` is a frozen dataclass — fields are read-only. A shorthand
`context.as_dict()` returns a JSON-friendly view (keys camelCased, paths
as POSIX strings) that Pyxle attaches to `request.scope["pyxle"]["route"]`
for downstream middleware.

### Built-in hooks

Pyxle applies two default hooks:

- **`attach_route_metadata`** -- adds route info to `request.scope["pyxle"]["route"]`
- **`enforce_allowed_methods`** -- returns 405 for disallowed API methods

## Middleware execution order

From outermost to innermost:

1. Custom middleware (from `middleware` config)
2. CORS middleware (if configured)
3. CSRF middleware (if enabled)
4. Vite proxy (dev mode)
5. Static file serving
6. Route hooks (from `routeMiddleware` config) — skipped for `HTTPEndpoint` classes and WebSocket handlers, which Starlette dispatches natively
7. Page/API handler

## Next steps

- Configure environment variables: [Environment Variables](environment-variables.md)
- Add CORS and CSRF: [Security](security.md)
