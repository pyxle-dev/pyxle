# Layouts

Layouts wrap pages in shared UI -- navigation bars, sidebars, footers, and other elements that persist across page navigations.

## Creating a layout

Add a `layout.pyxl` file to any directory inside `pages/`. It wraps all pages in that directory and its subdirectories:

```
pages/
  layout.pyxl         # Root layout -- wraps ALL pages
  index.pyxl
  about.pyxl
  dashboard/
    layout.pyxl       # Dashboard layout -- wraps only dashboard pages
    index.pyxl
    settings.pyxl
```

A layout is a React component that receives `children`:

```jsx
// pages/layout.pyxl
export default function RootLayout({ children }) {
  return (
    <div className="min-h-screen">
      <nav>
        <a href="/">Home</a>
        <a href="/about">About</a>
      </nav>
      <main>{children}</main>
      <footer>Built with Pyxle</footer>
    </div>
  );
}
```

### Slots

Slots let a page (or a nested layout) inject content into a named placeholder a
layout renders. There are two halves:

1. A **layout renders a `<Slot>`** — the placeholder.
2. A **page fills it** by exporting `slots`.

**The layout renders the placeholder.** Import `Slot` from `pyxle/client`, give
it a `name`, and optionally a `fallback`:

```jsx
// pages/layout.pyxl
import { Slot } from 'pyxle/client';

export default function RootLayout({ children }) {
  return (
    <div>
      <header>
        <strong>MyApp</strong>
        {/* Pages fill this; falls back to null when no page provides it. */}
        <Slot name="actions" fallback={<a href="/">Home</a>} />
      </header>
      <main>{children}</main>
    </div>
  );
}
```

**The page fills the slot.** Export a `slots` object whose values are
**factory functions** that return JSX (`() => <jsx/>`):

```jsx
// pages/incidents/[id].pyxl
import { Link } from 'pyxle/client';

export const slots = {
  actions: () => <Link href="/incidents">← All incidents</Link>,
};

export default function IncidentPage({ data }) {
  return <article>{/* ... */}</article>;
}
```

Now the `actions` slot in the layout renders the page's `<Link>` instead of the
fallback, and reverts to the fallback on pages that don't export it.

**`<Slot>` props:**

| Prop | Type | Description |
|------|------|-------------|
| `name` | `string` (required) | The slot name to render. |
| `fallback` | `ReactNode \| (props) => ReactNode` | Rendered when no filler exists. Defaults to `null`. |
| `props` | `object` | Forwarded to each filler factory (and to a function `fallback`). |

**Rules to know:**

- Each `slots` value must be a **function** returning JSX. A non-function value
  is silently ignored.
- `createSlots` is **optional**. A static `export const slots = { ... }` is
  enough. Use `export const createSlots = (props) => ({ ... })` only when a
  slot's content depends on the component's props/loader data — it receives the
  same props as the component (`{ data, layoutData, ... }`) and returns the
  slots map.
- Multiple fillers of the **same** slot name are **additive** — if a page and a
  nested layout both fill `actions`, all of them render (not last-wins).

> **Advanced:** `useSlot(name)` and `useSlots()` (both from `pyxle/client`) let
> a layout react to whether a slot is filled — e.g. `useSlot('actions')` returns
> the array of filler factories (or `null`), so you can hide a toolbar wrapper
> when nothing fills it.

## Nesting layouts

Layouts nest automatically. If both `pages/layout.pyxl` and `pages/dashboard/layout.pyxl` exist, a page at `pages/dashboard/settings.pyxl` is wrapped by both:

```
RootLayout
  DashboardLayout
    SettingsPage
```

Inner layouts are rendered inside outer layouts. The root layout is always the outermost wrapper.

## Templates

A `template.pyxl` file works like a layout but **resets component state on every navigation**. Use templates when you want a fresh React component tree for each page in the group:

```
pages/
  layout.pyxl           # Persists across navigation
  auth/
    template.pyxl       # Resets state on navigation between auth pages
    login.pyxl
    register.pyxl
```

Templates are useful for authentication flows, wizards, or any section where you want form state and scroll position to reset when moving between pages.

## Layout vs template

| Behaviour | `layout.pyxl` | `template.pyxl` |
|-----------|-------------|----------------|
| Wraps child pages | Yes | Yes |
| Preserves state on navigation | Yes | No (remounts) |
| Can nest | Yes | Yes |
| Typical use | Nav bars, sidebars | Auth flows, wizards |

## Layout data loaders

A layout (or template) can declare its own `@server` loader, just like a page. This is the clean way to load data the layout itself needs on **every** page it wraps -- a nav bar, a signed-in user/session banner, the current theme, the framework version, etc. -- without repeating it in every page's loader.

```python
# pages/layout.pyxl
from pyxle import __version__

@server
async def load(request):
    return {"version": __version__, "year": 2026}
```

```jsx
export default function RootLayout({ children, data }) {
    return (
        <>
            <nav>Pyxle v{data.version}</nav>
            {children}
            <footer>© {data.year}</footer>
        </>
    );
}
```

The layout component receives its loader's result on the **`data`** prop -- exactly like a page receives its loader's data. (The same data is also available on `layoutData`, the alias-free name the page component reads; either prop works inside a layout.) Details:

- The loader runs **once per request**, before the page renders, and must be `async` (same rules as a page loader).
- In a nesting chain, every layout/template loader runs and their results are **merged** into one dict (on a key conflict, the outermost layout wins).
- A layout **without** a loader receives the wrapped page's `data` instead, so existing JSX-only layouts are unchanged.
- The merged layout data is also exposed to the **page** component as a `layoutData` prop, if a page ever needs to read what its layouts loaded.

## Sections that are not part of the app: `STANDALONE`

Layouts **chain** — every one from the page up to the root wraps it. Sometimes
that is exactly wrong. A public status page inside an admin console, a print
view, an embedded widget: these are not the app, and they should not carry its
navigation, its stylesheet, its analytics tag, or the cost of its layout loader.

A layout **or a template** can declare itself the root of its own chain:

```python
@server
async def load(request):
    return {}


STANDALONE = True


import React from 'react';

export default function PublicLayout({ children }) {
    return <>{children}</>;
}
```

Pages beneath it get **this** layout and nothing above it. Specifically:

- ancestor layouts and templates do not wrap them,
- ancestor layout **loaders do not run** for them,
- ancestor head contributions — `<Head>` blocks *and* `HEAD` variables — do not
  land on them.

All three, because any one on its own is a trap. Stopping the wrapper but not
the loader means an outer layout's query running on every request to a section
that never renders it. Stopping the loader but not the head means a page that
opted out of the app shell still inheriting its analytics snippet.

The directive works the same way on a [`template.pyxl`](#templates) as on a
`layout.pyxl` — a template is a wrapper that remounts, and it stops the chain
above it exactly as a layout does.

### Why not just branch in the outer layout

You can write `if (isPublicSection) return <>{children}</>` in your root layout,
and its loader can return early on a path check. That works, and it puts
knowledge of every public section into the parent — a conditional that grows a
branch each time one is added, and a loader that has to know which paths it is
not for. `STANDALONE` lets the section declare its own independence instead.

`STANDALONE` must be `True` or `False`. Anything else is a compile error rather
than a guess, because `STANDALONE = 0` silently meaning "yes" would make a
section lose the app shell for no visible reason.

## How it works

When Pyxle compiles your pages, it:

1. Walks up from each page to the root, collecting `layout.pyxl` and `template.pyxl` files — stopping at any layout **or template** that declares `STANDALONE = True`
2. Generates a composed wrapper module that nests them in the correct order
3. At render time, the page component is passed as `children` to the innermost layout

## Next steps

- Style your layouts (plain CSS, CSS Modules, or opt-in Tailwind): [Styling](../guides/styling.md)
- Add navigation between pages: [Client Components](../guides/client-components.md)
