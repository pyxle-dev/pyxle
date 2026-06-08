# Pyxle Architecture

Welcome. This section is the **architecture handbook** — a guided tour of how
Pyxle is built on the inside, written for everyone from "I just installed
Pyxle yesterday" to "I want to send a PR that touches the SSR worker pool."

If the rest of the docs answer *what does Pyxle do?*, this section answers
*how does it do it, and why was it built that way?*

You don't need to read these in order. Pick whichever subsystem you're curious
about and start there. Each page is self-contained, links liberally back to
the source code, and uses real examples from real `.pyxl` files.

---

## A 30-second mental model

A Pyxle application is, fundamentally, a directory of `.pyxl` files.

```
my-app/
└── pages/
    ├── index.pyxl          → /
    ├── about.pyxl          → /about
    └── posts/
        └── [id].pyxl       → /posts/:id
```

Each `.pyxl` file is one *page*. It contains **Python** (your loaders, actions,
and head metadata) **and** **JSX** (your React component) — colocated in a
single file because they describe the same thing: one route.

When you run `pyxle dev`, six pieces of code spring into action:

```
                ┌──────────┐
                │  .pyxl    │  ← you write this
                │  files   │
                └────┬─────┘
                     │
        ┌────────────┴────────────┐
        │       1. Parser         │  Splits Python and JSX using ast.parse
        │   (compiler/parser.py)  │  No fence markers, no heuristics — pure AST.
        └────────────┬────────────┘
                     │
        ┌────────────┴────────────┐
        │       2. Compiler       │  Writes .py + .jsx + .json artifacts to
        │   (compiler/writers.py) │  .pyxle-build/ for the dev server.
        └────────────┬────────────┘
                     │
        ┌────────────┴────────────┐
        │     3. Dev Server       │  Starlette ASGI app serving your routes,
        │  (devserver/...)        │  with a Vite proxy for client assets.
        └────────────┬────────────┘
                     │
        ┌────────────┴────────────┐
        │     4. SSR Pipeline     │  Runs your @server loader, renders the
        │       (ssr/...)         │  React component on a Node.js worker.
        └────────────┬────────────┘
                     │
        ┌────────────┴────────────┐
        │  5. Document Assembly   │  Streams the HTML response back, head
        │  (ssr/template.py)      │  elements deduplicated, hydration ready.
        └────────────┬────────────┘
                     │
                ┌────┴─────┐
                │ Browser  │  Hydrates with React. Fast nav uses JSON.
                └──────────┘
```

That's the whole story. Every doc in this section is a closer look at one of
those six pieces.

---

## The handbook

### Start here

- **[Overview](overview.md)** — The full request lifecycle from URL to HTML,
  end to end, in one read. If you only read one doc here, read this one.

### The core compiler

- **[The .pyxl file format](pyxl-files.md)** — Why Pyxle invented a new file
  extension and what's actually in one. The "two languages, one file" idea.

- **[The parser](parser.md)** — How Pyxle splits Python from JSX without any
  fence markers, using only `ast.parse`. Includes the multi-section walker,
  the broken-Python detector, and the tolerant-mode diagnostic system.
  This is the most sensitive code in the framework — and the most fun.

- **[The compiler](compiler.md)** — How parsed pages become `.py` + `.jsx` +
  `.json` artifacts on disk, including the JSX import rewriter and the
  runtime injection pass.

### The serving stack

- **[Routing](routing.md)** — File-based routing, dynamic segments
  (`[id]`), catch-all routes (`[...slug]`), optional catch-alls
  (`[[...slug]]`), route groups (`(auth)`), and `index.pyxl` collapsing.

- **[The dev server](dev-server.md)** — Starlette + Vite + the file watcher
  + the incremental builder + the WebSocket error overlay. The whole `pyxle
  dev` experience explained.

- **[Server-side rendering](ssr.md)** — How a request becomes HTML. Loader
  execution, the Node.js worker pool, head merging from four sources,
  document assembly, streaming responses, and client-side navigation.

### Production

- **[Build and serve](build-and-serve.md)** — What `pyxle build` actually
  does, how the page manifest is constructed, and how `pyxle serve` runs
  a built app without Vite in the loop.

### Cross-cutting

- **[The runtime](runtime.md)** — The `@server` and `@action` decorators
  and the *zero-magic* contract that lets your code remain pure Python.

- **[The CLI](cli.md)** — `pyxle init`, `dev`, `build`, `serve`, `check`.
  Config precedence, tolerant-mode validation, and how the CLI bridges
  user input to the rest of the framework.

---

## Conventions used in this section

- **Source citations** look like `compiler/parser.py:178` — the file is
  relative to `pyxle/pyxle/`, and the number is the line where the thing
  starts. Click through; the docs are written so you can read them with the
  source open in a second window.

- **"Pyxle in [language X]"** boxes compare a Pyxle decision to how Next.js,
  Django, FastAPI, or Remix would handle the same thing. They're optional
  context for readers coming from other frameworks.

- **Examples** are real, runnable, copy-paste-ready code — never pseudocode.
  Every snippet either is, or could be, a real `.pyxl` file.

- **"How it works under the hood"** sections drop into the actual algorithms
  and data structures. These are the "advanced" parts — feel free to skim or
  skip if you're new.

---

## Design principles

Reading the architecture is easier if you know the rules the framework was
built to follow. There are seven, and they're enforced by the test suite:

1. **Python-first, not Python-only.** Great Python *and* great React.
   Neither half is a second-class citizen.

2. **Convention over configuration.** Zero config for the common cases.
   `pyxle init` and you're running.

3. **Compiler-driven.** Metadata is extracted at build time, not runtime.
   The parser does the heavy lifting once, so the request path stays fast.

4. **No magic.** Decorators add metadata, they don't wrap or transform.
   `@server` is a one-line decorator that sets a single attribute. You can
   read it. You can call it. You can debug it. There is no DI container,
   no metaclass, no runtime patching.

5. **Progressive disclosure.** Simple things are simple. Complex things are
   possible. You shouldn't need to know about the SSR worker pool to ship
   your first page — but if you want to, every layer is documented and
   accessible.

6. **Batteries includable.** Pyxle ships hooks and integration points, not
   opinions. Bring your own ORM, your own auth, your own state management.

7. **AI-first DX.** Predictable patterns, strong types, clear errors.
   Frictionless for both humans *and* coding agents.

You'll see these principles cited throughout the architecture docs. When a
design choice looks surprising, it's almost always one of these seven that's
driving it.

---

## A note about depth

These docs are deliberately **deep**. They're written with the assumption
that you want to understand Pyxle well enough to:

- Debug a confusing error message by tracing it back to the line of code
  that emitted it.
- Confidently file a bug report with a minimal reproduction.
- Send a pull request that fits the existing architecture.
- Build your own framework that learns from Pyxle's choices.

If you just want to ship a feature, the [Core Concepts](../core-concepts/pyxl-files.md)
and [Guides](../guides/comparison.md) sections are friendlier starting points. Come
back here when you're curious.

Welcome aboard. Let's take a tour.

→ Start with **[Overview](overview.md)**.
