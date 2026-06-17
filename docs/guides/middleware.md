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
    "apis": ["myapp.hooks:rate_limit"],
    "actions": ["myapp.hooks:require_auth_json"]
  }
}
```

The three lists wrap **page** handlers, **API** route functions, and **`@action`** endpoints respectively. They run through the same hook pipeline, so an `@action` POST is subject to its own policy chain instead of bypassing it.

> **Actions vs pages — return the right thing.** An action endpoint replies with JSON, so an action hook that rejects a request should return a JSON `401`/`403` (not an HTML redirect a page hook might use). That's why actions have a **separate** `actions` list rather than inheriting the page's hooks — protect a page *and* its actions, but with responses each side can parse.

> **Note:** Route hooks wrap function endpoints (`endpoint` in `pages/api/`), page handlers, and `@action` endpoints. [`HTTPEndpoint` classes](api-routes.md#using-httpendpoint-classes) are dispatched natively by Starlette and bypass route hooks — as do WebSocket handlers — so auth or rate-limiting for class-based endpoints must live in the endpoint methods themselves or in application-level middleware.

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

## Rate limiting

Pyxle ships a dependency-free **token-bucket** rate limiter, `pyxle.middleware.RateLimitMiddleware`. Enable it from `pyxle.config.json` — no code required:

```json
{
  "rateLimit": {
    "requests": 100,
    "window": 60,
    "exemptPaths": ["/health"],
    "trustForwardedFor": false
  }
}
```

Each client gets a bucket holding up to `requests` tokens that refills at `requests / window` tokens per second. A request spends one token; when the bucket is empty the request is rejected with `429 Too Many Requests` and a `Retry-After` header. This permits a short burst up to the capacity while bounding the sustained rate — friendlier than a hard fixed window, and it never blocks the event loop.

- **Client key** — the connection's remote IP, or the first `X-Forwarded-For` hop when `trustForwardedFor` is set. Only trust the header behind a proxy you control; otherwise a client can spoof it to dodge the limit.
- **Exempt paths** — `exemptPaths` skips the limiter on segment boundaries (same matching as `csrf.exemptPaths`). List health checks and metrics scrapes here so monitors aren't throttled.
- **Placement** — the limiter sits just inside request observability, so a throttled request is still assigned a correlation id and counted in metrics, but is rejected before CSRF, static serving, or your handler do any work.

**Multi-worker caveat.** The bucket store is in-memory and **per-process**. Under `pyxle serve --workers N` each worker enforces the limit independently, so the effective global cap is `N × requests`. When you need one shared limit across workers or hosts, rate-limit at your reverse proxy / load balancer instead. See the full schema in the [configuration reference](../reference/configuration.md#rate-limiting).

You can also apply it yourself in a custom ASGI stack:

```python
from pyxle.middleware import RateLimitMiddleware

app = RateLimitMiddleware(app, requests=100, window_seconds=60.0)
```

## Middleware execution order

From outermost to innermost:

1. Request observability (correlation id + timing, on by default)
2. Rate limiter (if `rateLimit.requests > 0`)
3. Custom middleware (from `middleware` config)
4. CORS middleware (if configured)
5. CSRF middleware (if enabled)
6. Vite proxy (dev mode)
7. Static file serving
8. Route hooks (from `routeMiddleware` config) — skipped for `HTTPEndpoint` classes and WebSocket handlers, which Starlette dispatches natively
9. Page/API handler

## Next steps

- Configure environment variables: [Environment Variables](environment-variables.md)
- Add CORS and CSRF: [Security](security.md)
