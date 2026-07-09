# Styling

Pyxle delegates all CSS to **Vite**. Import a stylesheet from a JSX module and
Vite compiles, bundles, hot-reloads (in dev), and content-hashes it (in build)
— exactly like it does for JavaScript. You never hand-bump a `?v=N` query
string to bust a cache, and there's no separate CSS build step to run.

Three things work out of the box in **every** project, with or without Tailwind:

- **Plain CSS** — `import './styles/app.css'`
- **CSS Modules** — `import styles from './Badge.module.css'` (locally scoped, hashed class names)
- **Any npm CSS** — `import 'highlight.js/styles/github-dark.css'`

Tailwind CSS is **opt-in** — choose it at `pyxle init` (or add it later).

## The default: plain CSS + CSS Modules

A project scaffolded **without** Tailwind ships a plain-CSS baseline. The
starter imports a global stylesheet and demonstrates a CSS Module:

```jsx
// pages/index.pyxl (JSX section)
import './styles/app.css';          // global CSS — applies everywhere it's imported
import Badge from './components/Badge.jsx';
```

```jsx
// pages/components/Badge.jsx
import styles from './Badge.module.css';   // CSS Module — class names are scoped + hashed

export default function Badge({ children }) {
  return <span className={styles.badge}>{children}</span>;
}
```

CSS Module class names are **deterministic and identical on the server and the
client**, so the server-rendered HTML and the hydrated React tree always agree
— no hydration mismatch. In a production build each stylesheet becomes a
content-hashed asset (`Badge-C9bn1NFT.css`) that the SSR template links
automatically.

## Tailwind CSS (opt-in, v4, Vite-native)

Enable Tailwind when you scaffold:

```bash
pyxle init my-app --tailwind
# or answer "y" to "Use Tailwind CSS?" in the interactive prompt
```

That gives you Tailwind **v4**, wired directly into Vite via the
[`@tailwindcss/vite`](https://tailwindcss.com/docs/installation/using-vite)
plugin. There is **no `tailwind.config.js`, no `postcss.config.js`, and no
standalone `tailwindcss --watch` process** — Tailwind runs inside Vite's normal
CSS pipeline, so it hot-reloads in dev and is hashed in build like any other
stylesheet.

The entire Tailwind setup is a single CSS entry:

```css
/* pages/styles/app.css */
@import "tailwindcss";
```

imported once from a JSX module (the scaffold imports it from
`pages/index.pyxl`). Use utility classes anywhere:

```jsx
<main className="flex min-h-screen items-center justify-center bg-slate-50">
  <h1 className="text-2xl font-semibold tracking-tight">Hello</h1>
</main>
```

Tailwind v4 auto-detects your content — it scans the files Vite processes, so
there's no `content` glob to maintain. Customise your theme with `@theme` in the
same CSS file; see the [Tailwind v4 docs](https://tailwindcss.com/docs).

### Adding Tailwind to an existing project

If you scaffolded without Tailwind and want it later:

```bash
npm install -D tailwindcss @tailwindcss/vite
```

Pyxle detects `@tailwindcss/vite` in your `package.json` and adds the plugin to
the Vite config it generates. Then replace your CSS entry's contents with
`@import "tailwindcss";` (keep any custom rules below it) and restart `pyxle dev`.

## shadcn/ui

Enable it at scaffold time (`pyxle init --shadcn`, which implies Tailwind). See
the [Third-party packages guide](third-party-packages.md#shadcnui) for the full,
verified `npx shadcn@latest add` walkthrough.

## Using another CSS library

Any CSS or CSS-in-JS library that works with React 19 and SSR works with Pyxle.
Install it with npm and import it in your JSX section — Vite handles the rest.

```jsx
// A third-party stylesheet:
import 'highlight.js/styles/github-dark.css';

// A UI kit's styles:
import 'some-ui-kit/dist/style.css';
```

CSS-in-JS libraries (styled-components, Emotion, etc.) install the same way —
`npm install` and import in the JSX section. Pick libraries with SSR support so
server- and client-rendered markup match.

## Legacy Tailwind v3 projects

Projects that hand-wired Tailwind v3 — a `tailwind.config.*` at the root, a
`build:css` npm script, and a compiled stylesheet in `public/` — keep working:
`pyxle dev` starts the standalone Tailwind watcher when it detects that shape,
and `pyxle build` runs the declared `build:css` script. Two things to know:

- Files under `public/` are served live from disk and are never part of the
  rebuild watch, so the compiled CSS updating in place never triggers reloads —
  refresh the browser to pick it up.
- The Tailwind v3 CLI's `--watch` mode can skip its initial write when its
  output is piped (non-TTY). If styles look missing under a process manager,
  run the build script once manually — or better, migrate to the v4 setup
  above (`@tailwindcss/vite`), which has neither issue.

## Global stylesheets (config-driven, inlined)

For CSS that should be **inlined** on every page (embedded in the SSR HTML, no
separate request), register it in `pyxle.config.json`:

```json
{
  "styling": {
    "globalStyles": ["styles/reset.css", "styles/typography.css"]
  }
}
```

Paths are relative to the project root; styles are inlined as `<style>` tags in
order, so they apply before JavaScript loads. Use this for tiny critical CSS —
for anything substantial prefer the JSX-import path so Vite can hash and cache
it.

## Global scripts

Register JavaScript loaded on every page:

```json
{
  "styling": {
    "globalScripts": ["scripts/analytics.js"]
  }
}
```

## Next steps

- Manage document head elements: [Head Management](head-management.md)
- Add scripts with loading strategies: [Client Components](client-components.md)
- Install packages and set up shadcn/ui: [Third-party packages](third-party-packages.md)
