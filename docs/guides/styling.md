# Styling

Pyxle ships with Tailwind CSS configured in a fresh `pyxle init` project. The
recommended way to load your stylesheets is to **import them from a JSX module**
so Vite can compile, bundle, and content-hash them — exactly like it does for
JavaScript modules. This means you never have to hand-bump a `?v=N` query string
after a deploy to invalidate stale browser caches.

> **Prerequisite: a project-root `postcss.config.*` is required for Tailwind.**
> Vite compiles your CSS through PostCSS, and it only loads the Tailwind plugin
> when it discovers a `postcss.config.{cjs,js,mjs,ts}` at the project root. The
> scaffold ships this file, so a fresh project works immediately — but a
> **hand-built or upgraded project with no `postcss.config.*` silently fails**:
> the raw `@tailwind base/components/utilities` directives are inlined uncompiled
> and the page renders **unstyled, with no error**. If your Tailwind classes do
> nothing, check for this file first.

## Recommended: Vite-managed CSS (auto-hashed)

When your project has a `postcss.config.{cjs,js,mjs,ts}` file alongside the
Tailwind config, Pyxle delegates CSS processing to Vite + PostCSS. The
standalone Tailwind CLI watcher is automatically skipped (you'll see a clear
log line on dev server start), and any CSS file you import from a JSX module
flows through PostCSS, gets a content hash, and is listed in the Vite
manifest under your page entry.

The SSR template reads that manifest on every request and emits

```html
<link rel="stylesheet" href="/client/dist/assets/style-DEADBEEF.css" />
```

automatically. The hash changes only when the CSS source changes, so users
with old caches always see the latest styles after a deploy.

### Setup

A new Pyxle project has everything wired by default. If you're upgrading an
existing project, here's the full setup:

**1. Add `postcss.config.cjs` to your project root:**

```javascript
module.exports = {
  plugins: {
    tailwindcss: {},
    autoprefixer: {},
  },
};
```

**2. Make sure `tailwindcss`, `autoprefixer`, and `postcss` are in `devDependencies`:**

```bash
npm install --save-dev tailwindcss autoprefixer postcss
```

(The `postcss.config.cjs` above references `tailwindcss: {}`, so all three must be installed.)

**3. Import your stylesheet from a JSX module.** The cleanest place is the
root `pages/layout.pyxl` so every route picks it up via the layout wrapper:

```jsx
import './styles/tailwind.css';
import React from 'react';

export default function RootLayout({ children }) {
  return <div className="min-h-screen">{children}</div>;
}
```

**4. Delete any manual `<link rel="stylesheet" href="/styles/tailwind.css" />`
tags from your `<Head>` blocks.** Vite owns the link now.

**5. (Optional) remove the now-inert `dev:css` and `build:css` scripts from
`package.json`.** Only relevant when upgrading an older project that defined
them — the current scaffold's `package.json` ships just `dev`/`build`/`lint`,
so a fresh project has nothing to remove. When PostCSS is configured, Pyxle
skips the standalone watcher, so those scripts never run on their own anyway.

### Verifying the setup

Start the dev server:

```bash
pyxle dev
```

You should see a log line like:

```
ℹ️  Detected postcss.config.cjs — skipping standalone Tailwind watcher;
   CSS will be processed and hashed by Vite via PostCSS.
```

Open the page in your browser. Tailwind classes work as before. In dev mode
Vite serves the CSS through its own runtime (with HMR), so changes hot-reload
instantly. There's a brief flash of unstyled content on the very first paint
in dev — this is a Vite quirk and does **not** happen in production builds.

For production:

```bash
pyxle build
```

Inspect `dist/page-manifest.json` and you'll see each route's `client.css`
array pointing at content-hashed assets:

```json
{
  "/": {
    "client": {
      "file": "dist/assets/index-DEADBEEF.js",
      "css": ["dist/assets/index-CAFEBABE.css"]
    }
  }
}
```

Two consecutive builds with no source changes produce the **same** hash. A
build after editing `tailwind.css` (or any source it depends on) produces a
**different** hash. Cache-busting is automatic.

### Tailwind config

The Tailwind config still lives at `tailwind.config.cjs` (or `.js`, `.ts`,
`.mjs`). PostCSS calls it during compilation, so your `content` globs still
control which files Tailwind scans for class names:

```javascript
module.exports = {
  content: ['./pages/**/*.{pyxl,jsx,js,tsx,ts}'],
  darkMode: 'class',
  theme: {
    extend: {},
  },
  plugins: [
    require('@tailwindcss/forms'),
    require('@tailwindcss/typography'),
  ],
};
```

### Dark mode

The scaffold includes a theme toggle script that reads from `localStorage`
and respects `prefers-color-scheme`. Toggle the `dark` class on `<html>` to
switch themes:

```jsx
function toggleTheme() {
  document.documentElement.classList.toggle('dark');
}
```

Use Tailwind's `dark:` modifier for dark-mode styles:

```jsx
<div className="bg-white dark:bg-slate-900 text-black dark:text-white">
  Content
</div>
```

## Importing CSS from any JSX module

The same pattern works for any CSS file, not just Tailwind. Component-scoped
stylesheets, third-party CSS modules, or vanilla CSS files all work:

```jsx
import './hero.css';
import 'highlight.js/styles/github-dark.css';
import React from 'react';

export default function Hero() {
  return <section className="hero">…</section>;
}
```

Vite resolves the specifier relative to the importer, runs it through
PostCSS, hashes the output, and lists it under the page's manifest entry.
The SSR template emits a `<link>` for every CSS file the page transitively
imports.

## Global stylesheets (config-driven)

For CSS that should be **inlined** on every page (no separate request, no
hashing — embedded directly in the SSR HTML), register it in `pyxle.config.json`:

```json
{
  "styling": {
    "globalStyles": [
      "styles/reset.css",
      "styles/typography.css"
    ]
  }
}
```

Paths are relative to the project root. Global styles are loaded in order
and inlined as `<style>` tags, so they work even before JavaScript loads.
Use this for tiny critical CSS — for anything substantial, prefer the
JSX-import path so Vite can hash and cache it properly.

## Global scripts

Register JavaScript files loaded on every page:

```json
{
  "styling": {
    "globalScripts": [
      "scripts/analytics.js"
    ]
  }
}
```

## Legacy: standalone Tailwind CLI watcher

If you don't want Vite-managed CSS (e.g. you're integrating with an external
build pipeline), you can use the standalone Tailwind CLI watcher instead.
**Skip the `postcss.config.*` file** — its presence is what tells Pyxle to
defer to Vite — and add the watcher scripts to `package.json`:

> **The legacy watcher does not compile JSX-imported CSS.** It writes a separate
> compiled file (e.g. `public/styles/tailwind.css`) that you must link manually
> (below). A stylesheet you `import './styles/tailwind.css'` from a JSX module is
> **not** processed by this watcher — without a `postcss.config.*`, that import
> is inlined with its `@tailwind` directives uncompiled, so the page renders
> unstyled. Use this path only when you link the watcher's output by hand and do
> **not** rely on JSX CSS imports.

```json
{
  "scripts": {
    "dev:css": "tailwindcss -i ./pages/styles/tailwind.css -o ./public/styles/tailwind.css --watch",
    "build:css": "tailwindcss -i ./pages/styles/tailwind.css -o ./public/styles/tailwind.css --minify"
  }
}
```

`pyxle dev` will auto-start the watcher whenever it detects a Tailwind
config and no PostCSS config. You can disable it explicitly with:

```bash
pyxle dev --no-tailwind
```

Then link the compiled output manually:

```jsx
import { Head } from 'pyxle/client';

export default function Layout({ children }) {
  return (
    <>
      <Head>
        <link rel="stylesheet" href="/styles/tailwind.css" />
      </Head>
      {children}
    </>
  );
}
```

Trade-off: this path has **no automatic cache-busting**. If you deploy a CSS
change and a user has the old file cached, they'll see stale styles until
their cache expires. Workarounds (`?v=N` query strings, hand-rolled
fingerprints) are inevitable. The Vite-managed path avoids all of this.

### Dev server rebuilds in a loop

The standalone Tailwind watcher writes its compiled CSS into `public/`. In
Pyxle **0.7.0 and later** `pyxle dev` never rebuilds on `public/` changes at
all — static assets are served live from disk and picked up on refresh (like
Next.js) — so this happens automatically and you don't need to do anything.
Your edits to the Tailwind *input* (`pages/styles/tailwind.css`) still
hot-reload normally through the source-watch path.

On **earlier versions** `public/` was on the rebuild watch, so the watcher
reacted to Tailwind's own output write as if it were an edit, rebuilt, and the
rebuild let Tailwind re-emit the CSS — a self-sustaining loop that rebuilds
roughly once a second with no edits (very visible on Linux, where inotify
reports every write; subtler on macOS). If you see that on an older version,
either upgrade, point the watcher's `-o` output outside `public/` and link it
from there, or run `pyxle dev --no-tailwind` and drive `tailwindcss --watch`
yourself.

## CSS-in-JS

Any CSS-in-JS library that works with React 18 and SSR should work with
Pyxle. Install it via npm and import it in your JSX section.

## Next steps

- Manage document head elements: [Head Management](head-management.md)
- Add scripts with loading strategies: [Client Components](client-components.md)
