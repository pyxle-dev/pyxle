# Pyxle for AI coding agents

**TL;DR.** Pyxle is built so a coding agent — Claude Code, Cursor,
Copilot, whatever you use — can read, write, debug, and ship real
full-stack features with a tiny fraction of the context, files,
tools, and languages that traditional frameworks demand. One file
per page, Python backend, React frontend, zero config, structured
errors, no magic. If your workflow involves pairing with an AI
agent, Pyxle is the framework most optimised for that pairing.

> This isn't a marketing claim about being "AI-powered" — Pyxle
> doesn't call any LLMs itself. It's a claim about **developer
> experience**: the framework is shaped in a way that makes agents
> dramatically more effective per token spent.

---

## The problem agents have with modern web frameworks

When a coding agent builds a feature in a typical Next.js +
FastAPI stack, it has to simultaneously hold in mind:

- **A TypeScript frontend and a Python backend** — two languages,
  two type systems, two ecosystems, two build tools.
- **A UI page component** in `app/users/page.tsx`.
- **An API route** in `app/api/users/route.ts` (or a separate
  FastAPI endpoint in a different repo).
- **A data-fetching hook** in `lib/hooks/useUsers.ts`.
- **A CORS config** connecting the two backends.
- **A shared types file** (or worse, diverging types on each side).
- **A `next.config.js`**, a `tsconfig.json`, a `package.json`, a
  `pyproject.toml`, a `.env`, and usually at least one Dockerfile.

For the agent, every one of those is context. Context is tokens,
tokens are money, and every additional file the agent has to open
is another opportunity to get the mental model wrong. Multi-file
edits are where most AI agents struggle the most — not because
they can't reason about code, but because keeping N files
consistent with each other across edits is genuinely hard.

Now watch what the same feature looks like in Pyxle.

---

## One page, one file, both languages

Here's the entire "list users and delete them" feature in Pyxle:

```python
# pages/users.pyxl
from myapp.db import fetch_users, delete_user

@server
async def load_users(request):
    users = await fetch_users()
    return {"users": [u.to_dict() for u in users]}

@action
async def remove(request):
    body = await request.json()
    await delete_user(body["id"])
    return {"ok": True}


import React from 'react';
import { Head, useAction } from 'pyxle/client';

export default function UsersPage({ data }) {
    const remove = useAction('remove');

    return (
        <>
            <Head>
                <title>Users</title>
            </Head>
            <h1>{data.users.length} users</h1>
            <ul>
                {data.users.map((u) => (
                    <li key={u.id}>
                        {u.name}
                        <button onClick={() => remove({ id: u.id })}>
                            Delete
                        </button>
                    </li>
                ))}
            </ul>
        </>
    );
}
```

That's it. One file, one route (`/users`), one feature. The agent
reads this file, understands the whole thing in a single pass,
and can make coherent edits to the loader, the mutation, and the
UI without opening anything else.

Compare the file count and cross-file coordination burden:

| What                  | Next.js + FastAPI  | Pyxle           |
|-----------------------|--------------------|-----------------|
| Files for one feature | 4-6                | **1**           |
| Languages             | TypeScript, Python | Python, JSX     |
| Type-share mechanism  | Manual / codegen   | Native dict     |
| Build configs         | 3-4                | **1**           |
| HTTP hops per render  | 1-2 (page → API)   | **0** (in-proc) |
| CORS to configure     | Yes                | No              |
| Context per feature   | High               | **Low**         |

---

## Why this matters for an AI agent specifically

### 1. Python is the language LLMs know best

Every major LLM has seen dramatically more Python code during
training than any other language. When an agent writes Python, it
is operating in its highest-confidence mode. When it writes
TypeScript, it is operating at one tier below that. When it writes
the *interaction* between two languages — a TypeScript fetch call
hitting a Python FastAPI endpoint — errors compound across the
boundary and the agent has to reason about the contract in both
directions simultaneously.

Pyxle keeps the agent in Python for everything data-related, and
only asks it to write JSX for the view layer. The agent spends
its strongest muscle on its strongest language.

### 2. Python is the language the AI ecosystem lives in

Every serious LLM SDK — the Anthropic SDK, OpenAI SDK, LangChain,
LlamaIndex, instructor, pydantic AI, the Claude Agent SDK — is
Python-first. Most have JavaScript ports that are perpetually
behind in features.

If your app integrates an LLM, a Pyxle loader can just `await
anthropic.messages.create(...)` directly. No microservice, no
separate Python worker, no HTTP hop, no API token shuttling between
frontends and backends.

```python
from anthropic import AsyncAnthropic

client = AsyncAnthropic()

@server
async def load_chat(request):
    message = await client.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=1024,
        messages=[{"role": "user", "content": "Hello Claude"}],
    )
    return {"reply": message.content[0].text}
```

One file, one async call, response as a React prop. An agent
building this feature doesn't need to open three repos.

### 3. No magic — the code you see is the code that runs

Pyxle's `@server` and `@action` decorators set **one attribute**
each and return the function unchanged. There is no dependency
injection container, no runtime monkey-patching, no metaclass
inheritance, no proxy object intercepting attribute access. You
can read `pyxle/runtime.py` in a few seconds — it's around 150
lines including docstrings.

This is a massive advantage for agents because **the agent's
mental model of the code matches the actual execution**. In
frameworks with heavy runtime reflection (Django admin, Spring,
Rails, NestJS), the agent's debugging loop can break when its
inferred behaviour doesn't match what the framework actually does
at runtime.

With Pyxle, the agent can reason locally. The decorator does
nothing special. The function runs when the router calls it. The
dict you return becomes the prop on the React side. That's all.

### 4. Conventional file layout is the routing table

There is no `urls.py`, no `routes.ts`, no route decorators. If
you have `pages/users/[id].pyxl`, the route is `/users/{id}`. The
agent doesn't have to open a router file to learn what routes
exist — it can `ls pages/` and be done.

```
pages/
├── index.pyxl            → /
├── users.pyxl            → /users
├── users/[id].pyxl       → /users/{id}
├── admin/
│   ├── layout.pyxl       → (wraps everything under /admin/*)
│   └── stats.pyxl        → /admin/stats
└── api/
    └── pulse.py         → /api/pulse
```

For an agent, this means zero tokens spent reading routing
configuration. The filesystem is the configuration.

### 5. `pyxle check` gives agents structured, per-file errors

Most framework linters or type checkers print errors as a wall of
text and the agent has to parse them. Pyxle's `check` command
prints diagnostics with a rigid, machine-friendly shape:

```
ℹ️  Checked 28 .pyxl file(s) in my-app/
  error: [python] line 2: @server loader must accept a `request` argument
    --> pages/users.pyxl
  error: [jsx] line 8:10: Unterminated JSX contents
    --> pages/settings.pyxl
  error: [python] line 1: unterminated string literal (detected at line 1)
    --> pages/broken.pyxl
❌ Check failed with 3 error(s)
```

Each line is `[section] line N: message` followed by the file
path on an indented next line. An agent can split on newlines,
extract the file, the line number, and the message, and use that
to plan its next edit. **This is the format AI agents love.** It's
exactly what a grep-based tool call wants to consume.

The checker also:

- **Runs every file every time** — one bad file never aborts the
  scan, so the agent gets a complete picture of project health
  after every edit.
- **Reports both Python and JSX errors** in the same pass.
- **Marks each finding `error:` or `warning:`** — an error is code
  that will break when it runs (unresolved reference, syntax
  error); a warning is code that runs fine but wants tidying (an
  unused import). Only errors affect the exit code, so an agent
  can gate on the exit code and still see the tidy-ups. See
  [errors vs warnings](../reference/cli.md#errors-vs-warnings).
- **Suppresses cascade noise** — fix Python first, and the
  downstream JSX errors that came from the broken Python vanish
  automatically. The agent isn't chasing ghosts.
- **Has an exit code** — `0` if no error (warnings alone still exit
  `0`), `1` if any error. Shell-friendly for agent workflows that
  gate subsequent commands on check success.

### 6. Tiny CLI surface

The CLI is small and single-purpose:

```
pyxle init <name>    # scaffold a new project
pyxle install        # install Python + Node dependencies
pyxle dev            # run the dev server
pyxle check          # validate the project without serving
pyxle build          # compile + bundle for production
pyxle serve          # serve a production build
pyxle routes         # print the file-based route table
```

(plus `pyxle typecheck` for TypeScript-checking compiled JSX — that's
the whole surface.) There is no `pyxle db migrate`, no
`pyxle generate component`, no `pyxle admin createsuperuser`. Every
command has a clear, single-purpose behaviour, and the agent doesn't
have to search the docs to figure out which command does what.

### 7. Async by default, enforced at compile time

AI SDKs are async. The Anthropic SDK, the OpenAI SDK, httpx, most
modern database drivers — they all expect async.

Pyxle enforces async at the parser level. `@server def load(...)`
is a compile error. `@action def handle(...)` is a compile error.
The agent *cannot* accidentally write a blocking handler that
would stall the event loop when calling an LLM. The framework
protects against an entire class of mistake before the agent
makes it.

### 8. The dev overlay shows agent-friendly errors in real time

When a loader raises at request time, the dev overlay shows:

- The route that failed
- The exception type (e.g. `NameError`)
- The exception message verbatim
- A breadcrumb list describing which stage failed (loader, head,
  renderer, hydration)

An agent with access to the browser (via a screenshot tool or a
browser MCP) can read the overlay directly and understand the
failure without needing to `tail -f` a log file. Most frameworks
either print errors to the server log (invisible to the agent
unless it knows to check), or render a generic 500 page with no
useful detail.

### 9. The architecture is fully documented

This is the part most frameworks don't bother with, and it's the
part that matters most for an agent trying to make non-trivial
changes.

The [architecture handbook](../architecture/README.md) is an
11-document deep-dive covering every subsystem of the framework:
the parser, the compiler, the dev server, the SSR pipeline, the
routing, the build, the runtime, and the CLI. It's written so
that an agent can read one doc to answer one question, instead
of inferring framework behaviour from source code.

Specifically:

- **[The parser](../architecture/parser.md)** explains how `.pyxl`
  files are split into Python and JSX. An agent debugging a
  parse issue can read this once and know exactly what's happening.
- **[Routing](../architecture/routing.md)** has a complete table
  mapping filenames to URLs (dynamic segments, catch-alls,
  layouts, error boundaries).
- **[The runtime](../architecture/runtime.md)** shows the full 83
  lines of `pyxle/runtime.py` inline. There is no hidden
  decorator magic to discover.

Point your agent at the architecture handbook once, and it has
the entire mental model of the framework. It doesn't have to grep
source code to answer "what does `@server` actually do" or "how
does dynamic routing work".

### 10. No codegen, no build artifacts the agent has to maintain

Some frameworks generate code as part of the build (tRPC types,
Prisma clients, SvelteKit's route manifest, etc.). Every generated
file is a thing the agent has to remember exists, know not to
edit, and re-trigger when the source changes.

Pyxle generates artifacts into `.pyxle-build/` during compilation,
but **the agent never touches them**. They're not checked into
git, they're not imported from user code, and the only
interaction with them is running `pyxle dev` or `pyxle build`.
The agent edits `pages/foo.pyxl`, and that's the only file that
matters for that feature.

---

## A concrete comparison: shipping a feature

Let's walk through a specific scenario: the user asks the agent
to "add a `/posts/[id]` page that loads a blog post from Postgres
and shows it with a delete button."

### In Next.js + FastAPI

The agent has to create or edit:

1. **`backend/app/routes/posts.py`** — FastAPI endpoint to fetch
   a post by ID, plus a DELETE endpoint.
2. **`backend/app/models/post.py`** — SQLAlchemy model (if not
   already present).
3. **`backend/app/schemas/post.py`** — Pydantic request/response
   schemas.
4. **`frontend/lib/api/posts.ts`** — TypeScript API client
   wrapping the fetch calls.
5. **`frontend/types/post.ts`** — Duplicated type definition to
   match the Python Pydantic schema.
6. **`frontend/app/posts/[id]/page.tsx`** — The React page
   component.
7. **`frontend/app/posts/[id]/actions.ts`** — A server action (if
   using the Next.js app router) to call the delete endpoint.

Seven files, two languages, two type systems, and a network
boundary in the middle. Every edit has to be kept consistent with
every other edit. If the agent gets the Pydantic schema and the
TypeScript type slightly out of sync, the feature ships with a
runtime bug.

Token cost in practice: typically 10,000-30,000 tokens of context
to execute cleanly, plus several round-trips for the agent to
verify the edits across files.

### In Pyxle

The agent creates **one file**:

```python
# pages/posts/[id].pyxl
from myapp.db import get_post, delete_post

@server
async def load_post(request):
    post_id = int(request.path_params["id"])
    post = await get_post(post_id)
    if post is None:
        from pyxle.runtime import LoaderError
        raise LoaderError("Post not found", status_code=404)
    return {"post": post.to_dict()}

@action
async def remove(request):
    body = await request.json()
    await delete_post(int(body["id"]))
    return {"ok": True}


import React from 'react';
import { Head, useAction, navigate } from 'pyxle/client';

export default function PostPage({ data }) {
    const remove = useAction('remove');

    async function handleDelete() {
        await remove({ id: data.post.id });
        navigate('/posts');
    }

    return (
        <>
            <Head>
                <title>{data.post.title}</title>
            </Head>
            <article>
                <h1>{data.post.title}</h1>
                <div dangerouslySetInnerHTML={{ __html: data.post.body_html }} />
                <button onClick={handleDelete}>Delete</button>
            </article>
        </>
    );
}
```

One file. Python and JSX colocated. The route (`/posts/{id}`) is
implied by the filename. The loader error boundary is inline with
`LoaderError`. The delete mutation is `@action`. The client calls
it with `useAction('remove')`. Done.

Token cost in practice: roughly 3,000-6,000 tokens of context.
**3-5x less than the multi-file equivalent**, with zero cross-file
consistency risk.

---

## Beyond individual features: what agents really want

The per-feature savings add up, but there are also framework-level
properties that compound over an entire project.

### Zero-config is agent-friendly config

When you ask an agent to add a feature, you want the agent to
think about the feature, not the build. Pyxle has zero to minimal
configuration for the 95% case:

```json
{
  "middleware": []
}
```

That's a complete `pyxle.config.json`. An agent spending zero
tokens on build configuration is an agent spending all its tokens
on the actual problem.

### The dev server reloads everything the right way

Pyxle's dev server watches `pages/`, incrementally rebuilds only
the changed files, purges stale `sys.modules` entries so Python
changes take effect on the next request, and invalidates the
relevant Vite HMR modules so the browser updates without a full
reload.

For an agent working in a tight edit-check-edit loop, this means
**every change is immediately reflected** without the agent having
to remember to restart anything. The feedback signal from the
running app matches the state of the source tree at all times.

### Error messages are written for humans *and* machines

Pyxle's error messages are written to be parseable by a regex:

- `[section] line N: message`
- `  --> path/to/file.pyxl`

Column information is included where relevant. Line numbers map
back to the original `.pyxl` source (not the generated `.py` or
`.jsx` artifacts), so when the agent goes to fix the error, it
can target the right line directly.

### The framework repo itself has a `CLAUDE.md`

The Pyxle core repository ships with a `CLAUDE.md` at the
root that describes the project's rules, architecture, commit
conventions, and testing expectations. An agent working **inside**
the Pyxle codebase has an explicit set of instructions. When you
set up a Pyxle *application*, you can do the same — drop a
project-level `CLAUDE.md` describing your business domain, and
the agent can consult it before making changes.

This is a small thing, but it matters. Most frameworks treat
AI-agent documentation as an afterthought. Pyxle was designed with
agent-pairing in mind from the start.

---

## Who should use Pyxle as their agent-first framework?

There are specific audiences where this positioning is most
valuable:

### Python engineers shipping web apps with AI agents

If you write Python as your primary language and you pair with a
coding agent daily, Pyxle removes the need to context-switch into
TypeScript or Node for the view layer. You stay in Python; the
agent stays in its comfort zone; features ship faster.

### ML engineers and data scientists

You already have Python models, Python notebooks, Python data
pipelines. When you want to ship a UI for a model, a RAG
pipeline, or an internal dashboard, every other framework asks
you to learn a new stack. Pyxle asks you to add one file per
page.

### AI startups building LLM-heavy applications

RAG apps, chat UIs, document analysis tools, agentic workflows
— every one of these is a Python-first problem with a React
frontend. Pyxle is the shortest distance between "working Python
logic" and "shipped product." Your entire engineering team stays
in one language instead of splitting into frontend and backend
silos.

### Indie hackers using AI as their engineering team

If you're shipping side projects with Claude Code or Cursor as
your primary development partner, every file the agent has to
touch is a cost. Pyxle minimises that cost. One file per feature
means the agent can build an entire CRUD app in one conversation
instead of needing multi-turn context management.

### Internal tools teams

Admin panels, dashboards, data viewers, approval workflows — the
kind of apps that don't need a separate frontend team but
absolutely need server-side logic. Pyxle lets one Python engineer
(paired with an agent) ship what would take a full-stack team in
a traditional stack.

---

## What Pyxle is *not*

Honesty builds trust. Here's where Pyxle isn't the right choice:

- **A static site generator.** If your site has no server logic,
  Astro or Eleventy are simpler.
- **A pure JSON API.** If you're building a backend-only service
  with no UI, FastAPI is simpler.
- **A mature production framework.** Pyxle is young (0.8.x).
  It's genuinely usable and under active development, but it
  doesn't have the decade of battle-testing that Django,
  Next.js, or Rails have. Best fit: greenfield projects,
  prototypes, internal tools, AI apps, and anywhere you can
  tolerate some version churn as the framework stabilises. Not
  ideal if you need a decade of API stability guarantees or a
  vetted audit trail of CVEs fixed.
- **A huge ecosystem.** Pyxle is new. Stack Overflow has fewer
  answers about it than about the major frameworks. The offset
  is that its documentation is comprehensive and its source is
  readable — an agent can learn the framework from scratch by
  reading ~15,000 lines of code plus the architecture handbook.
- **A magic solution for inherently complex UIs.** Pyxle
  eliminates the cross-file, cross-language overhead of a
  traditional stack — but it can't simplify a UI that's
  inherently complex. The agent-first win is proportional to
  how much of a feature lives in the Python half. For
  server-heavy apps (CRUD screens, dashboards, admin panels,
  data apps, RAG UIs, internal tools), one `.pyxl` file really
  can replace five or six files in a traditional stack. For
  apps where the UI *is* the product (drawing tools, rich
  editors, complex design surfaces, heavy client-side state),
  the JSX section of each `.pyxl` file will still be a full
  React component tree, and the agent still has to reason
  about the same React complexity. Pyxle helps there too — it's
  real React underneath — but the savings are smaller, and the
  "agent-first" pitch is weaker.

---

## Getting your agent set up with Pyxle

If you want to give a coding agent the best possible chance of
writing good Pyxle code, do these three things:

### 1. Drop a project-level `CLAUDE.md` (or equivalent) in your repo

At the root of your project, add a `CLAUDE.md` that tells the
agent the essentials of your app. Something like:

```markdown
# CLAUDE.md

This is a Pyxle application. Read the framework conventions at
https://pyxle.dev/docs/core-concepts/pyxl-files before making any
changes.

## Project structure

- `pages/` — the route tree. Each `.pyxl` file is one page.
- `pages/api/` — JSON API routes (plain `.py` files).
- `myapp/db.py` — database access layer (SQLAlchemy).
- `myapp/models.py` — SQLAlchemy models.

## Conventions

- Use `<Head>` for page metadata, not the `HEAD` variable.
- Use `@server` for loaders, `@action` for mutations.
- All loaders must be `async`.
- Run `pyxle check` before committing.
- Run `pyxle dev` to test changes locally.
```

The agent consults this file before editing and its mental model
stays aligned with your conventions.

### 2. Give the agent access to `pyxle check` and the dev server logs

Most coding agents can run shell commands. Make sure the agent
has permission to run:

- `pyxle check` — to get structured diagnostics
- `pyxle dev` in the background — to verify changes live
- `pytest` or your test runner — to verify nothing regressed

Pyxle's error format is designed to be grep-able, so an agent
running `pyxle check 2>&1 | grep error:` gets a clean,
per-file list of every problem in the project.

### 3. Point the agent at the architecture handbook for deep questions

If the agent needs to understand *why* Pyxle behaves a certain
way — especially around parsing, routing, SSR, or the build —
point it at the relevant chapter of the
[architecture handbook](../architecture/README.md). Most
questions are answered there directly, and the handbook is
written specifically so an agent can consume one doc per topic
instead of scanning the full source tree.

---

## Serve your whole app as Markdown

There's a second half to "agent-first": not just *writing* code an agent can reason about, but *serving* content an agent can read. Pyxle ships that too. Enable the [`llms`](llms.md) feature and every page of your app is available as clean Markdown — append `.md` to any URL, or send `Accept: text/markdown` — plus an [`/llms.txt`](https://llmstxt.org/) index and discovery headers:

```json
{ "llms": true }
```

Now a developer can paste `yourapp.com/docs/anything.md` straight into Claude, Cursor, or ChatGPT and get clean, token-efficient context instead of HTML. You decide where each page's Markdown comes from — a co-located `.md` file, a `to_markdown` handler, or a directory-wide `llms.py` — and a `wrap_markdown` hook can frame every page with instructions for the agent reading it. This very handbook is served that way: append `.md` to this page's URL to see it.

See the [AI accessibility guide](llms.md) for the full model.

---

## Next steps

- **New to Pyxle?** Start with the [quick start](../getting-started/quick-start.md).
- **Curious about the internals?** Read the [architecture handbook](../architecture/README.md).
- **Want to see a real Pyxle app?** The pyxle.dev site itself is
  a Pyxle app — its source is public and reads like a reference
  implementation.
- **Building something ambitious?** Open an issue on GitHub and
  tell us what you're shipping. We want to know.

Pyxle is young, but the bet is simple: **the framework that asks
the least of AI agents will attract the most AI-augmented
developers**. That's who we're building for.
