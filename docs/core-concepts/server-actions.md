# Server Actions

Server actions let your React components call Python functions on the server. They are the mutation counterpart to `@server` loaders -- loaders read data, actions write data.

## Defining an action

Use the `@action` decorator on an async function in your `.pyxl` file:

```python
@action
async def create_post(request):
    body = await request.json()
    title = body["title"]
    content = body["content"]
    # ... save to database ...
    return {"id": 1, "title": title}
```

The function:

- Must be `async`
- Receives a Starlette `Request` object
- Must return a JSON-serializable `dict`
- Can only be called via `POST` request

Multiple actions can exist in the same `.pyxl` file alongside a `@server` loader.

## Calling actions from React

### Using the `<Form>` component

The simplest way to call an action from a form. `<Form>` collects the inputs, posts them to the action, and exposes `onSuccess` / `onError` callbacks:

```pyxl
@action
async def create_post(request):
    body = await request.json()
    return {"id": 1, "title": body["title"]}

import { Form } from 'pyxle/client';

export default function NewPostPage() {
  return (
    <Form
      action="create_post"
      onSuccess={(data) => console.log('Created:', data.id)}
      onError={(msg) => console.error('Failed:', msg)}
    >
      <input name="title" placeholder="Post title" required />
      <textarea name="content" placeholder="Write something..." />
      <button type="submit">Create Post</button>
    </Form>
  );
}
```

`<Form>` props:

| Prop | Type | Description |
|------|------|-------------|
| `action` | `string` | Name of the `@action` function |
| `pagePath` | `string?` | Override which page the action belongs to (defaults to current page) |
| `onSuccess` | `(data) => void` | Called with response data on success |
| `onError` | `(message, fields) => void` | Called with the error message and, for a `422` validation failure, the per-field errors (`{ [field]: string[] }`) — otherwise `null` |
| `resetOnSuccess` | `boolean` | Reset form fields after success (default: `true`) |

### Using the `useAction` hook

For programmatic calls (not form submissions), use the `useAction` hook:

```jsx
import { useAction } from 'pyxle/client';

export default function ProfilePage({ data }) {
  const updateName = useAction('update_name');

  async function handleClick() {
    const result = await updateName({ name: 'Alice' });
    if (result.ok) {
      console.log('Updated!');
    }
  }

  return (
    <div>
      <p>Name: {data.user.name}</p>
      <button onClick={handleClick} disabled={updateName.pending}>
        {updateName.pending ? 'Saving...' : 'Change to Alice'}
      </button>
      {updateName.error && <p style={{ color: 'red' }}>{updateName.error}</p>}
    </div>
  );
}
```

`useAction` returns an async function with attached state:

| Property | Type | Description |
|----------|------|-------------|
| `pending` | `boolean` | `true` while the request is in flight |
| `error` | `string \| null` | Error message on failure, `null` otherwise |
| `fields` | `Record<string, string[]> \| null` | Per-field validation errors from the last failed submit (a `422`), or `null`. Cleared when a new request starts |
| `data` | `object \| null` | Last successful response data |

The value you `await` is the **flat** result — `{ ok: true, ...yourReturn }` on success — so read
returned fields directly off it (`result.title`), **not** under `result.data`. A successful result
has no `.data`. The `data` property in the table above is the *hook's* convenience copy of that same
payload, for reading it outside the call site — don't confuse the two.

Options:

| Option | Type | Description |
|--------|------|-------------|
| `pagePath` | `string?` | Override which page the action belongs to |
| `onMutate` | `(payload) => void` | Called immediately before the request (for optimistic updates) |

## Error handling in actions

Raise `ActionError` to return a structured error response:

```python
@action
async def delete_post(request):
    body = await request.json()
    post = await fetch_post(body["id"])
    if post is None:
        raise ActionError("Post not found", status_code=404)
    if post["author_id"] != request.state.user_id:
        raise ActionError("Not authorised", status_code=403)
    await db.delete(post)
    return {"deleted": True}
```

> Since 0.3.0, `ActionError` is auto-imported by the compiler for any `.pyxl` file that declares at least one `@action`. No explicit import is required. A user-defined `ActionError` class (or an existing import) is respected and takes precedence.

The client receives:

```json
{ "ok": false, "error": "Not authorised" }
```

### When an action crashes

`ActionError` is a refusal you wrote. An exception you did not expect is a bug,
and Pyxle treats the two differently.

In production the caller is told nothing about it:

```json
{ "ok": false, "error": "An unexpected error occurred." }
```

with status `500` — the real message could carry a file path, a row ID or a
connection string, so it never leaves the server. What does leave is a log
line, written once, with the full traceback and the action that raised it:

```
Action 'add_note' in module 'pyxle.server.pages.notes' failed: 'title'
Traceback (most recent call last):
  ...
  File "pages/notes.py", line 12, in add_note
    title = body["title"]
KeyError: 'title'
```

That line is your only record of the failure, so collect your server's log in
production — with nothing capturing stdout, a crash leaves no trace you can
read afterwards. Under `pyxle dev` the same traceback is written and the
caller additionally receives the real message.

Nothing the action scheduled is applied when it crashes: cookies set through
`request.state.cookies` and tasks added to `request.state.background` are both
dropped, because the action's intent is unknown. A deliberate `ActionError`
keeps both — see [Setting a cookie](#setting-a-cookie) and [Running work after
the response](#running-work-after-the-response).

## Validating request bodies with Pydantic

Reading `await request.json()` and pulling fields out by hand means writing the
same "is this present, is it the right type, is it in range" checks in every
action. Pyxle can do that for you: annotate a parameter with a
[Pydantic](https://docs.pydantic.dev/) model and the dispatcher parses, coerces,
and validates the request body **before** your action runs. Your function only
executes with a valid, fully-typed model.

```python
from pydantic import BaseModel, EmailStr, Field

class CreatePost(BaseModel):
    title: str = Field(min_length=1, max_length=120)
    content: str
    tags: list[str] = []

@action
async def create_post(request, body: CreatePost):
    # `body` is a validated CreatePost instance — no manual checks needed.
    post = await db.insert_post(title=body.title, content=body.content)
    return {"id": post.id, "title": post.title}
```

The body parameter:

- Is the first parameter (after `request`) annotated with a Pydantic `BaseModel`
  subclass. The name is up to you (`body`, `payload`, `form`, …).
- May be `Optional[Model]` / `Model | None` for an optional body.
- Is parsed from the JSON request body. `<Form>` submissions (which post form
  fields) validate the same way.

Pydantic is an **optional dependency**. Install it with the extra:

```bash
pip install "pyxle-framework[pydantic]"
```

If an action declares a **required** Pydantic body but Pydantic isn't installed, the request
fails with a clear `500` (and the dev server logs why) — actions without a model
parameter are unaffected. An **optional** body (`Model | None` with a default) silently
skips validation when Pydantic is absent: the action runs with the body never validated
and the parameter left at its default.

### What the client receives on a validation failure

When the body fails validation, the action is **not** called. The client gets a
`422` response with a top-level `fields` map — field path to a list of messages:

```json
{
  "ok": false,
  "error": "Validation failed",
  "fields": {
    "title": ["String should have at least 1 character"],
    "tags.0": ["Input should be a valid string"]
  }
}
```

Field paths are dotted for nested models (`address.zip`) and indexed for list
items (`tags.0`). Whole-body errors — a malformed JSON body, or a root-level
model error — use the key `__root__` rather than a field name; render it as a
form-level message.

### Showing field errors in the UI

Both `useAction` and `<Form>` surface the `fields` map so you can render messages
next to each input. With `useAction`, read `result.fields` (or the reactive
`submit.fields`):

```jsx
import { useAction } from 'pyxle/client';

export default function NewPostPage() {
  const submit = useAction('create_post');

  async function onSubmit(event) {
    event.preventDefault();
    const form = new FormData(event.target);
    const result = await submit(Object.fromEntries(form));
    if (result.ok) {
      // navigate, toast, etc.
    }
  }

  return (
    <form onSubmit={onSubmit}>
      <input name="title" />
      {submit.fields?.title && <p className="error">{submit.fields.title[0]}</p>}
      <textarea name="content" />
      <button disabled={submit.pending}>Create</button>
    </form>
  );
}
```

With `<Form>`, the field map is the second argument to `onError`:

```jsx
<Form
  action="create_post"
  onError={(message, fields) => setFieldErrors(fields ?? {})}
>
  {/* inputs */}
</Form>
```

### Raising validation errors yourself

For checks Pydantic can't express — uniqueness, cross-field rules, anything that
needs a database — raise `ValidationActionError`. It produces the same `422`
shape, so the client handles model-level and hand-rolled errors identically:

```python
from pyxle.runtime import ValidationActionError

@action
async def register(request, body: Signup):
    if await db.email_taken(body.email):
        raise ValidationActionError(fields={"email": ["That email is already registered."]})
    ...
```

`ValidationActionError` is auto-imported alongside `ActionError` in any `.pyxl`
file that declares an `@action`. See the [Runtime API reference](../reference/runtime-api.md#validationactionerror).

### Exporting an OpenAPI schema

Because the body models are real Pydantic models, Pyxle can generate an
[OpenAPI 3.1](https://spec.openapis.org/oas/v3.1.0) document describing every
action's request body straight from your code:

```bash
pyxle openapi --out openapi.json
```

See the [CLI reference](../reference/cli.md#pyxle-openapi) for options.

## Invalidating client caches after a mutation

Pyxle's client router caches loader payloads (for 2 minutes by default, or the route's configured `cache` TTL) so back/forward navigation is instant. A mutation that changes data the user might navigate back to should invalidate the stale route so the next visit refetches:

```python
from pyxle.runtime import invalidate_routes

@action
async def delete_post(request):
    body = await request.json()
    await db.delete_post(body["id"])
    # Drop the client's cached /posts list so the next visit refetches.
    return invalidate_routes({"ok": True}, "/posts", "/dashboard")
```

Pyxle attaches an `x-pyxle-invalidate: /posts, /dashboard` header to the response. `<Form>` and `useAction` honour the header automatically — neither the calling component nor the page component needs to know about it.

For purely client-driven invalidation (no server roundtrip), use [`invalidate(url)`](../reference/client-api.md#invalidateurl) from `pyxle/client`.

## Running work after the response

To do work without making the client wait — send an email, emit a webhook —
schedule it on `request.state.background`. Pyxle runs it after the response is
sent:

```python
@action
async def signup(request, body: Signup):
    user = await db.create_user(body.email)
    request.state.background.add_task(send_welcome_email, user.email)
    return {"id": user.id}
```

For a single task you can return the shorthand `{"background": [fn, *args]}`
instead. For fire-and-forget work that isn't tied to this response (including
from `@server` loaders), use [`pyxle.tasks.enqueue`](../guides/background-tasks.md#fire-and-forget-work-pyxletasks). See the [Background Tasks guide](../guides/background-tasks.md).

## Setting a cookie

An action returns a dict, so it never sees the response that dict becomes. To
put a cookie on it, record the call on `request.state.cookies`:

```python
@action
async def choose_theme(request, body: Theme):
    request.state.cookies.set(
        "theme", body.theme, max_age=31536000, httponly=True, samesite="lax"
    )
    return {"theme": body.theme}
```

`set()` and `delete()` take Starlette's
[`set_cookie`](https://www.starlette.io/responses/#set-cookie) and
`delete_cookie` arguments and are replayed onto the response unchanged. They
also apply to an [`ActionError`](#error-handling-in-actions) — a refusal is
still the action's own answer, and may want to clear a session or record an
attempt on the way out. An unexpected 500 sets nothing.

For a cookie the client must not be able to forge, sign the value with
[`pyxle.sign_cookie`](../guides/security.md) first.

## How actions are routed

Each `@action` function gets an automatic endpoint:

```
POST /api/__actions/{page_path}/{action_name}
```

For example, `create_post` in `pages/blog/new.pyxl` is available at:

```
POST /api/__actions/blog/new/create_post
```

You do not need to know these URLs -- `<Form>` and `useAction` resolve them automatically.

### Calling an action directly (scripts and tests)

App code never needs the URL, but a script or test can POST to it directly. Send a JSON body; the response is the action's return dict (with `ok: true`), or `{ "ok": false, "error": "..." }` when it raises an `ActionError`:

```bash
curl -X POST http://localhost:8000/api/__actions/blog/new/create_post \
  -H "Content-Type: application/json" \
  -d '{"title": "Hello"}'
```

CSRF protection is on by default, so a raw `curl` like this is rejected with `403` unless you either exempt the action path in [`pyxle.config.json`](../reference/configuration.md#csrf) (`"csrf": { "exemptPaths": ["/api/__actions/"] }`) or send a valid `X-CSRF-Token` header matching the CSRF cookie (`pyxle-csrf-<port>` by default, e.g. `pyxle-csrf-8000` — see [Security](../guides/security.md#csrf-protection)).

## CSRF protection

Actions are protected by CSRF middleware by default. The `<Form>` component and `useAction` hook handle token management automatically. See [Security](../guides/security.md) for details.

## Route policies on actions

Actions run through the same route-hook pipeline as pages and API routes, so an auth or rate-limit policy can apply to `@action` POSTs. Register action hooks under [`routeMiddleware.actions`](../reference/configuration.md#routemiddleware) — an action hook should reject with a JSON `401`/`403` (not an HTML redirect, which a page hook might use). See the [Middleware guide](../guides/middleware.md#route-level-hooks).

## Next steps

- Wrap pages in layouts: [Layouts](layouts.md)
- Handle errors with boundaries: [Error Handling](../guides/error-handling.md)
