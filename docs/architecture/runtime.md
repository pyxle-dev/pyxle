# The runtime

The "runtime" of Pyxle is the **smallest module in the framework**.
It contains two decorators (`@server` and `@action`), two
exception classes (`LoaderError` and `ActionError`), and one small
helper (`invalidate_routes`) — and nothing else. The whole file is
about **150 lines** including blank lines and docstrings.

This is on purpose. The runtime is what your application code
imports — it's the *contract* between your code and the framework.
A small contract means a small surface area for bugs, a small
mental model for users, and a small attack surface for prompt
injection through documentation.

This doc is the shortest in the architecture section because there
isn't much code to explain. But the *design decisions* behind the
runtime are some of the most important in Pyxle, and they're worth
understanding.

**File:** `pyxle/runtime.py` (~150 lines)

---

## What's in `pyxle/runtime.py`

The core contract is small enough to reproduce here:

```python
"""Runtime helpers exposed to compiled Pyxle artifacts."""

from __future__ import annotations

from typing import Any, Callable, TypeVar

F = TypeVar("F", bound=Callable[..., Any])


def server(function: F) -> F:
    """Mark a function as a Pyxle loader and return it unchanged."""
    setattr(function, "__pyxle_loader__", True)
    return function


def action(function: F) -> F:
    """Mark a function as a Pyxle server action and return it unchanged."""
    setattr(function, "__pyxle_action__", True)
    return function


class ActionError(Exception):
    """Raise from within a @action function to return a structured error."""
    def __init__(
        self,
        message: str,
        status_code: int = 400,
        data: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.data = data or {}


class LoaderError(Exception):
    """Raise from a @server loader to trigger the nearest error boundary."""
    def __init__(
        self,
        message: str,
        status_code: int = 500,
        data: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.data = data or {}


__all__ = ["server", "action", "ActionError", "LoaderError", "invalidate_routes"]
```

That's the core. The one remaining export is
`invalidate_routes(response, *urls)` — a small helper that tags a
response with an `x-pyxle-invalidate` header so the client router drops
its cached navigation payloads for the given URLs after a mutation (see
[Server Actions](../core-concepts/server-actions.md)). Two decorators,
two exceptions, one helper, one `__all__`.

---

## Why so small?

Most web frameworks have a runtime that's *enormous*: a request
context, a global state container, dependency injection, an event
loop, middleware execution, response builders, error handlers, the
works. Pyxle has none of those things in its runtime.

The reason is **the compiler does all the framework work, not the
runtime**. By the time your code runs at request time:

- The parser has already extracted your loader and action metadata.
- The compiler has already injected the runtime imports into the
  artifact.
- The dev server has already imported the compiled module.
- Starlette has already dispatched the request.
- The SSR pipeline has already validated everything.

When the framework finally calls your `@server`-decorated function,
the function is **just a function**. There is no context manager
wrapping it, no try/except catching its exceptions, no proxy object
intercepting attribute access. It runs as if you'd called it
yourself.

This is the *no-magic* design principle from the architecture README,
applied as literally as possible.

---

## What `@server` actually does

Here's the entire implementation of `@server`:

```python
def server(function: F) -> F:
    setattr(function, "__pyxle_loader__", True)
    return function
```

That's the whole decorator. It sets one attribute on the function
and returns it. The function is the *same* function — same identity,
same signature, same `__name__`, same `__doc__`, same `__module__`,
same closure cells, same everything. You can call it directly
without going through Pyxle:

```python
from pyxle.runtime import server

@server
async def load_home(request):
    return {"hello": "world"}

# All of these still work:
print(load_home.__name__)            # "load_home"
print(load_home.__pyxle_loader__)    # True
import asyncio
asyncio.run(load_home(fake_request))  # {"hello": "world"}
```

The framework's only use of `__pyxle_loader__` is at parse time, in
the compiler — and even then, the compiler reads it from the AST
(by checking decorator names), not from the runtime. The attribute
is set as a documentation aid for tooling and debuggers, not as a
mechanism the framework relies on.

`@action` works identically, with `__pyxle_action__` instead.

---

## Why decorators at all then?

If `@server` doesn't *do* anything, why is it a decorator? Why not
just a naming convention like Flask's `def hello():`?

Because **the parser uses the decorator name to find loaders and
actions in the AST**. When the parser walks your `.pyxl` file, it
asks each function definition: *"is one of your decorators called
`server`?"* If yes, it treats the function as a loader. Same for
`@action`.

Naming conventions (`def loader(request):`) would work, but they
have downsides:

- **Multiple functions per file would conflict.** If the convention
  is "the function named `loader` is the loader", you can't have
  helper functions named `loader_helper` without confusion. With a
  decorator, you can name the function whatever you want.
- **You'd lose the explicit "this is a Pyxle thing" signal.** When
  you read someone else's code, `@server` immediately tells you
  "this is the framework's entry point". A naming convention is
  invisible.
- **Refactoring tools can't track names as reliably as decorators.**
  Renaming a function is easy; changing a magic name convention
  isn't.

The decorator wins on every axis except one: it requires an
explicit `from pyxle.runtime import server`, and even that is
auto-injected by the compiler so you don't actually have to write
the import yourself.

> **Pyxle in plain Python:** You *can* write the import yourself.
> The compiler checks for an existing `from pyxle.runtime import
> server` and skips the auto-injection if you've already added it.
> The auto-injection exists because most users won't bother, not
> because the framework rejects explicit imports.

---

## The exception types

`LoaderError` and `ActionError` are the **only structured errors**
the runtime defines. Both follow the same shape:

```python
class XError(Exception):
    def __init__(self, message, status_code=..., data=None):
        super().__init__(message)
        self.message = message        # Human-readable message
        self.status_code = status_code  # HTTP status to return
        self.data = data or {}        # Additional structured payload
```

You raise them from inside your loader or action when something is
wrong:

```python
@server
async def load_post(request):
    slug = request.path_params["slug"]
    post = await db.fetch_post(slug)
    if post is None:
        raise LoaderError("Post not found", status_code=404)
    return {"post": post}
```

When `LoaderError` is raised:
- The framework catches it.
- It looks for the nearest `error.pyxl` boundary.
- It renders the boundary with the error context as a prop:
  `{error: {message, statusCode, data}}`.
- It returns the response with HTTP status `404`.

`ActionError` works the same way for `@action` functions, except it
returns a JSON error response (`{"error": {"message": "...",
"statusCode": 400, "data": {...}}}`) instead of rendering an HTML
boundary.

The default `status_code` differs:
- `LoaderError` defaults to **500** because most loader failures
  are server-side bugs.
- `ActionError` defaults to **400** because most action failures
  are client-side input problems.

You override the defaults with the keyword argument:

```python
raise LoaderError("Forbidden", status_code=403)
raise ActionError("Email already taken", status_code=409, data={"field": "email"})
```

### Why two exception types?

Because they're caught at different points in the request pipeline,
they translate to different response shapes (HTML page vs JSON), and
they trigger different debugging affordances (error boundary vs
client-side error handler).

A single `PyxleError` would force the framework to disambiguate at
catch time, and would force users to remember which fields apply to
which context. Two types make the contract explicit.

---

## The zero-dependency rule

`pyxle/runtime.py` has **zero imports from the rest of the
framework**. The only things it imports are:

- `from __future__ import annotations` (a Python 3.x compatibility
  thing)
- `from typing import Any, Callable, TypeVar` (standard library)

No `pyxle.compiler`, no `pyxle.devserver`, no `pyxle.ssr`, nothing.
This is enforced by `pyxle/CLAUDE.md` rule 5 (the module boundary
rule).

The reason is **runtime.py is the only module that ends up imported
by your application code**. When you write:

```python
from pyxle.runtime import server, action, LoaderError, ActionError
```

…you're pulling `runtime.py` into your project's import graph. If
runtime.py imported `pyxle.compiler.parser`, your project would
transitively depend on the parser, the AST module, the JSX import
rewriter, the Babel subprocess wrapper, and so on. That's a huge
dependency footprint for a module that ends up being five lines of
metadata-setting code.

The zero-dependency rule keeps the runtime fast to import and easy
to test. You can import `pyxle.runtime` in 5 milliseconds. You can
test it without setting up a full Pyxle project. You can use it from
*non-Pyxle* code if you wanted to, just to mark functions for some
other framework's use.

---

## How the framework actually finds your loader

Given that the runtime decorators don't do anything at runtime, how
does the framework find your loader when a request comes in?

The answer: **at parse time, not at runtime.**

When the parser processes `pages/index.pyxl`, `_detect_loader()`
walks the Python AST looking for `AsyncFunctionDef` nodes whose
`decorator_list` contains a `Name` named `server` or an `Attribute`
ending in `.server`. The first match becomes the loader. Its name
is recorded in the `LoaderDetails` dataclass:

```python
@dataclass(frozen=True)
class LoaderDetails:
    name: str            # e.g. "load_home"
    line_number: int     # for error mapping
    is_async: bool       # always True for loaders
    parameters: Sequence[str]  # ["request"]
```

This metadata flows into the `PageMetadata` (compiled), then into
the `.json` artifact, then into the `MetadataRegistry` at startup,
then into the `PageRoute` dataclass.

At request time, the page handler does:

```python
loader_fn = getattr(module, page.loader_name)
data = await loader_fn(request)
```

It looks up the loader by name. The name was recorded by the parser
at compile time. The decorator's role is *naming the metadata key*,
not *modifying behaviour*.

This is why the runtime can be three lines per decorator: by the
time the function runs, the framework already knows everything it
needs to know about it. The decorator doesn't have to do work
because the parser already did it.

---

## What if I need *real* request middleware?

If you need to wrap loaders with cross-cutting behaviour (auth,
logging, rate limiting, etc.), Pyxle has two options:

1. **Custom middleware** — Starlette ASGI middleware declared in
   `pyxle.config.json`:
   ```json
   {
     "middleware": ["my_auth_module:AuthMiddleware"]
   }
   ```
   This is the right place for things that should run on **every**
   request regardless of which loader is invoked.

2. **Route hooks** — Per-route policies declared in
   `pyxle.config.json`:
   ```json
   {
     "routeMiddleware": {
       "pages": ["my_module:require_login"]
     }
   }
   ```
   This is the right place for things that should run for **most**
   page or API routes but not all of them.

Both mechanisms wrap the request **outside** the loader, so the
loader itself stays untouched. The runtime decorators don't have to
participate. Source: `devserver/middleware.py`,
`devserver/route_hooks.py`.

---

## What if I want to test my loader?

Because `@server` doesn't wrap the function, testing is trivial:

```python
# tests/test_pages.py
import asyncio
from pages.index import load_home  # The compiled file

class FakeRequest:
    pass

def test_load_home():
    result = asyncio.run(load_home(FakeRequest()))
    assert result["hello"] == "world"
```

You don't need a test client, you don't need a fake Starlette app,
you don't need to mock the request context. The loader is a plain
async function — call it.

In practice, you probably want to use Starlette's `TestClient` for
end-to-end tests, but you don't *have* to. Unit-testing individual
loaders is straightforward because they're just functions.

---

## A note on async

Both `@server` and `@action` require the function to be `async`.
This isn't enforced by the runtime (the decorator runs at module
load time, before any function is called) — it's enforced by the
parser at compile time:

```
$ pyxle check
error: [python] line 5: @server loader must be declared as async
  --> pages/sync_loader.pyxl
```

The reason is consistency: every Pyxle handler runs on the asyncio
event loop, and a sync function would block the loop. Forcing async
at compile time avoids the runtime gotcha where a sync function
silently degrades performance.

If you need to call a sync library from inside an async loader, use
`asyncio.to_thread()`:

```python
@server
async def load_data(request):
    rows = await asyncio.to_thread(blocking_query, "SELECT ...")
    return {"rows": rows}
```

This pattern is documented in `core-concepts/data-loading.md`.

---

## Where to read next

- **[The parser](parser.md)** — How `_detect_loader()` and
  `_detect_actions()` walk the AST to find decorated functions and
  validate their signatures.

- **[The compiler](compiler.md)** — How the runtime imports get
  auto-injected into compiled `.py` artifacts so users don't have
  to write `from pyxle.runtime import server` themselves.

- **[Server-side rendering](ssr.md)** — How loaders are actually
  invoked at request time, including the dev-mode `sys.modules`
  purge that makes hot reload work.
