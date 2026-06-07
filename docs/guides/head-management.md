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
- **Supports common head elements**: `<title>`, `<meta>`, `<link>`, `<script>`. (`<base>` is intentionally rejected — a stray `<base href>` can rewrite every relative URL on the page, so the head sanitizer strips it as an XSS/redirection vector.)
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

## The `HEAD` variable (lower-level alternative)

`.pyxl` files can also define a `HEAD` variable in the Python section. This was Pyxle's original head mechanism and still works; the parser extracts it at compile time, before React is involved.

```python
# Static string
HEAD = '<title>My Page</title><meta name="description" content="Page description" />'

# Or a list of strings
HEAD = [
    '<title>My Page</title>',
    '<meta name="description" content="Page description" />',
    '<link rel="canonical" href="https://example.com/page" />',
]
```

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

The callable receives the loader's return value as its argument and must return a string or list of strings.

### When to prefer the `HEAD` variable

There are a few narrow cases where the `HEAD` variable is a better fit than `<Head>`:

1. **Pages with no React component.** A pure API-like page that returns minimal HTML and wants a static `<title>` without any client-side JavaScript.
2. **Absolute hot paths** where the few microseconds of skipping React's head capture matter, and the content is fully static. In practice this matters for approximately no one.
3. **You deliberately want the head to be decoupled from the render tree** — for example, if a page's head is determined by something the component doesn't need to know about.

Unless one of these applies, reach for `<Head>`.

### XSS safety

Both `<Head>` children and `HEAD` strings are automatically sanitised:

- Angle brackets (`<`, `>`) inside `<title>` text are escaped
- Event handler attributes (`onclick`, `onerror`, etc.) are stripped
- `javascript:` and `vbscript:` URLs in `href`/`src` attributes are removed

This protects against XSS when interpolating user-provided data into head elements. You should still escape user input as a best practice.

## Layouts and precedence

When multiple sources define the same head element, Pyxle deduplicates them. Later sources override earlier ones.

### Precedence order (lowest to highest)

1. Layout `<Head>` blocks and layout `HEAD` variable
2. Page `HEAD` variable
3. Page `<Head>` blocks

Within the same tier, deeper nesting wins (a `<Head>` in a child component overrides a `<Head>` in a parent component).

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

If no `<title>` element is provided by any source, Pyxle inserts a default:

```html
<title>Pyxle</title>
```

## Next steps

- Add third-party scripts: [Client Components](client-components.md)
- Build JSON APIs: [API Routes](api-routes.md)
