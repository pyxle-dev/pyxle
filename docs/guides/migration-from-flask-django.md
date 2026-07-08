# Migrating from Flask or Django

This guide maps the concepts you already know from **Flask** and **Django** to their Pyxle equivalents, so you can port an app a route at a time. It assumes you've read [Core Concepts](../core-concepts/pyxl-files.md); for the higher-level "should I use Pyxle at all" framing, see [Pyxle vs. the alternatives](comparison.md).

## The one mental shift

In Flask and Django, a request flows **view → template → response**: a view function fetches data and hands it to a server-side template language (Jinja or the Django Template Language) that renders HTML.

In Pyxle, a route is a single `.pyxl` file with two halves:

- a `@server` **loader** (the view's data-fetching job) that returns a plain dict, and
- a **React component** (the template's job) that receives that dict as its `data` prop and renders with SSR + hydration.

Mutations are a third piece — an `@action` function — instead of a second POST view.

```python
# pages/posts/[id].pyxl

@server
async def load(request):
    post = await get_post(request.path_params["id"])
    return {"post": post}          # <-- the loader's return value IS the data prop

# --- the component (JSX) ---
import React from 'react';

export default function Post({ data }) {
  return <article><h1>{data.post.title}</h1><p>{data.post.body}</p></article>;
}
```

The loader's return value **is** the component's `data` prop — there is no separate `render_template(...)` call. The biggest single rewrite in any migration is turning Jinja/DTL templates into React/JSX.

## Project layout

| Flask | Django | Pyxle |
|-------|--------|-------|
| `app.py`, `templates/`, `static/` | `project/`, `app/`, `urls.py`, `views.py`, `templates/`, `static/` | `pages/` (routes), `pages/api/` (JSON), `public/` (static), `layout.pyxl`, `pyxle.config.json` |

There is no application object you instantiate (`Flask(__name__)` / `django-admin startproject`). The `pages/` tree **is** the app; the framework assembles the ASGI app (Starlette under the hood) for you. Run `pyxle init` to scaffold the layout, then `pyxle routes` to see the URL map your files produce. See [Project structure](../getting-started/project-structure.md).

## Routing

| Flask / Django | Pyxle |
|----------------|-------|
| `@app.route("/about")` / `path("about/", ...)` | `pages/about.pyxl` → `/about` |
| `@app.route("/user/<int:id>")` / `path("user/<int:id>/", ...)` | `pages/user/[id].pyxl` → `/user/:id` |
| catch-all converters | `[...slug].pyxl` (catch-all), `[[...slug]].pyxl` (optional catch-all) |
| Blueprints / `include()` + `url_prefix` | folders under `pages/`; a `(group)/` folder groups files **without** adding a URL segment |
| `url_for("endpoint")` / `{% url %}` / `reverse()` | **no reverse-routing helper** — use literal `<Link href="/about">`; action URLs are auto-resolved |

Routing is **convention, not registration**: there is no `urls.py` or route decorator to keep in sync. See [Routing](../core-concepts/routing.md).

Two honest caveats:

- **No type converters.** A `[id].pyxl` param arrives as a string in `request.path_params["id"]`; cast it yourself in the loader. There is no `<int:id>` equivalent.
- **No `url_for`.** You write literal path strings in `<Link href>` / `<a href>`. Form and action endpoints are resolved automatically by `<Form>` / `useAction`, so you never hand-write those URLs.

## Views and data loading

A Flask/Django view does two jobs — fetch data and render a response. In Pyxle those split:

```python
@server
async def load(request):
    # request is Starlette's Request, not Flask's:
    q = request.query_params.get("q", "")
    user_id = request.path_params["id"]
    token = request.cookies.get("session")
    return {"results": await search(q)}      # dict, or (dict, status_code)
```

`request` is the **Starlette** `Request`: `request.query_params`, `request.path_params`, `request.headers`, `request.cookies`, `await request.json()`, `await request.form()`. A loader returns a JSON-serializable dict (or a `(dict, status_code)` tuple). Blocking calls (a sync DB driver, `requests`) should be wrapped in `asyncio.to_thread(...)` so they don't stall the event loop. See [Data loading](../core-concepts/data-loading.md).

## Templates → JSX (the big rewrite)

There is no Python template engine in Pyxle. Jinja and the Django Template Language become React/JSX in the same `.pyxl` file:

| Jinja / DTL | JSX |
|-------------|-----|
| `{{ post.title }}` | `{data.post.title}` |
| `{% for p in posts %}…{% endfor %}` | `{data.posts.map(p => <li key={p.id}>{p.title}</li>)}` |
| `{% if user %}…{% endif %}` | `{data.user && <span>{data.user.name}</span>}` |
| `{% extends "base.html" %}` / `{% block %}` | a `layout.pyxl` wrapping `children` (and named `slots`) |
| `{% include %}` | a React component you import |
| `{% static 'x.css' %}` | import the CSS from JSX (Vite hashes it) or reference `public/` at `/x.css` |

This is real React 19, server-rendered and hydrated — not a string-templating layer. Page `<title>`/meta come from the `HEAD` variable or the `<Head>` component, not a `{% block title %}`. See [.pyxl files](../core-concepts/pyxl-files.md), [Layouts](../core-concepts/layouts.md), and [Head management](head-management.md).

## Forms and mutations

| Flask / Django | Pyxle |
|----------------|-------|
| POST view + `request.form` + redirect | `@action async def submit(request)` in the same `.pyxl` |
| `<form method="post">` | `<Form action="submit">` or the `useAction("submit")` hook |
| WTForms / Django `forms.Form` + `is_valid()` | a Pydantic model annotation on the action param |
| CSRF token (`{{ csrf_token() }}` / `{% csrf_token %}`) | built-in, on by default, handled automatically by `<Form>`/`useAction` |
| `flash("Saved!")` / `django.contrib.messages` | **no flash store** — surface success/error as React state |

```python
@action
async def subscribe(request, body: Signup):   # Pydantic model -> auto 422 on bad input
    await save(body.email)
    return {"ok": True}
```

Annotating an `@action` parameter with a [Pydantic](https://docs.pydantic.dev/) model parses, coerces, and validates the body before the action runs, returning a `422` with a `fields` map (field → messages) that `<Form onError>` and `useAction().fields` surface next to inputs. For checks Pydantic can't express, raise `ValidationActionError`. Actions are POST-only and auto-routed at `POST /api/__actions/{page}/{name}` — you never write that URL. See [Server actions](../core-concepts/server-actions.md).

There is **no `flash()` / messages framework**: render feedback from `<Form>`'s `onSuccess`/`onError` or `useAction`'s `error`/`data` state.

## JSON APIs

A pure JSON endpoint (no UI) is a `.py` file under `pages/api/`:

```python
# pages/api/health.py  ->  GET /api/health
from starlette.responses import JSONResponse

async def endpoint(request):
    return JSONResponse({"status": "ok"})
```

| Flask / Django REST Framework | Pyxle |
|-------------------------------|-------|
| `jsonify(...)` / `JsonResponse` | return a Starlette `JSONResponse` from `pages/api/*.py` |
| class-based views / DRF `ViewSet` | a Starlette `HTTPEndpoint` subclass (per-method handlers) |
| DRF serializers | Pydantic models on actions; `pyxle openapi` emits an OpenAPI 3.1 doc from them |

There is no DRF-style router/viewset abstraction — endpoints are plain Starlette handlers. See [API routes](api-routes.md).

## The data layer

Pyxle core ships **no ORM**. The data layer is the first-party **`pyxle-db`** plugin (a separate package, declared in `pyxle.config.json`):

| Django ORM / Flask-SQLAlchemy | pyxle-db |
|-------------------------------|----------|
| `Model.objects.filter(...)` querysets | request-scoped `request.state.db` with explicit SQL (`fetchall`/`fetchone`/`execute`, portable `?` placeholders) |
| declarative models | optional SQLAlchemy ORM path (`request.state.session`) |
| `makemigrations` / `migrate` | checksum-tracked `NNN-slug.sql` migration files applied at startup (or Alembic for the ORM path) |
| Django admin | **no equivalent** — build CRUD UI yourself |

Honest gap: there is no Django-style declarative-model + manager layer in core, and no auto-generated admin. If a built-in admin or ORM is a hard requirement, [the comparison guide](comparison.md) is candid that Django remains the better fit. The closest match is pyxle-db's SQLAlchemy ORM path. See the pyxle-db plugin docs.

## Config and environment

| Flask / Django | Pyxle |
|----------------|-------|
| `app.config` / `settings.py` | `pyxle.config.json` (debug, host/port, middleware, CORS, CSRF, cache, rateLimit, observability, plugins) |
| `INSTALLED_APPS` | `plugins: []` (listed in start order) |
| `DATABASES` | the `pyxle-db` plugin's `settings` block |
| `SECRET_KEY`, env-specific config | `.env` files + `PYXLE_*` env-var overrides; secrets stay in `.env`, never in committed config |

Config is JSON + environment, not a Python config object, and unknown keys are rejected at startup. See [Configuration](../reference/configuration.md) and [Environment variables](environment-variables.md).

## Middleware, auth, and cross-cutting concerns

| Flask / Django | Pyxle |
|----------------|-------|
| `before_request`/`after_request`, `MIDDLEWARE` | app-level Starlette middleware in `middleware: []` (`module:Class` strings) |
| per-view decorators | route-level hooks under `routeMiddleware.{pages,apis,actions}` |
| `flask.session` / Django sessions | `request.state` for per-request data; real sessions via the `pyxle-auth` plugin |
| `login_required`, `django.contrib.auth` | `pyxle-auth` guards (`login_required`/`require_user_page`, `require_permission_*`), `request.user`, the `useAuth()` client hook |
| CORS / CSRF / rate-limit add-ons | built-in `cors`, `csrf`, `rateLimit`, `observability` config blocks |

Middleware is ASGI/Starlette, not WSGI. See [Middleware](middleware.md) and the pyxle-auth plugin docs.

## Errors and 404s

| Flask / Django | Pyxle |
|----------------|-------|
| `abort(404)` / `@app.errorhandler` | raise `LoaderError(message, status_code=...)` (or `ActionError`); the nearest `error.pyxl` renders it |
| custom 404 page | `not-found.pyxl` (directory-scoped, like `error.pyxl`) |

`error.pyxl` is also a **client-side** React error boundary, so a render fault after hydration shows the same page rather than a blank screen. See [Error handling](error-handling.md).

## Static files and styling

`public/` is served at the root URL (`public/favicon.ico` → `/favicon.ico`) — no `collectstatic`, no `{% static %}`. App CSS/JS is better **imported from JSX** so Vite bundles and content-hashes it; `pyxle build` produces hashed assets and a manifest. Plain CSS and CSS Modules work out of the box; Tailwind v4 is one prompt away at `pyxle init`. See [Styling](styling.md).

## Background work and signals

There is no signal/dispatcher bus (`post_save`, Flask signals). For work after a mutation, schedule it explicitly: `request.state.background.add_task(fn, ...)`, the `{"background": [fn, ...]}` action shorthand, or `pyxle.tasks.enqueue(...)` for fire-and-forget. See [Background tasks](background-tasks.md).

## What has no direct equivalent

Be honest with yourself before porting — these have **no** drop-in Pyxle replacement:

- **Django admin** — no auto-generated CRUD UI.
- **Reverse URL routing** (`url_for` / `reverse` / `{% url %}`) — write literal paths.
- **Flash / messages** — surface feedback as React state.
- **A core ORM with declarative models** — use the pyxle-db plugin (explicit SQL or SQLAlchemy).
- **Server-side HTML templating** (Jinja/DTL) — rewrite templates as JSX.
- **Signals / event bus** — use explicit background tasks.
- **Typed URL converters** (`<int:id>`) — cast string params yourself.
- **Widget-rendering form classes** (WTForms/Django widgets) — author inputs in JSX; validate with Pydantic.

## A migration checklist

1. `pyxle init` a new project alongside your existing app.
2. Move config into `pyxle.config.json` + `.env`. Host/port/debug/dir settings have `PYXLE_*` overrides; secrets like Django's `SECRET_KEY` become plain environment variables read directly (e.g. `PYXLE_SECRET_KEY`, used by `pyxle.security` cookie signing and the `pyxle-auth` plugin), not `pyxle.config.json` keys.
3. Port one route at a time: the view's data-fetch → a `@server` loader; the template → a React component; POST handling → an `@action`.
4. Wire the database via the `pyxle-db` plugin; port models to explicit SQL or the SQLAlchemy ORM path, and migrations to SQL files / Alembic.
5. Add auth via the `pyxle-auth` plugin (`login_required` guards, `request.user`, `useAuth()`).
6. Run `pyxle routes` and `pyxle check` to verify the URL map and catch issues.
7. `pyxle build` and `pyxle serve` for production.

## Next steps

- Understand the file format: [.pyxl files](../core-concepts/pyxl-files.md)
- See the full picture of when to choose Pyxle: [Comparison](comparison.md)
- Deploy your migrated app: [Deployment](deployment.md)
