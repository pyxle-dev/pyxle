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

In production mode (`debug=false`) the server also compresses responses larger
than 500 bytes with gzip automatically — no reverse-proxy configuration needed
for that. The gzip middleware is **streaming-aware** (it flushes the compressor
per chunk), so [streaming SSR](streaming.md) still delivers the shell first
behind gzip in production rather than buffering the whole response.

### Serve options

| Flag | Default | Description |
|------|---------|-------------|
| `--host` | `127.0.0.1` | Bind address |
| `--port` | `8000` | Port number |
| `--workers` / `-w` | `1` | Server worker processes — one per CPU core ([multi-core](#multi-core-worker-processes)) |
| `--ssr-workers` | `1` | Persistent Node.js SSR processes, **per server worker** (`0` = auto-size to CPU cores, capped at 4) |
| `--dist-dir` | `dist/` | Directory with production artifacts |
| `--skip-build` | `false` | Skip running build first |
| `--serve-static/--no-serve-static` | `true` | Serve static assets directly (disable when a CDN hosts them) |
| `--config` | `pyxle.config.json` | Path to an alternate config file |

### Build + serve in one step

By default, `pyxle serve` runs `pyxle build` first. Skip this with `--skip-build`:

```bash
# Build once, serve multiple times
pyxle build
pyxle serve --skip-build
```

## Multi-core (worker processes)

By default `pyxle serve` runs a **single** async server process, which uses one
CPU core. To use every core on a multi-core server, run one server worker
process per core with `--workers` (requires Pyxle 0.4.3+):

```bash
pyxle serve --workers $(nproc)   # explicit
pyxle serve --workers 0          # auto-detect from CPU cores (one per core)
```

`--workers 0` auto-detects the core count, so you don't have to hard-code it for
the target host. Each worker is an independent server process with its own SSR worker pool, all
sharing one listening socket — incoming connections are balanced across them
with no load balancer and no shared state to configure. Throughput on
CPU-bound endpoints scales near-linearly with the worker count.

`pyxle serve` builds the project once before the workers start, so the build is
never duplicated. Combine `--workers` with `--skip-build` only when `dist/`
already exists. Workers reconstruct their configuration from `PYXLE_SERVE_*`
environment variables exported by the parent process — these are internal;
don't set them yourself.

### SSR workers

Server-side rendering runs in persistent Node.js processes that stay warm
between requests:

```bash
pyxle serve --ssr-workers 2
```

`--ssr-workers` applies **per server worker**, so the total number of Node.js
render processes is `workers × ssr-workers`. For an SSR-heavy app,
`--workers $(nproc) --ssr-workers 1` is a good starting point — scale
`--ssr-workers` up only if profiling shows render queueing. Passing `0`
auto-sizes the pool to the machine's CPU count (capped at 4) per server worker.

### Sizing guidance

| Workload | Suggestion |
|----------|------------|
| API-heavy, little SSR | `--workers $(nproc)` |
| SSR-heavy pages | `--workers $(nproc) --ssr-workers 1`, raise SSR workers if renders queue |
| Small VPS (1–2 cores) | Defaults (`--workers 1`) are fine |
| Memory-constrained | Each worker is a full process — reduce `--workers` before reducing `--ssr-workers` |

### Per-worker state (multi-worker caveats)

Each worker is a **separate process with no shared memory**, which is what makes
multi-core serving trivial to operate — there's nothing to coordinate. The
trade-off is that any **in-process** state is per-worker:

| Feature | Per-worker behaviour | For cross-worker behaviour |
|---------|----------------------|----------------------------|
| [Page cache](caching.md) | Each worker has its own in-memory cache | Use the **Redis** backend (`PYXLE_PAGE_CACHE_BACKEND=redis`) — shared across workers and hosts |
| [WebSocket pub/sub](websockets.md) | Messages reach only clients on the same worker | Use the **Redis** broker (`PYXLE_REALTIME_BROKER=redis`) — relays channels across workers and hosts |
| [Metrics](observability.md) | `/metrics` reports that worker's numbers (with a `worker` label) | Aggregate at the Prometheus scraper |
| [Background tasks](background-tasks.md) | `pyxle.tasks` queue is per-worker | Use a real job queue (Celery / ARQ / Dramatiq) |

This is by design: Pyxle keeps the default path stateless so workers fork
cleanly and scale linearly. The escape hatch is always a shared backend (Redis
for cache/pub-sub) or an external service (a job queue, a Prometheus scraper),
not a shared in-process resource — so the same code runs at one worker or fifty.

> **Why not a single shared SSR pool across workers?** A shared Node pool over a
> Unix socket would save some memory but add a coordination point and a single
> contention bottleneck, undermining the share-nothing model. Per-worker pools
> stay simple and isolated — a crashed render can't affect another worker. Size
> with `--ssr-workers` instead.

### Rolling deploys & graceful restart

For zero-downtime deploys, don't restart Pyxle in place — drain and replace:

1. **Behind a load balancer / reverse proxy:** start a new instance (new
   `pyxle serve`) on a second port, wait for `/readyz` to return `200` (see
   [Observability → health probes](observability.md#health-probes)), shift
   traffic to it, then stop the old one. This gives true zero-downtime.
2. **Single host, process manager:** run `pyxle serve` under **systemd** (or
   supervisor). A `systemctl restart` cleanly stops the old workers (uvicorn
   handles `SIGTERM`, finishing in-flight requests) and starts new ones — a
   sub-second blip, fine for most apps. Point your readiness probe at `/readyz`
   so the proxy only routes once a worker is actually ready.
**Blue-green (option 1) is the recommended zero-downtime path** — and it
sidesteps the question of preloading entirely.

> **A note on preloading.** `pyxle serve` imports each worker's app *after*
> forking (uvicorn's model), so there's no copy-on-write sharing of read-only
> state between workers. In practice this rarely matters — Python's per-process
> memory is dominated by the interpreter and the SSR Node pool, not your page
> modules. If you specifically need import-once-then-fork, run the app under a
> process manager that supports it (e.g. **gunicorn** with
> `--worker-class uvicorn.workers.UvicornWorker --preload`), pointing at the
> `pyxle.build.production:create_app` factory with `PYXLE_SERVE_PROJECT_ROOT`
> set to your project directory — gunicorn then owns worker lifecycle and
> graceful rolling restarts. This is an advanced setup; the blue-green approach
> above is simpler and gives true zero-downtime.

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

| Variable | Overrides |
|----------|-----------|
| `PYXLE_HOST` | Bind address |
| `PYXLE_PORT` | Port number |
| `PYXLE_DEBUG` | Debug mode (`true`/`1` or `false`/`0`) |
| `PYXLE_PAGES_DIR` | Pages directory |
| `PYXLE_PUBLIC_DIR` | Static assets directory |
| `PYXLE_BUILD_DIR` | Build output directory |

CLI flags win over environment variables, which win over `pyxle.config.json`.
Variables prefixed `PYXLE_PUBLIC_` are exposed to client code — never put
secrets in them.

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

Match `--workers` to the container's CPU allocation — e.g.
`CMD ["pyxle", "serve", "--host", "0.0.0.0", "--skip-build", "--workers", "4"]`
for a 4-vCPU container. If you scale by running more single-worker containers
behind a load balancer instead, keep the default.

## Health checks

The scaffold includes a health endpoint at `/api/pulse`:

```bash
curl http://localhost:8000/api/pulse
# {"status": "ok", ...}
```

Use this for load balancer health checks and monitoring.

## Process management (systemd)

On a VM, run `pyxle serve` under a process supervisor so it restarts on
failure and starts on boot:

```ini
# /etc/systemd/system/myapp.service
[Unit]
Description=My Pyxle app
After=network.target

[Service]
User=app
WorkingDirectory=/srv/myapp
Environment=PYXLE_DEBUG=false
ExecStart=/srv/myapp/.venv/bin/pyxle serve --skip-build --workers 4 \
    --host 127.0.0.1 --port 8000
Restart=always

[Install]
WantedBy=multi-user.target
```

Build during deployment (`pyxle build`), then `systemctl restart myapp` —
with `--skip-build` the restart is fast because the unit only boots the
server. Node.js must be on the service's `PATH` for SSR.

## Checklist

Before deploying:

- [ ] `pyxle check` passes with no errors
- [ ] `pyxle build` completes successfully
- [ ] Set `PYXLE_DEBUG=false` in production
- [ ] Size `--workers` to the machine's CPU cores (multi-core serving)
- [ ] Configure CSRF `cookieSecure: true` if using HTTPS
- [ ] Add CORS origins if serving APIs to other domains
- [ ] Set up a reverse proxy for TLS
- [ ] Declare publicly-cacheable routes in the `cache` block (and opt your CDN in)
- [ ] Configure health check monitoring on `/api/pulse`
- [ ] Add `.env.local` to `.gitignore`

## Next steps

- Full CLI reference: [CLI Commands](../reference/cli.md)
- Full config reference: [Configuration](../reference/configuration.md)
