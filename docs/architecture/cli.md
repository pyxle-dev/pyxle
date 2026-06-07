# The CLI

`pyxle` is the command-line entry point users actually type. It's a
**Typer application** with a small set of single-purpose commands:

| Command | Purpose |
|---|---|
| `pyxle init <name>` | Scaffold a new Pyxle project |
| `pyxle install [path]` | Install Python + Node dependencies |
| `pyxle dev [path]` | Run the development server |
| `pyxle build [path]` | Compile and bundle for production |
| `pyxle serve [path]` | Serve a production build |
| `pyxle check [path]` | Validate the project without serving |
| `pyxle routes [path]` | Print the file-based route table |
| `pyxle typecheck [path]` | TypeScript-check compiled JSX |

This doc explains what each command does, how flags and configuration
flow from user input to the rest of the framework, and the design
choices behind the CLI surface.

**Files:**
- `cli/__init__.py` (~1220 lines) — the Typer commands
- `cli/init.py` (~115 lines) — `pyxle init` scaffolding
- `cli/scaffold.py` (~100 lines) — copies template files
- `cli/templates.py` (~50 lines) — template registry
- `cli/logger.py` (~190 lines) — `ConsoleLogger` with structured output
- `config.py` (~500 lines) — `pyxle.config.json` schema and validation

---

## Configuration precedence

Every Pyxle command reads configuration in the same order, from
**lowest to highest priority**:

1. **Built-in defaults** — Hard-coded in `PyxleConfig` field
   defaults (`config.py:44`).
2. **`pyxle.config.json`** — JSON file at the project root.
3. **Environment variables** — Variables starting with `PYXLE_`
   override the corresponding config field.
4. **CLI flags** — Anything you pass on the command line wins.

Higher priority overrides lower priority. So:

```bash
PYXLE_STARLETTE_PORT=9000 pyxle dev --port 8001
```

…ends up running on port 8001 (CLI beats env beats file beats
default).

The mechanism is `PyxleConfig.apply_overrides()` (`config.py:104`),
which returns a *new* frozen instance with selective field updates.
Each layer applies overrides on top of the previous one. The CLI
layer runs last:

```python
file_config = load_config(project_root / "pyxle.config.json")  # 1+2
env_config = file_config.apply_env_overrides()                  # 3
final_config = env_config.apply_overrides(                      # 4
    starlette_host=cli_host,    # only override if non-None
    starlette_port=cli_port,
    debug=cli_debug,
)
```

`apply_overrides()` ignores `None` values, so passing `--port 8001`
overrides while *not* passing `--port` leaves the lower layer alone.
This is the standard "explicit wins, implicit yields" pattern.

Source: `cli/__init__.py:267-340`.

---

## `pyxle init <name>`

Scaffolds a new Pyxle project from a template.

```
$ pyxle init my-app
✅ Created my-app/
ℹ️  Installing Python dependencies...
ℹ️  Installing Node dependencies...
✅ Done. Run `cd my-app && pyxle dev` to start.
```

Under the hood:

1. **Validates the name.** Must be a non-empty string that's safe to
   use as a directory name. The directory must not exist (unless
   `--force` is passed).
2. **Resolves the template.** Defaults to `default`. Templates live
   in `pyxle/templates/scaffold/` inside the package — they're
   shipped with `pyxle-framework`.
3. **Copies the template files** into the new directory using
   `shutil.copytree`. The template includes:
   - `pages/index.pyxl` — a working "Hello, Pyxle" page
   - `pages/api/pulse.py` — a sample API route
   - `package.json` — Vite + React 18 + Tailwind dependencies
   - `pyxle.config.json` — minimal config (`{"middleware": []}`)
   - `requirements.txt` — pinned `pyxle-framework` version
   - `postcss.config.cjs`, `tailwind.config.cjs` — Tailwind setup
   - `public/` — favicon and a few static files
   - `.gitignore` — sensible defaults
4. **Optionally installs dependencies.** With `--install` (the
   default), runs `pip install -e .` and `npm install` in the new
   directory. With `--no-install`, skips both — useful for CI or
   when you want to inspect the scaffold before installing.

Source: `cli/__init__.py:116-211`, `cli/init.py`,
`cli/scaffold.py`.

### Why does the scaffold ship with the framework?

A few alternatives we considered:

- **Download from a Git repository.** Requires the user to have
  network access and `git` installed. Doesn't pin a version.
- **Download from a CDN.** Requires the user to have network
  access. Versions can drift.
- **Generate from a template engine.** Adds a runtime dependency
  on Jinja2 or similar. Templates become harder to read and modify.

Shipping the scaffold *as part of the Pyxle package* means:

- It works offline.
- It's pinned to the framework version automatically.
- You can read it directly: `pip show pyxle-framework` to find the
  install path, then look at `pyxle/templates/scaffold/`.
- You can fork it locally without affecting the upstream package.

The downside is that updating the scaffold requires a Pyxle release,
not a separate publish step. We think the trade-off is worth it for
beta-stage software where the install story matters more than
ergonomics.

---

## `pyxle dev [path]`

Runs the development server. The full lifecycle is documented in
[The dev server](dev-server.md), but the CLI-specific bits are:

### Flags

| Flag | Default | Effect |
|---|---|---|
| `--host` | `127.0.0.1` | Starlette bind host |
| `--port` | `8000` | Starlette bind port |
| `--vite-host` | `127.0.0.1` | Vite bind host |
| `--vite-port` | `5173` | Vite bind port |
| `--debug / --no-debug` | `--debug` | Dev mode toggle |
| `--config <path>` | `pyxle.config.json` | Config file path |
| `--ssr-workers <n>` | from config | Override SSR worker count |
| `--tailwind / --no-tailwind` | auto | Force/disable Tailwind watcher |

### What it does

1. Resolves the project root (CLI argument or current directory).
2. Loads `pyxle.config.json`, applies env vars, applies CLI flags.
3. Resolves global stylesheets and scripts via the styling helpers.
4. Builds a `DevServerSettings` from the merged config.
5. Creates a `DevServer` instance and calls `asyncio.run(server.start())`.

The actual heavy lifting happens inside `DevServer.start()` —
spawning Vite, starting the SSR worker pool, building the Starlette
app, starting the file watcher, running uvicorn. See
[The dev server](dev-server.md).

Source: `cli/__init__.py:247-398`.

### Tailwind handling

Pyxle has optional first-class Tailwind support. The CLI auto-detects
Tailwind by checking for `tailwind.config.cjs` or `tailwind.config.js`
in the project root. If found:

- A separate Tailwind watcher process is spawned alongside Vite.
- The watcher rebuilds the Tailwind CSS file whenever a Tailwind
  source class changes.
- When `postcss.config.cjs` *also* exists, the standalone Tailwind
  watcher is *skipped* — Vite handles Tailwind via PostCSS instead,
  which is faster and integrates with HMR.

The flag `--tailwind / --no-tailwind` lets you force one mode or the
other. Most users never touch it. Source:
`cli/__init__.py:343-378`, `devserver/tailwind.py`.

---

## `pyxle build [path]`

Compiles and bundles the project for production. Detailed walkthrough
in [Build and serve](build-and-serve.md).

### Flags

| Flag | Default | Effect |
|---|---|---|
| `--config <path>` | `pyxle.config.json` | Config file path |
| `--incremental` | `false` (full rebuild) | Skip unchanged files |
| `--dist-dir <path>` | `./dist` | Output directory |

### What it does

1. Loads config, applies env, applies CLI flags.
2. Builds a `DevServerSettings` (yes, the same dataclass `pyxle dev`
   uses — `pyxle build` reuses 100% of the dev server's settings
   structure).
3. Lazily imports `pyxle.build.pipeline.run_build` (lazy because
   importing the build pipeline pulls in `vite.py`, `manifest.py`,
   etc., and we want `pyxle init` and `pyxle check` to start fast).
4. Calls `run_build(settings, dist_dir=...)`.
5. Prints a summary: pages, API routes, client assets.

Source: `cli/__init__.py:400-515`.

### The lazy import pattern

You'll notice `cli/__init__.py` uses lazy imports for the heavy
modules:

```python
def _resolve_run_build():
    from pyxle.build.pipeline import run_build
    return run_build
```

This is intentional. `pyxle init` should not have to load the build
pipeline. `pyxle check` should not have to load Starlette. By
deferring imports until they're actually needed, the CLI startup
stays fast — typically under 100ms for `pyxle --help` or `pyxle
check`, even on cold imports.

This pattern is documented in `pyxle/CLAUDE.md` rule 16 (Lazy
imports for heavy modules).

---

## `pyxle serve [path]`

Serves a production build. See [Build and serve](build-and-serve.md)
for the full walkthrough.

### Flags

| Flag | Default | Effect |
|---|---|---|
| `--host` | `127.0.0.1` | Starlette bind host |
| `--port` | `8000` | Starlette bind port |
| `--config <path>` | `pyxle.config.json` | Config file path |
| `--dist-dir <path>` | `./dist` | Where to read the build from |
| `--skip-build / --no-skip-build` | `false` | Skip the implicit `pyxle build` |
| `--serve-static / --no-serve-static` | `true` | Serve `dist/client/` and `dist/public/` |
| `--ssr-workers <n>` | from config | Override SSR worker count |

### What it does

1. Loads config, applies env, applies CLI flags.
2. **Forces `debug=False`** in the resolved config. This is the
   single most important production override.
3. Optionally runs `pyxle build` first (the default — set
   `--skip-build` to use existing artifacts).
4. Loads `dist/page-manifest.json` to populate the route registry.
5. Builds the Starlette app via `create_starlette_app()` (same
   factory as `pyxle dev`).
6. Spawns uvicorn to serve the app.

Source: `cli/__init__.py:517-733`.

### `--skip-build` is for CI

In a typical CI pipeline:

```yaml
- run: pyxle build              # produces dist/
- run: docker build -t myapp .  # bakes dist/ into the image
```

Then, on the production server:

```bash
pyxle serve --skip-build --host 0.0.0.0 --port 8000
```

`--skip-build` tells Pyxle "the artifacts are already in `dist/`,
don't rebuild them." This separates "compile time" from "run time"
cleanly. In dev, you usually omit the flag and let `pyxle serve`
build for you.

---

## `pyxle check [path]`

Validates the project without starting a server. This is the
linter / pre-commit hook command.

```
$ pyxle check
ℹ️  Checked 28 .pyxl file(s) in my-app/
  error: [python] line 1: invalid syntax
    --> pages/_errors_python_syntax/bad-keyword.pyxl
  error: [python] line 2: @server loader must accept a `request` argument
    --> pages/_errors_python_validation/missing-request.pyxl
  error: [jsx] line 1: JSX syntax error: Unexpected token (4:17)
    --> pages/_errors_jsx_syntax/invalid-expression.pyxl
  ...
❌ Check failed with 14 error(s)
```

### What it checks

1. **The project structure exists.** `pages/` must be a directory.
2. **The config is valid.** `pyxle.config.json` (if present) must
   parse and pass schema validation.
3. **Node.js dependencies are present.** `node_modules/` should
   exist (warning if missing).
4. **Every `.pyxl` file parses cleanly.** Both Python and JSX halves.

### Tolerant mode

The fourth check is the interesting one. `pyxle check` runs the
parser in **tolerant mode**:

```python
result = parser.parse(pyxl_file, tolerant=True, validate_jsx=True)
for diag in result.diagnostics:
    diagnostics.append(...)
```

Source: `cli/__init__.py:780-820`.

Tolerant mode means the parser collects every diagnostic in every
file in **a single pass** instead of stopping at the first error.
This is the right behaviour for a linter — you want to see all your
errors at once, not one error at a time.

`validate_jsx=True` adds Babel-backed JSX validation on top, so
JSX syntax errors (unclosed tags, mismatched braces, invalid
expressions) are also caught. This is the only Pyxle command that
runs Babel validation by default; `pyxle dev` and `pyxle build`
skip it because Vite catches JSX errors at bundle time.

### Defensive per-file wrapping

Each per-file parse is wrapped in a `try/except`:

```python
try:
    result = parser.parse(pyxl_file, tolerant=True, validate_jsx=True)
except Exception as exc:
    diagnostics.append((rel_path, f"[python] parser crashed: {type(exc).__name__}: {exc}"))
    continue
```

This is defense-in-depth. Tolerant mode shouldn't raise — it
collects errors instead — but a future parser bug could throw an
unexpected exception. The defensive wrap catches it, reports it as
a structured diagnostic, and continues scanning the rest of the
project.

This was added during the parser audit on 2026-04-08 after a
pathological fixture (200-level nested expression) was found to
crash CPython's parser stack and abort the entire `pyxle check`
run mid-scan. With the wrap, a single broken file is reported and
the scan continues.

Source: `cli/__init__.py:790-820`. Audit details:
`manual-tests/AUDIT.md`, "Bug 3".

### Cascade suppression

`pyxle check` runs both the Python parser AND Babel for every file.
When the Python parser finds a `[python]` error, the broken Python
content sometimes ends up in the `jsx_code` segment (because the
walker can't classify what isn't valid Python), and Babel then also
fails on it — producing a noisy `[jsx]` error.

The parser handles this internally with **cascade suppression**:
when any `[python]` diagnostic is collected, the JSX validation is
skipped for that file. Source: `compiler/parser.py:1080-1100`.
Result: each file with a Python syntax error reports just the
Python error, not both.

### Exit codes

| Exit code | Meaning |
|---|---|
| `0` | All checks passed, no errors |
| `1` | One or more errors found |
| `2` | Project structure invalid (no `pages/`, etc.) |

You can use `pyxle check` in CI as a pre-merge gate, or as a
pre-commit hook to catch errors before they hit `git`.

---

## The `ConsoleLogger`

All CLI output (info, warning, error, success) goes through
`ConsoleLogger` (`cli/logger.py`). It supports two output modes:

- **Human (`--log-format console`, default)** — colored emoji-prefixed
  output: `ℹ️ `, `✅`, `⚠️`, `❌`, `▶️`. Tested with both light and dark
  terminals.
- **Machine (`--log-format json`)** — newline-delimited JSON
  records. Useful for piping into log aggregators or other tooling.

The logger also supports `--verbose / -v` and `--quiet / -q` to
adjust verbosity. `-q` suppresses everything below warnings; `-v`
shows debug-level info.

The `diagnostic()` method is special — it formats parser
diagnostics with file path and line number in a consistent way:

```
  error: [python] line 5: @server loader must be declared as async
    --> pages/sync-loader.pyxl
```

This is the format `pyxle check` uses, but it's available to any
caller that wants it.

Source: `cli/logger.py:1-192`.

---

## How a CLI invocation flows

Putting everything together, here's what happens when you type
`pyxle dev --port 8001 my-app/`:

```
1. Typer parses the command-line:
   - command: "dev"
   - directory: "my-app/"
   - port: 8001 (override, not None)

2. CLI handler runs:
   - Resolve project_root = "my-app/" → absolute path
   - Load pyxle.config.json from project_root
   - Apply env overrides (PYXLE_*)
   - Apply CLI overrides (port=8001, host=None, debug=None, ...)
   - Resolve global styles/scripts
   - Build DevServerSettings.from_project_root(...)

3. Create the dev server:
   - DevServer(settings, logger, ...)

4. Run it:
   - asyncio.run(server.start())
     - Lifecycle steps from dev-server.md
     - uvicorn serves until Ctrl+C
```

The CLI's job is mostly **input parsing and config resolution**.
Once it has a `DevServerSettings`, it hands off to the dev server
and gets out of the way. The same pattern applies to every command:
parse → resolve → build settings → call into the relevant subsystem.

This is why `cli/__init__.py` is 1220 lines but most of those
lines are flag declarations and validation, not logic. The actual
work happens elsewhere.

---

## Why Typer?

We picked Typer for the CLI surface because:

- **Type hints become flags automatically.** A function parameter
  `port: int = 8000` becomes `--port 8000` with type validation.
  Less boilerplate than `argparse`, less magic than Click's
  `@click.option`.
- **It supports Rich rendering** for help output and error messages,
  which makes the help text readable.
- **It plays nicely with Python type checkers.** mypy, pyright, and
  IDEs all understand the function signatures.
- **It has good docs and a small API.** Easy to onboard.

The downside is that Typer doesn't expose every Click feature
directly — sometimes you have to drop into Click for advanced
behaviour. We've only had to do this once, for a custom shell
completion handler.

---

## Where to read next

- **[The dev server](dev-server.md)** — What `pyxle dev` actually
  starts up after the CLI hands off control.

- **[Build and serve](build-and-serve.md)** — What `pyxle build`
  and `pyxle serve` actually do.

- **[The parser](parser.md)** — How `pyxle check` uses tolerant
  mode to surface every error in every file in one pass.
