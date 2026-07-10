# Frequently Asked Questions

## General

### What is Pyxle?

Pyxle is a Python-first full-stack web framework. It combines Python server logic with React components in `.pyxl` files, providing file-based routing, server-side rendering, and a development experience inspired by Next.js.

### How is Pyxle different from Next.js?

Next.js uses JavaScript/TypeScript for both server and client code. Pyxle uses Python for server logic (data fetching, mutations, API routes) and React for the UI. If your team prefers Python for backend work but wants React for the frontend, Pyxle bridges the gap.

### How is Pyxle different from Django or Flask?

Django and Flask are backend frameworks that render templates. Pyxle is a full-stack framework that:

- Uses React for the UI instead of template engines
- Provides server-side rendering (SSR) with client-side hydration
- Colocates server logic and UI in the same file
- Includes a bundler (Vite) for hot reload and optimised builds
- Offers file-based routing instead of URL configuration

### How does Pyxle compare to Reflex, NiceGUI, or Streamlit?

Those frameworks let you build the UI in pure Python; Pyxle has you write real React/JSX — so the entire npm React ecosystem is available — colocated with your Python. The honest trade-offs, and when each of them is the better choice, are laid out in the full [framework comparison](guides/comparison.md).

### Is Pyxle production-ready?

Pyxle is in **beta** (the `0.7.x` series). The core features are implemented and tested (2,500+ tests, with a 95% coverage gate enforced in CI), but the API may change before 1.0. Use it for new projects and experiments, but be prepared for breaking changes.

### What Python version do I need?

Python 3.10 or later. Python 3.12 is recommended.

### What Node.js version do I need?

Node.js **20.19** or later (Vite 7's minimum). Node 18 is end-of-life and no longer supported.

## `.pyxl` files

### Can I use TypeScript in `.pyxl` files?

The JSX section is plain JSX — TypeScript *syntax* (type annotations, `interface`, generics, `as` casts) isn't supported there, and the compiler flags it with a clear, source-located error if you try. You still get typed editor support and a `pyxle typecheck` gate against the generated declarations. See the [TypeScript guide](guides/typescript.md) for the full picture.

### Can I import Python code in the JSX section?

No. The Python and JSX sections are compiled into separate files. Python code runs on the server; JSX runs on both server (SSR) and client. Data flows from Python to JSX through the `@server` loader's return value.

### Can I use any React library?

Yes. Any npm package that works with React 19 and Vite should work. Install it via `npm install` and import it in your JSX section.

### Can I have multiple loaders in one file?

No. Only one `@server` function per `.pyxl` file. You can have multiple `@action` functions.

## Routing

### How do I create a 404 page?

Create `pages/not-found.pyxl`:

```jsx
export default function NotFoundPage() {
  return <h1>Page not found</h1>;
}
```

### How do I create a catch-all route?

Use `[...slug].pyxl`:

```
pages/docs/[...slug].pyxl  -->  /docs/anything/here
```

### How do I exclude a folder from routing?

Wrap it in parentheses: `pages/(admin)/dashboard.pyxl` creates the route `/dashboard`, not `/admin/dashboard`.

## Data loading

### My loader is slow. Can I cache results?

Yes. Pyxle has a built-in **page cache**: return a `{"data": ..., "revalidate": <seconds>}`
envelope from your `@server` loader, and the rendered page (loader result *and* SSR output) is
served from cache until it goes stale, then refreshed in the background — so a slow loader runs at
most once per window:

```python
@server
async def load_post(request):
    slug = request.path_params["slug"]
    return {
        "data": {"post": await _fetch_post(slug)},
        "revalidate": 60,   # serve this render from cache for 60s
    }
```

Only cache pages that render no per-user data — a cached render is shared with every visitor.
Loader-less pages opt in with a module-level `CACHE = {"revalidate": N}` directive, and
`pyxle build --static` pre-renders them at build time (SSG/ISR). See the [Caching guide](guides/caching.md).

To cache just an expensive call *inside* a loader, a plain `functools.lru_cache` on the helper —
or an async cache like `aiocache` — still works:

```python
from functools import lru_cache

@lru_cache(maxsize=128)
def _fetch_post(slug):
    # expensive database call
    ...
```

### Can I access the database from a loader?

Yes. Loaders are standard async Python functions. Use any async database library (asyncpg, databases, SQLAlchemy async, Tortoise ORM, etc.).

### What happens if my loader throws an unhandled exception?

If you raise `LoaderError`, the nearest `error.pyxl` is rendered. Other exceptions render a default error page. In dev mode, the error overlay shows the full stack trace.

## Styling

### Do I have to use Tailwind CSS?

No. Tailwind is **opt-in** — `pyxle init` asks whether you want it. Decline it and a fresh project uses plain CSS and CSS Modules out of the box; you can also add Sass or any CSS-in-JS library. Choose it and you get Tailwind v4 wired into Vite. See [Styling](guides/styling.md).

### How do I add a global CSS reset?

Add it to the config:

```json
{
  "styling": {
    "globalStyles": ["styles/reset.css"]
  }
}
```

Or link it in your root layout's `<Head>`:

```jsx
import { Head } from 'pyxle/client';

export default function RootLayout({ children }) {
  return (
    <html lang="en">
      <Head>
        <link rel="stylesheet" href="/styles/reset.css" />
      </Head>
      <body>{children}</body>
    </html>
  );
}
```

## Deployment

### How do I deploy Pyxle?

1. Run `pyxle build` to compile assets
2. Run `pyxle serve --host 0.0.0.0` to start the production server
3. Place behind a reverse proxy (Nginx, Caddy) for TLS

See [Deployment](guides/deployment.md) for Docker examples and detailed instructions.

### Can I deploy to serverless platforms?

Pyxle requires a persistent server for SSR (Node.js workers). It is best suited for container-based deployments (Docker, Kubernetes) or VPS hosting. Serverless platforms that support long-running processes may work, but this is not tested.

### How do I set environment variables in production?

Use `PYXLE_*` environment variables or a `.env.production` file:

```bash
export PYXLE_HOST=0.0.0.0
export PYXLE_PORT=8000
export PYXLE_DEBUG=false
```

## Security

### Is CSRF protection enabled by default?

Yes. The double-submit cookie pattern is active for all `POST`, `PUT`, `PATCH`, and `DELETE` requests. The `<Form>` component and `useAction` hook handle tokens automatically.

### How do I disable CSRF for webhooks?

Exempt specific paths:

```json
{
  "csrf": {
    "exemptPaths": ["/api/webhooks"]
  }
}
```

### Are my environment variables safe?

Variables without the `PYXLE_PUBLIC_` prefix never appear in client-side code. Only `PYXLE_PUBLIC_*` variables are injected into JavaScript bundles.

## Troubleshooting

### `pyxle dev` fails with "npm install required"

Run `pyxle install` or `npm install` in your project directory.

### The page renders without styles

Vite compiles every stylesheet you **import from a JSX module**, so the most
common cause is a stylesheet that isn't imported anywhere. Make sure your CSS
(e.g. `pages/styles/app.css`) is imported from a page or layout. If you enabled
Tailwind, confirm that entry contains `@import "tailwindcss";`. See the
[styling guide](guides/styling.md).

### Hot reload is not working

1. Check that Vite is running (look for the Vite URL in the terminal output)
2. Check the browser console for WebSocket connection errors
3. Try restarting `pyxle dev`

### SSR fails with "Unable to load page module"

The compiled Python module could not be imported. Check:

1. Syntax errors in the Python section of your `.pyxl` file
2. Missing Python dependencies (run `pip install -r requirements.txt`)
3. Import errors in your `@server` function

### Actions return 403 Forbidden

CSRF token mismatch. If you are making manual fetch calls (not using `<Form>` or `useAction`), include the CSRF token header. See [Security](guides/security.md).
