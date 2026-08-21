# Build and serve

`pyxle dev` is what you run during development. `pyxle build` and
`pyxle serve` are what you run for production. They share most of
their code with the dev server but make a few important changes:

| Aspect | `pyxle dev` | `pyxle build` + `pyxle serve` |
|---|---|---|
| Vite | dev server on :5173 | one-time bundle, then no Vite |
| Source compilation | incremental on file change | full rebuild, all at once |
| File watcher | running | not running |
| Browser refresh | full page reload on every rebuild | none |
| Module reloading | per-request `sys.modules` purge | imported once at startup |
| Error responses | full stack trace + dev overlay | generic `Server Error` |
| Asset serving | proxy to Vite | static files from `dist/client/` |
| Route discovery | metadata files in `.pyxle-build/` | metadata files in `dist/` |
| Port | 8000 by default | 8000 by default |

This doc explains the production pipeline: what `pyxle build` does,
what artifacts it produces, and how `pyxle serve` runs them.

**Files (`pyxle/build/`):**

| File | What it does |
|---|---|
| `pipeline.py` | The `run_build()` orchestrator — compiles, bundles, writes `dist/` |
| `production.py` | Assembles the ASGI app `pyxle serve` runs |
| `manifest.py` | Loads `dist/page-manifest.json` |
| `static_gen.py` | Static pre-rendering for `pyxle build --static` |
| `analyze.py` | Bundle-size report for `pyxle build --analyze` |
| `__init__.py` | Re-exports |

The CLI commands themselves live in `pyxle/cli/__init__.py`, as the
`build` and `serve` functions.

---

## What `pyxle build` actually does

When you run `pyxle build`, the pipeline executes six steps in order:

```
1. Compile every .pyxl file to artifacts in .pyxle-build/
   (same as `pyxle dev`'s initial build)
   │
   ▼
2. Run `npx vite build` (preceded by `npm run build:css`
   on the legacy Tailwind v3 path only)
   - esbuild transforms JSX to JS
   - Vite bundles, code-splits, and hashes assets
   - Output goes to .pyxle-build/dist/ (Vite's output)
   │
   ▼
3. Read .pyxle-build/dist/.vite/manifest.json
   (Vite's mapping from source files to hashed bundle entries)
   │
   ▼
4. Build dist/page-manifest.json
   - For each page, find the bundled JS + CSS chunk(s)
   - Walk the import graph to collect transitively-required CSS
   - Resolve aliases (e.g., optional catch-all routes)
   │
   ▼
5. Copy artifacts into dist/
   - dist/server/   ← compiled .py loaders
   - dist/metadata/ ← compiled .json metadata
   - dist/client/   ← Vite's bundled JS/CSS (hashed)
   - dist/public/   ← static files from public/
   - dist/app/      ← source files the server reads at runtime
   - dist/page-manifest.json
   │
   ▼
6. Print a summary
   "✅ Build completed — 19 page(s), 1 API module(s), 5 asset(s)"
```

Source: `build/pipeline.py` (`run_build`).

The `dist/` directory is the **only build output your deployment
needs** — it carries the compiled artifacts *and* a copy of the source
files the server reads at runtime (`dist/app/`, below). Alongside it a
deployment supplies its configuration, `node_modules` (SSR runs React
on Node) and its Python dependencies. Once `dist/` exists you can
`pyxle serve` it on a server, copy it into a container image, push it
to a CDN, or do anything else you'd do with a static-plus-server build.

---

## Step 1: Compile sources

Steps 1 of `pyxle build` is identical to the initial build of `pyxle
dev` — same `build_once()` function, same `compile_file()` calls, same
`.pyxle-build/` layout. The compiler doesn't know or care that we're
in production mode; it just produces artifacts.

The result is the same three files per page in `.pyxle-build/`:

```
.pyxle-build/
├── server/pages/index.py
├── client/pages/index.jsx
└── metadata/pages/index.json
```

Plus any layout-composed route modules in
`.pyxle-build/client/routes/` and any `pages/api/*.py` files copied
to `.pyxle-build/server/api/`.

If anything fails to compile (a parser error, an unresolved import,
a missing decorator, etc.), the build aborts here and prints the
error. Production builds are **strict** — there is no tolerant mode
for `pyxle build`. The first error stops everything.

---

## Step 2: Run Vite build

`_run_npm_build()` (`build/pipeline.py`) invokes Vite to bundle the
client-side JavaScript:

1. The project's `package.json` must exist — it is what declares Vite
   and the project's React version.
2. `npm run build:css` runs only on the legacy Tailwind v3 path (the
   script is declared *and* there is no PostCSS config). Modern
   scaffolds let Vite own CSS in the bundle step.
3. **`npx vite build`** produces the bundle.

This step is the reason `pyxle build` needs npm as well as Node.js. If
`npx` is not on `PATH`, or `package.json` is missing, the build raises
`ClientBuildError` and stops: continuing would emit a `dist/` with no
browser JavaScript, which `pyxle serve` refuses to start on. A non-zero
exit from Vite itself aborts the build with Vite's own stderr.

The Vite invocation passes:

- `--config .pyxle-build/client/vite.config.js` — Pyxle's
  auto-generated Vite config
- `--manifest` — produce a manifest JSON that maps source files to
  hashed bundle entries
- `PYXLE_VITE_BASE=/client/dist/` env var — sets the asset base path
  so Vite emits URLs like `/client/dist/assets/index-abc123.js`
  instead of `/assets/index-abc123.js`

Vite's output:

- `.pyxle-build/dist/.vite/manifest.json` — the manifest
- `.pyxle-build/dist/assets/*.js` — bundled, code-split, hashed JS
- `.pyxle-build/dist/assets/*.css` — bundled, hashed CSS
- `.pyxle-build/dist/index.html` — Vite's default HTML output (we
  ignore this; Pyxle generates its own HTML at request time)

Vite's output is captured rather than streamed; on a non-zero exit the
build aborts and the captured stderr is included in the error.

Rollup and esbuild name the module they were given — `pages/about.jsx:2:8` —
which is the artifact Step 1 generated, at a line numbered from the start of the
page's JSX half. That stderr is run through
`pyxle.ssr.source_locations.remap_generated_locations`, the same map the SSR
error path uses.

**It only ever touches a `.jsx` path that carries a `:line`.** The line number is
what proves the path is a position a compiler reported, rather than a `.jsx` you
typed yourself — an import specifier, or a line of your own source echoed back
inside a code frame. Rewriting those would edit your source instead of
describing it, so a bare `.jsx` is always left alone.

**And it never touches a `.jsx` inside a URL**, coordinate or not. That is a
separate rule with a separate reason: a URL is a link, and rewriting the path
inside one breaks it. It also cannot be covered by the rule above, because a URL
can carry a coordinate of its own — a stack frame is one.

Precisely what it does to each `.jsx` path in the output:

| The failure names | You are shown | Why |
|---|---|---|
| `pages/about.jsx:2:8` — a compiled page, with a coordinate | `pages/about.pyxl:13:8` | The sidecar maps the generated line to the source line. |
| `pages/ui/Card.jsx:4:2` — a `.jsx` **you** wrote | `pages/ui/Card.jsx:4:2` | Pyxle copies your own components into the build tree unchanged, so the line and column are already yours. Nothing is translated and nothing is labelled. |
| A position inside code the compiler emitted | `pages/about.pyxl (in generated output at pages/about.jsx:41:1)` | The page is known, the line is not. Naming the page without claiming the position is the honest answer. |
| A `.jsx` path it cannot place, with a coordinate | unchanged, plus `(generated)` | Better an admitted artifact than an artifact mistaken for your file. |
| Any `.jsx` path with **no** coordinate | unchanged — see the limitation below | A bare path is indistinguishable from one you wrote yourself, so it is never rewritten. |
| A `.jsx` inside a quoted specifier or an esbuild code frame | unchanged | Same rule as the row above, and the reason for it: these are your words, and a build error must not rewrite them. |
| A `.jsx` inside a URL, **with or without** a coordinate | unchanged, byte for byte | A URL is a link, not a location, and rewriting the path inside it breaks it. Applies to `https`, `file://` and Vite's `/@fs/` alike. |

Only positions are translated. The line numbers in the gutter of an esbuild code
frame are numbered against the generated `.jsx`, so they will not match the
`.pyxl` line beside the message above them.

### Known limitation: an unresolved import still names the build artifact

Rollup reports a missing import with no line number at all, so nothing in that
message meets the bar above and the whole message passes through as Rollup wrote
it. Adding `./components/Missing.jsx` to `pages/rollup.pyxl` when that file does
not exist prints:

```
❌ Build failed: Vite build failed (exit 1): ✗ Build failed in 185ms
error during build:
Could not resolve "./components/Missing.jsx" from ".pyxle-build/client/pages/rollup.jsx"
file: /your/project/.pyxle-build/client/pages/rollup.jsx
    at getRollupError (…/node_modules/rollup/dist/es/shared/parseAst.js:317:41)
    …
```

**There is no reliable way to turn that path back into one of your files, so
don't try.** `.pyxle-build/client/pages/rollup.jsx` is a path inside Pyxle's
build directory. The module it names came from one of two places: the `.pyxl` of
the same name, or a `.jsx` component you wrote that Pyxle copied there
unchanged. Both live under `pages/`, which is why the table above spends a row
telling them apart — and why swapping the extension is right for one and
produces a file that cannot exist for the other.

What *is* unambiguous is the specifier in quotes: it is exactly the one you
typed. Search your own sources for it and fix it there. There is no line number
to look up, because Rollup did not report one.

Pyxle does not rewrite that path, even though it could often resolve it. Doing
so requires matching bare `.jsx` paths, and a bare `.jsx` in a build error is far
more often something you wrote — the specifier on the same line, an `import`
statement quoted back in an esbuild code frame — than it is an artifact path.
Corrupting your own source text in the message meant to help you read it is the
worse failure, so the narrower rule wins. A path with a coordinate, which is
every other row in the table above, is unaffected.

Vite's stderr under `pyxle dev` has the same limitation. Vite runs as a live
subprocess and its stderr is forwarded to your terminal verbatim, prefixed
`❌ [vite]` — it is Vite talking, not Pyxle, so it is not remapped and it names
build paths too:

```
❌ [vite] [vite] Internal server error: Failed to resolve import "./components/Missing.jsx" from ".pyxle-build/client/pages/index.jsx". Does the file exist?
❌ [vite]   Plugin: vite:import-analysis
❌ [vite]   File: /your/project/.pyxle-build/client/pages/index.jsx:25:0
```

The same caution applies, and one more: the coordinate on a line like that is
not necessarily numbered against the file the path names. Pyxle hands Vite a
source map for every compiled page in dev, so Vite reports the position it maps
to — `25` above is line 25 of `pages/index.pyxl`; the failing import sits on
line 4 of the `.jsx` the path names. Path and line can come from two different
files.

---

## Step 3: Load Vite's manifest

Vite's manifest looks like this:

```json
{
  "pages/index.jsx": {
    "file": "assets/index-abc123.js",
    "isEntry": true,
    "imports": ["_shared-def456.js"],
    "css": ["assets/index-789xyz.css"]
  },
  "_shared-def456.js": {
    "file": "assets/shared-def456.js",
    "imports": [],
    "css": ["assets/shared-ghi789.css"]
  },
  "pages/about.jsx": {
    "file": "assets/about-jkl012.js",
    "isEntry": true,
    "imports": ["_shared-def456.js"],
    "css": []
  }
}
```

Each entry tells you the **bundled file path**, the **list of
imported chunks** (so you can preload them), and the **direct CSS
dependencies**.

The interesting part is the import chain. `pages/index.jsx` imports
`_shared-def456.js`, which itself has CSS. To get the **complete CSS
list** for `index.jsx`, we need to walk the imports recursively and
collect all `css` arrays. Otherwise we'd ship pages with missing
styles from shared modules.

`_collect_css_from_vite_entry()` (`build/pipeline.py`) does this walk.
It uses a visited set to handle cycles (Vite shouldn't produce
cycles, but defense in depth) and returns the deduplicated list of
CSS files for each entry.

---

## Step 4: Build the page manifest

Vite's manifest is keyed by source file. Pyxle's `page-manifest.json`
is keyed by **route**:

```json
{
  "/": {
    "client": {
      "file": "client/dist/assets/index-abc123.js",
      "css": ["client/dist/assets/index-789xyz.css", "client/dist/assets/shared-ghi789.css"]
    },
    "server": {
      "file": "server/pages/index.py",
      "module_key": "pyxle.server.pages.index",
      "loader_name": "load_home"
    },
    "metadata": "metadata/pages/index.json"
  },
  "/about": {
    ...
  },
  "/posts/{id}": {
    ...
  }
}
```

`_build_page_manifest()` (`build/pipeline.py`) iterates the
metadata registry, looks up each page's bundled assets in the Vite
manifest, walks the import chain to collect CSS, and emits one entry
per route.

Aliases (from `[[...slug]].pyxl` optional catch-alls) get their own
entry pointing at the same data:

```json
{
  "/shop/{path:path}": { /* primary */ },
  "/shop": { /* alias — same data */ }
}
```

The page manifest is written to `dist/page-manifest.json` and is the
**source of truth for a production route's assets** — which hashed
bundle and stylesheets the template links for a given path.

The route table itself is still assembled from the per-page metadata,
because the manifest is a lossy projection of it (it carries no actions,
images, cache posture, or `standalone` flag). The difference between dev
and production is only *where* that metadata is read from: `pyxle dev`
reads `.pyxle-build/`, `pyxle serve` reads the copy inside `dist/`.

---

## Step 5: Copy into `dist/`

The final layout under `dist/` is:

```
dist/
├── server/                      ← compiled Python loaders
│   ├── pages/
│   │   ├── index.py
│   │   ├── about.py
│   │   └── posts/[id].py
│   └── api/
│       └── health.py
│
├── client/                      ← bundled JS + CSS for the browser
│   └── dist/
│       └── assets/
│           ├── index-abc123.js
│           ├── shared-def456.js
│           ├── index-789xyz.css
│           └── shared-ghi789.css
│
├── metadata/                    ← compiled .json metadata (one per page)
│   └── pages/
│       ├── index.json
│       └── ...
│
├── public/                      ← static files copied from public/
│   ├── favicon.ico
│   └── ...
│
├── app/                         ← source files read at runtime
│   └── pages/
│       ├── api/_shared.py
│       ├── llms.py
│       └── index.md
│
├── meta.json                    ← the list of compiled sources
└── page-manifest.json           ← the route → assets mapping
```

The `dist/server/` and `dist/metadata/` directories are direct copies
of `.pyxle-build/server/` and `.pyxle-build/metadata/`, and `meta.json`
is a copy of `.pyxle-build/meta.json`. The `dist/client/dist/` directory
is Vite's output (`.pyxle-build/dist/`) copied verbatim.

`dist/app/` is different in kind: it is not compiled output but a copy
of the project's own **source**, because compiling `pages/` emits one
module per route and the server still reads files that are not routes.
A private helper beside an endpoint (`pages/api/_shared.py`,
`pages/api/__init__.py`, `pages/api/_internal/…`, a non-route
`pages/s/[slug]/queries.py`) is imported by name off `sys.path` at
route-import time; `pages/**/llms.py` handlers and colocated
`pages/**/*.md` files are read through `settings.pages_dir` per request;
configured global stylesheets are read per render and inlined. The whole
`pages/` tree is mirrored (minus `__pycache__`), plus each configured
global stylesheet/script at its project-relative path.

At serve time `pyxle.build.production` appends `dist/app` to `sys.path`
and, when the project's `pages/` is not deployed, re-roots
`settings.pages_dir` onto `dist/app/pages`. The deployed source tree
wins whenever it exists — a build has to read the file the developer
edited, not the copy of it from the previous build.

Together these make `dist/` self-contained: it is everything
`pyxle serve` reads, so a deployment can ship it alone and the
intermediate `.pyxle-build/` can be discarded (or left to drift, which
it will — it is a cache that any recompile writes to). The exception is
application code outside `pages/` — a project-root `db.py`, a
`middleware.py` named in the config — whose import graph the framework
cannot enumerate; those ship with the rest of the application.

`dist/public/` is a copy of your project's `public/` directory (if
it exists). These are static assets that ship to the browser
unchanged: favicons, images, robots.txt, etc.

The double `dist/client/dist/` nesting is intentional — Vite's output
naturally lives under a `dist/` subdirectory of its base path, and
Pyxle preserves that. The serving layer is configured to mount it at
the right URL prefix (`/client/dist/...`).

**Only the inner `dist/` is public.** `dist/client/` is the *input* to
that bundle — every page's unbundled JSX, the layout route wrappers,
Pyxle's own client components, the generated `vite.config.js`,
`tsconfig.json` and `index.html`. The browser never requests any of it:
the rendered HTML references nothing outside `dist/client/dist/`. So
`pyxle serve` mounts `dist/client/dist/` at `/client/dist/`, not
`dist/client/` at `/client/`, and a request for
`/client/pages/guestbook.jsx` or `/client/vite.config.js` is a 404 like
any other unknown path. `pyxle build --analyze` walks the same directory,
so its totals count only bytes a browser downloads.

---

## What `pyxle serve` does

Once you have a `dist/` directory, `pyxle serve` runs it. The serve
command:

1. **Loads `pyxle.config.json`** with `debug=False`. This is the
   single most important setting that flips the framework into
   production mode.
2. **Loads `dist/page-manifest.json`** for each route's bundled assets.
3. **Re-roots the settings onto `dist/`**, so the compiled modules and
   metadata it reads are the ones it serves rather than whatever the
   intermediate `.pyxle-build/` cache currently holds.
4. **Makes `dist/app/` importable** (appended to `sys.path`, behind the
   project root) so a compiled route's `from pages.api._shared import …`
   resolves whether or not the source tree was deployed.
5. **Builds a `MetadataRegistry`** from `dist/meta.json` plus
   `dist/metadata/pages/*.json`.
6. **Creates a `RouteTable`** from the registry.
7. **Spawns the SSR worker pool.** Same code as dev mode.
8. **Builds a Starlette app** with the same `create_starlette_app()`
   factory. The factory checks `settings.debug` and includes the
   `GZipMiddleware` (production-only) and skips the Vite proxy
   middleware (which would have nothing to talk to).
9. **Runs uvicorn** to serve the Starlette app.

Source: `build/production.py` (`build_production_app`), driven by
`cli/__init__.py` (`serve`).

The result is a process that:

- Listens on port 8000 (or whatever `--port` you pass).
- Imports each compiled `.py` module **once at startup**, not per-request.
- Serves `dist/client/`, `dist/public/`, and `dist/server/api/*.py`
  routes via `StaticFiles` mounts.
- Handles page routes the same way `pyxle dev` does — same SSR
  pipeline, same loader execution, same component rendering, same
  head merging.
- Responds to errors with **opaque** generic pages instead of the
  developer overlay.

### Why is the SSR worker pool the same in production?

Because the rendering work doesn't change between dev and prod. The
React component is the same JavaScript. esbuild bundles it the same
way. `renderToString` produces the same output. The cost of running
React on the server is identical.

The only thing that changes is *how often* you pay for it: in dev,
the worker is mostly idle and serves your single-developer requests.
In production, the worker pool needs to scale to handle real
traffic — which is why the recommended worker count for production
is *the number of CPU cores*, not 1.

You can adjust:

```bash
pyxle serve --ssr-workers 4
```

…to spin up four persistent Node.js workers. Round-robin dispatch
gives you four-way SSR parallelism within one Pyxle process.

### Stateless processes, scale horizontally

Pyxle's serve process is **stateless**: nothing is stored
in-process between requests. This means you can run multiple Pyxle
instances behind a load balancer and they'll all serve the same
content. The scaling story is "run more processes" — not "run more
threads" or "tune a connection pool."

---

## Configuration overrides for production

`pyxle serve` builds the production config with three overrides:

```python
production_config = file_config.apply_overrides(
    debug=False,
    starlette_host=host,
    starlette_port=port,
)
```

Source: `cli/__init__.py` (`serve`).

`debug=False` is the critical one. It turns off:

- The hot-reload `sys.modules` purge
- The dev error overlay (replaced with opaque generic responses)
- The Vite client tag in the document `<head>`
- The React Refresh preamble
- The WebSocket overlay endpoint

It also turns *on*:

- GZip middleware
- Production asset path resolution via `dist/page-manifest.json`
- Streaming responses use the production document shell

The full list of what `debug` controls is scattered across
`devserver/`, `ssr/`, and `build/` — search the codebase for
`settings.debug` to see every gate.

### Other CLI flags for `serve`

- **`--port 8000`** — bind port (default 8000)
- **`--host 0.0.0.0`** — bind host (default 127.0.0.1; use 0.0.0.0
  for "listen on all interfaces", which you'll want behind a
  reverse proxy)
- **`--dist-dir ./dist`** — where to read the build output from
  (default: `./dist` in the current directory)
- **`--skip-build`** — skip the implicit `pyxle build` and use the
  existing `dist/` as-is. Useful when the build artifacts come
  from CI and you just want to run them.
- **`--no-skip-build`** — force a fresh build before serving (the
  default)
- **`--serve-static / --no-serve-static`** — whether to serve
  `dist/client/` and `dist/public/` directly. Disable this if you're
  putting Pyxle behind a CDN that handles static assets.
- **`--ssr-workers N`** — number of persistent Node.js workers
  (default 1)

---

## What's in a deployable artifact?

If you want to deploy a Pyxle app, the artifact is the `dist/`
directory plus the Python source for any files outside `pages/`
(your shared utilities, your dependencies, your `pyxle.config.json`).

A typical deployment looks like:

```
my-app/
├── dist/                        ← from pyxle build
├── pyxle.config.json
├── requirements.txt             ← pinned Python deps
├── pyproject.toml
└── public/                      ← optional, if not already in dist/
```

The deployment process:

1. **On the build machine:** `pip install -r requirements.txt && npm
   install && pyxle build`
2. **Copy `dist/`, `pyxle.config.json`, and the Python source** to
   the server (or build a container image).
3. **On the server:** `pip install -r requirements.txt`. Node.js
   isn't required for *serving* unless you have SSR workers
   spawning... wait, scratch that. Node.js **is** required — the
   SSR pipeline runs React on Node.js workers. You need Node.js on
   the production server too.
4. **Run** `pyxle serve --port 8000 --host 0.0.0.0`.
5. **Put it behind a reverse proxy** (nginx, Caddy, Cloudflare, ALB,
   whatever you like). Pyxle doesn't try to be a frontend proxy
   itself.

The `pyxle-dev` deployment (the marketing site) is a working
example: it builds in CI, deploys to an EC2 instance, and serves
behind nginx with TLS termination. It uses `pyxle serve --ssr-workers
2` because the box has 2 vCPUs.

---

## Why a separate build step?

You might wonder: why do `pyxle build` and `pyxle serve` exist as
separate commands? Why not just have `pyxle serve` build on demand,
or `pyxle dev` run in production mode?

Three reasons:

1. **Build is slow, serve is fast.** `pyxle build` takes 10-60
   seconds for a typical project (mostly Vite). `pyxle serve` takes
   under a second to come up. You don't want to rebuild every time
   you restart the server in CI. Separating the two lets you bake
   `dist/` into a container image and start instances quickly.

2. **Build needs the dev tools, serve doesn't.** `pyxle build`
   requires Node.js, npm, Vite, esbuild, and the JSX parser. `pyxle
   serve` only requires Python + Node.js for the SSR worker. You
   can ship a much smaller production runtime by not including
   the build toolchain.

3. **The dev server's incremental builder doesn't apply.** It's
   optimized for "one file changed, recompile only that one." A
   production build is the opposite — *everything* needs to be
   compiled at once, with full bundle optimization. Different code
   path, different concerns.

The separation also makes the framework easier to reason about:
"build" = "produce artifacts", "serve" = "consume artifacts". Each
verb has a clear input and output.

---

## Where to read next

- **[The CLI](cli.md)** — How `pyxle build` and `pyxle serve` parse
  their flags, apply config overrides, and bridge user input to the
  build pipeline.

- **[Server-side rendering](ssr.md)** — How the SSR pipeline serves
  pages in production mode (which is the same code as dev mode,
  with `debug=False`).

- **[The dev server](dev-server.md)** — The dev counterpart to
  `pyxle serve`. Shares most of its code with the production
  serving stack, but adds the file watcher, the Vite proxy, and the
  hot-reload mechanism.
