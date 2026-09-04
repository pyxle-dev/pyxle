# The `.pyxl` file format

A Pyxle page is a single file with two languages in it: Python and JSX.
This doc explains *why* that file exists, *what's actually in it*, and
*what mental model* the rest of the framework uses for it.

If you want to know *how* Pyxle splits the two languages, that's the
next doc — [The parser](parser.md).

---

## Why one file?

Web pages are the unit of work for most web developers. *"I'm working on
the checkout page. I'm fixing the 404. I'm adding a settings page."* The
page is the noun.

In most frameworks, that one mental noun is spread across at least two
files: a backend handler in one place and a UI component in another.
That separation has merits — different languages, different runtimes,
different deployment surfaces — but it also has a real cost. Every
non-trivial change has to keep the two files in sync.

Pyxle's bet is that **colocating both halves of a page in one file is
worth it**, even at the cost of inventing a new file extension. When
you change the loader, the component is right there. When you rename a
prop, both sides update in the same diff. When you delete a page, you
delete one file.

This isn't a unique idea — Vue's `.vue` files, Svelte's `.svelte` files,
Astro's `.astro` files, MDX's `.mdx` files all do something similar. What's
different about `.pyxl` is that the two languages inside are the *full*
ones: real Python (with `import`, `class`, `async def`, decorators,
asyncio, the works) and real JSX (with `import React`, hooks, ES module
syntax, the works). Neither is a stripped-down DSL.

---

## What a `.pyxl` file looks like

Let's start with the simplest one and grow it.

### A pure-JSX page

```pyxl
# pages/about.pyxl
import React from 'react';

export default function About() {
    return (
        <main className="p-8">
            <h1>About</h1>
            <p>This page has no Python at all.</p>
        </main>
    );
}
```

This is valid. Some pages just don't need a loader. Pyxle is fine with
that — the parser sees zero Python content, the compiled `.py` file is
empty, the route is registered but has no `@server` function.

> **Pyxle in Next.js terms:** This is the equivalent of a Next.js
> `.tsx` page that doesn't export `getServerSideProps`. It's a static
> component with no server-side logic.

### A page with a server loader

```pyxl
# pages/status.pyxl
import time

@server
async def load(request):
    return {"now": time.time(), "version": "0.1.7"}

import React from 'react';

export default function Status({ data }) {
    return <pre>{JSON.stringify(data, null, 2)}</pre>;
}
```

The Python comes first, then the JSX, in that order. The two halves
are separated only by **whitespace and the natural transition** from
Python statements to ES module syntax. The parser will figure it out.

A few things to notice in this example:

- **`@server` is a decorator with no parentheses.** It's not a factory
  function or a wrapper — it's a pure tag. See [The runtime](runtime.md).
- **The loader is async.** Always. The parser refuses to compile a sync
  one.
- **The loader takes a single argument named `request`.** This is also
  enforced by the parser.
- **The return value is a plain dict.** It will be serialized to JSON
  and passed to the React component as a prop named `data`.
- **The JSX block looks like a regular React file.** `import React from
  'react'`, an `export default function`, JSX syntax. There's nothing
  Pyxle-specific in the JSX section. You could copy-paste it into a
  standalone `.jsx` file and it would work.

### A multi-section page

Here's a more interesting pattern: alternating Python and JSX blocks.

```pyxl
# pages/dashboard.pyxl
from datetime import datetime

# A Python helper used by the loader.
def format_time(dt):
    return dt.strftime("%Y-%m-%d %H:%M:%S")


import React from 'react';

// A JSX helper used by the page component.
function StatCard({ label, value }) {
    return (
        <div className="stat-card">
            <strong>{label}</strong>
            <span>{value}</span>
        </div>
    );
}


@server
async def load_dashboard(request):
    return {
        "now": format_time(datetime.now()),
        "user_count": 142,
    }


@action
async def refresh(request):
    return {"refreshed_at": format_time(datetime.now())}


import { useAction } from 'pyxle/client';

export default function Dashboard({ data }) {
    const refresh = useAction('refresh');
    return (
        <main>
            <h1>Dashboard</h1>
            <StatCard label="Now" value={data.now} />
            <StatCard label="Users" value={data.user_count} />
            <button onClick={() => refresh()}>Refresh</button>
        </main>
    );
}
```

This one has **four** alternating sections: Python helper → JSX helper
→ Python loader+action → JSX export. The parser handles arbitrary
alternation. There's no fence marker, no separator comment, no special
syntax. The boundaries are detected by parsing — see [The parser](parser.md).

> Aside: Pyxle used to require explicit `# --- client ---` and `# ---
> server ---` markers to split sections. We removed them in v0.1.7
> because the AST-driven walker is reliable enough that markers became
> noise. Old files with markers still parse correctly because the
> markers are valid Python comments — they just no longer mean anything.

### A page with document `<head>` metadata

Use the `<Head>` component from `pyxle/client` to control what ends up in
the document head. It's the recommended approach for nearly every page:

```pyxl
# pages/blog/[slug].pyxl
@server
async def load_post(request):
    slug = request.path_params["slug"]
    post = await fetch_post(slug)
    return {"post": post}

import React from 'react';
import { Head } from 'pyxle/client';

export default function Post({ data }) {
    return (
        <article>
            <Head>
                <title>{data.post.title} — My Blog</title>
                <meta name="description" content={data.post.excerpt} />
            </Head>
            <h1>{data.post.title}</h1>
            <div dangerouslySetInnerHTML={{__html: data.post.html}} />
        </article>
    );
}
```

Pyxle's JSX compiler recognises `<Head>` blocks during parsing, extracts
them as metadata, and during SSR the head merger hoists them into the
document `<head>` alongside contributions from parent layouts. Dynamic
content interpolates through normal JSX expressions
(`{data.post.title}`), and nested components can contribute their own
head elements — see [SSR § Stage 3: resolving the head](ssr.md#stage-3-resolving-the-head).

For the rare case where a page has no JSX component at all and still
needs head metadata, Pyxle also supports a compile-time `HEAD` Python
variable:

```python
HEAD = '<title>Blog post</title><meta name="description" content="A blog post" />'
```

The parser extracts it from the AST at compile time and stores it in
the page metadata. It supports strings, lists of strings, or a callable
`def HEAD(data): ...` that receives the loader's return value. For most
real pages, reach for `<Head>` instead — the JSX form is easier to
write, easier to type-check, and composes naturally with layouts.

---

## What's *not* in a `.pyxl` file

A `.pyxl` file is **always** one route — one page. It is not:

- **A library file.** If you have shared Python utilities, put them in a
  regular `.py` file and `import` them from your `.pyxl`. The parser does
  not look inside `.py` files.
- **A reusable component file.** If you have shared JSX components, put
  them in a regular `.jsx` file and `import` them from your `.pyxl`. The
  bundler resolves these like any normal module import.
- **An API handler.** Plain JSON APIs go in `pages/api/*.py` files
  (regular Python, no JSX). See [Routing § API routes](routing.md#api-routes).
- **A layout or template.** Layouts use the same `.pyxl` extension but
  live in `layout.pyxl` files at strategic points in the directory tree.
  They wrap pages instead of being pages themselves. See
  [Routing § Layouts](routing.md#layouts).

---

## What the framework "sees"

When the compiler processes your `.pyxl` file, it produces a single
`PyxParseResult` object that captures everything the rest of the
framework needs. The shape is:

```python
@dataclass(frozen=True)
class PyxParseResult:
    python_code: str                              # The Python half
    jsx_code: str                                 # The JSX half
    loader: LoaderDetails | None                  # @server function info
    actions: tuple[ActionDetails, ...]            # @action function info
    head_elements: tuple[str, ...]                # static HEAD content
    head_is_dynamic: bool                         # HEAD is a callable?
    head_jsx_blocks: tuple[str, ...]              # <Head> JSX blocks
    script_declarations: tuple[dict, ...]         # <Script> components
    image_declarations: tuple[dict, ...]          # <Image> components
    python_line_numbers: Sequence[int]            # for error mapping
    jsx_line_numbers: Sequence[int]               # for error mapping
    diagnostics: tuple[PyxDiagnostic, ...]        # in tolerant mode
```

Source: `compiler/parser.py:83`.

A few things worth understanding here:

1. **`python_code` and `jsx_code` are strings, not ASTs.** The parser
   does parse them with `ast.parse` to find segment boundaries and
   extract metadata, but the *output* is just two strings ready to be
   written to disk as the compiled `.py` and `.jsx` files.

2. **`python_line_numbers` and `jsx_line_numbers` are line maps.**
   When the compiler finds an error in the joined Python output (say,
   line 7 of `python_code`), it can look up `python_line_numbers[6]`
   to find the original line number in the source `.pyxl` file. This is
   how Pyxle's error messages always point at the right line.

3. **`loader`, `actions`, `head_elements`, `head_jsx_blocks`,
   `script_declarations`, and `image_declarations` are extracted at
   parse time.** The dev server doesn't have to re-parse anything to
   answer questions like "does this page have a loader?" or "what
   `<Script>` tags should be injected before hydration?". The metadata
   is precomputed once and read at request time.

4. **`diagnostics` is populated only in tolerant mode.** When `pyxle
   check` runs the parser, it passes `tolerant=True`. Errors that
   would normally raise `CompilationError` are collected as
   `PyxDiagnostic` entries instead, so a single check pass can report
   every error in every file at once.

---

## File extension and tooling

Pyxle uses `.pyxl` because:

- It signals "this isn't a normal Python file" to anyone glancing at
  the directory.
- Most editors don't have Pyxle support yet (LSP support is on the
  roadmap), so the convention is: **set your editor to treat `.pyxl` as
  Python for syntax highlighting**. The Python half lights up
  correctly; the JSX half ends up looking like a string of nonsense,
  which is honest about the situation.
- The Cython project uses `.pyxl` for a different purpose. We know.
  Pyxle is for web pages, Cython is for compiled Python extensions —
  in practice they don't overlap, and the Pyxle parser has nothing to
  do with the Cython parser.

When the Pyxle LSP ships (separate package, `pyxle-langkit`), editors
will get correct dual-language highlighting, jump-to-definition for
both halves, and inline diagnostics. Until then, the convention is
"highlight as Python and squint at the JSX".

---

## What if I'm coming from another framework?

| You know... | And you're wondering... | Answer |
|---|---|---|
| Next.js | Where's `getServerSideProps`? | It's the `@server` decorator on a function called whatever you want. |
| Next.js | Where's `app/page.tsx` and `route.ts`? | One file: the loader and the component live together in `pages/foo.pyxl`. |
| Remix | Where's `loader` and `action`? | `@server` for loaders, `@action` for actions. Same idea, different name. |
| Django | Where's `views.py`? | The loader function is the view. The template is the JSX in the same file. |
| FastAPI | Where's the route decorator? | Pyxle uses **file-based routing** — no `@app.get("/")`. The file path *is* the route. |
| Rails | Where's the controller? | The loader is the controller. The action is the controller's mutation handler. |
| Flask | Where's `@app.route`? | Same as FastAPI: file-based. |
| SvelteKit | Where's `+page.server.ts`? | One file, not two. The Python half *is* `+page.server.ts`. |

The recurring theme: Pyxle compresses what other frameworks split into
multiple files into a single file per route, while keeping the two
halves in their original languages.

---

## Where to read next

You now know what a `.pyxl` file is, what's in it, and what the parser
produces from it. The next question is: **how does the parser actually
split Python from JSX without any markers?**

That's an interesting algorithm with a few subtle correctness traps,
and it gets its own deep-dive doc:

→ **[The parser](parser.md)**.
