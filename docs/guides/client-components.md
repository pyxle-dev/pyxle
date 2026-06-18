# Client Components

Pyxle provides built-in React components and hooks importable from `pyxle/client`.

```jsx
import { Head, Script, Image, ClientOnly, Form, useAction, Link, navigate, prefetch } from 'pyxle/client';
```

## `<Head>`

Manages document `<head>` elements. See [Head Management](head-management.md) for full details.

```jsx
<Head>
  <title>My Page</title>
  <meta name="description" content="Page description" />
</Head>
```

Renders nothing in the DOM. During SSR, its children are extracted and merged into the document head.

## `<Script>`

Loads external scripts with configurable loading strategies:

```jsx
<Script src="https://analytics.example.com/script.js" strategy="afterInteractive" />
```

### Props

| Prop | Type | Default | Description |
|------|------|---------|-------------|
| `src` | `string` | (required for external) | Script URL |
| `strategy` | `string` | `"afterInteractive"` | Loading strategy |
| `async` | `boolean` | `false` | Add `async` attribute |
| `defer` | `boolean` | `false` | Add `defer` attribute |
| `module` | `boolean` | `false` | Load as `type="module"` |
| `noModule` | `boolean` | `false` | Add the `nomodule` attribute (legacy-browser fallback) |
| `crossOrigin` | `string` | -- | `crossorigin` attribute |
| `integrity` | `string` | -- | Subresource Integrity (SRI) hash |
| `referrerPolicy` | `string` | -- | `referrerpolicy` attribute |
| `onLoad` | `() => void` | -- | Called when script loads |
| `onError` | `(error: Error) => void` | -- | Called on load failure; receives the `Error` |
| `children` | `string` | -- | Inline script source, used when `src` is omitted |

Inline scripts (`children`, no `src`) honour only `module`; the other props apply to external (`src`) scripts. See the [`<Script>` reference](../reference/client-api.md#script) for the full behaviour.

### Loading strategies

| Strategy | When it loads |
|----------|--------------|
| `"beforeInteractive"` | In `<head>` before hydration (blocking) |
| `"afterInteractive"` | After hydration completes (default) |
| `"lazyOnload"` | During browser idle time |

## `<Image>`

An optimized `<img>` with lazy loading, layout-shift prevention, a blur
placeholder, and — with a `loader` — responsive `srcset`:

```jsx
<Image src="/photos/hero.jpg" alt="Hero image" width={800} height={600} priority />
```

`priority` loads the image eagerly with `fetchpriority="high"` (use it for the
LCP image); everything else lazy-loads. Pass a `loader` (a CDN or build plugin)
for responsive images, or `fill` to cover a positioned parent. See the
[Build Optimization guide](build-optimization.md#image-optimization) for the
full prop list and CDN/plugin integration, and the
[`<Image>` reference](../reference/client-api.md#image).

## `<ClientOnly>`

Renders children only on the client, after hydration. Useful for components that depend on browser APIs:

```jsx
<ClientOnly fallback={<p>Loading map...</p>}>
  <InteractiveMap />
</ClientOnly>
```

### Props

| Prop | Type | Default | Description |
|------|------|---------|-------------|
| `children` | `ReactNode` | -- | Content to render on the client |
| `fallback` | `ReactNode` | `null` | Shown during SSR and before hydration |

This prevents hydration mismatches for components that render differently on server vs client.

## `<Form>`

Progressive-enhancement form component for calling server actions. See [Server Actions](../core-concepts/server-actions.md) for full details.

```jsx
<Form action="create_post" onSuccess={(data) => console.log(data)}>
  <input name="title" />
  <button type="submit">Create</button>
</Form>
```

## `useAction`

Hook for calling server actions programmatically. See [Server Actions](../core-concepts/server-actions.md) for full details.

```jsx
const deletePost = useAction('delete_post');
await deletePost({ id: 42 });
```

## `<Link>`

Client-side navigation link. Prevents full page reloads:

```jsx
import { Link } from 'pyxle/client';

<Link href="/about">About</Link>
```

## `navigate(path)`

Programmatic client-side navigation:

```jsx
import { navigate } from 'pyxle/client';

function handleClick() {
  navigate('/dashboard');
}
```

## `prefetch(path)`

Prefetch a page's data and assets before navigation:

```jsx
import { prefetch } from 'pyxle/client';

<a href="/dashboard" onMouseEnter={() => prefetch('/dashboard')}>
  Dashboard
</a>
```

## Next steps

- Protect your app: [Security](security.md)
- Deploy to production: [Deployment](deployment.md)
