# Head Management

Pyxle offers two ways to control the document `<head>`: the `<Head>` component (**recommended**) and the `HEAD` Python variable. Both merge together with automatic deduplication, and either works — but we recommend `<Head>` for almost every real page.

## TL;DR — use the `<Head>` component

```jsx
import { Head } from 'pyxle/client';

export default function Page({ data }) {
  return (
    <>
      <Head>
        <title>{data.post.title} — My Blog</title>
        <meta name="description" content={data.post.excerpt} />
        <link rel="canonical" href={`https://example.com/posts/${data.post.slug}`} />
      </Head>
      <article>
        <h1>{data.post.title}</h1>
        {/* ... */}
      </article>
    </>
  );
}
```

That's the pattern you'll use for the vast majority of pages. It reads like React, interpolates props naturally, and lives next to the body markup that depends on the same data.

## Why `<Head>` is the recommended approach

- **It's just JSX.** If you know React, you already know how to use it. No new concepts, no callable-vs-string rules to memorise.
- **Dynamic content is effortless.** You can interpolate `{data.foo}`, map over arrays, use conditionals, extract to subcomponents — everything JSX lets you do with the body works in the head too.
- **Colocation.** Your `<title>` sits right next to the `<h1>` that uses the same data. Refactoring one updates the other in the same diff.
- **Works in nested components.** Any component in your render tree can contribute head elements. A `<BlogPostCard>` component can set its own `og:image`; an `<AdminOnly>` wrapper can add `<meta name="robots" content="noindex" />`.
- **Plays well with layouts.** Layouts can set defaults with their own `<Head>`, and pages override them automatically through Pyxle's deduplication rules.
- **Familiar to developers coming from other frameworks.** The `<Head>` API is intentionally similar to Next.js's `next/head`, Remix's `Meta` export, and React Helmet.

## The `<Head>` component

Import it from `pyxle/client` and drop it anywhere in your component tree. Its children become elements in the document `<head>`:

```jsx
import { Head } from 'pyxle/client';

export default function Page({ data }) {
  return (
    <>
      <Head>
        <title>{data.title}</title>
        <meta name="description" content={data.description} />
        <meta name="robots" content="noindex" />
        <link rel="canonical" href={data.canonicalUrl} />
      </Head>
      <h1>{data.title}</h1>
    </>
  );
}
```

The `<Head>` component:

- **Renders nothing in the DOM** (it returns `null`).
- During SSR, Pyxle extracts its children at compile time and registers them as head elements for the response.
- **Works in any component**, including nested ones. A reusable component can inject its own head metadata.
- **Supports head elements**: `<title>`, `<meta>`, `<link>`, `<script>`, `<style>`. Anything outside this allowlist is dropped by the head sanitiser — notably `<base>`, which is rejected because a stray `<base href>` can rewrite every relative URL on the page (an XSS/redirection vector).
- **Normalises multi-part `<title>` children** since 0.3.0. `<title>{name} — My Blog</title>` compiles to multiple children — `[name, " — My Blog"]` — which React warns about. `<Head>` joins string and number children into a single text node so the warning is silenced and the rendered HTML is unchanged. You don't need template literals or `{ \`${name} — My Blog\` }` workarounds.

### Multiple `<Head>` blocks in one tree

You can use `<Head>` multiple times — it's not a singleton. Elements from all `<Head>` blocks are collected and merged:

```jsx
export default function BlogPost({ data }) {
  return (
    <article>
      <Head>
        <title>{data.post.title}</title>
      </Head>
      <Header />
      <PostBody post={data.post} />
      {data.post.isPremium && (
        <Head>
          <meta name="robots" content="noindex" />
        </Head>
      )}
    </article>
  );
}
```

This lets you put head contributions close to the code that decides them, without having to lift that logic all the way up to the page root.

### Using `<Head>` in reusable components

Head contributions from any rendered component get merged into the final document:

```jsx
// components/SeoTags.jsx
import { Head } from 'pyxle/client';

export function SeoTags({ title, description, image }) {
  return (
    <Head>
      <title>{title}</title>
      <meta name="description" content={description} />
      <meta property="og:title" content={title} />
      <meta property="og:description" content={description} />
      <meta property="og:image" content={image} />
      <meta name="twitter:card" content="summary_large_image" />
    </Head>
  );
}

// pages/blog/[slug].pyxl
import { SeoTags } from '../../components/SeoTags.jsx';

export default function BlogPost({ data }) {
  return (
    <article>
      <SeoTags
        title={data.post.title}
        description={data.post.excerpt}
        image={data.post.coverImage}
      />
      <h1>{data.post.title}</h1>
      {/* ... */}
    </article>
  );
}
```

This is the idiomatic way to build SEO presets, third-party tracking tags, and theme toggles.

### Expressions are evaluated by the render

Pyxle reads your `<Head>` block twice, and it helps to know which read you're getting.

At **compile time** the block is extracted from your `.pyxl` source as text, before any of it has run. At **render time** the `<Head>` component executes and produces the same elements with their values in them. The rendered version always wins.

Any element that still holds a JSX expression when the compile-time copy is read is **dropped**, because its `{...}` is source text that nothing downstream evaluates — `href="{faviconUrl}"` would be requested by the browser as a relative URL, and `<title>{name}</title>` would put the braces in the tab. The rule covers the whole element, not just attributes:

```jsx
<Head>
  <title>{data.title}</title>                                    {/* child text */}
  <meta name="description" content={data.excerpt} />             {/* attribute */}
  <link rel="canonical" href={`${site}/posts/${data.slug}`} />   {/* attribute */}
  <meta {...seoProps} />                                         {/* spread */}
  {data.isPremium && <meta name="robots" content="noindex" />}   {/* the element itself */}
</Head>
```

None of those reach the document from the compile-time read; all of them reach it from the render, evaluated. **You do not need to do anything** — this is what the recommended `<Head>` pattern already does, and it is why interpolating loader data "just works".

Braces that are *content* are never touched. These are emitted exactly as written:

```jsx
<Head>
  <meta name="description" content="Braces {like these} are prose" />
  <script type="application/ld+json">{JSON.stringify(schema)}</script>
  <style>{`.hero { color: red }`}</style>
</Head>
```

The `<meta>` survives the compile-time read because its value is a quoted string. The JSON-LD and the `<style>` are expressions, so their compile-time copies are dropped — and their rendered output, real braces and all, is passed through untouched.

The one case to know about is **streaming**, where there is no render to fall back on: see [The head is static while streaming](streaming.md#the-head-is-static-while-streaming).

## The `HEAD` variable (lower-level alternative)

`.pyxl` files can also define a `HEAD` variable in the Python section. This was Pyxle's original head mechanism and still works. A literal `HEAD` is read straight from your source at compile time; anything Pyxle can't read that way — an f-string, a concatenation, a `json.dumps(...)` call, a callable — is evaluated on the server when the page is rendered. Either way React is never involved.

**Use a list, with one element per entry.** That is the idiom for everything except a single tag:

```python
HEAD = [
    '<title>My Page</title>',
    '<meta name="description" content="Page description" />',
    '<link rel="canonical" href="https://example.com/page" />',
]
```

A lone element may be a bare string:

```python
HEAD = '<title>My Page</title>'
```

**One entry is one element — two elements in one string is an error.** Each entry is parsed and rebuilt from its first element, and anything after it is discarded. That is the same pass that throws away markup injected after an attribute quote breakout (see [XSS safety](#xss-safety)), so it is a security boundary rather than a limit to work around:

```python
# Wrong — a build error. Only the <title> would ever reach the browser.
HEAD = '<title>My Page</title><meta name="description" content="D" />'

# Right — one element per entry.
HEAD = ['<title>My Page</title>', '<meta name="description" content="D" />']
```

Where Pyxle can read the entry from your source it refuses to build, naming the file, the line and the markup that would have been dropped. Where the entry is computed — an f-string, a callable — its value is only known while the page is rendering, so it is a logged warning instead: the tag is lost, but a second `<meta>` appearing for one row of data never takes the page down with it. The warning names the file and the dropped markup, and is logged once per distinct problem rather than once per request.

For head content that depends on loader data, use a callable:

```python
@server
async def load_post(request):
    post = await fetch_post(request.path_params["slug"])
    return {"post": post}

def HEAD(data):
    return [
        f'<title>{data["post"]["title"]} - My Blog</title>',
        f'<meta name="description" content="{data["post"]["excerpt"]}" />',
    ]
```

The callable receives the loader's return value as its argument and must return a string or list of strings, synchronously. If the page has no loader it receives an empty dict, so `data.get("key")` is the safe idiom.

A `HEAD` variable is finished HTML — Python that has already run — so a brace in it is always content, never an expression. That is true at every level: site-wide JSON-LD or a critical-CSS `<style>` in a root `layout.pyxl`'s `HEAD` reaches every page below it exactly as written, and unlike a `<Head>` block there is no second, rendered read of it to fall back on.

### `HEAD` in a layout

A `layout.pyxl` (or `template.pyxl`) supports every form a page does — string, list, computed value, or callable — and its elements are inherited by every page below it. This is where site-wide metadata belongs:

```python
# pages/layout.pyxl
import json

@server
async def load_shell(request):
    return {"site_name": "Acme", "site_url": "https://acme.example"}

def HEAD(data):
    schema = {
        "@context": "https://schema.org",
        "@type": "Organization",
        "name": data["site_name"],
        "url": data["site_url"],
    }
    return [
        '<script type="application/ld+json">' + json.dumps(schema) + "</script>",
        f'<link rel="canonical" href="{data["site_url"]}" />',
    ]
```

A layout's callable `HEAD` receives **that layout's own loader data** — the same contract a page's callable has, applied one level up. In a chain of layouts each one is handed its own loader's return value, not the merged data the layout components receive as props, so an outer layout can never change what an inner layout's head says. A layout with no `@server` loader gets an empty dict.

If a layout's `HEAD` can't be evaluated — a callable that raises, or one returning something that isn't a string — the request fails like any other server error and the log names the layout file. It is never dropped silently.

> **Version note.** Before 0.9.0 only a *literal* `HEAD` in a layout reached the document; computed values and callables were dropped without a warning. Page-level `HEAD` was unaffected. If you worked around this by moving site-wide tags into a `<Head>` block or duplicating them onto every page, you can move them back.

### When to prefer the `HEAD` variable

There are a few narrow cases where the `HEAD` variable is a better fit than `<Head>`:

1. **Pages with no React component.** A pure API-like page that returns minimal HTML and wants a static `<title>` without any client-side JavaScript.
2. **Absolute hot paths** where the few microseconds of skipping React's head capture matter, and the content is fully static. In practice this matters for approximately no one.
3. **You deliberately want the head to be decoupled from the render tree** — for example, if a page's head is determined by something the component doesn't need to know about.

Unless one of these applies, reach for `<Head>`.

### XSS safety

Both `<Head>` children and `HEAD` strings are parsed and rebuilt through a sanitising allowlist before they reach the document:

- Only `<title>`, `<meta>`, `<link>`, `<script>`, and `<style>` are permitted in the head — anything else (`<base>`, `<iframe>`, …) is dropped
- Every attribute value is HTML-escaped, so a quote inside interpolated data cannot break out of its attribute — building `<meta>`/`<link>`/`<title>` values from loader data (as in the `HEAD` callable above) is safe
- Angle brackets (`<`, `>`) inside `<title>` text are escaped, and any markup injected after a closing `</title>` is discarded
- Event handler attributes (`onclick`, `onerror`, etc.) are stripped
- `javascript:`, `vbscript:`, and `data:` URLs in `href`/`src`/`action` attributes are neutralised
- `<meta http-equiv="refresh">` is rejected

One deliberate exception: the text content of inline `<script>` and `<style>` elements is treated as trusted author code and preserved verbatim — never interpolate user-supplied data into inline script or style content. Note that `<meta>` and `<link>` elements are re-serialised self-closing (`<meta … />`), so the output markup may differ cosmetically from the input string.

This protects against XSS when interpolating user-provided data into head elements. You should still escape user input as a best practice.

## Layouts and precedence

When multiple sources define the same head element, Pyxle deduplicates them. Later sources override earlier ones.

### Precedence order (lowest to highest)

1. Layout `<Head>` blocks and layout `HEAD` variable
2. Page `HEAD` variable
3. Page `<Head>` blocks

Within the same tier, deeper nesting wins (a `<Head>` in a child component overrides a `<Head>` in a parent component). Inside tier 1, a layout's `<Head>` wins over that same layout's `HEAD` variable — the same way a page's `<Head>` outranks its `HEAD` variable.

The same merge applies to a page rendered through an [error boundary](error-handling.md#the-error-pages-head): an `error.pyxl` is a page, so it inherits its layouts' head and contributes its own.

### Example: layout defaults + page overrides

```jsx
// pages/layout.pyxl
import { Head } from 'pyxle/client';

export default function RootLayout({ children }) {
  return (
    <html lang="en">
      <Head>
        <title>My Site</title>
        <meta name="description" content="The default description for My Site." />
        <link rel="icon" href="/favicon.ico" />
      </Head>
      <body>{children}</body>
    </html>
  );
}
```

```jsx
// pages/about.pyxl
import { Head } from 'pyxle/client';

export default function About() {
  return (
    <>
      <Head>
        <title>About — My Site</title>
        <meta name="description" content="The story behind My Site." />
      </Head>
      <h1>About</h1>
    </>
  );
}
```

When the `/about` route renders, the layout's `<title>My Site</title>` and its description are both overridden by the page's values. The favicon `<link>` survives because the page doesn't define one.

### Deduplication rules

| Element | Deduplicated by |
|---------|----------------|
| `<title>` | Tag name (only one title allowed) |
| `<meta name="X">` | The `name` attribute |
| `<meta property="X">` | The `property` attribute |
| `<meta charset>` | Always one charset |
| `<link rel="canonical">` | Only one canonical |
| `<link rel="X" href="Y">` | `rel` + `href` combination |
| `<script src="X">` | The `src` attribute |
| Elements with `data-head-key="X"` | The key value |
| Everything else | Not deduplicated (all instances kept) |

### Manual deduplication keys

Use `data-head-key` to control deduplication for custom elements that don't have a natural identity attribute:

```jsx
<Head>
  <script src="/analytics.js" data-head-key="analytics"></script>
</Head>
```

If a layout and a page both define an element with the same `data-head-key`, the higher-priority source wins. This is useful for tag managers, feature-flag bootstrap scripts, or A/B testing snippets.

## Default title

A `<title>` is resolved in this order — the first one that exists wins:

1. The page's own `<Head>` (or its `HEAD` variable)
2. The nearest layout's `<Head>` / `HEAD`, walking up to the root layout
3. `name` in `pyxle.config.json`
4. The project directory name

Steps 1 and 2 are the ordinary head merge described above. Steps 3 and 4 are the
fallback for a page that declares no title anywhere: Pyxle names the tab after
*your app*, never after the framework.

```json
{
  "name": "Acme Dashboard"
}
```

`pyxle init` writes the `name` you scaffolded with, so a new project has one
already. Deleting the key is fine — the project directory name is used instead.

The fallback is a floor, not a substitute for real titles. A layout `<Head>` is
the right place for a site-wide default, because pages can then override it and
still fall back to something meaningful:

```jsx
// pages/layout.pyxl
<Head>
  <title>Acme Dashboard</title>
</Head>
```

## Next steps

- Add third-party scripts: [Client Components](client-components.md)
- Build JSON APIs: [API Routes](api-routes.md)
