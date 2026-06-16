# Environment Variables

Pyxle loads environment variables from `.env` files and supports special prefixes for configuration overrides and client-side injection.

## `.env` files

Create `.env` files in your project root:

```bash
# .env -- base defaults (commit to source control)
DATABASE_URL=postgres://localhost/mydb
API_KEY=dev-key-123
PYXLE_PUBLIC_APP_NAME=My App
```

### Load order

Files are loaded in order of increasing precedence. Later files override earlier ones:

1. `.env` -- base defaults
2. `.env.development` or `.env.production` -- mode-specific
3. `.env.local` -- local overrides (gitignore this)
4. `.env.development.local` or `.env.production.local` -- local mode overrides

The mode is `development` when running `pyxle dev` and `production` when running `pyxle build` or `pyxle serve`.

### The shell always wins

Variables already set in your shell environment are **never** overwritten by `.env` files. This means CI/CD environment variables and Docker env take precedence.

### Syntax

```bash
# Simple values
KEY=value

# Quoted values
KEY="value with spaces"
KEY='literal value (no escape processing)'

# Optional export prefix
export KEY=value

# Comments
# This is a comment
KEY=value  # Inline comments work too (unquoted values only)
```

Double-quoted values support escape sequences: `\"`, `\n`, `\r`, `\t`, `\\`.

## `PYXLE_PUBLIC_` prefix

Variables starting with `PYXLE_PUBLIC_` are injected into client-side JavaScript at build time:

```bash
# .env
PYXLE_PUBLIC_API_URL=https://api.example.com
PYXLE_PUBLIC_APP_VERSION=1.2.3
```

Access them in your JSX via `import.meta.env`:

```jsx
export default function Page() {
  return <p>API: {import.meta.env.PYXLE_PUBLIC_API_URL}</p>;
}
```

**Server-only variables** (without the `PYXLE_PUBLIC_` prefix) are **never** exposed to the client. They are only available via `os.environ` in your Python code.

## `PYXLE_` config overrides

These environment variables override settings from `pyxle.config.json`:

| Variable | Config field | Example |
|----------|-------------|---------|
| `PYXLE_HOST` | `starlette.host` | `0.0.0.0` |
| `PYXLE_PORT` | `starlette.port` | `9000` |
| `PYXLE_VITE_HOST` | `vite.host` | `localhost` |
| `PYXLE_VITE_PORT` | `vite.port` | `3000` |
| `PYXLE_DEBUG` | `debug` | `true` or `false` |
| `PYXLE_PAGES_DIR` | `pagesDir` | `src/pages` |
| `PYXLE_PUBLIC_DIR` | `publicDir` | `static` |
| `PYXLE_BUILD_DIR` | `buildDir` | `.cache` |

### Precedence

From lowest to highest:

1. Defaults in Pyxle
2. `pyxle.config.json`
3. `.env` files
4. `PYXLE_` environment variables
5. CLI flags (`--host`, `--port`, etc.)

## Page cache backend

These select where the server-side [page cache](caching.md) stores rendered
HTML. The default needs no configuration.

| Variable | Meaning |
|----------|---------|
| `PYXLE_PAGE_CACHE_BACKEND` | `memory` (default), `file`, `redis`, or `off` |
| `PYXLE_PAGE_CACHE_MAX_ENTRIES` | in-memory: max entries before LRU eviction (default `512`) |
| `PYXLE_PAGE_CACHE_MAX_BYTES` | in-memory: max total body bytes (default `67108864`, 64 MiB) |
| `PYXLE_PAGE_CACHE_DIR` | file backend: directory for cache files (required for `file`) |
| `PYXLE_PAGE_CACHE_REDIS_URL` | redis backend: connection URL (falls back to `REDIS_URL`) |

The cache is active only for production serves (`pyxle serve`); `pyxle dev`
never caches. The `redis` backend needs `pip install 'pyxle[redis]'`.

## Using environment variables in loaders

```python
import os

@server
async def load_page(request):
    api_key = os.environ.get("API_KEY", "")
    # Use api_key to call an external service
    return {"data": "..."}
```

## `.gitignore` recommendations

```gitignore
# Commit these
.env
.env.development
.env.production

# Do NOT commit these (contain secrets)
.env.local
.env.*.local
```

## Next steps

- Handle errors gracefully: [Error Handling](error-handling.md)
- Secure your app: [Security](security.md)
