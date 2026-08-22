# Testing

Pyxle apps are mostly plain Python — your `@server` loaders and `@action`
handlers are ordinary `async` functions, and your business logic is whatever
Python you write. That means you can test with the tools you already know
(`pytest`, `httpx`), at three levels: the loader/action in isolation, the plain
logic underneath it, and the whole route end-to-end.

> A `.pyxl` file is **not** an importable Python module — it carries a JSX
> component next to the Python and compiles into a build directory, so
> `from pages.index import load_home` does not work. Use `pyxle.testing` (below)
> to load a page's functions, or keep your logic in plain `.py` modules.

## Unit-testing a loader

`pyxle.testing.load_loader` compiles a page and returns its `@server` loader as
a plain `async` function. Call it with a request — for a loader that only reads
its `request`, a lightweight stand-in like `types.SimpleNamespace` is enough:

```python
# tests/test_index.py
import asyncio
from types import SimpleNamespace
from pyxle.testing import load_loader

def test_home_loader():
    load_home = load_loader("pages/index.pyxl")
    data = asyncio.run(load_home(SimpleNamespace()))
    assert data["title"] == "Home"
```

If your loader reads request attributes (query params, headers, `request.state`),
give the fake request just those attributes:

```python
def test_search_loader():
    load_search = load_loader("pages/search.pyxl")
    request = SimpleNamespace(query_params={"q": "pyxle"})
    data = asyncio.run(load_search(request))
    assert data["query"] == "pyxle"
```

## Unit-testing an action

`pyxle.testing.load_page` returns the whole compiled module, so you can reach any
`@action` (or the loader) by name:

```python
# tests/test_contact.py
import asyncio
from types import SimpleNamespace
from pyxle.testing import load_page
from pyxle.runtime import ActionError

def test_submit_rejects_blank_email():
    page = load_page("pages/contact.pyxl")

    async def request():
        # @action handlers read the body via `await request.json()`
        return {"email": ""}

    req = SimpleNamespace(json=request)
    try:
        asyncio.run(page.submit(req))
        assert False, "expected ActionError"
    except ActionError as err:
        assert "email" in str(err)
```

`load_page(...).__pyxle_metadata__` exposes the compiled page's metadata,
including `loader_name`, if you need it.

## Best practice: keep loaders thin

The cleanest code to test is code that doesn't need a request at all. Put real
business logic in plain Python modules and let the loader/action be a thin
adapter:

```python
# lib/posts.py  — plain module, trivially testable
async def list_published(db, *, limit=20):
    rows = await db.query("SELECT * FROM posts WHERE published = 1 LIMIT ?", limit)
    return [dict(r) for r in rows]
```

```python
# pages/blog/index.pyxl
@server
async def load_blog(request):
    return {"posts": await list_published(request.state.db)}
```

```python
# tests/test_posts.py — no Pyxle machinery needed
import asyncio
from lib.posts import list_published

def test_list_published(fake_db):
    posts = asyncio.run(list_published(fake_db))
    assert all(p["published"] for p in posts)
```

This keeps the bulk of your test suite fast and framework-free; use
`pyxle.testing` for the thin loader/action adapters.

## End-to-end tests

To exercise a real route — routing, SSR, middleware, and actions together —
run the app and drive it over HTTP with a client like
[`httpx`](https://www.python-httpx.org/). This needs Node.js, because Pyxle
renders React on the server.

```python
# tests/test_e2e.py  (run against `pyxle serve` on port 8000)
import httpx

def test_home_renders():
    r = httpx.get("http://127.0.0.1:8000/")
    assert r.status_code == 200
    assert "Home" in r.text

def test_subscribe_action():
    # Actions are POSTed to /api/__actions/{page_path}/{action_name}.
    # State-changing requests need the CSRF token — fetch a page first to get
    # the `pyxle-csrf-<port>` cookie, then echo it in the X-CSRF-Token header.
    with httpx.Client(base_url="http://127.0.0.1:8000") as client:
        client.get("/")  # issues the CSRF cookie
        token = client.cookies[next(k for k in client.cookies if k.startswith("pyxle-csrf"))]
        r = client.post(
            "/api/__actions/index/subscribe_newsletter",
            json={"email": "test@example.com"},
            headers={"X-CSRF-Token": token},
        )
    assert r.status_code == 200
```

Start the server in your test harness (for example, a `pytest` fixture that runs
`pyxle serve --skip-build` as a subprocess after a `pyxle build`, waits for
`/healthz`, and tears it down afterwards). See [Deployment](deployment.md) for
the production server, and [Observability](observability.md#health-probes) for
the `/healthz` and `/readyz` endpoints a fixture can poll.

## Component tests with Vitest

Pyxle does not install a JavaScript test runner, and does not need one to run
the Python tests above. If you want to test your React components,
[Vitest](https://vitest.dev/) works with the Vite configuration Pyxle already
generates:

```bash
npm install -D vitest
```

One Pyxle-specific detail, and it is the whole reason this section exists. The
`vite.config.js` in your project root re-exports the configuration Pyxle
generates into `.pyxle-build/client/`, and that configuration's `root` is the
generated directory — not your project. A bare `vitest run` therefore looks for
tests inside the build output and reports:

```
No test files found, exiting with code 1
```

Point it at your project instead:

```bash
npx vitest run --root .
```

Keep your tests in your own source tree. Everything under `.pyxle-build/` is
regenerated by `pyxle dev` and `pyxle build`, so tests placed there are
overwritten without warning.

## On the roadmap

A first-class `PyxleTestClient` — spin the app up in-process and drive pages and
actions (with CSRF handled for you) without managing a subprocess — is on the
roadmap. Until then, the patterns above cover unit and end-to-end testing with
standard tools.

## Next steps

- [Server Actions](../core-concepts/server-actions.md) — the `@action` contract
- [Deployment](deployment.md) — run a production build for end-to-end tests
- [Observability](observability.md) — health endpoints for test fixtures
