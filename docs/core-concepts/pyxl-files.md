# `.pyxl` Files

A `.pyxl` file is the fundamental building block of a Pyxle application. It combines Python server logic with a React component in a single file -- your data fetching and your UI live together.

## Anatomy of a `.pyxl` file

A `.pyxl` file has two sections:

```pyxl
# 1. Python section -- runs on the server
from datetime import datetime

@server
async def load_page(request):
    return {"now": datetime.now().isoformat()}

// 2. JSX section -- runs on both server (SSR) and client
import { Head } from 'pyxle/client';

export default function MyPage({ data }) {
  return (
    <>
      <Head>
        <title>My Page</title>
      </Head>
      <h1>Current time: {data.now}</h1>
    </>
  );
}
```

The compiler finds the boundary automatically: it grows the largest region that parses as valid **Python** (using Python's own AST), and treats whatever isn't valid Python as **JSX**. There are no separator comments or directives to write.

## The Python section

The Python section runs entirely on the server. It can:

- **Import modules** -- any Python package available in your environment
- **Define a `@server` loader** -- an async function that fetches data for the component
- **Define `@action` mutations** -- async functions callable from the client

```python
from pyxle.runtime import server, action
import httpx

@server
async def load_users(request):
    async with httpx.AsyncClient() as client:
        resp = await client.get("https://api.example.com/users")
    return {"users": resp.json()}

@action
async def delete_user(request):
    body = await request.json()
    user_id = body["id"]
    # ... delete from database ...
    return {"deleted": user_id}
```

### Rules for the Python section

- **One `@server` loader per file.** The loader receives a Starlette `Request` and must return a JSON-serializable dict.
- **Multiple `@action` functions are allowed.** Each becomes a callable endpoint.
- **The `@server` function must be `async`.** Pyxle enforces this at compile time.
- **Use any Python imports.** Standard `import` / `from` statements stay in the Python section because they parse as valid Python. (A JavaScript import such as `import React from 'react'` isn't valid Python, so it is correctly treated as part of the JSX section — the split is by what parses, not by the leading keyword.)
- **The `@server` and `@action` decorators are available globally** -- you do not need to import them (the compiler injects the import automatically).
- **An optional `CACHE = {"revalidate": N}` directive** caches the rendered page for `N` seconds (server-side, with incremental regeneration). See [Caching](../guides/caching.md).

## The JSX section

The JSX section is a standard React component. It runs on both the server (for SSR) and the client (for hydration and interactivity).

```jsx
import { Head } from 'pyxle/client';

export default function MyPage({ data }) {
  const [count, setCount] = React.useState(0);

  return (
    <>
      <Head>
        <title>Users</title>
        <meta name="description" content="Our user directory." />
      </Head>
      <div>
        <h1>Users: {data.users.length}</h1>
        <button onClick={() => setCount(c => c + 1)}>
          Clicked {count} times
        </button>
      </div>
    </>
  );
}
```

### Rules for the JSX section

- **Must have a default export.** The default export is the page component.
- **Receives `{ data }` as props.** The `data` prop contains whatever the `@server` loader returned. If there is no loader, `data` is an empty object `{}`.
- **Can import from `pyxle/client`.** This gives you `<Head>`, `<Script>`, `<Image>`, `<ClientOnly>`, `<Form>`, `useAction`, `<Link>`, `navigate`, and `prefetch`.
- **Can import from `node_modules`.** Any npm package in your `package.json` is available.
- **Cannot import Python code.** The Python and JSX sections are compiled separately.
- **Must be a pure function of its props.** No data fetching, I/O, secrets, Node built-ins, side effects, or non-deterministic values (`Date.now()`, `Math.random()`) in the render body — load in `@server`, mutate in `@action`, and put browser-only or effectful code in `useEffect` / event handlers / `<ClientOnly>`. See [Component purity](#component-purity) below.

## Component purity

A component's **render** is a pure function of its props: given the same `data`, it produces the same HTML, with no side effects. Whatever runs while the page renders on the server must be deterministic.

**Don't, in the render body:**

- fetch data, query a database, or touch the filesystem / network — that's the loader's job
- read secrets or `process.env`
- import Node built-ins (`fs`, `path`, `crypto`, `child_process`, …)
- mutate state or cause other side effects
- use non-deterministic values like `Date.now()`, `Math.random()`, or `new Date()` — they differ between the server render and the client and cause hydration mismatches

**Do instead:**

| You want to… | Use |
|---|---|
| load data for the page | a `@server` loader (runs before render; the result arrives as `data`) |
| change data | an `@action`, called with `useAction` or `<Form>` |
| run code after mount, or anything browser-only | `useEffect`, an event handler, or `<ClientOnly>` |

This isn't a stylistic preference — purity is what makes a component **hydration-safe** (server and client render the same thing), **testable** (render is a function you can assert on), and **fast** (no hidden per-render I/O). It also keeps rendering portable to faster, sandboxed SSR backends in the future.

Effects and event handlers are **exempt** — they don't run during the server render. Purity is about the *render pass*, not the whole component.

## Controlling the document `<head>`

Use the `<Head>` component from `pyxle/client` to control what goes in the document `<head>`:

```jsx
import { Head } from 'pyxle/client';

export default function AboutPage({ data }) {
  return (
    <>
      <Head>
        <title>About Us</title>
        <meta name="description" content="Our story" />
        <link rel="canonical" href="https://example.com/about" />
      </Head>
      <h1>About Us</h1>
    </>
  );
}
```

Anything inside `<Head>` is extracted during SSR and inlined into the document `<head>`. It supports dynamic values via normal JSX interpolation:

```pyxl
@server
async def load_post(request):
    post = await fetch_post(request.path_params["slug"])
    return {"post": post}

import { Head } from 'pyxle/client';

export default function BlogPost({ data }) {
  return (
    <>
      <Head>
        <title>{data.post.title} — My Blog</title>
        <meta name="description" content={data.post.excerpt} />
      </Head>
      <article>
        <h1>{data.post.title}</h1>
        {/* ... */}
      </article>
    </>
  );
}
```

Head values are automatically sanitised to prevent XSS injection -- angle brackets inside `<title>` text are escaped, event handler attributes are stripped, and `javascript:` URLs are neutralised.

> **Note:** Pyxle also supports a lower-level `HEAD` Python variable for the rare cases where you want fully static head metadata extracted at compile time. For everyday pages, prefer the `<Head>` component. See [Head Management](../guides/head-management.md) for both mechanisms and when to use each.

## A complete example

```pyxl
from datetime import datetime, timezone

@server
async def load_home(request):
    hour = datetime.now(tz=timezone.utc).hour
    if hour < 12:
        greeting = "Good morning"
    elif hour < 18:
        greeting = "Good afternoon"
    else:
        greeting = "Good evening"
    return {"greeting": greeting}

import { Head } from 'pyxle/client';

export default function HomePage({ data }) {
  return (
    <main>
      <Head>
        <title>{data.greeting}</title>
      </Head>
      <h1>{data.greeting}</h1>
      <p>Welcome to Pyxle.</p>
    </main>
  );
}
```

## JSX-only files

If a page has no server logic, you can write a JSX-only `.pyxl` file:

```jsx
export default function AboutPage() {
  return (
    <main>
      <h1>About</h1>
      <p>This page has no loader -- it renders the same content every time.</p>
    </main>
  );
}
```

## Next steps

- Learn how files map to URLs: [Routing](routing.md)
- Add data fetching: [Data Loading](data-loading.md)
