# Build Optimization

`pyxle build` produces a hashed, minified, code-split client bundle and a
matching SSR shell. Most of the optimization is automatic; this guide covers
what you get for free, how to inspect it, and how to push image and load
performance further.

## What you get automatically

Pyxle builds the client with [Vite](https://vitejs.dev/) in production mode, so
out of the box:

- **Minification** — JavaScript and CSS are minified with esbuild.
- **Tree-shaking** — unused exports (including unused `pyxle/client` helpers)
  are dropped by Rollup.
- **Code splitting** — shared code lands in its own chunks, so two pages that
  import the same component download it once and cache it across navigations.
- **Content hashing** — every asset is fingerprinted (`index-DzNTdgZx.js`) for
  immutable, long-lived caching.
- **Per-page CSS** — only the CSS a page actually uses is linked from its shell.

## Preload hints

Pyxle's SSR shell injects a `<link rel="modulepreload">` for the page's entry
module **and every chunk it statically imports**, derived from the build
manifest's import graph. The browser then fetches those chunks in parallel with
HTML parsing instead of discovering them only after parsing the entry — a
meaningful first-load win on multi-chunk pages. It's automatic. (Browsers that
don't support `modulepreload` simply ignore the hints — the page still loads.)

Every hint carries `fetchpriority="low"`: the chunks exist to hydrate a page
the server has already painted, so they must not compete with the resources
that produce that paint (the document, its CSS, the LCP image). With idle
bandwidth — the common case once the shell has arrived — low-priority requests
still start immediately, so hydration timing is unchanged; on a contended
connection the paint-critical resources win, which is the order you want.

For content-first pages you can go further:
[`assets.modulePreload: false`](../reference/configuration.md#asset-delivery)
drops the hints entirely, and
[`assets.hydration: "after-paint"`](../reference/configuration.md#asset-delivery)
holds the entry `<script>` itself until the first frame has been presented.
Together they guarantee the server-rendered document paints with **zero
JavaScript in flight** — no hydration chunk is even discovered before first
paint. Interactivity arrives a beat later (one frame plus the network's
fetch time); nothing about the paint changes, because the document was
complete server HTML all along. Leave the defaults for app-like pages where
time-to-interactive is the product.

## Inline stylesheets: trading cacheability for first paint

By default a production page links its compiled CSS with ordinary
`<link rel="stylesheet">` tags. Links are cache-friendly — a returning visitor
already has the sheets — but they are **render-blocking**: on a first visit the
browser cannot paint until every linked sheet has made its own round trip, which
is routinely the largest chunk of First Contentful Paint on a fast server.

The `assets.inlineStylesheets` config knob embeds the same compiled CSS
directly into the HTML document instead:

```json
{
  "assets": {
    "inlineStylesheets": "auto",
    "inlineStylesheetLimit": 8192
  }
}
```

| Value | Behaviour |
|-------|-----------|
| `"never"` (default) | Always link. Best when most traffic is returning visitors with warm caches. |
| `"auto"` | Inline any sheet whose file is at most `inlineStylesheetLimit` bytes (default 8192); larger sheets keep their link. |
| `"always"` | Inline every sheet. First paint never waits on a stylesheet request — the right trade for landing/marketing pages where most visits are first visits. |

The styles are byte-identical either way — inlining changes how they arrive,
never what applies — and each inlined block carries a
`data-pyxle-css="<asset url>"` attribute naming the file it replaced, plus a
`disabled` link marker so the client runtime knows the sheet is already
present and never downloads it a second time on hydration. The cost is that
inlined CSS rides along in every HTML response instead of being cached once
per visitor (the HTML itself is gzipped, so the wire cost is the sheet's
gzipped size). Client-side navigation is unaffected: other pages' sheets still
load on demand, and a sheet that fails to read at render time (a truncated
deploy) degrades to its normal link rather than an unstyled page.

## Inspecting the bundle — `pyxle build --analyze`

To see what ships, add `--analyze`:

```bash
pyxle build --analyze
```

It prints every JS/CSS asset with its raw and gzipped size, largest first, plus
a total:

```
Bundle analysis (raw / gzip):
  assets/use-auth-BoNlCwWb.js          76.2KB /   25.2KB gzip
  assets/layout-B_QEnH4f.css           65.3KB /   12.7KB gzip
  assets/index-D27bdBTk.js             40.1KB /   10.8KB gzip
  ...
  ─ total                             675.1KB /  202.5KB gzip (35 file(s))
```

Paths are relative to Vite's bundle directory (`dist/client/dist/`), and only
what the browser downloads is counted — the build *inputs* beside it
(`vite.config.js`, `client-entry.js`, the per-page JSX and CSS sources Vite
consumed) are neither served nor measured.

Use it to catch a dependency that ballooned a chunk, or to confirm a refactor
shrank the bundle. (It's dependency-free — no extra tooling to install.)

## Image optimization

`<Image>` (from `pyxle/client`) is an optimized `<img>` on par with Next.js's
component for everything that doesn't require a server-side image optimizer:

```jsx
import { Image } from 'pyxle/client';

<Image src="/hero.jpg" alt="Hero" width={1200} height={630} priority />
```

Out of the box it:

- **Prevents layout shift** — `width`/`height` reserve the right space before
  the image loads (no CLS).
- **Lazy-loads** below-the-fold images (`loading="lazy"`), and **prioritizes**
  the LCP image when you pass `priority` (`fetchpriority="high"` + eager + sync
  decode).
- Supports a **blur-up placeholder** (`placeholder="blur"` + `blurDataURL`),
  an automatic **`fallbackSrc`**, and **`fill`** mode (cover a positioned
  parent).

### Responsive images with a loader

Actual resizing and format conversion (WebP/AVIF) need a backend — a CDN or a
build plugin. Provide a `loader` and `<Image>` emits a responsive `srcset`
across a device-size ladder; the browser downloads the size it needs:

```jsx
// Cloudinary-style loader.
function cloudinary({ src, width, quality }) {
  return `https://res.cloudinary.com/demo/image/fetch/w_${width},q_${quality || 'auto'},f_auto${src}`;
}

<Image src="/hero.jpg" alt="Hero" width={1200} height={630}
       sizes="(max-width: 768px) 100vw, 1200px"
       loader={cloudinary} quality={80} priority />
```

Most CDN loaders also do format negotiation (serving AVIF/WebP based on the
`Accept` header) and compression, so a single `loader` gives you resizing *and*
modern formats. imgix, Cloudflare Images, Vercel, and ImageKit all fit the same
`({ src, width, quality }) => url` shape.

> **Why no default resizing backend?** Without one, a `srcset` would just point
> at the original image at every width — the browser would download the full
> image regardless, which is slower, not faster. So Pyxle stays honest: no
> loader → a clean, CLS-safe `<img>`; a loader → real responsive images.

### Build-time optimization with a Vite plugin

If you'd rather optimize images at build time than at the edge, add a Vite image
plugin to your project and reference the optimized output. For example,
[`vite-plugin-image-optimizer`](https://github.com/FatehAK/vite-plugin-image-optimizer)
(compression) or [`vite-imagetools`](https://github.com/JonasKruckenberg/imagetools)
(on-the-fly resizing + `srcset` generation via import queries). These are
opt-in: install the npm package and add the plugin to your project's Vite
config — Pyxle doesn't bundle an image-processing dependency.

## See also

- [`<Image>` reference](../reference/client-api.md#image)
- [Deployment → CDN and edge caching](deployment.md#cdn-and-edge-caching)
- [Caching](caching.md)
