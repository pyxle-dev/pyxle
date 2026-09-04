# Routing

Pyxle uses **file-based routing**. The file structure inside your `pages/` directory determines the URL routes of your application. No router configuration is needed.

## Basic routes

Every `.pyxl` file in `pages/` becomes a route:

| File | URL |
|------|-----|
| `pages/index.pyxl` | `/` |
| `pages/about.pyxl` | `/about` |
| `pages/contact.pyxl` | `/contact` |
| `pages/blog/index.pyxl` | `/blog` |
| `pages/blog/archive.pyxl` | `/blog/archive` |

The `index.pyxl` filename is special -- it maps to the parent directory's path.

## Dynamic segments

Wrap a filename segment in square brackets to create a dynamic route parameter:

| File | URL Pattern | Example |
|------|-------------|---------|
| `pages/blog/[slug].pyxl` | `/blog/:slug` | `/blog/hello-world` |
| `pages/users/[id].pyxl` | `/users/:id` | `/users/42` |
| `pages/[category]/[id].pyxl` | `/:category/:id` | `/electronics/99` |

> The `:slug` notation above is illustrative. The actual registered
> path uses Starlette's `{slug}` (and `{slug:path}` for catch-alls)
> placeholders — this is what `pyxle routes` prints.

Access dynamic parameters in your loader via `request.path_params`:

```pyxl
@server
async def load_post(request):
    slug = request.path_params["slug"]
    post = await fetch_post(slug)
    return {"post": post}

export default function BlogPost({ data }) {
  return <h1>{data.post.title}</h1>;
}
```

### Parameter name rules

- Hyphens in parameter names become underscores: `[user-id].pyxl` --> `request.path_params["user_id"]`
- The parameter name is sanitised to be a valid Python identifier

## Catch-all routes

Use `[...param].pyxl` to match any number of path segments:

| File | URL Pattern | Example Matches |
|------|-------------|----------------|
| `pages/docs/[...slug].pyxl` | `/docs/*` | `/docs/getting-started`, `/docs/api/config` |

The parameter captures the entire remaining path as a string:

```python
@server
async def load_docs(request):
    slug = request.path_params["slug"]  # "getting-started" or "api/config"
    return {"slug": slug}
```

## Optional catch-all routes

Use `[[...param]].pyxl` (double brackets) to match the parent path **and** any sub-paths:

| File | URL Pattern | Example Matches |
|------|-------------|----------------|
| `pages/docs/[[...slug]].pyxl` | `/docs` and `/docs/*` | `/docs`, `/docs/intro`, `/docs/api/ref` |

This creates two routes: one for the exact parent path and one for the catch-all.

## Route groups

Wrap a directory name in parentheses to **exclude it from the URL**:

```
pages/
  (marketing)/
    pricing.pyxl      -->  /pricing     (not /marketing/pricing)
    features.pyxl     -->  /features
  (dashboard)/
    settings.pyxl     -->  /settings
    profile.pyxl      -->  /profile
```

Route groups let you organise files by concern without affecting URLs.

## API routes

Python files under `pages/api/` become API endpoints:

| File | URL |
|------|-----|
| `pages/api/pulse.py` | `/api/pulse` |
| `pages/api/users.py` | `/api/users` |
| `pages/api/users/[id].py` | `/api/users/:id` |

An `api` directory may sit at any depth, so `pages/s/[slug]/api/v2/export.py`
serves `/s/:slug/api/v2/export`. A `.py` file outside one is ignored — it
becomes neither an API route nor a page, which is what makes it safe to
colocate helpers with your pages.

An `api` directory is server ground throughout: it holds endpoints, so a
`.pyxl` page in one is refused, and the client assets beside them are never
shipped to the browser. Only directories count — `pages/api.pyxl` is an
ordinary page.

API routes are standard Python files (not `.pyxl`). See [API Routes](../guides/api-routes.md) for details.

## Viewing your route table

Run `pyxle routes` to see all registered routes:

```bash
pyxle routes

# Or as JSON:
pyxle routes --json
```

## Special files

These filenames have special meaning and do not create their own routes:

| File | Purpose |
|------|---------|
| `layout.pyxl` | Wraps sibling and descendant pages in a shared layout |
| `template.pyxl` | Like layout but resets state on navigation |
| `error.pyxl` | Error boundary for sibling and descendant pages |
| `not-found.pyxl` | 404 page for the current directory scope |

See [Layouts](layouts.md) and [Error Handling](../guides/error-handling.md).

## Next steps

- Load data for your pages: [Data Loading](data-loading.md)
- Wrap pages in shared layouts: [Layouts](layouts.md)
