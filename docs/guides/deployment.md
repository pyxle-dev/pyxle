# Deployment

Deploy a Pyxle application with `pyxle build` to compile assets and `pyxle serve` to run in production.

## Build for production

```bash
pyxle build
```

This:

1. Compiles all `.pyxl` files into Python and JSX modules
2. Runs a Vite production build — bundling JS and compiling every imported stylesheet (plain CSS, CSS Modules, and Tailwind v4 via the `@tailwindcss/vite` plugin when enabled) into content-hashed assets
3. Outputs production artifacts to the `dist/` directory

> **`pyxle build` requires Node.js 20.19+ *and* npm**, plus the project's
> `package.json` and its installed dependencies — step 2 runs Vite through
> `npx`. This bites most often in a container or CI image: Debian and Ubuntu's
> `nodejs` package does **not** include npm (`apt install npm` as well), and
> some slim base images ship `node` alone. Without them the build stops with a
> non-zero exit rather than producing a `dist/` that `pyxle serve` cannot start
> on. Serving the result needs Node.js too, but not npm or Vite — see
> [What to ship](#what-to-ship).

### Build options

```bash
pyxle build --out-dir ./output     # Custom output directory
pyxle build --incremental          # Reuse cached artifacts
pyxle build --config ./custom.json # Custom config file
```

### What to ship

`dist/` is self-contained: it holds every compiled module, every bundled asset,
the routing metadata `pyxle serve` needs, and a copy of the source files the
running server reads. Ship it alongside your `pyxle.config.json`, your
`node_modules` (server-side rendering runs React on Node) and your Python
dependencies, and `pyxle serve --skip-build` will run it.

A minimal deployment is therefore:

```
dist/                 # everything pyxle build produced
pyxle.config.json     # your configuration
package.json          # so Node resolves react / react-dom
node_modules/         # server-side rendering runs React on Node — and esbuild
```

> **Installing with `--omit=dev` is not enough on its own.** Server rendering
> loads **esbuild** from your project at request time. Before 0.9.0 the scaffold
> did not declare it — it arrived only transitively through Vite, a
> devDependency — so a production-only install produced a server that started
> cleanly and then returned a 500 for *every* page, with the real cause visible
> only in the log. 0.9.0's scaffold lists `esbuild` under `dependencies`; if you
> generated your project with an earlier version, add it.


plus your Python dependencies and your environment variables — including
`PYXLE_SECRET_KEY`, which `pyxle serve` requires. `.env` files are **not** part
of `dist/`: secrets belong in the environment, not in a build artifact.

Inside `dist/`:

| Path | What it holds |
|------|---------------|
| `server/` | Compiled Python for every page and API route |
| `client/dist/` | Vite output — hashed JS and CSS, served under `/client/dist`. It is the only part of `client/` that is served; the rest is the input Vite bundled |
| `metadata/`, `meta.json`, `page-manifest.json` | Routing metadata |
| `public/` | A copy of your `public/` directory |
| `app/` | A copy of the source files the server reads at runtime |
| `prerendered/` | Pre-rendered pages, when you build with `--static` |

`dist/app/` is there because compiling `pages/` produces one module per route
and nothing else, while a running server still reads plain source files:

- **Colocated Python helpers.** `pages/api/_shared.py`, `pages/api/__init__.py`,
  anything under `pages/api/_internal/`, and non-route modules beside your pages
  such as `pages/s/[slug]/queries.py` are deliberately
  [not routes](api-routes.md#private-modules-inside-an-api-directory) — so
  nothing compiles them, and the compiled route that says
  `from pages.api._shared import …` needs the module itself at import time.
- **AI-accessibility files.** `pages/**/llms.py` handlers and colocated
  `pages/**/*.md` files are read per request — see [AI accessibility](llms.md).
- **Global stylesheets.** `styling.globalStyles` entries are read and inlined
  into every rendered document.

Because they travel with the build, a deployment that ships only `dist/` — a
Docker `COPY --from=build /app/dist ./dist`, a CI artifact, an rsync of the
build output — boots and serves exactly like one that carries the whole
repository. When your source tree *is* deployed it stays authoritative; the copy
is only a fallback, so editing a helper and rebuilding never picks up a stale
one.

What `dist/` cannot carry is application code **outside** `pages/`: a
project-root `db.py`, a `middleware.py` named in `pyxle.config.json`, a local
package your loaders import. Where that import graph ends is your application's
business, not the framework's, so deploy those files the way you deploy the rest
of your code (in a Dockerfile, copy your Python sources alongside `dist/`).
Third-party packages come from your Python dependencies as usual.

`.pyxle-build/` is an **intermediate** directory — a build cache for
`pyxle dev` and for the next `pyxle build`. It is not part of a deployment, and
`pyxle serve` does not read routing from it. That matters beyond disk space: it
is a cache, so anything that recompiles a page into it — a test helper, an
editor tool, an interrupted dev server — leaves it disagreeing with the `dist/`
you built. Serving reads `dist/`, so those disagreements cannot reach
production.

## Serve in production

```bash
pyxle serve
```

`pyxle serve` **rebuilds before serving** by default, so you can never serve a
stale `dist/` by accident. On a deploy target that already has a built `dist/` —
the usual case — skip that step:

```bash
pyxle serve --skip-build
```

That skips the Vite build (no `npx` needed on the deploy host) and starts in
about two seconds instead of rebuilding. **Node.js is still required**:
server-side rendering runs React on Node, so keep shipping `node_modules` as
described under [What to ship](#what-to-ship). Without Node on `PATH` the server
refuses to start rather than serving broken pages.

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
| `--ssr-workers` | `auto` | Persistent Node.js SSR processes, **per server worker** (`0`/`auto` = size to CPU cores, capped at 4). `pyxle serve` defaults to auto; pass a number to pin it |
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
between requests. `pyxle serve` **auto-sizes** the pool by default — the pool
grows to the machine's CPU count, capped at 4, per server worker — so you don't
have to think about it for most deployments:

```bash
pyxle serve                 # auto-sized SSR pool (default)
pyxle serve --ssr-workers 2 # pin the pool to exactly 2 processes per server worker
```

`--ssr-workers` applies **per server worker**, so the total number of Node.js
render processes is `workers × ssr-workers`. Passing `0` (or omitting the flag)
auto-sizes the pool to the machine's CPU count (capped at 4) per server worker.

#### Concurrency within a worker

Each SSR worker handles **many renders concurrently**, not one at a time. This
matters most for [streaming SSR](streaming.md): a streaming render spends almost
all of its wall-clock time idle, awaiting loader promises and `<Suspense>`
boundaries. The worker interleaves those idle windows, so overlapping requests
to a streaming page all start rendering immediately instead of queueing behind
each other. Per-request state (pathname, CSRF token, `<Head>` tags, styles) is
isolated per render, so interleaving never mixes one visitor's page into
another's.

The in-worker concurrency cap defaults to 16 in-flight renders and is tunable
with the `PYXLE_SSR_WORKER_CONCURRENCY` environment variable — raise it only for
a workload dominated by slow, I/O-bound loaders where renders sit idle waiting
on the network. Because the cap governs *idle* concurrency, adding SSR
**processes** (`--ssr-workers`) is what adds CPU-parallel render throughput.

### Sizing guidance

| Workload | Suggestion |
|----------|------------|
| API-heavy, little SSR | `--workers $(nproc)` |
| SSR-heavy / streaming pages | Defaults are fine — the auto-sized pool renders concurrently. Raise `--ssr-workers` only if CPU-bound renders queue |
| Small VPS (1–2 cores) | Defaults (`--workers 1`, auto SSR pool) are fine |
| Memory-constrained | Each worker is a full process — reduce `--workers` before reducing `--ssr-workers` |

### Per-worker state (multi-worker caveats)

Each worker is a **separate process with no shared memory**, which is what makes
multi-core serving trivial to operate — there's nothing to coordinate. The
trade-off is that any **in-process** state is per-worker:

| Feature | Per-worker behaviour | For cross-worker behaviour |
|---------|----------------------|----------------------------|
| [Page cache](caching.md) | Each worker has its own in-memory cache | Use the **Redis** backend (`PYXLE_PAGE_CACHE_BACKEND=redis`) — shared across workers and hosts |
| [WebSocket pub/sub](websockets.md) | Messages reach only clients on the same worker | Use the **Redis** broker (`PYXLE_REALTIME_BROKER=redis`) — relays channels across workers and hosts |
| [Metrics](observability.md) | `/api/__pyxle/metrics` (opt-in via `metricsEndpoint: true`) reports that worker's numbers (with a `worker` label) | Aggregate at the Prometheus scraper |
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

> **Blue-green gotcha — pin the CSRF cookie name and share the secret.** The two
> instances run on **different ports**, and by default the CSRF cookie is named
> `pyxle-csrf-<port>` (per-port, so multiple apps on one host don't collide). In
> a blue-green cutover that backfires: a token the blue instance issued as
> `pyxle-csrf-8000` is rejected by green on `pyxle-csrf-8001`, so mid-shift POSTs
> `403`. Pin an explicit, shared name so both instances agree:
> ```json
> { "csrf": { "cookieName": "pyxle-csrf" } }
> ```
> Both instances must also share the **same `PYXLE_SECRET_KEY`** — a token
> signed by one is only valid on the other if the signing key matches.

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
export PYXLE_SECRET_KEY=$(python -c "import secrets; print(secrets.token_hex(32))")
pyxle serve --skip-build
```

> **`PYXLE_SECRET_KEY` is required in production.** It signs CSRF tokens and any
> signed cookies (sessions, unsubscribe links). `pyxle serve` **refuses to
> start** without it (unless you've set `csrf.enabled=false`). Generate a long
> random value once and keep it stable — store it in your process manager's
> environment or a secrets manager, never in the repo. Rotating it invalidates
> outstanding tokens and signed links.

Or in a `.env.production` file:

```bash
PYXLE_HOST=0.0.0.0
PYXLE_PORT=8000
PYXLE_DEBUG=false
PYXLE_PUBLIC_API_URL=https://api.production.com
```

| Variable | Overrides |
|----------|-----------|
| `PYXLE_SECRET_KEY` | Signing secret for CSRF tokens and signed cookies (**required in production**) |
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
# Maps the Upgrade header so a WebSocket request sends "Connection: upgrade"
# while an ordinary request sends "Connection: close".
map $http_upgrade $connection_upgrade {
    default upgrade;
    ''      close;
}

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

        # Required if any page exposes a `websocket` handler / uses useWebSocket.
        # Without these, nginx proxies the wss:// handshake as a plain GET and the
        # connection silently never upgrades (the client just sees the page HTML).
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection $connection_upgrade;
        proxy_read_timeout 3600s;   # keep long-lived WebSockets from idling out
    }

    # Cache static assets. Everything Pyxle serves here is a content-hashed
    # bundle, so a one-year immutable cache is safe.
    location /client/dist/ {
        proxy_pass http://127.0.0.1:8000;
        expires 1y;
        add_header Cache-Control "public, immutable";
    }
}
```

> **WebSocket apps:** the `proxy_http_version 1.1` + `Upgrade`/`Connection` lines
> above are what let `wss://` connections through. A reverse proxy that omits them
> serves the page fine but every WebSocket silently fails to connect. (Browsers do
> the WebSocket handshake over HTTP/1.1 even when the listener also speaks HTTP/2 —
> that's expected; the headers above handle it.)

### Caddy example

```
example.com {
    reverse_proxy localhost:8000
}
```

Caddy's `reverse_proxy` upgrades WebSocket connections automatically — no extra
configuration is needed.

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

Content-hashed client bundles (under `/client/dist/assets/`) are already sent
`Cache-Control: public, max-age=31536000, immutable`, and other static files get
`public, max-age=3600` -- independent of the `cache` block above, which governs
*page* responses.

## Docker

```dockerfile
FROM python:3.12-slim

# Install Node.js — Pyxle requires Node.js >= 20.19 (Vite 7). The 22.x LTS
# line satisfies that; the older setup_20.x stream can ship 20.16–20.18, which
# Vite 7 rejects at startup, so use 22.x (or a 20.x image pinned to >= 20.19).
RUN apt-get update && apt-get install -y curl \
    && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
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

### Multi-stage builds

Building in one stage and copying the result into a clean runtime image works
too, because `dist/` carries everything the server reads (see
[What to ship](#what-to-ship)):

```dockerfile
COPY --from=build /app/dist ./dist
COPY --from=build /app/node_modules ./node_modules
COPY pyxle.config.json package.json ./
CMD ["pyxle", "serve", "--host", "0.0.0.0", "--skip-build"]
```

Add your own Python modules that live outside `pages/` — a `db.py`, a
`middleware.py` referenced from `pyxle.config.json` — to that copy list.

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
# Keep the secret out of the unit file — point at an environment file that is
# root-owned, chmod 600, and NOT in your repo.
EnvironmentFile=/etc/myapp/pyxle.env       # contains PYXLE_SECRET_KEY=...
ExecStart=/srv/myapp/.venv/bin/pyxle serve --skip-build --workers 4 \
    --host 127.0.0.1 --port 8000
Restart=always

[Install]
WantedBy=multi-user.target
```

Build during deployment (`pyxle build`), then `systemctl restart myapp` —
with `--skip-build` the restart is fast because the unit only boots the
server. Node.js must be on the service's `PATH` for SSR.

## Database migrations

If your app uses [`pyxle-db`](../plugins/pyxle-db.md), schema changes live as
checksum-tracked files in `migrations/` and are **applied automatically at
startup** — so a plain deploy (build, restart) also migrates. For production you
usually want migrations to be an **explicit, observable deploy step** instead of
a side effect of the first request:

```bash
pyxle-db migrate     # apply pending migrations, then start the server
pyxle serve --skip-build
```

Guidelines:

- **Run migrations once per deploy, before starting the new server** — not per
  worker. Workers that do race a pending migration serialize on a database
  lock — one applies it, the rest verify the recorded checksum and continue —
  but running migrations up front keeps the outcome visible in your deploy
  logs instead of spread across worker startups.
- **Never edit an already-applied migration** — the checksum tracker rejects it.
  Add a new migration file instead.
- **For zero-downtime (blue-green) deploys, keep migrations backward-compatible**
  with the currently-running version (expand, then contract): add columns/tables
  in one release, backfill, and only drop the old shape in a later release once no
  instance references it. A destructive migration applied while the old instance
  is still serving will break it mid-cutover.

Using the ORM path? Drive Alembic as your migration step instead — pick one
migration tool per app. See [pyxle-db → Migrations](../plugins/pyxle-db.md#migrations).

## Checklist

Before deploying:

- [ ] `pyxle check` passes with no errors (it exits `0` on warnings alone — an unused import will not block you; an unresolved reference **in the Python half**, or an `@action` asking for a body parameter it never annotated, will). It does not resolve identifiers in the JSX half, so it is not a substitute for opening the page — do that too.
- [ ] `pyxle build` completes successfully (it exits non-zero if it cannot run Vite — never ship a `dist/` from a build that failed)
- [ ] Node.js `>= 20.19` **and npm** are on the build machine's `PATH`
- [ ] Node.js is `>= 20.19` on the server's `PATH`
- [ ] **`node_modules` is installed on the server too** — copy `package.json` and
      `package-lock.json` next to `dist/` and run `npm ci` there. Server-side
      rendering runs React through Node at request time and loads `esbuild` from
      your project, so a `dist/` deployed without them answers **500 on every
      page**. The errors name one missing package at a time, so installing them
      individually goes in circles; `npm ci` is the fix. (The Dockerfile below
      already does this.)
- [ ] Set `PYXLE_DEBUG=false` in production
- [ ] Set `PYXLE_SECRET_KEY` to a long random value (**required** — `pyxle serve` won't start without it)
- [ ] Apply database migrations, if you use `pyxle-db` (see below)
- [ ] Size `--workers` to the machine's CPU cores (multi-core serving)
- [ ] Configure CSRF `cookieSecure: true` if using HTTPS
- [ ] Pin a shared `csrf.cookieName` **and** `PYXLE_SECRET_KEY` across instances for blue-green deploys
- [ ] Add CORS origins if serving APIs to other domains
- [ ] Set up a reverse proxy for TLS
- [ ] Declare publicly-cacheable routes in the `cache` block (and opt your CDN in)
- [ ] Configure health check monitoring on `/api/pulse`
- [ ] Configure Python logging if you rely on your own `log.info` calls — `pyxle serve` does not, so `INFO` records are dropped while `WARNING` and above still reach stderr ([Debugging](debugging.md#your-own-loginfo-is-silent-in-production-until-you-configure-it))
- [ ] Add `.env.local` to `.gitignore`

## Next steps

- Full CLI reference: [CLI Commands](../reference/cli.md)
- Full config reference: [Configuration](../reference/configuration.md)
