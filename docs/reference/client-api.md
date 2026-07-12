# Client API Reference

All client-side components and hooks are importable from `pyxle/client`:

```jsx
import {
  Head, Script, Image, ClientOnly,
  Form, useAction, useAuth, useWebSocket,
  Link, navigate, prefetch, refresh, invalidate, usePathname
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
| `onError` | `(error: Error) => void` | -- | Callback on load failure; receives the `Error` |
| `children` | `string` | -- | Inline script source, used when `src` is omitted |

**Strategies:**

| Value | Description |
|-------|-------------|
| `"beforeInteractive"` | Injected in `<head>` before hydration (render-blocking) |
| `"afterInteractive"` | Loaded after React hydration (default) |
| `"lazyOnload"` | Loaded during browser idle time |

**Behaviour:** Renders `null`. For a **statically written** `<Script>` with a `src`, the SSR pipeline extracts the declaration at build time and the bootstrap loader honours its `strategy`. A `<Script>` rendered dynamically (conditionally, in a mapped list, or with a runtime `src`) is not seen by the compiler and is loaded by the runtime component on mount; `strategy="beforeInteractive"` is impossible in that case — it logs a `[Pyxle Script]` warning and degrades to `afterInteractive`.

**Inline scripts** (`children` with no `src`) are appended to `<head>` on mount and honour only `module` (rendered as `type="module"`); `strategy`, `async`, `defer`, `noModule`, `crossOrigin`, `integrity`, `referrerPolicy`, and `onError` apply to external (`src`) scripts only, and `onLoad` fires synchronously after the inline element is inserted. For an inline script that must run **before** hydration, use a native `<script>` in a layout's `<Head>` (preserved verbatim) instead.

---

### `<Image>`

An optimized `<img>` with responsive `srcset` (via a `loader`), `fill` mode,
priority loading, layout-shift prevention, and a blur placeholder. See the
[Image optimization guide](../guides/build-optimization.md#image-optimization).

```jsx
<Image src="/hero.jpg" alt="Hero" width={1200} height={630} priority />
```

**Props:**

| Prop | Type | Default | Description |
|------|------|---------|-------------|
| `src` | `string` | (required) | Image source URL |
| `alt` | `string` | `""` | Alt text for accessibility |
| `width` | `number` | -- | Intrinsic width (px). With `height`, reserves space to avoid layout shift |
| `height` | `number` | -- | Intrinsic height (px) |
| `fill` | `boolean` | `false` | Fill the nearest positioned ancestor (omit width/height) |
| `sizes` | `string` | -- | `sizes` attribute — which srcset candidate the browser picks |
| `loader` | `({src,width,quality}) => string` | -- | Builds optimized URLs per width; **enables a responsive `srcset`**. Without it, a plain `<img>` |
| `quality` | `number` | -- | 1–100, passed to the `loader` |
| `objectFit` | `string` | `cover` (under fill) | CSS `object-fit`. Applied whether or not `fill` is set; the `cover` default only kicks in under `fill` |
| `priority` | `boolean` | `false` | Eager + `decoding="sync"` + `fetchpriority="high"` — use for the LCP image |
| `lazy` | `boolean` | `true` | Native lazy loading (ignored when `priority`) |
| `placeholder` | `string` | `"empty"` | `"blur"` shows a blur-up placeholder while loading |
| `blurDataURL` | `string` | -- | Data URL used for the `placeholder="blur"` preview |
| `placeholderColor` | `string` | `"#e5e5e5"` | Solid placeholder colour before the image loads |
| `fallbackSrc` | `string` | -- | Image swapped in automatically if `src` fails to load |
| `onLoad` | `(event) => void` | -- | Fires when the image loads. On a cache hit it receives a synthetic `{ nativeEvent, target, fromCache }` object rather than a React event |
| `onError` | `(event) => void` | -- | Fires only **after** `fallbackSrc` (if set) has also failed |

**Responsive images:** provide a `loader` (a CDN such as Cloudinary/imgix, or a build plugin) and `<Image>` emits a `srcset` across a device-size ladder. Without a loader it stays a plain `<img>` — resizing needs a real backend, so Pyxle never emits a fake srcset that re-downloads the full image. All additional props are forwarded to the `<img>`.

**Behaviour:** `<Image>` always renders `data-pyxle-image-state="loading" | "loaded" | "error"` on the underlying `<img>`, so you can target it from CSS or end-to-end tests (e.g. `[data-pyxle-image-state="loading"]` to style the placeholder).

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
| `onError` | `(message, fields) => void` | -- | Called on failure with the message and, for a `422` validation error, the per-field errors (`{ [field]: string[] }`) — otherwise `null` |
| `resetOnSuccess` | `boolean` | `true` | Reset form fields after success |
| `children` | `ReactNode` | -- | Form contents |

**Behaviour:**
- With JavaScript: intercepts submit, serialises form data to JSON, POSTs to the action endpoint
- Without JavaScript: falls back to a standard HTML form POST. The POST reaches the action and runs it, but action endpoints always return JSON, so a no-JS submit navigates to a page showing the raw JSON response (e.g. `{"ok":true,...}`) — there is no redirect or re-render. Full no-JS form flows that re-render the page are not yet supported; treat the no-JS path as a graceful-degradation safety net, not a primary UX.
- Displays inline error messages on failure
- For an action with a [Pydantic body model](../core-concepts/server-actions.md#validating-request-bodies-with-pydantic), a `422` passes the field map to `onError` so you can render messages next to inputs
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
| `scroll` | `boolean \| 'preserve'` | -- | Scroll behaviour after navigation (`'preserve'` keeps the current scroll position) |
| `shallow` | `boolean` | -- | Update the URL without re-running the loader |
| `passHref` | `boolean` | -- | Pass `href` through to a custom child anchor |
| `onClick` | `(event) => void` | -- | Called before navigation; calling `preventDefault()` cancels client-side nav |
| `onMouseEnter` | `(event) => void` | -- | Called on hover (alongside prefetch) |

All standard anchor attributes — `className`, `target`, `rel`, `style`, `key`, `aria-*`, and so on — are forwarded to the underlying `<a>`.

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
| `.fields` | `Record<string, string[]> \| null` | Per-field validation errors from the last failed submit (a `422`), or `null`. Cleared when a new request starts |
| `.data` | `object \| null` | Last successful response data |

**Calling the returned function:**

```jsx
const result = await actionFn(payload);
// result.ok: boolean
// result.error?: string          (when !ok)
// result.fields?: Record<string, string[]> | null   (when !ok — per-field 422 errors)
// result.*: response data fields  (when ok)
```

The `.data` row in the properties table above is the **hook's** convenience copy of the last
successful result, for reading outside the call. The value you `await` is the flat result itself —
read returned fields directly off it (`result.title`), **not** under `result.data`. A successful
result has no `.data`.

**Behaviour:**
- New calls abort previous in-flight requests
- State resets on each new call
- `onMutate` fires synchronously before the fetch (use for optimistic UI)
- For an action with a [Pydantic body model](../core-concepts/server-actions.md#validating-request-bodies-with-pydantic), a `422` populates `.fields` / `result.fields` with `{ [fieldPath]: string[] }`

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

### `navigate(path, options?)`

Trigger client-side navigation programmatically. Returns a `Promise<boolean>` that resolves `true` once the navigation completes, or `false` when there is no router or the target is not client-navigable (in which case it falls back to a full page load).

```jsx
import { navigate } from 'pyxle/client';

await navigate('/dashboard');
await navigate('/dashboard', { replace: true, scroll: 'preserve' });
```

**Options:**

| Option | Type | Description |
|--------|------|-------------|
| `replace` | `boolean` | Replace the current history entry instead of pushing |
| `scroll` | `boolean \| 'preserve'` | Scroll behaviour after navigation |
| `shallow` | `boolean` | Update the URL without re-running the loader |

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

Client-side loader payloads are cached per URL so back/forward navigation is instant while data stays reasonably fresh. The lifetime for a route is resolved to mirror the server's real cacheability:

- A route listed in the `cache` block of `pyxle.config.json` (or with a `CACHE` directive) reuses that **edge-cache TTL** as its navigation-cache lifetime.
- A **dynamic page** — one with a `@server` loader but **no** declared cache lifetime — is **not** navigation-cached (TTL `0`). It renders `private, no-cache` and its data can change between requests (live updates, per-user content), so back/forward always refetches and a mutation shows immediately instead of being hidden behind a stale window.
- A **static, loader-less page** falls back to the client default of **2 minutes**.

So navigation caching for a loader page is **opt-in**: add a `cache` entry to give it a TTL. Tune the static default with [`navigation.defaultPrefetchTtl`](configuration.md#navigation) (seconds) in `pyxle.config.json`:

```json
{
  "navigation": {
    "defaultPrefetchTtl": 60
  }
}
```

This default applies to **static, loader-less** routes only — dynamic loader pages without a `cache` entry are never navigation-cached regardless of this value. Useful values:

- `0` — disable caching for static routes too; every navigation hits the server.
- `120` (default) — keep static-page prefetched and seeded payloads fresh for 2 minutes.
- a large number — cache static pages for the lifetime of the tab.

As a runtime escape hatch, the default can also be overridden by setting a global (in milliseconds) before Pyxle's client runtime boots — note it does not affect routes with a `cache` entry. Put it in a native inline `<script>` inside a layout's `<Head>` (head `<script>` content is preserved verbatim and runs before hydration). Do **not** use an inline `<Script>{...}` component for this — the compiler discards `<Script>` children and only injects scripts that have a `src`, so an inline `<Script>` runs on mount (after boot), too late to set this global:

```jsx
// pages/layout.pyxl
import { Head } from 'pyxle/client';

export default function RootLayout({ children }) {
  return (
    <>
      <Head>
        <script>{`window.__PYXLE_NAV_STALE_MS__ = 60000;`}</script>  {/* 60s */}
      </Head>
      {children}
    </>
  );
}
```

For per-mutation control, prefer [`invalidate(url)`](#invalidateurl) or the `x-pyxle-invalidate` response header over global TTL tuning.
