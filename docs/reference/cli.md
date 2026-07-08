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
pyxle init [name] [options]
```

`pyxle init` is **interactive**: when stdin is a terminal and no flag pins the
choice, it prompts for Tailwind CSS, shadcn/ui (only if Tailwind is enabled),
and the import alias. When stdin is **not** a terminal (CI, pipes), it never
prompts — it uses the flags and defaults below.

| Argument / Flag | Default | Description |
|----------------|---------|-------------|
| `name` | `.` | Project directory to create. Use `.` (or omit) to scaffold into the current directory and derive the name from it. |
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
| `--verbose` / `-v` | `false` | Restore full output: the raw Vite log firehose, debug-level internals, and `DEBUG` server logs in the browser console. Equivalent to the global `pyxle -v dev`. |

**Examples:**

```bash
pyxle dev
pyxle dev --host 0.0.0.0 --port 3000
pyxle dev --no-tailwind --ssr-workers 4
pyxle dev ./my-app --print-config
pyxle dev --verbose             # troubleshoot: full Vite + debug output
```

**What it does:**

1. Loads configuration from `pyxle.config.json` + environment variables + CLI flags
2. Compiles `.pyxl` files into Python and JSX modules
3. Starts the Vite dev server for React hot reload (Tailwind v4 compiles here via the `@tailwindcss/vite` plugin)
4. Starts the legacy Tailwind v3 watcher only if a hand-written `tailwind.config.*` is detected
5. Starts the Starlette ASGI server
6. Watches for file changes and recompiles automatically

**Console output.** By default `pyxle dev` prints a clean, curated console — a
startup summary (the local URL, the Vite URL, the route count, and the total
"ready in X ms"), a concise one-line notice per incremental rebuild
(`Rebuilt … in X ms`), and any warnings or errors. The raw line-by-line Vite
firehose and debug-level internals are hidden. Pass `--verbose` (or `-v`, or the
global `pyxle -v dev`) to restore the full output when troubleshooting. Genuine
signal — errors, warnings, the URLs, and rebuild success/failure — is always
shown, at every verbosity.

**Server logs in the browser console (dev only).** While `pyxle dev` is running,
your server-side `logging` output (from loaders, actions, and your own modules)
is forwarded to the browser devtools console, prefixed `[pyxle:server]` and
mapped to the matching `console` method (`info` → `console.info`, `warning` →
`console.warn`, `error` → `console.error`). This lets you follow server logs
without leaving the browser. By default only `INFO` and above from your own
loggers are forwarded; `--verbose` additionally forwards `DEBUG` records and the
framework's own internal loggers. This is strictly a development feature — it
never runs under `pyxle serve` and never appears in the production bundle.

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

Validate `.pyxl` files, configuration, and dependencies without starting the server. Each `.pyxl` file is checked at three levels:

- **Python syntax** (via `ast`) and Pyxle structural rules (loader/action shape, `HEAD`, …).
- **Python semantics** (via pyflakes): undefined names — e.g. a symbol you `raise` but never imported — unused imports, redefinitions. Compiler-injected runtime names (`server`, `action`, `LoaderError`, `ActionError`, …) are recognized, so the idiomatic patterns never read as undefined.
- **JSX syntax** (via Babel): unclosed tags/expressions, mismatched braces, TypeScript in a client block, and **duplicate `export default`** (which Babel accepts but the build rejects).

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

**Example output:**

```
ℹ️  Checked 12 .pyxl file(s) in my-app/
✅ All checks passed
```

Errors are reported per file as `[section] line N: message`, with the file path on the next line (every file is checked, so one broken file never aborts the scan):

```
ℹ️  Checked 12 .pyxl file(s) in my-app/
  error: [python] line 15: @server function must be async
    --> pages/index.pyxl
  error: [jsx] line 8:10: Unterminated JSX contents
    --> pages/settings.pyxl
❌ Check failed with 2 error(s)
```

Exit code is `0` when clean, `1` when any error is found.

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

  Pages:
  ▶️  /  — pages/index.pyxl  [loader=load_home]
  ▶️  /about  — pages/about.pyxl
  ▶️  /blog/{slug}  — pages/blog/[slug].pyxl  [loader=load_post]

  API Routes:
  ▶️  /api/pulse  — pages/api/pulse.py
```

Use `--json` for machine-readable output.

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

# Write a file with custom metadata
pyxle openapi --out openapi.json --title "Acme API" --api-version 2.0.0
```

The schema is derived from runtime introspection of the compiled action modules, so it always matches what the dispatcher actually validates. Requires the `[pydantic]` extra (`pip install "pyxle-framework[pydantic]"`); the command exits with an error if Pydantic isn't installed or a page module can't be imported.
