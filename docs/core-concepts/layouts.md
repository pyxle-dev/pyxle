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

Layouts can export a `slots` object and a `createSlots` function for passing additional content from pages:

```jsx
// pages/layout.pyxl
export const slots = {};
export const createSlots = () => slots;

export default function RootLayout({ children }) {
  return <div>{children}</div>;
}
```

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

The layout component receives its loader's result on the **`data`** prop -- exactly like a page receives its loader's data. Details:

- The loader runs **once per request**, before the page renders, and must be `async` (same rules as a page loader).
- In a nesting chain, every layout/template loader runs and their results are **merged** into one dict (on a key conflict, the outermost layout wins).
- A layout **without** a loader receives the wrapped page's `data` instead, so existing JSX-only layouts are unchanged.
- The merged layout data is also exposed to the **page** component as a `layoutData` prop, if a page ever needs to read what its layouts loaded.

## How it works

When Pyxle compiles your pages, it:

1. Walks up from each page to the root, collecting `layout.pyxl` and `template.pyxl` files
2. Generates a composed wrapper module that nests them in the correct order
3. At render time, the page component is passed as `children` to the innermost layout

## Next steps

- Style your layouts with Tailwind: [Styling](../guides/styling.md)
- Add navigation between pages: [Client Components](../guides/client-components.md)
