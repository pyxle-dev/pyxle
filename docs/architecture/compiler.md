# The compiler

The compiler is the bridge between the parser and the rest of the
framework. The parser produces an in-memory `PyxParseResult`; the
compiler turns that result into **three files on disk** that the dev
server, the SSR pipeline, and the Vite bundler all consume:

```
pages/index.pyxl                    ← your source
   │
   │  PyxParser().parse()
   ▼
PyxParseResult                     ← in-memory
   │
   │  ArtifactWriter().write()
   ▼
.pyxle-build/server/pages/index.py     ← Python loader, importable by Starlette
.pyxle-build/client/pages/index.jsx    ← JSX component, bundleable by Vite
.pyxle-build/metadata/pages/index.json ← extracted metadata, used by route discovery
```

This doc walks through what's in each of those files, why they exist
as separate artifacts, and the small but important transformations
the compiler applies along the way.

**Files:**
- `compiler/core.py` (~70 lines) — top-level `compile_file()` entry
- `compiler/writers.py` (~310 lines) — `ArtifactWriter` and the
  runtime-import injection helpers
- `compiler/jsx_imports.py` (~370 lines) — the `.pyxl → .jsx` import
  rewriter
- `compiler/jsx_parser.py` (~125 lines) — Babel subprocess wrapper
- `compiler/model.py` (~130 lines) — `CompilationResult`,
  `PageMetadata`, and the small declaration dataclasses

---

## The top-level entry: `compile_file`

```python
def compile_file(
    source_path: Path,
    *,
    build_root: Path,
    client_root: Path | None = None,
    server_root: Path | None = None,
) -> CompilationResult:
```

Source: `compiler/core.py:15`.

This is the only function the rest of the framework calls. Its job
is small:

1. Resolve the page-relative path (the file's path inside `pages/`).
2. Compute the route paths (primary and any aliases — see
   [Routing](routing.md)).
3. Parse the source file with `PyxParser().parse(source_path)`.
4. Hand the result to `ArtifactWriter` to emit the three files.
5. Return a `CompilationResult` describing what was written.

It's deliberately thin. All the interesting decisions live in the
parser and the writer.

---

## Three artifacts, three jobs

### 1. The Python artifact (`.py`)

`.pyxle-build/server/pages/index.py` is the **executable Python
module** for one route. It's what `pyxle dev` and `pyxle serve`
actually `import` and call.

Here's a real example. Source `.pyxl`:

```python
# pages/index.pyxl
import time

@server
async def load_home(request):
    return {"now": time.time()}


import React from 'react';

export default function Home({ data }) {
    return <h1>Now: {data.now}</h1>;
}
```

Compiled `.py`:

```python
from pyxle.runtime import server
import time

@server
async def load_home(request):
    return {"now": time.time()}
```

A few things happened:

- **The JSX is gone.** The compiled `.py` is purely the Python half of
  the source. The dev server never imports the JSX.
- **`from pyxle.runtime import server` was added at the top.** The
  source file uses `@server` without importing it because the user
  never has to import Pyxle's runtime decorators — the compiler does
  it for them. Same for `@action`.
- **The original imports (`import time`) are preserved verbatim.**
  The compiler doesn't reformat your code, doesn't reorder imports,
  doesn't rewrite anything. The only change is the runtime import
  insertion.

### 2. The JSX artifact (`.jsx`)

`.pyxle-build/client/pages/index.jsx` is the **bundleable JSX module**
for the same route. Vite reads it, bundles it with esbuild, and ships
it to the browser.

For our example:

```jsx
import React from 'react';

export default function Home({ data }) {
    return <h1>Now: {data.now}</h1>;
}
```

This is a verbatim copy of the JSX half of the source — almost. The
"almost" is the JSX import rewriter (next section).

### 3. The metadata artifact (`.json`)

`.pyxle-build/metadata/pages/index.json` is the **extracted metadata**
for the route. It's a small JSON document the dev server reads at
startup to build its routing table without re-parsing the source:

```json
{
  "route_path": "/",
  "alternate_route_paths": [],
  "client_path": "/pages/index.jsx",
  "server_path": "/pages/index.py",
  "loader_name": "load_home",
  "loader_line": 4,
  "head": [],
  "head_dynamic": false,
  "scripts": [],
  "images": [],
  "head_jsx_blocks": [],
  "actions": []
}
```

The dev server's `MetadataRegistry` (`devserver/registry.py`) loads
all of these at startup, builds a `RouteTable`, and uses it to
dispatch requests. No `.pyxl` file is parsed during a request — that
work is done once, at compile time.

---

## The runtime import injection pass

When you write:

```python
@server
async def loader(request):
    return {}
```

…you don't write `from pyxle.runtime import server` first. Pyxle's
philosophy is that the framework should stay out of your way for the
common case. The compiler adds the import for you, but in a careful,
AST-aware way.

The injection logic lives in three helper functions in
`compiler/writers.py`:

- `ensure_server_import(code)` — adds `from pyxle.runtime import server`
- `ensure_action_import(code)` — adds `from pyxle.runtime import action`
- `ensure_server_action_import(code)` — adds the combined import when
  both decorators are present

The compiler chooses one of the three based on what the parser found:

```python
if has_loader and has_actions:
    python_code = ensure_server_action_import(python_code)
elif has_loader:
    python_code = ensure_server_import(python_code)
elif has_actions:
    python_code = ensure_action_import(python_code)
```

Source: `compiler/writers.py:55-60`.

### Why AST-aware?

A naive version would do:

```python
def ensure_server_import(code):
    return "from pyxle.runtime import server\n" + code
```

This works for most files. But it breaks on:

```python
"""Module docstring.

This must remain the first statement in the module.
"""
from __future__ import annotations

@server
async def loader(request):
    ...
```

PEP 257 says the module docstring must be the first statement. PEP
236 says `from __future__ import annotations` must come before any
other code (after the docstring). A naive prepend would put the
runtime import *before* the docstring, breaking both PEPs and
producing a `SyntaxError` on the next compile cycle.

The injection helper uses `ast.parse` to find the right insertion
point: after the docstring, after any `from __future__` imports, but
before any other code. It also checks for an existing
`from pyxle.runtime import server` and skips the injection if one is
already present (so you *can* import it explicitly if you want).

The complete logic is short — about 70 lines of careful AST walking
in `compiler/writers.py:139-215`.

> **Pyxle in plain Python:** This is the only place where the
> compiler "modifies" your code. Everywhere else, the compiled `.py`
> file is byte-for-byte the same as the Python half of your source.
> The injection is the smallest possible change consistent with not
> requiring you to write boilerplate imports.

---

## The JSX import rewriter

JSX files import each other. A page might import a shared component:

```jsx
// pages/index.pyxl
import Sidebar from './Sidebar.pyxl';

export default function Home() {
    return <Sidebar />;
}
```

But after compilation, `Sidebar.pyxl` doesn't exist on disk anymore —
its compiled version is `Sidebar.jsx`. The bundler can't resolve
`./Sidebar.pyxl` because there's nothing there. We need to rewrite
the import specifier so it points at the compiled artifact:

```jsx
import Sidebar from './Sidebar.jsx';   // ← rewritten
```

The rewriter (`compiler/jsx_imports.py`) handles this. It's a
character-by-character JS lexer that walks the JSX source tracking:

- When we're inside a string literal (`'`, `"`, `` ` ``)
- When we're inside a `//` line comment or `/* ... */` block comment
- When we're inside a JSX tag (so a `from` keyword inside JSX is
  *not* a module import)
- The current parsing context (top-level, inside an import statement,
  inside an export-from, inside a dynamic `import(...)` expression)

When the lexer sees a string literal *in import-specifier position*
(which can be `import x from "..."`, `import "..."`, `export ...
from "..."`, `import("...")`, etc.), it checks if the specifier
ends with `.pyxl` (with optional `?query` and `#fragment` suffixes
preserved) and rewrites the extension to `.jsx`.

It is **not** a complete JS parser — it only tracks enough state to
know when a string literal is a module specifier vs ordinary string
content. The decision was deliberate: we don't want a Babel
subprocess in the inner loop of every compile.

**Source:** `compiler/jsx_imports.py:1-372`.

### Why a custom lexer instead of regex?

A regex like `import\s+\w+\s+from\s+["']([^"']+)["']` would catch
most cases but break on:

- Strings that contain the word "import" (`const msg = "import was
  removed"`)
- Comments that contain import statements (`// import './foo.pyxl'`)
- Template literals containing import statements
- Dynamic imports with concatenated paths (`import("./" + name +
  ".pyxl")` — we *don't* rewrite these because we don't know the
  literal value)
- Re-exports (`export { foo } from "./bar.pyxl"`)

The lexer handles all of these correctly because it tracks state.
Regexes don't track state.

---

## The Babel-backed JSX validator

Sometimes the compiler needs to *understand* the JSX, not just rewrite
imports. Specifically, it needs to find:

- `<Script src="..." strategy="..." />` declarations (so the SSR
  pipeline can inject scripts at the right hydration point)
- `<Image src="..." width="..." height="..." />` declarations (so the
  build can optimize image assets)
- `<Head>...</Head>` JSX blocks (so their children can be hoisted
  into the server-rendered `<head>`)

To extract this information reliably, Pyxle calls **Babel** via a
small Node.js helper script (`jsx_component_extractor.mjs`). The
helper parses the JSX, walks the AST looking for the target
components, and returns a JSON description of each match.

The Python wrapper:

```python
def parse_jsx_components(jsx_code: str, target_components: set[str]) -> Result:
    """Returns parsed component declarations or an error."""
```

Source: `compiler/jsx_parser.py:33-127`.

The wrapper:
1. Writes the JSX to a temp file.
2. Spawns Node.js with the helper script and the temp file path.
3. Parses the JSON output (or captures the error if parsing failed).
4. Returns a `Result` with either `components` or `error`.

This is the same Babel integration that backs `validate_jsx=True` in
the parser. Both use cases share one Babel call per file.

If Node.js or the helper script isn't available (e.g., the user
hasn't run `npm install` yet), the wrapper returns an empty result
and the compiler proceeds without the metadata. The dev server logs a
warning but doesn't fail.

---

## The data flow, end to end

Putting it all together, here's what happens when the compiler
processes one `.pyxl` file:

```
pages/index.pyxl
   │
   │ 1. PyxParser().parse(source_path)
   ▼
PyxParseResult
   ├── python_code: str
   ├── jsx_code: str
   ├── loader: LoaderDetails | None
   ├── actions: tuple[ActionDetails, ...]
   ├── head_elements: tuple[str, ...]
   ├── head_is_dynamic: bool
   ├── script_declarations: tuple[dict, ...]
   ├── image_declarations: tuple[dict, ...]
   └── head_jsx_blocks: tuple[str, ...]
   │
   │ 2. ArtifactWriter().write(...)
   │    a. ensure_*_import(python_code)         ← inject runtime decorators
   │    b. rewrite_pyxl_import_specifiers(jsx)  ← .pyxl → .jsx in imports
   │    c. PageMetadata(...).to_json()          ← serialize the metadata
   ▼
.pyxle-build/server/pages/index.py
.pyxle-build/client/pages/index.jsx
.pyxle-build/metadata/pages/index.json
```

Source: `compiler/core.py` + `compiler/writers.py:28-136`.

Every `.pyxl` file produces exactly three artifacts. There's no
in-memory state shared between compilation of different files —
each `compile_file()` call is independent and reentrant. This is
what makes incremental compilation possible: when the file watcher
sees one file change, it can call `compile_file()` for just that
one file and trust that all the artifacts are correct.

---

## Stubs for "empty" cases

Two edge cases produce stub content instead of the user's source:

**A pure-JSX file** (no `@server`, no Python code at all) gets a
Python stub:

```python
"""Generated by Pyxle for a static page."""
```

This stub is just a docstring; the file is importable but has no
loader. The dev server detects this and skips the loader-execution
step at request time.

**A pure-Python file** (no JSX, no `export default`, just a Python
loader and possibly some server-side helpers) gets a JSX stub:

```jsx
// Generated by Pyxle: no client component provided.
```

Vite can still bundle this — the bundle is empty — and the dev
server falls back to a minimal placeholder rendering. This is mostly
useful for `pages/api/*.py` files (which never have a client half by
definition).

Source: `compiler/writers.py:14-15`.

---

## CompilationResult and PageMetadata

The compiler returns a single dataclass that summarises everything
it did:

```python
@dataclass(frozen=True)
class CompilationResult:
    source_path: Path
    python_code: str
    jsx_code: str
    server_output: Path
    client_output: Path
    metadata_output: Path
    metadata: PageMetadata
```

`PageMetadata` (`compiler/model.py:66`) is the data structure that
gets serialized to the `.json` artifact. It's a frozen dataclass with
an exhaustive list of everything the dev server might need to know
about a page without re-parsing it:

```python
@dataclass(frozen=True)
class PageMetadata:
    route_path: str
    alternate_route_paths: tuple[str, ...]
    client_path: str
    server_path: str
    loader_name: str | None
    loader_line: int | None
    head_elements: tuple[str, ...]      # serialized as "head"
    head_is_dynamic: bool               # serialized as "head_dynamic"
    scripts: tuple[ScriptDeclaration, ...] = ()
    images: tuple[ImageDeclaration, ...] = ()
    head_jsx_blocks: tuple[str, ...] = ()
    actions: tuple[ActionDeclaration, ...] = ()
```

A content hash of each source file (a SHA256, computed by the
incremental builder — it is **not** stored on `PageMetadata`) lets the
builder detect "this file's content hasn't changed since the last
compile" and skip recompilation. Source: `devserver/builder.py`.

---

## Why three files instead of one?

You might reasonably ask: *"Why not put everything in one file?"*

Three reasons:

1. **The dev server imports `.py` files with Python's normal import
   machinery.** That requires the file to be syntactically valid
   Python. A file containing `export default function ...` is not
   valid Python and never will be.

2. **Vite expects `.jsx` files for client-side bundling.** Vite has
   no idea what a `.pyxl` file is. Giving it actual JSX files lets us
   take advantage of Vite's existing toolchain (esbuild, Vue plugin,
   React Refresh, HMR) without modifying Vite at all.

3. **Metadata is read more often than it's written.** A typical
   project has 20-100 `.pyxl` files but the dev server reads the
   metadata for every request. Keeping it as parsed JSON instead of
   re-parsing source files on each request is a major performance
   win.

---

## Where to read next

- **[Routing](routing.md)** — How `.pyxl` file paths get translated
  into URL routes, including dynamic segments, catch-all routes, and
  index collapsing.

- **[The dev server](dev-server.md)** — How the dev server uses the
  compiled artifacts at runtime, including the file watcher that
  triggers incremental compilation when you save.

- **[The build pipeline](build-and-serve.md)** — How `pyxle build`
  takes the same compiled artifacts and packages them for production
  deployment, including how it bridges Pyxle's compiled JSX to
  Vite's bundle output via the page manifest.
