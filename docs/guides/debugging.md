# Debugging

A Pyxle app has two runtimes: **Python** (your `@server` loaders, `@action`
handlers, and the request pipeline) and **Node.js** (the SSR worker that renders
your React components to HTML). Knowing which side an error comes from tells you
where to look. This guide covers both — including how to set a breakpoint
directly in a `.pyxl` file, on a line inside a `@server` loader *and* on a line
inside the JSX below it, and have both bind.

There is no bespoke Pyxle debugger: `pyxle dev --inspect` exposes the Python half
to any debugger, dev source maps expose the React half to the browser, and the
VS Code extension turns each into one launch.

---

## Install the VS Code extension

Breakpoint debugging from the editor needs **[Pyxle Language Tools][ext]**
(0.3.0 or newer). Install it from the command line:

```bash
code --install-extension pyxle.pyxle-language-tools
```

…or search for **Pyxle Language Tools** in the Extensions view (`Ctrl+Shift+X` /
`Cmd+Shift+X`), or install it from the [VS Code Marketplace][ext] in a browser.

Two more requirements, both one-time:

- **pyxle-framework 0.8.0 or newer** in the environment VS Code has selected. The
  debugger relies on line mapping added in 0.8.0 and launches the dev server as
  `python -m pyxle`, which earlier versions don't provide. Upgrade with
  `pip install --upgrade pyxle-framework`. While a `.pyxl` file is open, a
  status-bar item shows which interpreter that is — click it (or run **Pyxle:
  Select Python Interpreter**) to change it.
- **The Python extension** (`ms-python.python`) for Python-side breakpoints. The
  debugger prompts you if it's missing, and still debugs the React side without it.

Nothing else to install — debugpy ships with the framework.

The extension also gives you syntax highlighting, diagnostics, completions,
hover, go-to-definition and formatting for `.pyxl` files; see
[Editor setup](editor-setup.md) for the full feature list and the language-server
details.

[ext]: https://marketplace.visualstudio.com/items?itemName=pyxle.pyxle-language-tools

---

## Quick start: breakpoints in a .pyxl file

Open a `.pyxl` file and press **F5**. With no `launch.json` yet, Pyxle asks which
half to debug — **Backend — Python** or **Frontend — React**. Pick one, or commit
the configurations to skip the prompt:

```json
{
  "version": "0.2.0",
  "configurations": [
    { "type": "pyxle", "request": "launch", "name": "Debug Pyxle app" },
    { "type": "pyxle", "request": "launch", "name": "Debug Pyxle app (React browser)", "server": false }
  ]
}
```

Choosing **Backend** runs your dev server under the Python debugger — one clean
debug session that VS Code owns. A breakpoint in a loader or action pauses the
request with the `.pyxl` frame in the stack. Once the server is ready, your app
opens in the browser. **Stop**, **Restart**, and **Pause** all act on that single
session — Stop tears the whole server (Vite, SSR workers) down with it.

**To debug the React half too**, run the **"Debug Pyxle app (React browser)"**
configuration (it's just `"server": false`) — a standalone browser session pointed
at the running dev server. A breakpoint you set in the component pauses there, in
the same `.pyxl` file you set the loader breakpoint in. It's a *separate* session
on purpose: keeping the browser out of the Python session's toolbar means each
side has clean, unambiguous Stop / Restart / Pause.

If the Frontend session had to start its own `pyxle dev` (nothing was running),
**stopping it offers to stop that server too** — a one-click "Stop server".
Restarting the Frontend session keeps the server up. If instead it attached to a
server something else started (Backend, or a `pyxle dev` you ran yourself),
stopping Frontend leaves that server running untouched.

To debug **both** halves, start **Backend** first — it runs the dev server — then
start **Frontend**, which attaches its browser to that same server. Stop on either
side then does the right thing: stopping Frontend leaves the server (Backend owns
it), stopping Backend tears it down. Frontend on its own works standalone too;
just don't start Backend on top of a Frontend-started server, or they'll race for
the port (the second fails to bind with "address already in use" — stop the first,
then start the other).

The launch configuration accepts a few knobs:

| Property | Default | Description |
|----------|---------|-------------|
| `cwd` | `${workspaceFolder}` | Pyxle project root (contains `pages/` and `pyxle.config.json`). |
| `server` | `true` | Run the dev server under the Python debugger so breakpoints in loaders and actions bind. Set `false` to debug **only** the React side in a standalone browser session. |
| `browser` | `true` | When debugging Python, open the app in your browser once the server is ready. |
| `url` | dev server root | Page to open. |
| `args` | `[]` | Extra arguments passed to `pyxle dev` (e.g. `["--port", "3000"]`). |
| `justMyCode` | `true` | Keep stepping inside your own code. Set `false` to step into framework internals. |

To attach to an already-running server instead of launching one, use
`"request": "attach"` (see [Attaching from any DAP client](#attaching-from-any-dap-client)).

The extension also contributes a **"Pyxle: Open Studio"** command, which opens the
running server's [Studio dashboard](studio.md) from the command palette.

---

## Read the error first

In development, Pyxle surfaces errors in three places at once:

- **The browser error overlay** — a full-screen panel with the message, the file
  and line, and a breadcrumb of what the framework was doing (loading, rendering,
  head evaluation). This is the fastest signal for a broken page, and it survives
  a reload: the current error is replayed when the page reconnects.
- **The terminal** running `pyxle dev` — the same error, plus the Python
  traceback for loader/action failures. A file that will not compile is reported
  as `pages/about.pyxl:7:9: unexpected indent`, and that URL serves the compile
  error rather than the last version that built.
- **The browser devtools console** — your server-side `logging` output is
  forwarded here during `pyxle dev`, prefixed `[pyxle:server]`, so you can watch
  server logs without leaving the page. The same records also print in the
  terminal. A page's own `log = logging.getLogger(__name__)` is labelled by its
  source file — `[pyxle:server pages/about.pyxl] loading` — while a logger you
  name yourself keeps that name. `pyxle -v dev` also forwards `DEBUG` records
  and framework-internal logs to the console.

In production (`debug=false`) error responses are intentionally generic — no
stack traces or file paths in the body. The full detail goes to the **server
log** instead. Check your process manager's logs (e.g. `journalctl -u myapp`).

### Your own `log.info` is silent in production until you configure it

`pyxle dev` installs a logging bridge for you: it lowers the root logger to
`INFO` so your records reach both the terminal and the browser console.
`pyxle serve` installs nothing — the process is yours, and Python's defaults
apply. So a `log.info(...)` you watched work all through development is
**silent** once you deploy, while `log.warning` and above still reach stderr
(unformatted, through Python's last-resort handler). Nothing is broken and
nothing warns you; the level was never configured.

Configure it once, at import time, in a module the server loads — a page, or a
module your pages import:

```python
import logging

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")

log = logging.getLogger(__name__)
```

Two things this is **not**:

- It is not the [observability](observability.md) `accessLog` option. That
  configures the `pyxle.access` logger only and leaves the level of your own
  loggers untouched — turning it on will not make your `log.info` appear.
- It is not something a layout can reliably do for the whole app. A layout is
  not a startup hook: its module is imported lazily during a render, so
  configuration written at the top of `layout.pyxl` can land *after* the first
  page loader has already logged. Put it where the pages that log will import
  it.

Framework errors do not depend on any of this: an uncaught exception in a
`@server` loader or an `@action` is logged with its full traceback at `ERROR`
regardless of what you configure.

---

## Debugging a loader or action (the Python side)

`@server` and `@action` functions are plain `async` functions — debug them like
any Python. Set a breakpoint in the editor as above, or drop one in code:

```python
@server
async def load_dashboard(request):
    breakpoint()          # or: import pdb; pdb.set_trace()
    data = await fetch_stats(request.state.db)
    return {"stats": data}
```

For `pdb`, run `pyxle dev` **in a terminal you can type into** (not detached) and
the prompt appears there when the loader runs. Or add logging — it shows in the
terminal *and* the browser console in dev:

```python
import logging
log = logging.getLogger(__name__)

@server
async def load_dashboard(request):
    log.info("loading dashboard for %s", request.url.path)
    ...
```

To unit-test a loader/action in isolation (no server, no browser), use the
[`pyxle.testing` helpers](testing.md).

Two common Python-side errors have targeted messages:

- **`request.state.<name>` is missing** (e.g. `request.state.db` without the
  `pyxle-db` plugin) — the error names the attribute and the plugin that provides
  it. Add the plugin to `pyxle.config.json`.
- **A loader raising `LoaderError`** triggers the nearest `error.pyxl` boundary;
  see [Error Handling](error-handling.md).

---

## Debugging rendering (the Node/SSR side)

Your React component runs **on the server** during SSR before it ever runs in the
browser. Errors that mention rendering come from the Node worker:

- **`window is not defined`** (or `document`, `localStorage`, …) — the component
  touched a browser global at render scope. These don't exist during SSR. Move
  the code into a `useEffect` or an event handler, or wrap the subtree in
  `<ClientOnly>`. The dev error names your `.pyxl` file and the remedy — see
  [Client Components](client-components.md).
- **`Dynamic require of "react" is not supported`** — a dependency resolved to a
  CommonJS-only build. Pyxle resolves dependencies ESM-first; a package that is
  genuinely CommonJS-only reports an actionable error naming the module. See
  [Third-party packages → CommonJS packages and SSR](third-party-packages.md#commonjs-packages-and-ssr).

`console.log` inside a component prints to the **`pyxle dev` terminal** during the
server render (and to the browser console after hydration) — so a value that logs
twice is telling you it ran on both the server and the client.

To step through the SSR worker itself, run it under the Node inspector by setting
`NODE_OPTIONS`:

```bash
NODE_OPTIONS="--inspect" pyxle dev
```

then open `chrome://inspect`. This is rarely needed for app code — it's for
diagnosing the render transport itself.

---

## How the breakpoint mapping works

The feature is two stock debuggers plus line mapping, which is why it works so
broadly:

- **The Python half** (`@server` loaders, `@action` handlers). The compiler embeds
  a line map in every generated server module, and in development the server
  imports those modules with `co_filename` and line numbers remapped to the
  original `.pyxl` source. To [debugpy](https://github.com/microsoft/debugpy) the
  running code simply *is* your `.pyxl` file, so breakpoints set there bind
  natively. The default launch runs the dev server under debugpy directly;
  `--inspect` instead hosts a debugpy server for clients to attach to.
- **The React half** (the JSX in the same file). The compiler writes a source-map
  sidecar for each generated `.jsx` module, and the dev Vite config includes a
  small dev-only plugin that attaches those maps pointing back at the real `.pyxl`
  file on disk. VS Code's built-in js-debug resolves that file and binds
  breakpoints in it; browser devtools map every position the same way. The map is
  fetched by the browser, so it carries only the JSX lines — the `@server` and
  `@action` Python in the same file is never sent to a client.

Both mappings exist only in development. A production build ships neither.

---

## CLI reference

The debugger flags live on `pyxle dev` (and `pyxle studio`, which accepts
`--inspect` and `--inspect-port`):

```bash
pyxle dev --inspect
```

| Flag | Default | Description |
|------|---------|-------------|
| `--inspect` / `--no-inspect` | `false` | Host a debugpy debug server inside the dev-server process, bound to `127.0.0.1`. debugpy ships with the framework. |
| `--inspect-port` | `5678` | Port for the debug server. When it's busy, Pyxle falls back to an ephemeral port and records the actual endpoint in the discovery file, so editor attach flows keep working. |
| `--inspect-wait` / `--no-inspect-wait` | `false` | With `--inspect`: block startup until a debugger attaches — for breakpoints in code that runs during boot. |

Only the dev-server process is debugged: the debugger environment is never
injected into subprocesses (Vite, SSR workers, or anything a loader shells out to).

---

## Attaching from any DAP client

debugpy speaks the Debug Adapter Protocol, so VS Code is a convenience, not a
requirement. From neovim (`nvim-dap`), Emacs (`dape`), or any other DAP client,
attach to the endpoint `--inspect` printed — the equivalent of:

```json
{
  "type": "debugpy",
  "request": "attach",
  "connect": { "host": "127.0.0.1", "port": 5678 }
}
```

Set breakpoints in `.pyxl` files by their real path. Because the server's code
objects already carry `.pyxl` filenames, no path mappings or source translations
are needed — stack frames, stepping, and `evaluate` in a paused loader all just
work.

---

## Tracebacks point at .pyxl

The same line mapping improves life with no debugger attached at all: in
development, a Python traceback from a loader or action names your `.pyxl` file
and line — not a compiled module under `.pyxle-build/`. The terminal, the browser
error overlay, and your logs all agree on where the error actually is.

---

## The discovery file

While running, the dev server writes `.pyxle-build/dev-server.json` describing
itself, and removes it on shutdown. This is what editor tooling (the extension's
F5 flow and "Open Studio") reads instead of guessing ports:

| Key | Type | Meaning |
|-----|------|---------|
| `pid` | `integer` | Dev-server process id. |
| `version` | `string` | Pyxle version. |
| `startedAt` | `number` | Unix timestamp of server start. |
| `projectRoot` | `string` | Absolute path of the project. |
| `server` | `object` | `{ "host", "port" }` of the Starlette server. |
| `vite` | `object` | `{ "host", "port" }` of the Vite dev server. |
| `url` | `string` | Browser-reachable base URL. |
| `studio` | `string` \| `null` | Studio dashboard URL (`null` when Studio is disabled). |
| `debugpy` | `object` \| `null` | `{ "host", "port" }` of the debug server (`null` without `--inspect`). |

The file is written atomically, and a failed write never takes down the server.

---

## Reading the compiled output

Pyxle compiles each `.pyxl` into a Python module and a JavaScript module under
the build directory (`.pyxle-build/` in dev, `dist/` for a production build).
Reading them demystifies "where did my code go":

- `.pyxle-build/server/pages/…​.py` — the **Python** half: your `@server` /
  `@action` functions with the runtime imports the compiler injected at the top
  (`from pyxle.runtime import server`, etc.).
- `.pyxle-build/client/…​` — the **JavaScript** half: your component, ready for
  Vite.

In development you rarely need this, because tracebacks and breakpoints already
point at your `.pyxl`. A traceback from a **production build** points at the
compiled `.py`; the compiler preserves your code verbatim below the injected
header, so mapping back to your `.pyxl` is a small, constant offset. The build
directory is disposable — delete it and the next `pyxle dev`/`build` regenerates it.

---

## Static checks

Two commands catch problems before you run the page:

- **`pyxle check`** — validates `.pyxl` syntax, Python semantics, and JSX. Note
  that a green check does not prove a page *renders*: a component reading a loader
  key that doesn't exist is a runtime error, not a static one.
- **`pyxle typecheck`** — runs TypeScript over your compiled JSX when you've opted
  into TypeScript (see the [TypeScript guide](typescript.md)).

---

## Caveats

Worth knowing before you rely on breakpoint debugging:

- **A paused breakpoint pauses the whole server.** `pyxle dev` is a single
  process, so while you sit at a breakpoint every in-flight request waits. That's
  the right trade-off for development — and one reason production never hosts a
  debugger.
- **`justMyCode` defaults to `true`.** Stepping stays inside your own code; set it
  to `false` in the launch configuration to step into Pyxle's internals.
- **React debugging happens in the browser, not the SSR worker.** Your component
  renders first in the Node SSR process, but the debugger doesn't attach there —
  it's the same code with the same source maps in the browser, which is where you
  debug it. Server-render `console.log` output still prints to the `pyxle dev`
  terminal (see [the SSR side](#debugging-rendering-the-nodessr-side)).
- **Production is untouched.** `pyxle serve` never remaps line numbers and never
  hosts debugpy, regardless of flags or config.

---

## Troubleshooting

**"No module named pyxle" when you press F5.** VS Code has a Python interpreter
selected that doesn't have pyxle installed — a common split when pyxle lives in
one environment (a virtualenv, pyenv, or system Python) while VS Code points at
another. The launch runs `python -m pyxle dev` under the *selected* interpreter,
so it has to be the one where `pyxle` is installed. The extension checks this
before launching and offers a **Select Interpreter** button; you can also run
**Python: Select Interpreter** from the command palette and pick the environment
your terminal's `pyxle` command uses (`which pyxle` / `where pyxle` shows it).

**"No module named pyxle.\_\_main\_\_", or the debugger says the interpreter is
too old.** The selected interpreter has pyxle, but a version older than 0.8.0.
Either switch to the environment that has 0.8.0+ (**Select Interpreter** in the
message, or the status-bar item while a `.pyxl` file is open) or run
`pip install --upgrade pyxle-framework` in that environment. The check asks what
the interpreter can actually *do*, not what its package metadata says, so an
editable install whose recorded version is stale still launches normally.

---

## Common symptoms

| Symptom | Likely cause | Where to look |
|---------|--------------|---------------|
| Blank page, overlay shows a Python traceback | Loader/action raised | `pyxle dev` terminal; set a breakpoint in the loader |
| Page shows "Build failed" | The `.pyxl` (or a layout wrapping it) has a syntax error | The file, line and column named on the page and in the `pyxle dev` terminal |
| A new page 404s in dev | Almost never routing — a page that has never compiled registers no route | The page itself: in dev its URL answers "Build failed" with the file, line and message. A plain 404 means no source claims that address |
| `window is not defined` | Browser global at render scope | The named `.pyxl`; use `useEffect`/`<ClientOnly>` |
| `'State' object has no attribute 'db'` | Missing plugin | `plugins` in `pyxle.config.json` |
| Component data is `undefined` | Loader key ≠ component prop | Compare the loader's return dict to the component's `data` usage |
| Change didn't take effect | Editing built output, or a stale build | Edit the `.pyxl` source, not `.pyxle-build/` |
| Works in dev, fails in prod | Debug-only behavior / missing env | Server log (prod hides detail from the response) |
| Breakpoint never binds | Wrong interpreter, or framework older than 0.8.0 | See [Troubleshooting](#troubleshooting) |

---

## Next steps

- [Editor setup](editor-setup.md) — installing the extension and language server, and everything else it does
- [Pyxle Studio](studio.md) — the dev dashboard; `pyxle studio --inspect` runs both
- [Error Handling](error-handling.md) — `LoaderError`, error boundaries, 404s
- [Testing](testing.md) — unit-test loaders and actions
- [Client Components](client-components.md) — browser-only code and `<ClientOnly>`
- [CLI reference → `pyxle dev`](../reference/cli.md#pyxle-dev) — every dev-server flag
