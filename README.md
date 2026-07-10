<p align="center">
  <br />
  <a href="https://pyxle.dev">
    <img src="https://raw.githubusercontent.com/pyxle-dev/pyxle/main/.github/pyxle-logo.svg" alt="Pyxle" height="52" />
  </a>
  <br />
  <br />
  <strong>Python and React. One file.</strong>
  <br />
  A full-stack framework where your server logic (Python) and your UI (React) live in one
  <br />
  <code>.pyxl</code> file &mdash; no second service, no glue. Built for the age of AI coding agents.
  <br />
  <br />
  <a href="https://pypi.org/project/pyxle-framework/"><img src="https://img.shields.io/pypi/v/pyxle-framework?color=22c55e&labelColor=0a0a0b&label=pypi" alt="PyPI" /></a>
  &nbsp;
  <a href="https://github.com/pyxle-dev/pyxle/blob/main/LICENSE"><img src="https://img.shields.io/pypi/l/pyxle-framework?color=22c55e&labelColor=0a0a0b" alt="License" /></a>
  &nbsp;
  <a href="https://pyxle.dev/playground"><img src="https://img.shields.io/badge/playground-try%20it-0a0a0b?labelColor=0a0a0b&color=22c55e" alt="Playground" /></a>
</p>

---

A `.pyxl` file holds your Python server logic and your React UI side by side. Pyxle splits them
at compile time: `@server` loaders and `@action` mutations run on the backend, the JSX renders
with real server-side rendering, and React hydrates on the client. No FastAPI-plus-Next.js,
no two repos, no CORS, no API glue.

```python
# pages/users.pyxl
from db import fetch_users, delete_user

@server
async def load(request):
    users = await fetch_users()        # runs on the server
    return {"users": users}            # ...the returned dict becomes the component's `data`

@action
async def remove(request):
    body = await request.json()
    await delete_user(body["id"])      # a real server-side mutation
    return {"users": await fetch_users()}   # hand back the fresh list


import React, { useState } from 'react';
import { useAction } from 'pyxle/client';

export default function Users({ data }) {
    const [users, setUsers] = useState(data.users);
    const remove = useAction('remove');               // calls the @action above — no fetch, no route

    async function onRemove(id) {
        const res = await remove({ id });             // framework wraps the return as { ok, ...data }
        if (res.ok) setUsers(res.users);              // the server stays the source of truth
    }

    return (
        <ul>
            {users.map((u) => (
                <li key={u.id}>
                    {u.name}
                    <button onClick={() => onRemove(u.id)}>Delete</button>
                </li>
            ))}
        </ul>
    );
}
```

Data loading, a server mutation, and a React UI &mdash; one file, two languages, zero glue.

**[&#9654; Try it in your browser &rarr;](https://pyxle.dev/playground)** &nbsp; Edit a real `.pyxl` and run it. No install.

## Quickstart

```bash
pip install pyxle-framework
pyxle init my-app && cd my-app
pyxle install
pyxle dev
```

Open **http://localhost:8000**, edit `pages/index.pyxl`, and watch it hot-reload.

## Why Pyxle

|  | Pyxle | Next.js + FastAPI | Reflex / FastHTML |
|---|:---:|:---:|:---:|
| Server + UI in one file | ✅ | ❌ | ✅ |
| You write real React / JSX | ✅ | ✅ | ❌ (UI is Python) |
| Services to run | **one** | two | one |
| API glue you write | none — `@action` *is* the call | yes | none |
| SSR + file-based routing | ✅ | ✅ | ✅ |
| Languages | Python + JS | Python + TS | Python |

Pyxle's spot is the one nobody else occupies: **real React, like Next.js &mdash; but one file and one
service, like a single-language Python framework.** The honest trade is that you do write some
JavaScript; in return you get the whole React ecosystem with none of the two-repo, two-runtime tax.

**[Full framework comparison &rarr;](https://pyxle.dev/docs/guides/comparison)** &nbsp; Honest about when to reach for Reflex, Django, NiceGUI, Streamlit, or Next.js + FastAPI *instead*.

**Fast, and benchmarked in the open.** Pyxle renders dynamic SSR **~2&times; faster than Next.js** per core &mdash; the same DOM in 2&ndash;3&times; fewer bytes &mdash; and runs **on par with FastAPI** on API throughput, pulling ahead once a request does real database work.
&rarr; **[See the benchmarks](https://pyxle.dev/benchmarks)** &mdash; full data with framework versions, honest caveats, and a reproducible harness.

**Built for AI coding agents.** Shipping a feature on a Next.js + FastAPI stack means holding two
languages, two type systems, an API contract, and a CORS config in your head &mdash; and your agent's
context window &mdash; at the same time. In Pyxle it's one file with one predictable shape, so an agent
(Claude Code, Cursor, Copilot) ships a working full-stack feature in a fraction of the tokens, files,
and round-trips. Strong types, structured errors, no magic.

Every `pyxle init` scaffolds an **`AGENTS.md`** guide, so coding agents pick up Pyxle's conventions
from the very first prompt — no priming, no setup.
→ **[Pyxle for AI coding agents](https://pyxle.dev/docs/guides/for-ai-agents)**

## Features

- **`.pyxl` files** — Python + React in one file, split at compile time
- **`@server` / `@action`** — typed data loading and server mutations, called from the client with zero API boilerplate
- **SSR** — server-side rendering (esbuild + React 19) with hydration
- **File-based routing** — `pages/` maps to URLs; `[param].pyxl` for dynamic segments
- **Layouts & slots** — nested layouts and slot composition
- **Vite HMR** — instant hot reload in development (Vite 7)
- **Styling** — plain CSS and CSS Modules out of the box; opt into Tailwind v4 or shadcn/ui at `pyxle init`
- **Production build** — `pyxle build` + `pyxle serve`; deploy anywhere Python runs
- **AI accessibility** — serve any page as clean Markdown (append `.md`, or `Accept: text/markdown`) plus an `llms.txt` index, so AI agents read your app as text — one flag, agent-friendly out of the box
- **Editor tooling** — LSP, linter, and a [VS Code extension](https://marketplace.visualstudio.com/items?itemName=pyxle.pyxle-language-tools)

## Status

Pyxle is **early (0.7.x) but real.** The framework, SSR, routing, CLI, editor tooling, and the
official plugins (auth, database, mail) all work today — [pyxle.dev](https://pyxle.dev) itself is
built with Pyxle. APIs may still shift before 1.0. Feedback and contributions are very welcome.

## Documentation

Full docs at **[pyxle.dev/docs](https://pyxle.dev/docs/getting-started/installation)**:

[Installation](https://pyxle.dev/docs/getting-started/installation) &middot;
[Quick Start](https://pyxle.dev/docs/getting-started/quick-start) &middot;
[`.pyxl` Files](https://pyxle.dev/docs/core-concepts/pyxl-files) &middot;
[Routing](https://pyxle.dev/docs/core-concepts/routing) &middot;
[Data Loading](https://pyxle.dev/docs/core-concepts/data-loading) &middot;
[Server Actions](https://pyxle.dev/docs/core-concepts/server-actions) &middot;
[Layouts](https://pyxle.dev/docs/core-concepts/layouts) &middot;
[Deployment](https://pyxle.dev/docs/guides/deployment) &middot;
[CLI](https://pyxle.dev/docs/reference/cli) &middot;
[Configuration](https://pyxle.dev/docs/reference/configuration)

## CLI

```
pyxle init <name>     Scaffold a new project
pyxle install         Install Python + Node dependencies
pyxle dev             Development server with hot reload
pyxle build           Production build
pyxle serve           Serve the production build
```

## Requirements

Python 3.10+ and Node.js 20.19+.

## Contributing

```bash
git clone https://github.com/pyxle-dev/pyxle.git
cd pyxle
pip install -e ".[dev]"
pytest
```

Issues and pull requests are welcome.

## Links

[pyxle.dev](https://pyxle.dev) &middot;
[Playground](https://pyxle.dev/playground) &middot;
[Docs](https://pyxle.dev/docs) &middot;
[Benchmarks](https://pyxle.dev/benchmarks) &middot;
[PyPI](https://pypi.org/project/pyxle-framework/) &middot;
[Issues](https://github.com/pyxle-dev/pyxle/issues)

## License

[MIT](LICENSE)
