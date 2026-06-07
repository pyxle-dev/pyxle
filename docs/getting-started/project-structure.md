# Project Structure

After running `pyxle init my-app`, you get this file tree:

```
my-app/
  pages/
    api/
      pulse.py            # Example API route
    styles/
      tailwind.css        # Tailwind CSS input file
    index.pyxl             # Home page (Python + React)
    layout.pyxl            # Root layout wrapper (React only)
  public/
    branding/             # SVG logos and assets
    styles/
      tailwind.css        # Compiled Tailwind output (pre-built in scaffold)
    favicon.ico
  AGENTS.md               # Conventions guide for AI coding agents
  package.json            # Node.js dependencies and scripts
  pyxle.config.json       # Framework configuration
  requirements.txt        # Python dependencies
  tailwind.config.cjs     # Tailwind CSS configuration
  postcss.config.cjs      # PostCSS configuration
  .gitignore
```

## Key directories

### `pages/`

The pages directory is the heart of your app. Every `.pyxl` file here becomes a route, and every `.py` file under `pages/api/` becomes an API endpoint.

```
pages/
  index.pyxl        -->  /
  about.pyxl        -->  /about
  blog/
    index.pyxl      -->  /blog
    [slug].pyxl     -->  /blog/:slug
  api/
    pulse.py       -->  /api/pulse
    users.py       -->  /api/users
```

See [Routing](../core-concepts/routing.md) for the full rules.

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
| `npm run dev` | Start Vite dev server (used internally by `pyxle dev`) |
| `npm run build` | Bundle with Vite (used by `pyxle build`); Tailwind is compiled in-pipeline via PostCSS |

Tailwind is wired through PostCSS (see `postcss.config.cjs`), so Vite
handles CSS on both `dev` and `build` without a separate script. If you
want a standalone Tailwind watcher anyway, see
[Styling guide → Standalone Tailwind](../guides/styling.md).

### `tailwind.config.cjs`

Tailwind CSS configuration. The scaffold configures it to scan your `pages/` directory for class names:

```javascript
module.exports = {
  content: [
    './pages/**/*.{pyxl,js,jsx,ts,tsx}',
    './.pyxle-build/client/pages/**/*.{js,jsx,ts,tsx}',
  ],
  theme: { extend: {} },
  plugins: [],
};
```

## Next steps

- Learn how `.pyxl` files work: [`.pyxl` Files](../core-concepts/pyxl-files.md)
- Understand routing: [Routing](../core-concepts/routing.md)
