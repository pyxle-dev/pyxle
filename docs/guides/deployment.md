# Deployment

Deploy a Pyxle application with `pyxle build` to compile assets and `pyxle serve` to run in production.

## Build for production

```bash
pyxle build
```

This:

1. Compiles all `.pyxl` files into Python and JSX modules
2. Runs `npm run build` — Vite bundles JS and processes CSS through PostCSS (Tailwind is compiled in-pipeline when `postcss.config.cjs` is present, which is the scaffold default)
3. Outputs production artifacts to the `dist/` directory

### Build options

```bash
pyxle build --out-dir ./output     # Custom output directory
pyxle build --incremental          # Reuse cached artifacts
pyxle build --config ./custom.json # Custom config file
```

## Serve in production

```bash
pyxle serve
```

This starts a production Starlette server without Vite (static assets are served directly):

```bash
pyxle serve --host 0.0.0.0 --port 8000
```

### Serve options

| Flag | Default | Description |
|------|---------|-------------|
| `--host` | `127.0.0.1` | Bind address |
| `--port` | `8000` | Port number |
| `--dist-dir` | `dist/` | Directory with production artifacts |
| `--skip-build` | `false` | Skip running build first |
| `--serve-static/--no-serve-static` | `true` | Serve static assets directly |
| `--ssr-workers` | `1` | Number of persistent SSR worker processes |

### Build + serve in one step

By default, `pyxle serve` runs `pyxle build` first. Skip this with `--skip-build`:

```bash
# Build once, serve multiple times
pyxle build
pyxle serve --skip-build
```

## Environment configuration

Set production settings via environment variables:

```bash
export PYXLE_HOST=0.0.0.0
export PYXLE_PORT=8000
export PYXLE_DEBUG=false
pyxle serve --skip-build
```

Or in a `.env.production` file:

```bash
PYXLE_HOST=0.0.0.0
PYXLE_PORT=8000
PYXLE_DEBUG=false
PYXLE_PUBLIC_API_URL=https://api.production.com
```

## Reverse proxy setup

In production, place Pyxle behind a reverse proxy (Nginx, Caddy, etc.) for TLS termination, load balancing, and static asset caching.

### Nginx example

```nginx
server {
    listen 443 ssl;
    server_name example.com;

    ssl_certificate /etc/ssl/cert.pem;
    ssl_certificate_key /etc/ssl/key.pem;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    # Cache static assets
    location /client/ {
        proxy_pass http://127.0.0.1:8000;
        expires 1y;
        add_header Cache-Control "public, immutable";
    }
}
```

### Caddy example

```
example.com {
    reverse_proxy localhost:8000
}
```

## CDN and edge caching

Pages that render the same HTML for everyone -- a landing page, docs, marketing
routes -- don't need to hit your origin on every request. Declare them in the
[`cache`](../reference/configuration.md#edge-caching) block and Pyxle serves them
`Cache-Control: public, s-maxage=<seconds>` (with a `stale-while-revalidate`
window) so a CDN or reverse proxy absorbs the load. This is what lets a small
origin survive a traffic spike.

```json
{
  "cache": {
    "/": 60,
    "/docs/*": 300
  },
  "csrf": {
    "exemptPaths": ["/api/__actions/"]
  }
}
```

Two things to know before relying on it:

1. **A cacheable response carries no CSRF cookie**, because a shared cache must
   never replay one user's token to another (and most CDNs won't cache a
   response that sets a cookie). Any `@action` reachable from a cached route must
   therefore be CSRF-exempt -- hence the `csrf.exemptPaths` above. Only cache
   routes that render no per-user state and whose actions are safe to exempt.

2. **The headers make a response *eligible*; the CDN still has to opt in.** Many
   CDNs don't cache HTML by default:

   - **Cloudflare** — add a Cache Rule (or Page Rule) with *Cache Everything*
     for the cached paths. Cloudflare honors `s-maxage` for the edge TTL once the
     rule is in place. (Cloudflare ignores `Vary` headers other than
     `Accept-Encoding`; Pyxle's SPA navigation accounts for this and falls back
     to a normal full-page load if the edge ever serves cached HTML to an
     in-app navigation request, so nothing breaks.)
   - **Nginx / Caddy / Varnish** — enable `proxy_cache` (or the equivalent) for
     those routes; they respect `s-maxage` out of the box.

Content-hashed client bundles (under `/client/.../dist/assets/`) are already sent
`Cache-Control: public, max-age=31536000, immutable`, and other static files get
`public, max-age=3600` -- independent of the `cache` block above, which governs
*page* responses.

## Docker

```dockerfile
FROM python:3.12-slim

# Install Node.js
RUN apt-get update && apt-get install -y curl \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y nodejs \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN pip install --no-cache-dir pyxle-framework

# Install Node dependencies
COPY package.json package-lock.json* ./
RUN npm ci

# Copy application code
COPY . .

# Build for production
RUN pyxle build

# Run
EXPOSE 8000
CMD ["pyxle", "serve", "--host", "0.0.0.0", "--skip-build"]
```

## Health checks

The scaffold includes a health endpoint at `/api/pulse`:

```bash
curl http://localhost:8000/api/pulse
# {"status": "ok", ...}
```

Use this for load balancer health checks and monitoring.

## SSR workers

For production SSR performance, configure persistent Node.js workers:

```bash
pyxle serve --ssr-workers 4
```

Workers stay running between requests, avoiding subprocess startup overhead. Set to `0` for subprocess-per-request mode (simpler but slower).

## Checklist

Before deploying:

- [ ] `pyxle check` passes with no errors
- [ ] `pyxle build` completes successfully
- [ ] Set `PYXLE_DEBUG=false` in production
- [ ] Configure CSRF `cookieSecure: true` if using HTTPS
- [ ] Add CORS origins if serving APIs to other domains
- [ ] Set up a reverse proxy for TLS
- [ ] Configure health check monitoring on `/api/pulse`
- [ ] Add `.env.local` to `.gitignore`

## Next steps

- Full CLI reference: [CLI Commands](../reference/cli.md)
- Full config reference: [Configuration](../reference/configuration.md)
