# Introduction

Pyxle is a Python-first full-stack web framework that brings the Next.js developer experience to the Python ecosystem. You write your server logic in Python, your UI in React, and ship them together in a single `.pyxl` file — no separate API layer, no second service, no glue code between them.

**Current version:** 0.4.0 (beta)

## One file, two languages

A `.pyxl` file colocates a Python `@server` loader (and `@action` mutations) with the React component that renders its data. The compiler splits them apart at build time, so the Python runs on the server and the JSX runs in the browser — but you author them as one unit, because they describe one thing: a single route.

```python
# pages/index.pyxl
from pyxle.runtime import server

@server
async def load(request):
    return {"message": "Hello from Python"}
```

```jsx
export default function Home({ data }) {
    return <h1>{data.message}</h1>;
}
```

The loader's return value *is* the component's `data` prop. There's no `fetch`, no API route to wire up, no type contract to keep in sync — the seam between frontend and backend simply isn't there.

## Why Pyxle

- **No API plumbing.** A `@server` loader feeds props straight into your component; an `@action` is an endpoint you call with `useAction` instead of hand-writing `fetch` + a route.
- **Real React.** `npm install` any React library and use it directly — Pyxle renders genuine React 18 with server-side rendering and hydration, not a Python wrapper around a component library.
- **Convention over configuration.** File-based routing, automatic SSR, and zero config for the common cases. Run `pyxle init` and you're shipping.
- **Batteries includable.** Official plugins for [database](../plugins/pyxle-db.md) and [auth](../plugins/pyxle-auth.md), plus hooks for middleware, edge caching, and your own integrations.
- **AI-first DX.** Predictable patterns, strong types, and clear errors make Pyxle one of the easiest frameworks for an AI coding agent to ship a whole feature in a single pass. See [Pyxle for AI coding agents](../guides/for-ai-agents.md).

## Where to go next

- **New here?** Start with [Installation](installation.md), then build your first app in [Quick Start](quick-start.md).
- **Want the mental model?** Read [`.pyxl` Files](../core-concepts/pyxl-files.md), [Routing](../core-concepts/routing.md), and [Data Loading](../core-concepts/data-loading.md).
- **Coming from another framework?** See how Pyxle stacks up in [Pyxle vs. other frameworks](../guides/comparison.md).
- **Shipping to production?** Read the [Deployment](../guides/deployment.md) guide.
- **Curious how it works inside?** Take the tour in the [Architecture handbook](../architecture/README.md).

## What's new

Pyxle 0.4.0 adds **edge caching**, **hardened production errors**, and **faster static serving**. See the full [Changelog](../changelog.md) for release notes.

---

- GitHub: [github.com/pyxle-dev/pyxle](https://github.com/pyxle-dev/pyxle)
- Install: `pip install pyxle-framework`
- PyPI: [pypi.org/project/pyxle-framework](https://pypi.org/project/pyxle-framework/)
