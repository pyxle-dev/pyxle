# Quick Start

Create a working Pyxle app in under 5 minutes.

## 1. Scaffold a new project

```bash
pyxle init my-app
```

`pyxle init` is **interactive** — it asks a few questions and generates a project tailored to your answers:

```
◆ Use Tailwind CSS?                          › No / Yes   (↑/↓ + Enter)
◆ Add shadcn/ui components?                  › No / Yes   (only when Tailwind is on)
◆ Customize the default import alias (@/*)?  › No / Yes
```

Each question is an arrow-key selection with the default highlighted — press
Enter to accept it.

- **Tailwind CSS** — opt in to Tailwind v4, wired straight into Vite (no `postcss.config`, no separate watcher). Decline it and you get a clean baseline where plain CSS **and** CSS Modules work out of the box.
- **shadcn/ui** — sets up [shadcn/ui](https://ui.shadcn.com) (implies Tailwind), so `npx shadcn@latest add button` just works.
- **Import alias** — the shorthand for absolute imports of your own modules: `import { Button } from '@/components/ui/button'` instead of a brittle `../../` path. `@/*` is the convention (and what shadcn expects); almost everyone keeps it, so the value input only appears if you choose to customize.

This creates a `my-app/` directory with a complete starter project.

### Non-interactive (CI / scripts)

When stdin isn't a terminal, `pyxle init` never blocks on a prompt — it uses flags and defaults. Drive it explicitly:

```bash
pyxle init my-app --yes                      # accept all defaults (no Tailwind)
pyxle init my-app --tailwind --no-shadcn     # Tailwind, no shadcn
pyxle init my-app --shadcn                   # shadcn (implies Tailwind)
pyxle init my-app --import-alias '~/*'       # custom alias
```

### Scaffold into the current directory

Already made the directory (or ran `git init`)? Scaffold in place with `.` — the project name is derived from the directory name:

```bash
mkdir my-app && cd my-app
pyxle init .            # requires an empty directory (or pass --force)
```

## 2. Install dependencies

```bash
cd my-app
pyxle install
```

This runs both `pip install -r requirements.txt` and `npm install`. You can also run them separately:

```bash
pip install -r requirements.txt
npm install
```

`requirements.txt` lists your app's runtime dependencies (Starlette, Uvicorn, HTTPX). Pyxle itself comes from the `pip install pyxle-framework` in step 1.

## 3. Start the dev server

```bash
pyxle dev
```

The console prints a short summary — the local URL, the Vite URL, the route count, and how long startup took — then stays quiet unless something needs your attention:

```
✅ Pyxle dev server ready in 512 ms
ℹ️    Local:   http://127.0.0.1:8000
ℹ️    Vite:    http://127.0.0.1:5173
ℹ️    Routes:  1 page(s), 1 API route(s)
```

Open [http://localhost:8000](http://localhost:8000) in your browser. You should see the Pyxle starter page — a centered card showing the framework version, server time, and a link to edit `pages/index.pyxl`.

Each edit you save prints one concise `Rebuilt … in X ms` line. Your server-side `logging` output also streams to the **browser** devtools console (prefixed `[pyxle:server]`) so you can follow loaders and actions without switching windows. Need the full picture — raw Vite logs and debug internals? Run `pyxle dev --verbose`. See the [CLI reference](../reference/cli.md#pyxle-dev) for details.

CSS just works: Vite compiles every stylesheet you import — plain CSS, CSS Modules, and (if you enabled it) Tailwind v4 via the `@tailwindcss/vite` plugin — with hot reload. There's nothing separate to start. See the [Styling guide](../guides/styling.md).

## What just happened?

When you ran `pyxle dev`, the framework:

1. **Compiled** `pages/index.pyxl` -- split the Python server code from the React JSX
2. **Started Vite** -- the JavaScript bundler that serves your React components with hot reload
3. **Started Starlette** -- the Python ASGI server that handles routing, SSR, and API requests
4. **Ran the `@server` loader** -- fetched data on the server and passed it as props to React
5. **Rendered HTML on the server** -- sent fully-rendered HTML to the browser (SSR)
6. **Hydrated on the client** -- React took over the server-rendered HTML for interactivity

## 4. Make a change

Open `pages/index.pyxl` in your editor. Change the `message` returned by `load_home`:

```python
@server
async def load_home(request):
    now = datetime.now(tz=timezone.utc)
    return {
        "version": __version__,
        "time": now.strftime("%H:%M:%S UTC"),
        "message": "Hello from my Pyxle app!",
    }
```

Save the file. The browser reloads automatically with your updated message.

## 5. Check your routes

```bash
pyxle routes
```

This prints the route table derived from your `pages/` directory:

```
ℹ️  Routes for my-app/

  Pages:
  ▶️  / — index.pyxl  [loader=load_home]

  API Routes:
  ▶️  /api/pulse — api/pulse.py

✅ 2 route(s) found
```

Paths are shown relative to `pages/`.

## 6. Validate your project

```bash
pyxle check
```

This validates your `.pyxl` files — Python syntax, JSX, and semantic issues like undefined names — plus your config file, and reports any problems it finds. Everything Python-side ships with `pip install pyxle-framework`, and the JSX pass runs on the Node.js install you already have from step 1 — so on a fresh project you should see:

```
ℹ️  Checked 2 .pyxl file(s) in my-app/
✅ All checks passed
```

## Next steps

- Understand what each file does: [Project Structure](project-structure.md)
- Learn the `.pyxl` file format: [`.pyxl` Files](../core-concepts/pyxl-files.md)
- Add a new page with data loading: [Data Loading](../core-concepts/data-loading.md)
