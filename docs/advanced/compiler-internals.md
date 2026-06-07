# Compiler Internals

The Pyxle compiler transforms `.pyxl` files into separate Python and JSX artifacts. This document explains how the compilation process works.

## Compilation overview

When you run `pyxle dev` or `pyxle build`, the compiler:

1. Scans the `pages/` directory for `.pyxl` files
2. Parses each file to separate Python from JSX
3. Writes server-side Python modules to `.pyxle-build/server/`
4. Writes client-side JSX modules to `.pyxle-build/client/`
5. Generates layout composition wrappers in `.pyxle-build/client/routes/`
6. Creates a Vite configuration at `.pyxle-build/vite.config.js`

## The parser

The parser (`pyxle/compiler/parser.py`) is **AST-driven** — there are no fence markers, string directives, or per-line keyword heuristics. It finds the Python/JSX boundary by walking the source and, at each position, growing the largest region that parses as valid Python via `ast.parse`; when Python stops parsing, it grows a JSX segment until valid Python resumes.

### How the split works

- The boundary is decided by **what parses as Python**, not by the leading keyword. `import React from 'react'` is not valid Python, so it lands in JSX; `from db import users` is, so it stays in Python.
- Arbitrary alternation is supported (`python | jsx | python | jsx | ...`), including JSX-first files.
- Inside a JSX segment the parser tracks JS structural state (string and template literals, block comments, brace/paren/bracket depth), so Python-looking text inside a JSX body or template literal is never misclassified.

### What the parser extracts

| Artifact | Description |
|----------|-------------|
| `python_code` | All Python lines concatenated |
| `jsx_code` | All JSX lines concatenated |
| `loader` | `@server` function metadata (name, line number, parameters) |
| `actions` | `@action` function metadata (name, line number, parameters) |
| `head_elements` | Static `HEAD` variable content |
| `head_is_dynamic` | Whether `HEAD` is a callable |
| `head_jsx_blocks` | `<Head>...</Head>` JSX blocks extracted for server-side use |
| `script_declarations` | `<Script>` component props |
| `image_declarations` | `<Image>` component props |

### Validation

The parser enforces (raising a compile-time error otherwise):

- At most one `@server` loader per file
- `@server` and `@action` functions must be `async`
- `@server` / `@action` must be defined at module scope (not nested)
- Their first parameter must be named `request`
- `@action` function names must be unique within the file

## Code generation

### Server module (`.pyxle-build/server/pages/*.py`)

The compiled Python module contains:

```python
from pyxle.runtime import server, action

# Original imports from the .pyxl file
from datetime import datetime

# Loader function
@server
async def load_page(request):
    return {"now": datetime.now().isoformat()}

# Action functions
@action
async def delete_item(request):
    body = await request.json()
    return {"deleted": True}
```

### Client module (`.pyxle-build/client/pages/*.jsx`)

The compiled JSX module contains:

```jsx
import React from 'react';
import { Head } from 'pyxle/client';

export default function MyPage({ data }) {
  return (
    <>
      <Head>
        <title>My Page</title>
      </Head>
      <h1>{data.now}</h1>
    </>
  );
}
```

### Composed route module (`.pyxle-build/routes/*.jsx`)

When layouts exist, the compiler generates a wrapper:

```jsx
import Page from '../client/pages/index.jsx';
import Layout from '../client/pages/layout.jsx';

const WRAPPERS = [
  { kind: 'layout', component: Layout, reset: false },
];

export default function PyxleWrappedPage(props) {
  // Nests: Layout(Page)
  let element = <Page {...props} />;
  for (const wrapper of WRAPPERS.reverse()) {
    const Wrapper = wrapper.component;
    element = <Wrapper>{element}</Wrapper>;
  }
  return element;
}
```

## Vite configuration

The compiler generates `.pyxle-build/vite.config.js` that:

- Configures `@vitejs/plugin-react` for JSX transforms and React Refresh
- Sets the `root` to the build directory
- Maps import aliases for `pyxle/client`
- Injects `PYXLE_PUBLIC_*` environment variables via Vite's `define` option

## Incremental compilation

During `pyxle dev`, the file watcher triggers recompilation only for changed files:

1. The watcher detects a file change in `pages/`
2. Only the changed `.pyxl` file is recompiled
3. The server module is re-imported (with module cache invalidation)
4. Vite's HMR picks up the client-side changes automatically

## Build artifacts

After `pyxle build`, the output structure is:

```
dist/
  server/              # Compiled Python modules
  client/              # Vite-bundled JS/CSS assets
  page-manifest.json   # Route-to-asset mapping
```

The `page-manifest.json` maps each route to its client-side assets:

```json
{
  "/": {
    "client": {
      "file": "assets/index-abc123.js",
      "css": ["assets/index-def456.css"]
    }
  }
}
```
