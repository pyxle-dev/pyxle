# Debugging

A Pyxle app has two runtimes: **Python** (your `@server` loaders, `@action`
handlers, and the request pipeline) and **Node.js** (the SSR worker that renders
your React components to HTML). Knowing which side an error comes from tells you
where to look. This guide covers both.

## Read the error first

In development, Pyxle surfaces errors in three places at once:

- **The browser error overlay** — a full-screen panel with the message, the file
  and line, and a breadcrumb of what the framework was doing (loading, rendering,
  head evaluation). This is the fastest signal for a broken page.
- **The terminal** running `pyxle dev` — the same error, plus the Python
  traceback for loader/action failures.
- **The browser devtools console** — your server-side `logging` output is
  forwarded here during `pyxle dev`, prefixed `[pyxle:server]`, so you can watch
  server logs without leaving the page. `pyxle -v dev` also forwards `DEBUG`
  records and framework-internal logs.

In production (`debug=false`) error responses are intentionally generic — no
stack traces or file paths in the body. The full detail goes to the **server
log** instead. Check your process manager's logs (e.g. `journalctl -u myapp`).

## Breakpoints in .pyxl files

Beyond `pdb`, Pyxle has a full breakpoint-debugging story: `pyxle dev --inspect`
hosts a debug server so VS Code (or any DAP client) can pause a request at a
breakpoint set **directly in a `.pyxl` file** — Python loaders/actions on the
server side, and the React half through the browser. That workflow has its own
guide: [Debugging `.pyxl` files](debugging-pyxl.md).

## Debugging a loader or action (the Python side)

`@server` and `@action` functions are plain `async` functions — debug them like
any Python. Drop a breakpoint:

```python
@server
async def load_dashboard(request):
    breakpoint()          # or: import pdb; pdb.set_trace()
    data = await fetch_stats(request.state.db)
    return {"stats": data}
```

Run `pyxle dev` **in a terminal you can type into** (not detached) and the `pdb`
prompt appears there when the loader runs. Or add logging — it shows in the
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

## Reading the compiled output

Pyxle compiles each `.pyxl` into a Python module and a JavaScript module under
the build directory (`.pyxle-build/` in dev, `dist/` for a production build).
Reading them demystifies "where did my code go":

- `.pyxle-build/server/pages/…​.py` — the **Python** half: your `@server` /
  `@action` functions with the runtime imports the compiler injected at the top
  (`from pyxle.runtime import server`, etc.).
- `.pyxle-build/client/…​` — the **JavaScript** half: your component, ready for
  Vite.

In development, Python tracebacks already point at your `.pyxl` file and line
(see [Debugging `.pyxl` files](debugging-pyxl.md)). A traceback from a
**production build** points at the compiled `.py`; the compiler preserves your
code verbatim below the injected header, so mapping back to your `.pyxl` is a
small, constant offset. The build directory is disposable — delete it and the
next `pyxle dev`/`build` regenerates it.

## Static checks

Two commands catch problems before you run the page:

- **`pyxle check`** — validates `.pyxl` syntax, Python semantics, and JSX. Note
  that a green check does not prove a page *renders*: a component reading a loader
  key that doesn't exist is a runtime error, not a static one.
- **`pyxle typecheck`** — runs TypeScript over your compiled JSX when you've opted
  into TypeScript (see the [TypeScript guide](typescript.md)).

## Common symptoms

| Symptom | Likely cause | Where to look |
|---------|--------------|---------------|
| Blank page, overlay shows a Python traceback | Loader/action raised | `pyxle dev` terminal; add a `breakpoint()` |
| `window is not defined` | Browser global at render scope | The named `.pyxl`; use `useEffect`/`<ClientOnly>` |
| `'State' object has no attribute 'db'` | Missing plugin | `plugins` in `pyxle.config.json` |
| Component data is `undefined` | Loader key ≠ component prop | Compare the loader's return dict to the component's `data` usage |
| Change didn't take effect | Editing built output, or a stale build | Edit the `.pyxl` source, not `.pyxle-build/` |
| Works in dev, fails in prod | Debug-only behavior / missing env | Server log (prod hides detail from the response) |

## Next steps

- [Debugging `.pyxl` files](debugging-pyxl.md) — breakpoints in `.pyxl` source with VS Code or any DAP client
- [Error Handling](error-handling.md) — `LoaderError`, error boundaries, 404s
- [Testing](testing.md) — unit-test loaders and actions
- [Client Components](client-components.md) — browser-only code and `<ClientOnly>`
