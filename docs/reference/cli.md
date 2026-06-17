# CLI Reference

The `pyxle` CLI manages Pyxle projects from scaffolding to production.

## Global options

| Flag | Description |
|------|-------------|
| `--version` | Print Pyxle version and exit |
| `--log-format [console\|json]` | Output format (default: `console`) |
| `--verbose` / `-v` | Show debug-level output |
| `--quiet` / `-q` | Suppress informational output; show only warnings and errors |

## `pyxle init`

Create a new Pyxle project.

```bash
pyxle init <name> [options]
```

| Argument / Flag | Description |
|----------------|-------------|
| `name` | Project directory name (required) |
| `--force` / `-f` | Overwrite existing directory |
| `--template` / `-t` | Project template. Only `"default"` is supported today (other values error). |
| `--install` / `--no-install` | Run `pip install` and `npm install` after scaffolding (default: no) |

**Examples:**

```bash
pyxle init my-app
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

**Examples:**

```bash
pyxle install
pyxle install --no-python    # Node only
pyxle install ./my-app
```

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
| `--tailwind` / `--no-tailwind` | `true` | Auto-start Tailwind CSS watcher |

**Examples:**

```bash
pyxle dev
pyxle dev --host 0.0.0.0 --port 3000
pyxle dev --no-tailwind --ssr-workers 4
pyxle dev ./my-app --print-config
```

**What it does:**

1. Loads configuration from `pyxle.config.json` + environment variables + CLI flags
2. Compiles `.pyxl` files into Python and JSX modules
3. Starts the Vite dev server for React hot reload
4. Starts the Tailwind watcher (if detected)
5. Starts the Starlette ASGI server
6. Watches for file changes and recompiles automatically

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

**Examples:**

```bash
pyxle build
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
| `--workers` / `-w` | `1` | Number of server worker processes (one per CPU core); `>1` enables multi-core serving |

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

Validate `.pyxl` syntax, configuration, and dependencies.

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
