# Project Structure

`pyxle init` is interactive — the exact files depend on your answers. A default
project (no Tailwind) looks like this:

```
my-app/
  pages/
    api/
      pulse.py            # Example API route
    components/
      Badge.jsx           # Example component using a CSS Module
      Badge.module.css    # Locally-scoped, hashed class names
    styles/
      app.css             # Global CSS (imported from index.pyxl)
    index.pyxl            # Home page (Python + React)
    layout.pyxl           # Root layout wrapper (React only)
  public/
    branding/             # SVG logos and assets
    favicon.ico
  README.md               # Your app's readme — prerequisites and commands
  AGENTS.md               # Conventions guide for AI coding agents
  jsconfig.json           # Import alias (@/*) + editor hints
  vite.config.js          # Re-exports Pyxle's generated Vite config
  package.json            # Node.js dependencies and scripts
  pyxle.config.json       # Framework configuration
  requirements.txt        # Python dependencies
  .env.local              # Generated dev secret (PYXLE_SECRET_KEY) — gitignored
  .gitignore
```

Opting into **Tailwind** replaces `pages/styles/app.css` with an
`@import "tailwindcss";` entry (and drops the CSS-Module example) — there are no
`tailwind.config` or `postcss.config` files. Opting into **shadcn/ui** adds
`components.json` and `lib/utils.js`. See [Styling](../guides/styling.md).

## Key directories

### `pages/`

The pages directory is the heart of your app. Every `.pyxl` file here becomes a route, and every `.py` file under `pages/api/` becomes an API endpoint — unless its name starts with an underscore, which marks it private.

```
pages/
  index.pyxl        -->  /
  about.pyxl        -->  /about
  blog/
    index.pyxl      -->  /blog
    [slug].pyxl     -->  /blog/:slug
  api/
    pulse.py        -->  /api/pulse
    users.py        -->  /api/users
    _db.py          -->  (no route — a helper the endpoints import)
```

A leading underscore follows Python's own convention for "not part of the public surface": `api/_db.py`, `api/__init__.py`, and everything under `api/_internal/` serve no URL but stay importable by the endpoints beside them. The rule reads only the segments at or below `api/` — above it an underscore is just a URL character, so `pages/_admin/api/health.py` still serves `/_admin/api/health`.

See [Routing](../core-concepts/routing.md) and [API Routes](../guides/api-routes.md) for the full rules.

### `public/`

Static files served directly. Anything in `public/` is available at the root URL:

- `public/favicon.ico` --> `http://localhost:8000/favicon.ico`
- `public/branding/logo.svg` --> `http://localhost:8000/branding/logo.svg`

### `.pyxle-build/` (generated at runtime)

Created automatically when you run `pyxle dev` or `pyxle build`. Contains compiled Python modules, transpiled JSX, and Vite configuration. This directory is gitignored -- do not edit files here.

```
.pyxle-build/
  server/           # Compiled Python modules from @server blocks
  client/           # Transpiled JSX components + composed page/layout wrappers
  metadata/         # Per-page metadata (route, loader, head, scripts)
  vite.config.js    # Auto-generated Vite configuration
```

## Key files

### `pages/index.pyxl`

A `.pyxl` file combines Python server logic with a React component. The scaffold's index page demonstrates:

- `@server` decorator for data loading
- React JSX for the UI
- The `<Head>` component from `pyxle/client` for document `<head>` elements

```python
# Python section
from datetime import datetime, timezone
from pyxle import __version__

@server
async def load_home(request):
    now = datetime.now(tz=timezone.utc)
    return {
        "version": __version__,
        "time": now.strftime("%H:%M:%S UTC"),
        "message": "You're ready to build with Pyxle.",
    }
```

```jsx
// JSX section -- receives loader data as props
import React from 'react';
import { Head } from 'pyxle/client';

export default function HomePage({ data }) {
  return (
    <main>
      <Head>
        <title>Pyxle App</title>
      </Head>
      <h1>{data.message}</h1>
      <p>Pyxle v{data.version} &middot; {data.time}</p>
    </main>
  );
}
```

### `pages/layout.pyxl`

The root layout wraps every page. It is JSX-only (no Python section needed):

```jsx
import React from 'react';

export default function AppLayout({ children }) {
  return <>{children}</>;
}
```

### `pages/api/pulse.py`

A plain Python file that serves as an API endpoint. It exports an `endpoint` callable and returns JSON:

```python
from starlette.requests import Request
from starlette.responses import JSONResponse

from pyxle import __version__

async def endpoint(request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok", "pyxle": __version__})
```

### `pyxle.config.json`

Framework configuration. The scaffold ships with a minimal config:

```json
{
  "middleware": []
}
```

See [Configuration Reference](../reference/configuration.md) for all available options.

### `package.json`

Defines Node.js dependencies and npm scripts:

| Script | Purpose |
|--------|---------|
| `npm run dev` | Alias for `pyxle dev` |
| `npm run build` | Alias for `pyxle build` |

It also declares `"engines": { "node": ">=20.19" }` (Vite 7's floor). Vite
compiles CSS on both `pyxle dev` and `pyxle build` — including Tailwind v4 via
the `@tailwindcss/vite` plugin when you opt into it — so there's no separate CSS
script. See [Styling](../guides/styling.md).

### `.env.local`

Every scaffold generates a `.env.local` with a unique `PYXLE_SECRET_KEY` for
development (it signs the CSRF tokens). The file is gitignored — never commit
it; set a real secret in your production environment instead. See
[Environment Variables](../guides/environment-variables.md).

### `jsconfig.json` and `vite.config.js`

`jsconfig.json` declares the import alias (default `@/*`) so `@/lib/utils`
resolves from anywhere; Pyxle wires the same alias into both the Vite build and
the SSR runtime. `vite.config.js` re-exports Pyxle's generated config so the
wider Vite ecosystem (shadcn/ui, editor plugins) finds a config at the project
root — you normally never edit it.

## Next steps

- Learn how `.pyxl` files work: [`.pyxl` Files](../core-concepts/pyxl-files.md)
- Understand routing: [Routing](../core-concepts/routing.md)
