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

```python
@action
async def create_post(request):
    body = await request.json()
    return {"id": 1, "title": body["title"]}
```

```jsx
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
| `onError` | `(message) => void` | Called with error message on failure |
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
| `data` | `object \| null` | Last successful response data |

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

CSRF protection is on by default, so a raw `curl` like this is rejected with `403` unless you either exempt the action path in [`pyxle.config.json`](../reference/configuration.md#csrf) (`"csrf": { "exemptPaths": ["/api/__actions/"] }`) or send a valid `X-CSRF-Token` header matching the `pyxle-csrf` cookie.

## CSRF protection

Actions are protected by CSRF middleware by default. The `<Form>` component and `useAction` hook handle token management automatically. See [Security](../guides/security.md) for details.

## Next steps

- Wrap pages in layouts: [Layouts](layouts.md)
- Handle errors with boundaries: [Error Handling](../guides/error-handling.md)
