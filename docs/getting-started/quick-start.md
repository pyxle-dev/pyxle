# Quick Start

Create a working Pyxle app in under 5 minutes.

## 1. Scaffold a new project

```bash
pyxle init my-app
```

This creates a `my-app/` directory with a complete starter project.

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

Open [http://localhost:8000](http://localhost:8000) in your browser. You should see the Pyxle starter page — a centered card showing the framework version, server time, and a link to edit `pages/index.pyxl`.

Tailwind compiles automatically because the scaffold ships with `postcss.config.cjs` — PostCSS runs as part of the Vite pipeline, so there's nothing separate to start.

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
  ▶️  /  — pages/index.pyxl  [loader=load_home]

  API Routes:
  ▶️  /api/pulse  — pages/api/pulse.py
```

## 6. Validate your project

```bash
pyxle check
```

This validates `.pyxl` syntax, checks your config file, and reports any issues.

## Next steps

- Understand what each file does: [Project Structure](project-structure.md)
- Learn the `.pyxl` file format: [`.pyxl` Files](../core-concepts/pyxl-files.md)
- Add a new page with data loading: [Data Loading](../core-concepts/data-loading.md)
