# Configuration Reference

Pyxle is configured via `pyxle.config.json` in the project root. All fields are optional -- sensible defaults are used when omitted.

## Full schema

```json
{
  "pagesDir": "pages",
  "publicDir": "public",
  "buildDir": ".pyxle-build",
  "debug": true,
  "starlette": {
    "host": "127.0.0.1",
    "port": 8000
  },
  "vite": {
    "host": "127.0.0.1",
    "port": 5173
  },
  "middleware": [],
  "routeMiddleware": {
    "pages": [],
    "apis": [],
    "actions": []
  },
  "styling": {
    "globalStyles": [],
    "globalScripts": []
  },
  "cors": {
    "origins": [],
    "methods": ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    "headers": [],
    "credentials": false,
    "maxAge": 600
  },
  "csrf": {
    "enabled": true,
    "cookieName": "pyxle-csrf",
    "headerName": "x-csrf-token",
    "cookieSecure": false,
    "cookieSameSite": "lax",
    "exemptPaths": []
  },
  "cache": {},
  "navigation": {},
  "rateLimit": {
    "requests": 0,
    "window": 60,
    "exemptPaths": [],
    "trustForwardedFor": false
  },
  "observability": {
    "requestId": true,
    "requestIdHeader": "X-Request-Id",
    "trustIncomingRequestId": false,
    "timing": true,
    "metricsEndpoint": false,
    "metricsEndpointPath": "/api/__pyxle/metrics",
    "metricsEndpointToken": null,
    "accessLog": false,
    "logFormat": "console",
    "logLevel": "INFO",
    "otel": false,
    "otelServiceName": "pyxle-app",
    "otelSampleRatio": 0.05
  },
  "plugins": []
}
```

## Directory settings

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `pagesDir` | `string` | `"pages"` | Directory containing page routes and API files |
| `publicDir` | `string` | `"public"` | Directory for static assets served at `/` |
| `buildDir` | `string` | `".pyxle-build"` | Directory for compiled build artifacts |

## Server settings

### `starlette`

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `starlette.host` | `string` | `"127.0.0.1"` | Starlette server bind address |
| `starlette.port` | `integer` | `8000` | Starlette server port (1-65535) |

### `vite`

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `vite.host` | `string` | `"127.0.0.1"` | Vite dev server bind address |
| `vite.port` | `integer` | `5173` | Vite dev server port (1-65535) |

### `debug`

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `debug` | `boolean` | `true` | Enable debug mode (hot reload, detailed errors) |

## Middleware

### `middleware`

Application-level middleware classes applied to every request.

```json
{
  "middleware": [
    "myapp.middleware:LoggingMiddleware",
    "myapp.middleware:TimingMiddleware"
  ]
}
```

Each entry is a `"module.path:ClassName"` string pointing to a Starlette-compatible middleware class.

### `routeMiddleware`

Route-level hooks applied to specific route types.

```json
{
  "routeMiddleware": {
    "pages": ["myapp.hooks:require_auth"],
    "apis": ["myapp.hooks:rate_limit"],
    "actions": ["myapp.hooks:require_auth_json"]
  }
}
```

| Key | Type | Description |
|-----|------|-------------|
| `routeMiddleware.pages` | `string[]` | Hooks for page routes |
| `routeMiddleware.apis` | `string[]` | Hooks for API routes |
| `routeMiddleware.actions` | `string[]` | Hooks for `@action` endpoints. An action hook should reject with a JSON `401`/`403` (not an HTML redirect) — see the [Middleware guide](../guides/middleware.md#route-level-hooks). |

## Styling

### `styling`

```json
{
  "styling": {
    "globalStyles": ["styles/reset.css", "styles/typography.css"],
    "globalScripts": ["scripts/analytics.js"]
  }
}
```

| Key | Type | Description |
|-----|------|-------------|
| `styling.globalStyles` | `string[]` | CSS files inlined on every page (relative to project root) |
| `styling.globalScripts` | `string[]` | JS files loaded on every page (relative to project root) |

## CORS

```json
{
  "cors": {
    "origins": ["https://app.example.com"],
    "methods": ["GET", "POST"],
    "headers": ["Authorization"],
    "credentials": true,
    "maxAge": 3600
  }
}
```

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `cors.origins` | `string[]` | `[]` | Allowed origins. CORS is disabled if empty. |
| `cors.methods` | `string[]` | `["GET","POST","PUT","PATCH","DELETE","OPTIONS"]` | Allowed HTTP methods |
| `cors.headers` | `string[]` | `[]` | Allowed request headers |
| `cors.credentials` | `boolean` | `false` | Allow cookies and auth headers |
| `cors.maxAge` | `integer` | `600` | Preflight cache duration (seconds) |

## CSRF

```json
{
  "csrf": {
    "enabled": true,
    "cookieName": "pyxle-csrf",
    "headerName": "x-csrf-token",
    "cookieSecure": true,
    "cookieSameSite": "strict",
    "exemptPaths": ["/api/webhooks"]
  }
}
```

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `csrf.enabled` | `boolean` | `true` | Enable CSRF protection |
| `csrf.cookieName` | `string` | `"pyxle-csrf"` | CSRF cookie name |
| `csrf.headerName` | `string` | `"x-csrf-token"` | CSRF header name |
| `csrf.cookieSecure` | `boolean` | `false` | Set `Secure` flag on cookie |
| `csrf.cookieSameSite` | `string` | `"lax"` | `SameSite` attribute (`"strict"`, `"lax"`, `"none"`) |
| `csrf.exemptPaths` | `string[]` | `[]` | Paths exempt from CSRF checks (matched on segment boundaries) |

**Exemption matching.** An entry matches its exact path and anything beneath it at a `/` segment boundary: `/api/webhooks` exempts `/api/webhooks` and `/api/webhooks/stripe`, but not `/api/webhooks-admin`. Request paths are normalised first (`.`/`..` resolved, repeated slashes collapsed), so `/api/webhooks/../other` is checked as `/api/other`. An entry of `/` (or an empty string) is ignored rather than disabling CSRF site-wide — use `"csrf": false` to disable CSRF entirely.

Shorthand to disable: `"csrf": false`

## Edge caching

Mark page routes that render **no per-user data** as publicly cacheable. A matched page is served `Cache-Control: public, s-maxage=<seconds>` (plus a `stale-while-revalidate` window) instead of the default `private, no-cache`, so a shared cache -- a CDN or reverse proxy -- can absorb the traffic instead of your origin. It's Pyxle's config-driven equivalent of Next.js per-route revalidation.

```json
{
  "cache": {
    "/": 60,
    "/blog/*": 600,
    "/docs/*": { "sMaxage": 300 }
  }
}
```

Each key is a route pattern; each value is the shared-cache lifetime in seconds:

| Form | Example | Meaning |
|------|---------|---------|
| Integer shorthand | `"/": 60` | `s-maxage=60` for the exact path `/` |
| Prefix wildcard | `"/docs/*": 300` | `s-maxage=300` for `/docs` and anything beneath it |
| Object form | `"/": { "sMaxage": 60 }` | Same as the shorthand, with room to grow |

**Matching.** An exact path wins over a wildcard; among wildcards the longest (most specific) prefix wins. A pattern must be an absolute path. A wildcard (`/docs/*`) matches the bare prefix (`/docs`) and any segment beneath it (`/docs/guides/intro`), but not a path that merely shares the string (`/docsearch`).

**A cacheable response carries no CSRF cookie.** A per-user `Set-Cookie` must never ride on a response that a shared cache can replay to other users -- and most CDNs refuse to cache any response that sets a cookie at all. So on a cacheable route Pyxle omits the CSRF cookie, which means any `@action` reachable from that route must be exempt via [`csrf.exemptPaths`](#csrf). **Only mark routes that render no per-user state and whose actions are safe to exempt.**

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `cache` | `object` | `{}` | Map of route pattern → shared-cache lifetime. Empty = no shared caching (every page stays `private, no-cache`). |
| `cache."<pattern>"` | `integer` \| `{ "sMaxage": integer }` | -- | Lifetime in seconds (≥ 0) for the matched route. |

> **Turning it on at the edge.** These headers make a response *eligible* for shared caching, but some CDNs don't cache HTML by default. On Cloudflare, you must also add a Cache Rule ("Cache Everything") for the cached paths -- the headers alone are necessary but not sufficient. See [Deployment → CDN and edge caching](../guides/deployment.md#cdn-and-edge-caching).

## Navigation

Pyxle's client keeps an in-memory **navigation cache** so client-side navigation -- and the data prefetched when a `<Link>` scrolls into view or is hovered -- resolves instantly instead of refetching the loader. The page you land on is seeded into this cache from the server render, so its loader never runs a second time on the initial load.

```json
{
  "navigation": {
    "defaultPrefetchTtl": 120
  }
}
```

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `navigation` | `object` | `{}` | Client navigation/prefetch cache settings. |
| `navigation.defaultPrefetchTtl` | `integer` | `120` | Lifetime in seconds (≥ 0) that a prefetched or seeded page stays fresh in the client navigation cache, for routes **without** a `cache` entry. `0` disables navigation caching. |

A route listed in the [`cache`](#edge-caching) block **reuses its edge-cache TTL** as its navigation-cache lifetime, overriding `defaultPrefetchTtl` for that route -- so a page's client freshness matches how long a CDN would serve it. (Consequence: raising a route's `cache` TTL also lengthens its edge cache, so purge the CDN on deploy -- which you should do regardless.) After a mutation, the client router's `invalidate(href)` drops a cached entry so the next navigation refetches.

## Rate limiting

A built-in [token-bucket](../guides/middleware.md#rate-limiting) limiter. **Disabled by default** (`requests: 0`). Set `requests` to a positive number to cap how fast a single client may call the app.

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

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `rateLimit.requests` | `integer` | `0` | Bucket capacity = max burst. `0` disables the limiter. |
| `rateLimit.window` | `number` | `60` | Seconds for a full bucket to refill, so the sustained rate is `requests / window` per second. |
| `rateLimit.exemptPaths` | `string[]` | `[]` | Paths skipped by the limiter, matched on segment boundaries (same rules as [`csrf.exemptPaths`](#csrf)). Use for health checks and metrics scrapes. |
| `rateLimit.trustForwardedFor` | `boolean` | `false` | Key clients by the first `X-Forwarded-For` hop instead of the socket IP. **Only enable behind a trusted proxy** — a client can otherwise spoof the header to dodge the limit. |

**Burst vs. sustained.** `requests` is the burst a client can spend at once; the bucket then refills at `requests / window` per second, so the long-run average is bounded without rejecting brief spikes. A blocked request gets `429 Too Many Requests` with a `Retry-After` header.

**Multi-worker caveat.** The bucket store is in-memory and **per-process**, so under `pyxle serve --workers N` each worker enforces the limit independently and the effective global cap is `N × requests`. For a single shared limit, rate-limit at your reverse proxy or load balancer. See the [middleware guide](../guides/middleware.md#rate-limiting).

## Observability

Request correlation IDs and timing. Both are **on by default** -- generating an id and reading two timestamps per request is sub-microsecond and adds no I/O, and every request gets a correlation key surfaced as the response header and on `request.state.request_id` (readable from any loader or `@action`). See the [Observability guide](../guides/observability.md).

```json
{
  "observability": {
    "requestId": true,
    "requestIdHeader": "X-Request-Id",
    "trustIncomingRequestId": false,
    "timing": true,
    "metricsEndpoint": false,
    "metricsEndpointPath": "/api/__pyxle/metrics",
    "metricsEndpointToken": null
  }
}
```

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `observability` | `object` \| `boolean` | `{}` | Request observability settings. `false` disables request-id and timing; `true` enables both. |
| `observability.requestId` | `boolean` | `true` | Assign each request a correlation id, expose it on `request.state.request_id`, and echo it as the response header. |
| `observability.requestIdHeader` | `string` | `"X-Request-Id"` | The request/response header carrying the correlation id. |
| `observability.trustIncomingRequestId` | `boolean` | `false` | Honour a well-formed incoming id from `requestIdHeader` instead of generating one. **Leave off** unless behind a trusted reverse proxy -- echoing a client-supplied id is a log-injection/spoofing vector. Even when on, an incoming id must match `[A-Za-z0-9._-]{1,128}` or it is replaced. |
| `observability.timing` | `boolean` | `true` | Measure wall-clock request duration (to the response start). |
| `observability.metricsEndpoint` | `boolean` | `false` | Expose a Prometheus metrics endpoint (request/render/loader/action durations and page-cache hit ratio). **Off by default** -- it exposes internal state. |
| `observability.metricsEndpointPath` | `string` | `"/api/__pyxle/metrics"` | Path the metrics endpoint is served at when enabled. |
| `observability.metricsEndpointToken` | `string` \| `null` | `null` | Optional bearer token: when set, the endpoint requires `Authorization: Bearer <token>` (compared in constant time). |
| `observability.accessLog` | `boolean` | `false` | Emit one structured access-log line per request (`method`, `path`, `status`, `duration_ms`, `request_id`). Off by default so it doesn't surprise existing log scrapers. |
| `observability.logFormat` | `string` | `"console"` | `"console"` (human-readable) or `"json"` (one JSON object per line). |
| `observability.logLevel` | `string` | `"INFO"` | Access-logger level (`CRITICAL`/`ERROR`/`WARNING`/`INFO`/`DEBUG`). |
| `observability.otel` | `boolean` | `false` | Emit OpenTelemetry spans for requests, SSR renders, loaders, and actions. Requires the `[observability-otel]` extra; with it absent, enabling this fails loudly at startup. |
| `observability.otelServiceName` | `string` | `"pyxle-app"` | `service.name` resource attribute for the tracer. |
| `observability.otelSampleRatio` | `number` | `0.05` | Trace sampling ratio (0.0–1.0). Low by default so tracing can't swamp a busy server. The exporter endpoint comes from the standard `OTEL_EXPORTER_OTLP_ENDPOINT` env var. |

Shorthand to disable request-id and timing: `"observability": false`. The page-cache hit-ratio metric requires the [server-side page cache](../guides/caching.md) (active under `pyxle serve`). **Multi-worker note:** under `pyxle serve --workers N` each worker process exposes its own metrics (with a `worker` label), so aggregate across workers at the scraper.

## Plugins

### `plugins`

```json
{
  "plugins": [
    "pyxle-db",
    {
      "name": "pyxle-auth",
      "module": "pyxle_auth.plugin",
      "attribute": "plugin",
      "settings": {
        "cookieDomain": ".pyxle.app",
        "strict": true
      }
    }
  ]
}
```

Each entry declares one plugin. Pyxle imports each plugin, calls its `on_startup` hook inside the ASGI lifespan, and exposes the services they register to loaders and actions.

Entries can be either a bare string or an object:

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `name` | `string` | *required* | User-facing plugin name. Shown in error messages and used to namespace registered services. |
| `module` | `string` | derived from `name` | Python module containing the plugin export. Default transforms `pyxle-foo` → `pyxle_foo.plugin`. Override for non-conventional package layouts. |
| `attribute` | `string` | `"plugin"` | Attribute on `module` that holds the `PyxlePlugin` class or instance. |
| `settings` | `object` | `{}` | Plugin-specific configuration dict. The framework doesn't validate the shape — each plugin documents its own settings keys. |

**Plugin ordering matters.** Plugins are started in the order you list them. List a plugin *before* any plugin that depends on it — e.g. `pyxle-db` before `pyxle-auth`, since auth needs a database.

See the [Plugins guide](../guides/plugins.md) for authoring and consuming patterns, and [Plugins API reference](plugins-api.md) for the full API.

**First-party plugins:**

- [`pyxle-db`](../plugins/pyxle-db.md) — SQLite-first database with migrations
- [`pyxle-auth`](../plugins/pyxle-auth.md) — Email+password session auth

## Environment variable overrides

These environment variables override config file values:

| Variable | Overrides | Type |
|----------|-----------|------|
| `PYXLE_HOST` | `starlette.host` | string |
| `PYXLE_PORT` | `starlette.port` | integer |
| `PYXLE_VITE_HOST` | `vite.host` | string |
| `PYXLE_VITE_PORT` | `vite.port` | integer |
| `PYXLE_DEBUG` | `debug` | `"true"`, `"1"`, `"yes"` / `"false"`, `"0"`, `"no"` |
| `PYXLE_PAGES_DIR` | `pagesDir` | string |
| `PYXLE_PUBLIC_DIR` | `publicDir` | string |
| `PYXLE_BUILD_DIR` | `buildDir` | string |

## Precedence

From lowest to highest priority:

1. Pyxle defaults
2. `pyxle.config.json`
3. `.env` files
4. `PYXLE_*` environment variables
5. CLI flags (`--host`, `--port`, etc.)

## Validation

Pyxle validates your config file at startup:

- Unknown keys are rejected with an error listing the invalid keys
- Port numbers must be between 1 and 65535
- Directory values must be non-empty strings
- Middleware entries must be non-empty `"module:Class"` strings
- CORS and CSRF sub-fields are type-checked
- Cache routes must be absolute-path patterns mapping to a non-negative integer lifetime
