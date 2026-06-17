# Client API Reference

All client-side components and hooks are importable from `pyxle/client`:

```jsx
import {
  Head, Script, Image, ClientOnly,
  Form, useAction, useAuth, useWebSocket,
  Link, navigate, prefetch, refresh, usePathname
} from 'pyxle/client';
```

---

## Components

### `<Head>`

Manages document `<head>` elements during server-side rendering.

```jsx
<Head>
  <title>Page Title</title>
  <meta name="description" content="Description" />
</Head>
```

**Props:** Children only (standard React children).

**Behaviour:**
- Renders `null` in the DOM
- During SSR, extracts children markup and registers head elements
- Elements are merged and deduplicated with the `HEAD` Python variable and layout head blocks
- Can be used in any component (page, layout, or nested)

---

### `<Script>`

Loads external scripts with configurable loading strategies.

```jsx
<Script src="https://analytics.example.com/script.js" strategy="afterInteractive" />
```

**Props:**

| Prop | Type | Default | Description |
|------|------|---------|-------------|
| `src` | `string` | (required) | Script URL |
| `strategy` | `string` | `"afterInteractive"` | When to load the script |
| `async` | `boolean` | `false` | HTML `async` attribute |
| `defer` | `boolean` | `false` | HTML `defer` attribute |
| `module` | `boolean` | `false` | Load as `type="module"` |
| `noModule` | `boolean` | `false` | Add the `nomodule` attribute (legacy-browser fallback) |
| `crossOrigin` | `string` | -- | `crossorigin` attribute |
| `integrity` | `string` | -- | Subresource Integrity (SRI) hash |
| `referrerPolicy` | `string` | -- | `referrerpolicy` attribute |
| `onLoad` | `() => void` | -- | Callback on successful load |
| `onError` | `() => void` | -- | Callback on load failure |

**Strategies:**

| Value | Description |
|-------|-------------|
| `"beforeInteractive"` | Injected in `<head>` before hydration (render-blocking) |
| `"afterInteractive"` | Loaded after React hydration (default) |
| `"lazyOnload"` | Loaded during browser idle time |

**Behaviour:** Renders `null`. The SSR pipeline extracts `<Script>` declarations and handles loading according to the strategy.

---

### `<Image>`

Renders an `<img>` tag with automatic lazy loading.

```jsx
<Image src="/hero.jpg" alt="Hero" width={1200} height={630} />
```

**Props:**

| Prop | Type | Default | Description |
|------|------|---------|-------------|
| `src` | `string` | (required) | Image source URL |
| `alt` | `string` | `""` | Alt text for accessibility |
| `width` | `number` | -- | Image width in pixels |
| `height` | `number` | -- | Image height in pixels |
| `priority` | `boolean` | `false` | Eager loading (above-the-fold images) |
| `lazy` | `boolean` | `true` | Lazy loading (below-the-fold images) |
| `placeholder` | `string` | `"empty"` | `"blur"` shows a blur-up placeholder while loading |
| `blurDataURL` | `string` | -- | Data URL used for the `placeholder="blur"` preview |
| `placeholderColor` | `string` | `"#e5e5e5"` | Solid placeholder colour shown before the image loads |
| `fallbackSrc` | `string` | -- | Image swapped in automatically if `src` fails to load |

**Behaviour:** Renders a standard `<img>` with `loading="eager"` when `priority` is true, `loading="lazy"` otherwise. All additional props are forwarded to the `<img>` element.

---

### `<ClientOnly>`

Renders children only on the client after hydration.

```jsx
<ClientOnly fallback={<p>Loading...</p>}>
  <MapWidget />
</ClientOnly>
```

**Props:**

| Prop | Type | Default | Description |
|------|------|---------|-------------|
| `children` | `ReactNode` | -- | Content to render on the client |
| `fallback` | `ReactNode` | empty `<div>` | Placeholder shown during SSR and the first client render |

**Behaviour:** Returns `fallback` — or an empty `<div>` when no fallback is given — during SSR and on the first client render. After `useEffect` fires (hydration complete), switches to rendering `children`. Prevents hydration mismatch for browser-only content.

---

### `<Form>`

Progressive-enhancement form component for calling server actions.

```jsx
<Form action="create_post" onSuccess={(data) => alert(`Created: ${data.id}`)}>
  <input name="title" required />
  <button type="submit">Create</button>
</Form>
```

**Props:**

| Prop | Type | Default | Description |
|------|------|---------|-------------|
| `action` | `string` | (required) | Name of the `@action` function |
| `pagePath` | `string` | current page | Page where the action is defined |
| `onSuccess` | `(data) => void` | -- | Called with response data on success |
| `onError` | `(message) => void` | -- | Called with error message on failure |
| `resetOnSuccess` | `boolean` | `true` | Reset form fields after success |
| `children` | `ReactNode` | -- | Form contents |

**Behaviour:**
- With JavaScript: intercepts submit, serialises form data to JSON, POSTs to the action endpoint
- Without JavaScript: falls back to a standard HTML form POST
- Displays inline error messages on failure
- Automatically resolves the action endpoint URL
- All additional props are forwarded to the `<form>` element

---

### `<Link>`

Client-side navigation link that prevents full page reloads.

```jsx
<Link href="/about">About Us</Link>
```

Imported from `pyxle/client`. Renders an `<a>` tag that intercepts clicks for client-side navigation.

**Props:**

| Prop | Type | Default | Description |
|------|------|---------|-------------|
| `href` | `string` | -- | Target path (required) |
| `prefetch` | `boolean` | `true` | Prefetch the target page's data and module before the click |
| `replace` | `boolean` | `false` | Replace the current history entry instead of pushing |

**Behaviour:**
- With `prefetch` enabled (the default), the target page's data (its `@server` loader) and component module are prefetched when the link scrolls into view (within 200px) or is hovered — before any click
- A subsequent click reuses the prefetched or in-flight result instead of refetching, so the loader runs once per hover-then-click
- Pass `prefetch={false}` for pages whose loaders are expensive or have side effects
- Prefetched payloads obey the [navigation cache TTL](#navigation-cache-ttl) below

---

## Hooks

### `useAction(actionName, options?)`

Hook for calling server actions programmatically.

```jsx
const deleteItem = useAction('delete_item');

async function handleDelete(id) {
  const result = await deleteItem({ id });
  if (result.ok) {
    console.log('Deleted');
  }
}
```

**Parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| `actionName` | `string` | Name of the `@action` function |
| `options.pagePath` | `string?` | Page where the action is defined (defaults to current page) |
| `options.onMutate` | `(payload) => void` | Called immediately before the request (for optimistic updates) |

**Returns:** An async function with attached state properties.

**Return value properties:**

| Property | Type | Description |
|----------|------|-------------|
| `.pending` | `boolean` | `true` while request is in flight |
| `.error` | `string \| null` | Error message on failure |
| `.data` | `object \| null` | Last successful response data |

**Calling the returned function:**

```jsx
const result = await actionFn(payload);
// result.ok: boolean
// result.error?: string
// result.*: response data fields
```

**Behaviour:**
- New calls abort previous in-flight requests
- State resets on each new call
- `onMutate` fires synchronously before the fetch (use for optimistic UI)

---

### `usePathname()`

Reactive hook that returns the current URL pathname and re-renders on
client-side navigation.

```jsx
import { usePathname, Link } from 'pyxle/client';

function NavLink({ href, children }) {
  const pathname = usePathname();
  const active = pathname === href;
  return (
    <Link href={href} className={active ? 'text-emerald-400' : 'text-zinc-400'}>
      {children}
    </Link>
  );
}
```

**Returns:** `string` — the current pathname (e.g. `/dashboard/settings`).

**Behaviour:**
- Reads `window.location.pathname` on the client
- During SSR, returns the path currently being rendered (via
  `globalThis.__PYXLE_CURRENT_PATHNAME__`) so the first client render matches
  — no hydration mismatch
- Subscribes to framework navigation events (`Link`, `navigate()`,
  `refresh()`, `popstate`) and re-renders on change

---

### `useAuth()`

Read and mutate the signed-in user. State is **shared across every component**
that calls the hook, so a sign-out in the navbar updates the user everywhere at
once.

```jsx
import { useAuth } from 'pyxle/client';

function AccountMenu() {
  const { user, isAuthenticated, loading, logout } = useAuth();

  if (loading) return <span>…</span>;
  if (!isAuthenticated) return <a href="/login">Sign in</a>;

  return (
    <div>
      <span>{user.email}</span>
      <button onClick={() => logout()}>Sign out</button>
    </div>
  );
}
```

A login form is just `login()` plus the error/loading state the hook exposes:

```jsx
function LoginForm() {
  const { login, loading, error } = useAuth();

  async function onSubmit(e) {
    e.preventDefault();
    const data = new FormData(e.currentTarget);
    const result = await login({
      email: data.get('email'),
      password: data.get('password'),
    });
    if (result.ok) navigate('/dashboard');
  }

  return (
    <form onSubmit={onSubmit}>
      <input name="email" type="email" />
      <input name="password" type="password" />
      {error && <p role="alert">{error}</p>}
      <button disabled={loading}>Sign in</button>
    </form>
  );
}
```

**Returns** an object with:

| Field | Type | Description |
|-------|------|-------------|
| `user` | `PyxleUser \| null` | The signed-in user, or `null` when anonymous |
| `isAuthenticated` | `boolean` | `true` when a user is signed in |
| `loading` | `boolean` | `true` while a sign-in / sign-up / refresh is in flight |
| `error` | `string \| null` | The last error message, or `null` |
| `login(credentials)` | `(creds) => Promise<AuthResult>` | Sign in via `POST {prefix}/login` |
| `signup(credentials)` | `(creds) => Promise<AuthResult>` | Create an account via `POST {prefix}/signup` |
| `logout()` | `() => Promise<void>` | Sign out via `POST {prefix}/logout` and clear local state |
| `refresh()` | `() => Promise<PyxleUser \| null>` | Re-fetch the user from `GET {prefix}/me` |

**Behaviour:**
- Requires the [`pyxle-auth`](../plugins/pyxle-auth.md) plugin, which serves the
  endpoints and seeds the initial user.
- Seeds the user from the server render (`window.__PYXLE_AUTH__`), so a
  signed-in user appears on the first client frame **with no round-trip** and
  no hydration mismatch. When no seed is present (e.g. a client-only render),
  the session is resolved once on mount via `{prefix}/me`.
- `login` / `signup` / `logout` send the CSRF token automatically, exactly like
  [`useAction`](#useactionactionname-options) and [`<Form>`](#form).
- `login` / `signup` resolve to `{ ok, user?, error?, code? }`; the `code`
  mirrors the server (`invalid_credentials`, `account_exists`, `weak_password`,
  `email_not_verified`, `rate_limited`).

> The login/signup endpoints are on by default. Apps that own their sign-in flow
> set `enableCredentialsApi: false` on the plugin and call their own `@action`,
> then `refresh()` — `useAuth` still manages shared user state, `/me`, and
> `logout`.

---

### `useWebSocket(path, options?)`

Connect to a WebSocket endpoint — a page's `async def websocket(ws)` handler, or
any `ws` path — with auto-reconnect, JSON parsing, and connection state.

```jsx
import { useWebSocket } from 'pyxle/client';

function Chat({ room }) {
  const { status, send, lastMessage, error } = useWebSocket(`/chat/${room}`, {
    onMessage(data) { /* data is JSON-parsed when possible */ },
  });

  return (
    <button disabled={status !== 'open'} onClick={() => send({ text: 'hi' })}>
      {status === 'open' ? 'Send' : status}
    </button>
  );
}
```

**Returns** `{ status, send, lastMessage, error }`:

| Field | Type | Description |
|-------|------|-------------|
| `status` | `'connecting' \| 'open' \| 'closed'` | Connection state |
| `send(data)` | `(data) => boolean` | Send a string as-is, or JSON-encode anything else; `false` if not open |
| `lastMessage` | `unknown` | The most recent received message (JSON-parsed when the frame is valid JSON) |
| `error` | `string \| null` | The last error message |

**Options:** `onMessage(data, event)`, `protocols`, `reconnect` (default `true`),
`maxRetries` (default `Infinity`).

**Behaviour:**
- **Never connects during SSR** — all socket code is gated behind a window check,
  so there's no hydration mismatch.
- Reconnects with **exponential backoff** (capped at 30s, with jitter) unless
  `reconnect: false`.
- A relative `path` resolves against the current origin with the matching scheme
  (`wss:` on `https:`); an absolute `ws://` / `wss://` URL passes through.
- See the [WebSockets guide](../guides/websockets.md) for the server side.

---

## Functions

### `navigate(path)`

Trigger client-side navigation programmatically.

```jsx
import { navigate } from 'pyxle/client';

navigate('/dashboard');
```

### `prefetch(path)`

Prefetch a page's data and assets.

```jsx
import { prefetch } from 'pyxle/client';

<button onMouseEnter={() => prefetch('/dashboard')}>
  Go to Dashboard
</button>
```

### `refresh()`

Re-run the current page's `@server` loader and re-render with fresh data. Does not reload the page or change scroll position.

```jsx
import { refresh } from 'pyxle/client';

<button onClick={() => refresh()}>
  Refresh data
</button>
```

### `invalidate(url?)`

Drop a URL from the client-side navigation cache so the next `navigate(url)` refetches the loader payload instead of replaying the cached one. Call this after a mutation (create, update, delete) that affects a list view the user might navigate back to.

Without an argument, clears every cached entry. Returns `true` if an entry was evicted.

```jsx
import { invalidate, navigate } from 'pyxle/client';

async function handleDelete(id) {
  await deletePost({ id });
  invalidate('/posts');      // drop the cached /posts list
  navigate('/posts');         // next visit refetches
}
```

**Related: server-driven invalidation.** Your `@action` can tell the client which URLs to invalidate via the [`invalidate_routes()`](runtime-api.md#invalidate_routesresponse-urls) helper. Responses carrying an `x-pyxle-invalidate` header are honoured automatically by `useAction` and `<Form>`, so most apps never call `invalidate()` in client code directly.

### Navigation cache TTL

Client-side loader payloads are cached per URL so back/forward navigation is instant while data stays reasonably fresh. A route listed in the `cache` block of `pyxle.config.json` reuses its edge-cache TTL as its navigation-cache lifetime; all other routes default to **2 minutes**. Tune the default with [`navigation.defaultPrefetchTtl`](configuration.md#navigation) (seconds) in `pyxle.config.json`:

```json
{
  "navigation": {
    "defaultPrefetchTtl": 60
  }
}
```

Useful values:

- `0` — disable caching for routes without a `cache` entry; every navigation hits the server.
- `120` (default) — keep prefetched and seeded payloads fresh for 2 minutes.
- a large number — cache for the lifetime of the tab.

As a runtime escape hatch, the default can also be overridden by setting a global (in milliseconds) before Pyxle's client runtime boots (e.g. in a `<Script strategy="beforeInteractive">` block) — note it does not affect routes with a `cache` entry:

```jsx
<Script strategy="beforeInteractive">
  {`window.__PYXLE_NAV_STALE_MS__ = 60000;`}  {/* 60s */}
</Script>
```

For per-mutation control, prefer [`invalidate(url)`](#invalidateurl) or the `x-pyxle-invalidate` response header over global TTL tuning.
