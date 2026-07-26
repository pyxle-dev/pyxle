# Debugging .pyxl files

Set a breakpoint directly in your `.pyxl` source — on a line inside a `@server` loader *and* on a line inside the JSX below it — and both bind. `pyxle dev --inspect` exposes the Python half to any debugger, dev source maps expose the React half to the browser, and the VS Code extension turns each into one launch: **F5** for the Python side, a second configuration for the React side. No custom debugger, no compiled-file archaeology.

---

## Quick start (VS Code)

1. Install the **Pyxle Language Tools** extension (0.3.0 or later). For Python-side breakpoints, also install the **Python** extension (`ms-python.python`) — the debugger prompts you if it's missing and still debugs the React side without it.
2. Make sure your project uses **pyxle-framework 0.8.0 or newer** (the debugger relies on line mapping shipped in 0.8.0). Nothing else to install: debugpy ships with the framework.
3. Open a `.pyxl` file and press **F5**. With no `launch.json` yet, Pyxle asks which half to debug — **Backend — Python** or **Frontend — React**. Pick one, or commit the configurations to skip the prompt:

```json
{
  "version": "0.2.0",
  "configurations": [
    { "type": "pyxle", "request": "launch", "name": "Debug Pyxle app" },
    { "type": "pyxle", "request": "launch", "name": "Debug Pyxle app (React browser)", "server": false }
  ]
}
```

Choosing **Backend** runs your dev server under the Python debugger — one clean debug session that VS Code owns. A breakpoint in a loader or action pauses the request in VS Code with the `.pyxl` frame in the stack. Once the server is ready, your app opens in the browser. **Stop**, **Restart**, and **Pause** all act on that single session — Stop tears the whole server (Vite, SSR workers) down with it. It needs the **Python** extension (`ms-python.python`); the debugger offers to install it if it's missing.

**To debug the React half too**, run the **"Debug Pyxle app (React browser)"** configuration (it's just `"server": false`) — a standalone Chrome session pointed at the running dev server. A breakpoint you set in the component pauses there, in the same `.pyxl` file you set the loader breakpoint in. It's a *separate* session on purpose: keeping the browser out of the Python session's toolbar means each side has clean, unambiguous Stop / Restart / Pause.

If the Frontend session had to start its own `pyxle dev` (nothing was running), **stopping it offers to stop that server too** — a one-click "Stop server". Restarting the Frontend session keeps the server up (no prompt). If instead it attached to a server something else started (Backend, or a `pyxle dev` you ran yourself), stopping Frontend leaves that server running untouched — you stop it where you started it.

To debug **both** halves, start **Backend** first — it runs the dev server — then start **Frontend**, which attaches its browser to that same server. That way Stop on either side does the right thing: stopping Frontend leaves the server (Backend owns it), stopping Backend tears it down. Frontend on its own works standalone too; just don't start Backend on top of a Frontend-started server, or they'll race for the port (the second one fails to bind with an "address already in use" error — stop the first, then start the other).

The launch configuration accepts a few knobs:

| Property | Default | Description |
|----------|---------|-------------|
| `cwd` | `${workspaceFolder}` | Pyxle project root (contains `pages/` and `pyxle.config.json`). |
| `server` | `true` | Run the dev server under the Python debugger so breakpoints in loaders and actions bind. Set `false` to debug **only** the React side in a standalone Chrome session. |
| `browser` | `true` | When debugging Python, open the app in your browser once the server is ready. |
| `url` | dev server root | Page to open. |
| `args` | `[]` | Extra arguments passed to `pyxle dev` (e.g. `["--port", "3000"]`). |
| `justMyCode` | `true` | Keep stepping inside your own code. Set `false` to step into framework internals. |

To attach to an already-running server instead of launching one, use `"request": "attach"` (see [Attaching from any DAP client](#attaching-from-any-dap-client)).

The extension also contributes a **"Pyxle: Open Studio"** command, which opens the running server's [Studio dashboard](studio.md) from the command palette.

---

## How it works

There is no bespoke Pyxle debugger — the feature is two stock debuggers plus line mapping, which is why it works so broadly:

- **The Python half** (`@server` loaders, `@action` handlers). The compiler embeds a line map in every generated server module, and in development the server imports those modules with `co_filename` and line numbers remapped to the original `.pyxl` source. To [debugpy](https://github.com/microsoft/debugpy) the running code simply *is* your `.pyxl` file, so breakpoints set there bind natively. The default launch runs the dev server under debugpy directly; `--inspect` instead hosts a debugpy server for clients to attach to.
- **The React half** (the JSX in the same file). The compiler writes a source-map sidecar for each generated `.jsx` module, and the dev Vite config includes a small dev-only plugin that attaches those maps pointing back at the real `.pyxl` file on disk. VS Code's built-in js-debug resolves that file and binds breakpoints in it; browser devtools map every position the same way.

Both mappings exist only in development. A production build ships neither.

---

## CLI reference

The debugger flags live on `pyxle dev` (and `pyxle studio`, which accepts `--inspect` and `--inspect-port`):

```bash
pyxle dev --inspect
```

| Flag | Default | Description |
|------|---------|-------------|
| `--inspect` / `--no-inspect` | `false` | Host a debugpy debug server inside the dev-server process, bound to `127.0.0.1`. debugpy ships with the framework. |
| `--inspect-port` | `5678` | Port for the debug server. When it's busy, Pyxle falls back to an ephemeral port and records the actual endpoint in the discovery file, so editor attach flows keep working. |
| `--inspect-wait` / `--no-inspect-wait` | `false` | With `--inspect`: block startup until a debugger attaches — for breakpoints in code that runs during boot. |

Only the dev-server process is debugged: the debugger environment is never injected into subprocesses (Vite, SSR workers, or anything a loader shells out to).

---

## Attaching from any DAP client

debugpy speaks the Debug Adapter Protocol, so VS Code is a convenience, not a requirement. From neovim (`nvim-dap`), Emacs (`dape`), or any other DAP client, attach to the endpoint `--inspect` printed — the equivalent of:

```json
{
  "type": "debugpy",
  "request": "attach",
  "connect": { "host": "127.0.0.1", "port": 5678 }
}
```

Set breakpoints in `.pyxl` files by their real path. Because the server's code objects already carry `.pyxl` filenames, no path mappings or source translations are needed — stack frames, stepping, and `evaluate` in a paused loader all just work.

---

## Tracebacks point at .pyxl now

The same line mapping improves life with no debugger attached at all: in development, a Python traceback from a loader or action names your `.pyxl` file and line — not a compiled module under `.pyxle-build/`. The terminal, the browser error overlay, and your logs all agree on where the error actually is.

---

## The discovery file

While running, the dev server writes `.pyxle-build/dev-server.json` describing itself, and removes it on shutdown. This is what editor tooling (like the VS Code extension's F5 flow and "Open Studio") reads instead of guessing ports:

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

## Caveats

Worth knowing before you rely on it:

- **A paused breakpoint pauses the whole server.** `pyxle dev` is a single process, so while you sit at a breakpoint every in-flight request waits. That's the right trade-off for development — and one reason production never hosts a debugger.
- **`justMyCode` defaults to `true`.** Stepping stays inside your own code; set it to `false` in the launch configuration to step into Pyxle's internals.
- **React debugging happens in the browser, not the SSR worker.** Your component renders first in the Node SSR process, but the debugger doesn't attach there — it's the same code with the same source maps in the browser, which is where you debug it. (Server-render `console.log` output still prints to the `pyxle dev` terminal — see [Debugging](debugging.md#debugging-rendering-the-nodessr-side).)
- **Production is untouched.** `pyxle serve` never remaps line numbers and never hosts debugpy, regardless of flags or config.
- **debugpy ships with the framework** — `--inspect` works on a default install, no extra needed.

---

## Troubleshooting

**"No module named pyxle" when you press F5.** VS Code has a Python interpreter selected that doesn't have pyxle installed — a common split when pyxle lives in one environment (a virtualenv, pyenv, or system Python) while VS Code points at another. The launch runs `python -m pyxle dev` under the *selected* interpreter, so it has to be the one where `pyxle` is installed. The extension checks this before launching and, when it doesn't match, offers a **Select Interpreter** button; you can also run **Python: Select Interpreter** from the command palette and pick the environment your terminal's `pyxle` command uses (`which pyxle` / `where pyxle` shows it). This is the same interpreter selection every Python project needs — nothing pyxle-specific.

---

## See also

- [Debugging](debugging.md) — the wider guide: reading errors, `pdb`, the SSR side, common symptoms
- [Pyxle Studio](studio.md) — the dev dashboard; `pyxle studio --inspect` runs both
- [CLI reference → `pyxle dev`](../reference/cli.md#pyxle-dev) — every dev-server flag
- [Editor setup](editor-setup.md) — installing the VS Code extension and language server
