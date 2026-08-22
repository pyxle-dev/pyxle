# CLI Reference

The `pyxle` CLI manages Pyxle projects from scaffolding to production.

## Global options

| Flag | Description |
|------|-------------|
| `--version` | Show Pyxle version and exit |
| `--log-format [console\|json]` | Output format (default: `console`) |
| `--verbose` / `-v` | Show debug-level output |
| `--quiet` / `-q` | Suppress informational output; show only warnings and errors |
| `--install-completion` | Install shell completion for the current shell |
| `--show-completion` | Print shell completion (to copy or customize) |

## `pyxle init`

Create a new Pyxle project.

```bash
pyxle init <name> [options]
```

`pyxle init` is **interactive**: when stdin is a terminal and no flag pins the
choice, it walks you through arrow-key selections — Tailwind CSS, shadcn/ui
(only if Tailwind is enabled), and whether to customize the default import
alias (`@/*`; the value input appears only if you say yes). When stdin is
**not** a terminal (CI, pipes), it never prompts — it uses the flags and
defaults below. The target is validated *before* the first question, so an
unusable name or an occupied directory never costs you a set of answers.

| Argument / Flag | Default | Description |
|----------------|---------|-------------|
| `name` | *(required)* | Project **name**, not a path. `my-app` creates a `my-app/` directory here; `.` scaffolds into the current directory (deriving the name from it). An argument containing a path separator (`apps/my-app`, `~/code/my-app`) is rejected — `cd` to the parent first. Running `pyxle init` with no argument is an error. |
| `--force` / `-f` | `false` | Overwrite an existing directory (or scaffold into a non-empty current directory). |
| `--template` / `-t` | `default` | Project template. Only `"default"` is supported today (other values error). |
| `--tailwind` / `--no-tailwind` | prompt → off | Set up Tailwind CSS v4 (wired into Vite). |
| `--shadcn` / `--no-shadcn` | prompt → off | Set up shadcn/ui (implies `--tailwind`). |
| `--import-alias` | `@/*` | Import alias for project modules (e.g. `~/*`). |
| `--yes` / `-y` | `false` | Accept all defaults without prompting (no Tailwind, no shadcn, default alias). |
| `--install` / `--no-install` | `false` | Run `pip install` and `npm install` after scaffolding. |

**Examples:**

```bash
pyxle init my-app                          # interactive prompts
pyxle init my-app --yes                     # accept defaults (no Tailwind)
pyxle init my-app --tailwind --no-shadcn    # Tailwind only, no prompts
pyxle init my-app --shadcn                  # shadcn/ui (implies Tailwind)
pyxle init .                                # scaffold into an empty current dir
pyxle init my-app --force --install
```

## `pyxle install`

Install Python and Node.js dependencies.

```bash
pyxle install [directory] [options]
```

| Argument / Flag | Default | Description |
|----------------|---------|-------------|
| `directory` | `.` | Project directory |
| `--python` / `--no-python` | `true` | Install Python deps via `pip` |
| `--node` / `--no-node` | `true` | Install Node deps via `npm` |
| `--break-system-packages` | `false` | Pass `--break-system-packages` to `pip`, for externally-managed (PEP 668) environments without a virtualenv. Use with care. |

**Examples:**

```bash
pyxle install
pyxle install --no-python    # Node only
pyxle install ./my-app
pyxle install --break-system-packages   # PEP 668 system Python, no venv
```

Outside a virtualenv, `pyxle install` warns about PEP 668 ("externally-managed-environment") and recommends creating a venv first; pass `--break-system-packages` to install anyway.

pip and npm output is captured, not printed — a successful install shows only the step lines and the result:

```
▶️  Python dependencies — /usr/bin/python -m pip install -r requirements.txt
▶️  Node dependencies — npm install
✅ Dependencies installed.
```

Nothing is hidden that you need. A **failure** replays everything the installer said, verbatim, before the error, and `pyxle -v install` streams its output live.

## `pyxle dev`

Start the development server with hot reload.

```bash
pyxle dev [directory] [options]
```

| Argument / Flag | Default | Description |
|----------------|---------|-------------|
| `directory` | `.` | Project directory |
| `--host` | `127.0.0.1` | Starlette server bind address |
| `--port` | `8000` | Starlette server port |
| `--vite-host` | `127.0.0.1` | Vite dev server bind address |
| `--vite-port` | `5173` | Vite dev server port |
| `--debug` / `--no-debug` | `true` | Enable debug mode |
| `--ssr-workers` | `1` | Number of persistent SSR worker processes (`0` = per-request subprocess mode) |
| `--config` | -- | Path to `pyxle.config.json` |
| `--print-config` / `--no-print-config` | `false` | Print merged configuration before starting |
| `--tailwind` / `--no-tailwind` | `true` | Auto-start the **legacy** standalone Tailwind v3 CLI watcher when a hand-written `tailwind.config.*` is present. Tailwind **v4** projects (the scaffold default when you opt into Tailwind) run through the `@tailwindcss/vite` plugin and ignore this flag. |
| `--dashboard` / `--no-dashboard` | `false` | Periodically print a live [observability](../guides/observability.md#dev-dashboard) panel (request/SSR metrics) to the terminal |
| `--inspect` / `--no-inspect` | `false` | Host a debugpy debug server (bound to `127.0.0.1`) so VS Code or any DAP client can set breakpoints directly in `.pyxl` files — see [Debugging](../guides/debugging.md). debugpy ships with the framework. |
| `--inspect-port` | `5678` | Port for the debug server (with `--inspect`). Falls back to an ephemeral port when busy; the actual endpoint is recorded in `.pyxle-build/dev-server.json`. |
| `--inspect-wait` / `--no-inspect-wait` | `false` | With `--inspect`: wait for a debugger to attach before starting the server — for breakpoints in code that runs during boot. |
| `--verbose` / `-v` | `false` | Restore full output: the raw Vite log firehose, debug-level internals, and `DEBUG` server logs in the browser console. Equivalent to the global `pyxle -v dev`. |

**Examples:**

```bash
pyxle dev
pyxle dev --host 0.0.0.0 --port 3000
pyxle dev --no-tailwind --ssr-workers 4
pyxle dev ./my-app --print-config
pyxle dev --verbose             # troubleshoot: full Vite + debug output
```

**If the port is taken.** `pyxle dev` and `pyxle serve` check the port before
they start anything — before Vite, before `npm install`, and before a build — so
an occupied port costs you a message rather than a build:

```
❌ Port 8000 is already in use, so pyxle dev cannot start.

Something is already listening on 127.0.0.1:8000 — most often a pyxle dev from
an earlier session that is still running, or another application using the same
port.

Start on a free port:   pyxle dev --port 8001
See what is holding it: lsof -i :8000
```

The suggested port is one that was free when the message was written; nothing is
reserved. Note that only the **Vite** port moves on its own (`Vite port 5173 in
use; retrying on 5174`) — the port you asked for is never silently changed,
because a server quietly listening somewhere other than where you pointed your
browser is worse than one that did not start.

**What it does:**

1. Loads configuration from `pyxle.config.json` + environment variables + CLI flags
2. Compiles `.pyxl` files into Python and JSX modules
3. Starts the Vite dev server for React hot reload (Tailwind v4 compiles here via the `@tailwindcss/vite` plugin)
4. Starts the legacy Tailwind v3 watcher only if a hand-written `tailwind.config.*` is detected
5. Starts the Starlette ASGI server
6. Watches for file changes and recompiles automatically

**Opening the page from another device.** `--host 0.0.0.0` makes the dev server
answer on your machine's network address, and the startup banner prints the
`Network:` URL to use. Pyxle widens the whole dev server to match: Vite binds the
same address, and both servers accept the private-network origins that address
implies — the JavaScript modules, the hot-reload socket and the error overlay all
reach that browser. Open the printed URL; a page opened at some *other* address
(a hostname, a public IP, an https tunnel) is served but cannot load its modules,
and the terminal says so on the first request. See
[which browsers the dev server trusts](../architecture/dev-server.md#which-browsers-the-dev-server-trusts).

**Console output.** By default `pyxle dev` prints a clean, curated console — a
startup summary (the local URL, the Vite URL, the route count, the
[Studio](../guides/studio.md) URL, and the total "ready in X ms"), a concise
one-line notice per incremental rebuild
(`Rebuilt … in X ms`), and any warnings or errors. The raw line-by-line Vite
firehose and debug-level internals are hidden. Pass `--verbose` (or `-v`, or the
global `pyxle -v dev`) to restore the full output when troubleshooting. Genuine
signal — errors, warnings, the URLs, and rebuild success/failure — is always
shown, at every verbosity.

**Server logs in the browser console (dev only).** While `pyxle dev` is running,
your server-side `logging` output (from loaders, actions, and your own modules)
is forwarded to the browser devtools console, prefixed `[pyxle:server]` and
mapped to the matching `console` method (`info` → `console.info`, `warning` →
`console.warn`, `error` → `console.error`). The same records also print in the
terminal, so you can follow server logs from whichever window you are already
in. By default only `INFO` and above from your own loggers are forwarded;
`--verbose` additionally forwards `DEBUG` records and the framework's own
internal loggers to the browser. This is strictly a development feature — it
never runs under `pyxle serve` and never appears in the production bundle.

The idiomatic Python logger works as-is inside a `.pyxl` page:

```python
import logging

log = logging.getLogger(__name__)

@server
async def load_dashboard(request):
    log.info("loading dashboard")
    ...
```

```
[pyxle:server pages/dashboard.pyxl] loading dashboard
```

A page's records are labelled with the `.pyxl` file that emitted them rather
than the module name it is compiled under. A logger you name yourself keeps
that name: `logging.getLogger("shopapp")` prints as `[pyxle:server shopapp]`.

## `pyxle studio`

Run the dev server and open the [Pyxle Studio](../guides/studio.md) dashboard.

```bash
pyxle studio [directory] [options]
```

`pyxle studio` runs the same development server as `pyxle dev` — Studio is part of it, served at `/__pyxle/studio` — and opens your browser on the dashboard once the server is ready. Debug mode is forced on (Studio is dev-only), and the dashboard is enabled for this run even when the config sets `"studio": false`.

| Argument / Flag | Default | Description |
|----------------|---------|-------------|
| `directory` | `.` | Project directory |
| `--host` | `127.0.0.1` | Starlette server bind address |
| `--port` | `8000` | Starlette server port |
| `--vite-host` | `127.0.0.1` | Vite dev server bind address |
| `--vite-port` | `5173` | Vite dev server port |
| `--ssr-workers` | `1` | Number of persistent SSR worker processes (`0` = per-request subprocess mode) |
| `--config` | -- | Path to `pyxle.config.json` |
| `--tailwind` / `--no-tailwind` | `true` | Auto-start the legacy Tailwind v3 watcher when a hand-written `tailwind.config.*` is present (same behavior as `pyxle dev`) |
| `--open` / `--no-open` | `true` | Open the Studio dashboard in the system browser once the server is ready |
| `--inspect` / `--no-inspect` | `false` | Host a debugpy debug server for `.pyxl` breakpoint debugging — see [Debugging](../guides/debugging.md) |
| `--inspect-port` | `5678` | Port for the debug server (with `--inspect`; falls back to an ephemeral port when busy) |
| `--verbose` / `-v` | `false` | Restore full output (raw Vite logs, debug internals). Equivalent to `pyxle -v studio`. |

**Examples:**

```bash
pyxle studio                     # dev server + dashboard in the browser
pyxle studio --no-open           # dashboard enabled, but don't launch a browser
pyxle studio --inspect           # dashboard + debugger in one command
pyxle studio ./my-app --port 3000
```

## `pyxle build`

Build production-ready assets.

```bash
pyxle build [directory] [options]
```

| Argument / Flag | Default | Description |
|----------------|---------|-------------|
| `directory` | `.` | Project directory |
| `--config` | -- | Path to `pyxle.config.json` |
| `--out-dir` | `dist/` | Output directory for build artifacts |
| `--incremental` / `--no-incremental` | `false` | Reuse cached artifacts |
| `--static` / `--no-static` | `false` | Pre-render loader-less, non-dynamic pages to HTML at build time (SSG) — see [Caching](../guides/caching.md#static-pre-rendering-pyxle-build-static) |
| `--analyze` / `--no-analyze` | `false` | Print a JS/CSS bundle-size report (raw + gzip, largest first) after the build — see [Build Optimization](../guides/build-optimization.md#inspecting-the-bundle-pyxle-build-analyze) |

**Examples:**

```bash
pyxle build
pyxle build --analyze
pyxle build --out-dir ./output --incremental
pyxle build --static
```

## `pyxle serve`

Serve a production build (without Vite).

```bash
pyxle serve [directory] [options]
```

| Argument / Flag | Default | Description |
|----------------|---------|-------------|
| `directory` | `.` | Project directory |
| `--host` | `127.0.0.1` | Bind address |
| `--port` | `8000` | Port number |
| `--dist-dir` | `dist/` | Directory with production artifacts |
| `--skip-build` / `--no-skip-build` | `false` | Skip running build first |
| `--config` | -- | Path to `pyxle.config.json` |
| `--serve-static` / `--no-serve-static` | `true` | Serve static assets directly from Pyxle |
| `--ssr-workers` | `1` | Number of persistent SSR worker processes, per server worker (`0` = auto-size to CPU cores, capped at 4) |
| `--workers` / `-w` | `1` | Number of server worker processes (one per CPU core); `>1` enables multi-core serving; `0` auto-detects from CPU cores |

**Examples:**

```bash
pyxle serve
pyxle serve --host 0.0.0.0 --port 8000 --skip-build
pyxle serve --ssr-workers 4
pyxle serve --workers $(nproc)   # one server process per CPU core
```

With `--workers N` (N > 1), Pyxle serves the build across `N` uvicorn worker
processes. Each is an independent server with its own SSR pool, so `--ssr-workers`
applies per worker (total render processes = `workers × ssr-workers`). See the
[deployment guide](../guides/deployment.md#multi-core-worker-processes) for
sizing guidance.

## `pyxle check`

Validate `.pyxl` files, configuration, and dependencies without starting the server. Each `.pyxl` file is checked at four levels:

- **Python syntax** (via `ast`) and Pyxle structural rules (loader/action shape, `HEAD`, …).
- **Python semantics** (via pyflakes): undefined names — e.g. a symbol you `raise` but never imported — unused imports, redefinitions. Compiler-injected runtime names (`server`, `action`, `LoaderError`, `ActionError`, …) are recognized, so the idiomatic patterns never read as undefined.
- **JSX syntax** (via Babel): unclosed tags/expressions, mismatched braces, TypeScript in a client block, and **duplicate `export default`** (which Babel accepts but the build rejects).
- **`@action` signatures**: an action that asks for a request body it never described — `async def bump(request, payload)` with no annotation on `payload` — is reported here rather than failing the first time someone triggers it. This is read off the parsed source, so `check` still never imports your module. An *annotated* body parameter is not flagged: whether Pydantic is installed is a property of the environment you deploy into, and `pyxle openapi` and the dev server answer that one.

As of 0.7.0 the JSX level works out of the box — `pyxle-langkit`, which provides the Babel-based checker, ships with the framework. On earlier versions it required the `[langkit]` extra (`pip install 'pyxle-framework[langkit]'`); without it, the JSX check reported itself unavailable.

> **What a green check proves — and what it doesn't.** `pyxle check` is a
> static gate: it validates `.pyxl` syntax, Python semantics, and JSX syntax.
> It does **not** render your pages, so a mistake that only exists at runtime —
> a component reading `data.posts` when the loader returns `{"items": ...}`, a
> loader whose query fails against the real database — surfaces when the page
> renders, not here. Treat a clean check as "this compiles"; loading the page
> under `pyxle dev` (or a test that requests it) remains the runtime proof.

```bash
pyxle check [directory] [options]
```

| Argument / Flag | Default | Description |
|----------------|---------|-------------|
| `directory` | `.` | Project directory |
| `--config` | -- | Path to `pyxle.config.json` |

### Errors vs warnings

Findings are split by whether the code will actually break when it runs.

| Severity | What it covers | Exit code |
|----------|----------------|-----------|
| **error** | Syntax errors, Pyxle structural rules, JSX syntax, an `@action` body parameter with no annotation, and semantics that fail at runtime: unresolved references (`undefined name 'x'`), unbound locals, malformed `%`/`.format()` strings, `raise NotImplemented` | `1` |
| **warning** | Code that runs correctly but wants tidying: unused imports, unused locals and annotations, redefinitions, duplicate dict keys, f-strings with no placeholders, `is` against a literal | `0` |

A semantic rule Pyxle does not recognise — one added by a future pyflakes — is reported as a warning, so upgrading a dependency can never turn into a surprise deploy blocker. This is what lets [the deployment checklist](../guides/deployment.md) gate a release on `pyxle check`: a leftover `import json` will not stop a deploy, an unresolved reference will.

**Example output:**

```
ℹ️  Checked 12 .pyxl file(s) in my-app/
✅ All checks passed
```

Findings are reported per file as `[section] line N: message`, with the file path on the next line (every file is checked, so one broken file never aborts the scan). Warnings print first, then errors:

```
ℹ️  Checked 12 .pyxl file(s) in my-app/
  warning: [python] line 3: 'json' imported but unused
    --> pages/index.pyxl
  error: [python] line 15: @server function must be async
    --> pages/index.pyxl
  error: [jsx] line 8:10: Unterminated JSX contents
    --> pages/settings.pyxl
❌ Check failed with 2 error(s) (1 warning(s))
```

Exit code is `0` when no error is found (warnings alone do not fail the command), `1` otherwise.

Every line number in a finding is a line of the `.pyxl` file — including a second
one named inside the message, such as the `[` a mismatched `)` should have closed
or the earlier binding a redefinition shadows:

```
  error: [python] line 11: closing parenthesis ')' does not match opening parenthesis '[' on line 8
    --> pages/products.pyxl
  warning: [python] line 9: import 'os' from line 7 shadowed by loop variable
    --> pages/products.pyxl
```

The checkers behind these levels each see one extracted half of your file and
number their findings from the start of it; Pyxle translates those coordinates
back to the file you are editing before printing them.

## `pyxle typecheck`

Run TypeScript type-checking on compiled JSX output.

```bash
pyxle typecheck [directory] [options]
```

Requires `typescript` in your `devDependencies`. Runs `tsc --noEmit` against the compiled JSX in `.pyxle-build/client/`.

| Argument / Flag | Default | Description |
|----------------|---------|-------------|
| `directory` | `.` | Project directory |
| `--config` | -- | Path to `pyxle.config.json` |

## `pyxle routes`

Display the route table for your project.

```bash
pyxle routes [directory] [options]
```

| Argument / Flag | Default | Description |
|----------------|---------|-------------|
| `directory` | `.` | Project directory |
| `--config` | -- | Path to `pyxle.config.json` |
| `--json` | `false` | Output as JSON |

**Example output:**

```
ℹ️  Routes for my-app/

ℹ️    Pages:
▶️  / — index.pyxl  [loader=load_home]
▶️  /about — about.pyxl
▶️  /blog/{slug} — blog/[slug].pyxl  [loader=load_post]

ℹ️    API Routes:
▶️  /api/pulse — api/pulse.py

ℹ️    Special Files (no URL of their own):
▶️  error boundary — error.pyxl  [covers /]
▶️  404 page — not-found.pyxl  [covers /]
▶️  loading fallback — blog/loading.pyxl  [covers /blog/*]

✅ 4 route(s) found
```

`error.pyxl`, `not-found.pyxl` and `loading.pyxl` are compiled but serve no URL of their own, so they are listed by what they do and by the URL subtree they cover — not under a path you could visit. They are not counted in the route total.

Use `--json` for machine-readable output; it lists page and API routes only. Under `--json` the array is the only thing written to stdout and messages go to stderr, so `pyxle routes --json > routes.json` captures valid JSON or an empty file. The human table above is not data and stays on stdout.

## `pyxle openapi`

Generate an [OpenAPI 3.1](https://spec.openapis.org/oas/v3.1.0) document from your `@action` request models. For every action that declares a [Pydantic body parameter](../core-concepts/server-actions.md#validating-request-bodies-with-pydantic), Pyxle emits a `POST` operation with the model's JSON Schema as the request body and a structured `422` validation response; actions without a model get a permissive object body.

```bash
pyxle openapi [directory] [options]
```

| Argument / Flag | Default | Description |
|----------------|---------|-------------|
| `directory` | `.` | Project directory |
| `--config` | -- | Path to `pyxle.config.json` |
| `--out` / `-o` | -- | Write the schema to this file (default: stdout) |
| `--title` | `Pyxle API` | OpenAPI `info.title` |
| `--api-version` | `0.1.0` | OpenAPI `info.version` |

```bash
# Print to stdout
pyxle openapi

# Redirect or pipe it — stdout carries the document and nothing else
pyxle openapi > openapi.json
pyxle openapi | jq '.paths'

# Write a file with custom metadata
pyxle openapi --out openapi.json --title "Acme API" --api-version 2.0.0
```

The document is the only thing written to stdout; progress and errors go to stderr. So a redirect captures valid JSON or an empty file — never a document with a message in it — and a failure is still visible in the terminal. The same applies to [`pyxle routes --json`](#pyxle-routes).

The schema is derived from runtime introspection of the compiled action modules, so it always matches what the dispatcher actually validates.

Pydantic is only needed for the actions that actually declare a model body. A project whose actions take no body — or which has no actions yet — generates its document without the `[pydantic]` extra installed, and an empty `paths` object is the correct answer for a project with no actions. The command exits with an error if an action declares a model body and Pydantic isn't installed (`pip install "pyxle-framework[pydantic]"`; the message names the action and its file), or if a page module can't be imported.
