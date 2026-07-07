# AI accessibility — your app in Markdown

Every page of a Pyxle app can serve a clean **Markdown** rendition of itself — so AI assistants and coding agents (Claude, ChatGPT, Cursor, Copilot, Perplexity) read your app as text instead of scraping HTML. Turn it on with one flag; decide where the Markdown comes from with a few conventions in your project.

```json
{ "llms": true }
```

That single line gives you:

- **Per-page Markdown** at each URL with `.md` appended — `/docs/routing` → `/docs/routing.md`.
- **Content negotiation** — the *same* URL returns Markdown to any request whose `Accept` header asks for `text/markdown`, negotiated with proper [RFC 9110](https://www.rfc-editor.org/rfc/rfc9110#section-12.5.1) q-values. Browsers never send it, so humans are unaffected.
- **An `/llms.txt` index** — the [llms.txt](https://llmstxt.org/) convention: a Markdown map of your site.
- **Discovery headers** on every response — `Link: </llms.txt>; rel="llms-txt"` and `X-Llms-Txt: /llms.txt` — so an agent finds the index without parsing HTML.

This very page proves it: append `.md` to its URL to read the Markdown you're looking at.

---

## The mental model

There are two moving parts, and they're cleanly separated:

1. **Routing is the framework's job.** When `llms` is enabled, Pyxle registers a `.md` route for every page, wires up `Accept` negotiation, serves `/llms.txt`, and adds the discovery headers. You don't configure any of that.
2. **Content is your job — expressed as files, not config.** *Where* a page's Markdown comes from is resolved from your project on each request. The `llms` config block is just an on/off switch (plus one opt-in fallback); everything substantive lives in `.md` files and `llms.py` handlers next to your routes.

The feature is **off by default** and adds nothing to the normal page render path — the `.md` routes are separate and only run for `.md` (or `Accept: text/markdown`) requests.

---

## Enabling it

In `pyxle.config.json`:

```json
{
  "llms": {
    "enabled": true,
    "autoConvert": false
  }
}
```

| Key | Default | What it does |
|-----|---------|--------------|
| `enabled` | `false` | Turns the whole feature on. `"llms": true` is shorthand for `{ "enabled": true }`. |
| `autoConvert` | `false` | A last-resort fallback: convert a page's rendered HTML to Markdown when no authored source exists. Off by default because it's lossy — see [autoConvert](#autoconvert-the-lossy-fallback). |

That is the entire configuration surface. See [Configuration → AI accessibility](../reference/configuration.md#ai-accessibility).

---

## How a page's Markdown is resolved

For any `<page>.md` request (or an `Accept: text/markdown` request to the page), Pyxle walks this ladder and uses the **first source that returns text**:

| # | Source | Scope | Best for |
|---|--------|-------|----------|
| 1 | Co-located `<page>.md` file | one page | static, hand-written pages |
| 2 | `to_markdown` in the page's own module | one page (a catch-all covers its subtree) | pages that already load their content |
| 3 | `to_markdown` in the nearest ancestor `llms.py` | a route subtree (`pages/llms.py` = app-wide) | one handler for many pages |
| 4 | `autoConvert` (only if enabled) | any page | a rough fallback when nothing else exists |
| 5 | **Redirect** `/<page>.md` → `/<page>` | — | so a guessed `.md` URL never 404s |

Whatever a rung returns is then passed through the optional [`wrap_markdown`](#framing-every-page-wrap_markdown) hook before it's sent.

### 1. A co-located `.md` file

Drop a Markdown file next to the page. Simplest possible option — no code:

```
pages/
  about.pyxl
  about.md        ← served at /about.md
  index.pyxl
  index.md        ← served at /index.md (i.e. the / page)
```

Best for static, content-heavy pages you'd rather write by hand than generate — a landing page, a manifesto, a pricing page.

### 2. A page-local `to_markdown` handler

Add a `to_markdown` function to a page's Python (server) section. It receives a [`MarkdownContext`](#the-markdowncontext-ctx) and returns a Markdown string — or `None` to defer to the next rung:

```python
# pages/products/[id].pyxl

@server
async def load(request):
    return {"product": await get_product(request.path_params["id"])}

async def to_markdown(ctx):
    product = await get_product(ctx.request.path_params["id"])
    return f"# {product.name}\n\n{product.description}\n"
```

Because a **catch-all page** (`[[...slug]].pyxl`) is a single page that handles every sub-path, its `to_markdown` already covers the whole subtree — read `ctx.request.path_params["slug"]` to know which page was asked for.

### 3. A directory `llms.py` handler (covers a route subtree)

To serve many pages under a directory with **one** handler, put a `to_markdown` in an `llms.py` at that directory. Resolution walks **from the page's own directory up to `pages/`, nearest ancestor first** — exactly like `layout.pyxl`, `error.pyxl`, and `loading.pyxl`:

```
pages/
  llms.py              ← app-wide: to_markdown for any page below
  docs/
    llms.py            ← handles everything under /docs
    intro.pyxl
    routing.pyxl
```

```python
# pages/docs/llms.py — one handler for the whole /docs subtree
import json
from pathlib import Path

DOCS = Path("public/docs-data")

def to_markdown(ctx):
    slug = ctx.path.removeprefix("/docs/")          # e.g. "guides/routing"
    page = DOCS / f"{slug}.json"
    if not page.is_file():
        return None                                 # decline → try a broader handler
    return json.loads(page.read_text())["markdown"]
```

Returning `None` **declines** and defers to the next ancestor (and ultimately to `autoConvert`/redirect). So a `/docs` handler can answer the slugs it knows and let everything else fall through — handlers compose down the tree. `llms.py` is also the intended home for any future per-directory AI hooks (it already hosts [`llms_txt`](#the-llmstxt-index) and [`wrap_markdown`](#framing-every-page-wrap_markdown) at the root).

### autoConvert (the lossy fallback)

If nothing above resolves and you've set `"autoConvert": true`, Pyxle renders the page and converts its HTML to Markdown with a small, **dependency-free** converter. It's **off by default** and deliberately best-effort: headings, paragraphs, lists, links, emphasis, and code survive; layout chrome, tables, and rich components may not. Treat it as "something is better than a redirect" — prefer an authored `.md` or a handler for anything you care about.

Converted pages keep agents **on the Markdown channel**: internal links to other pages are rewritten to their `.md` renditions — `[About](/about)` becomes `[About](/about.md)`, with query strings and fragments preserved (`/about?x=1#y` → `/about.md?x=1#y`). External URLs, `mailto:`/`tel:` links, `/api/` routes, asset paths with a file extension, and links already ending in `.md` are left untouched. See [`html_to_markdown`](#ctxrender_html--post-processing-the-rendered-page) for the same behavior in your own handlers.

### The redirect fallback

With the feature on but no Markdown source and `autoConvert` off, `/<page>.md` returns a **`307` redirect** to `/<page>`. An agent that guesses a `.md` URL lands on the real page instead of a 404.

---

## The `MarkdownContext` (`ctx`)

Every `to_markdown` and `wrap_markdown` handler receives a single argument — a `MarkdownContext`. It's a small, read-only object with everything you need to produce a page's Markdown:

| Member | Type | Description |
|--------|------|-------------|
| `ctx.request` | `starlette.requests.Request` | The incoming request. Use it for route params, query string, headers, and the body. |
| `ctx.path` | `str` | The **canonical page path**, always without `.md` — e.g. `/docs/routing`, or `/` for the home page. Use this, not `request.url.path`. |
| `await ctx.run_loader()` | `Any` | Runs *only* the page's `@server` loader and returns its data — the dict the page would receive — skipping the render. The cheap path when you just want the loaded data. Returns `{}` for a page with no loader. |
| `await ctx.render_html()` | `str` | Renders the *original* page — running its `@server` loader and full SSR — and returns the **body HTML** (the component output, without the document shell). Lazy: nothing renders unless you call it. |

### `ctx.request` — the request object

A standard Starlette [`Request`](https://www.starlette.io/requests/). The members you'll actually reach for:

- **`ctx.request.path_params`** — the matched route parameters. For `pages/docs/[[...slug]].pyxl`, `ctx.request.path_params["slug"]` is `"guides/routing"`. This is how one handler serves many pages.
- **`ctx.request.query_params`** — the query string (`?q=…`), a multidict.
- **`ctx.request.headers`** — request headers.
- **`await ctx.request.body()`** / **`await ctx.request.json()`** — the request body, if any.

### `ctx.path` vs `ctx.request.url.path` — an important distinction

Use **`ctx.path`**. It is always the canonical page path with no `.md` suffix, whether the request arrived as `/docs/routing.md` or as `/docs/routing` with `Accept: text/markdown`. In contrast, `ctx.request.url.path` carries the *raw* request path — which **includes** `.md` on a `.md` request and **omits** it on an `Accept`-negotiated one. Reading `ctx.path` means your handler behaves identically on both entry points.

```python
def to_markdown(ctx):
    # ctx.path        -> "/docs/routing"      (always canonical)
    # ctx.request.url.path -> "/docs/routing.md" OR "/docs/routing"
    slug = ctx.path.removeprefix("/docs/")
    ...
```

### `ctx.render_html()` — post-processing the rendered page

When you want to *derive* Markdown from what the page actually renders — rather than from source data — call `await ctx.render_html()`. It runs the page's loader and server render and hands you the body HTML, which you can transform:

```python
from pyxle.devserver.llms import html_to_markdown   # the built-in converter

async def to_markdown(ctx):
    html = await ctx.render_html()
    return html_to_markdown(html)          # what autoConvert does, but on your terms
```

`html_to_markdown` rewrites internal page links to their `.md` renditions by default (exactly like [autoConvert](#autoconvert-the-lossy-fallback)); pass `rewrite_links=False` to keep every `href` verbatim.

It's lazy and potentially expensive (a full SSR pass), so only call it when you need it. When you want the page's *data* rather than its rendered HTML, reach for `ctx.run_loader()` instead — it's much cheaper.

### `ctx.run_loader()` — the loader's data, without the render

Often you don't want rendered HTML at all — you want the same data the page loads, to format as Markdown yourself. `await ctx.run_loader()` runs just the page's `@server` loader and returns its result (the dict the page would receive as `data`), skipping SSR entirely:

```python
async def to_markdown(ctx):
    data = await ctx.run_loader()          # runs the @server loader, no render
    post = data["post"]
    return f"# {post['title']}\n\n{post['body']}\n"
```

This is the cheap path — a loader call, not a full render — and it reuses the exact data-loading your page already does. A page with no loader returns `{}`.

### Handler contract

- **Sync or async** — both work. An async handler is awaited.
- **Return `str`** to serve that Markdown.
- **Return `None`** to decline and fall through to the next rung.
- Returning anything else raises a `TypeError` (surfaced in logs); the `.md` request degrades gracefully to a redirect, and an `Accept`-negotiated request falls back to HTML.

---

## Framing every page (`wrap_markdown`)

To add a consistent **header/footer to every `.md` response** — agent instructions, navigation hints, a canonical-URL banner — define a `wrap_markdown(ctx, markdown)` function in the **root** `pages/llms.py`. Pyxle calls it with the `MarkdownContext` and the already-resolved Markdown, and serves whatever string it returns:

```python
# pages/llms.py
BASE = "https://example.com"

def wrap_markdown(ctx, markdown):
    header = (
        f"> Markdown rendition of {BASE}{ctx.path}, served for AI agents.\n"
        f"> Append `.md` to any URL for its Markdown. Index: {BASE}/llms.txt\n"
    )
    return f"{header}\n{markdown}"
```

Because it runs on Markdown from *every* source (co-located files, `to_markdown` handlers, `autoConvert`), the framing is defined **once** and applied everywhere — and it always receives the *final* resolved Markdown, so `autoConvert` output arrives with its internal links already rewritten to `.md`. Return `None` to leave the Markdown untouched. And because it's applied at serve time — not baked into your source — your `/llms-full.txt` corpus and the raw `.md` files stay clean.

---

## The `/llms.txt` index

[`/llms.txt`](https://llmstxt.org/) is a Markdown map of your site that agents (and humans) can read to discover what's available. Pyxle resolves it, first hit wins:

1. a **static `public/llms.txt`** (served by the static-asset layer before anything else) — full manual control;
2. a **`llms_txt`** function in the root `pages/llms.py` — generate it dynamically;
3. a **generated** default: an `H1` plus a `## Pages` list of every concrete (non-parameterised) page.

The generated default is precise about what it advertises:

- **Links are absolute**, derived from the request's scheme and host (`https://example.com/about.md`) — so the index still points home when it's saved to disk or pasted into a chat. Behind a reverse proxy, run uvicorn with proxy-header support (`pyxle serve` does) so the advertised origin is the public one.
- **A page is linked at its `.md` URL only when that URL actually serves Markdown.** Each page is checked against the same [resolution ladder](#how-a-pages-markdown-is-resolved) `.md` requests use: a co-located `.md` file, a page-local `to_markdown`, or an ancestor `llms.py` handler qualifies it — and with `autoConvert` on, every page qualifies. A page with no source is listed at its **canonical HTML URL** instead, so the index never advertises a `.md` link that would just redirect.

For a site with dynamic content — docs, a blog, a catalog — the generated default can't enumerate your dynamic routes, so provide a `llms_txt` hook:

```python
# pages/llms.py
def llms_txt(ctx):
    lines = ["# My App", "", "> One-line summary of the app.", "", "## Docs", ""]
    for slug, title in load_doc_index():
        lines.append(f"- [{title}](https://example.com/docs/{slug}.md): short description")
    return "\n".join(lines) + "\n"
```

The hook receives an **`LlmsTxtContext`**:

| Member | Type | Description |
|--------|------|-------------|
| `ctx.request` | `Request` | The incoming request. |
| `ctx.pages` | `tuple[LlmsPageInfo, ...]` | Your app's concrete pages (see below). |
| `ctx.render_default()` | `str` | The framework's generated index — return it verbatim, extend it, or ignore it. |

Each entry in `ctx.pages` is an **`LlmsPageInfo`** — `path` (e.g. `/about`), `md_url` (`/about.md`), and `title` (a humanized label). Return a string, or `None` to fall back to the generated default.

> **`llms.txt` vs `llms-full.txt`.** `/llms.txt` is a *map* (links + descriptions) an agent reads to decide what to fetch. A `/llms-full.txt` is the *whole corpus* concatenated into one file for one-shot ingestion. Pyxle generates `/llms.txt`; if you want `/llms-full.txt`, produce it in your build (Pyxle serves it as a static file). Both are complementary — the index for discovery, the full file for bulk reading, the per-page `.md` for precise pulls.

---

## Content negotiation and discovery headers

Beyond the `.md` URLs, two things make the feature work for agents that don't append `.md`:

- **`Accept: text/markdown` negotiation.** A request to the *canonical* URL (`/docs/routing`) whose `Accept` header prefers `text/markdown` gets the Markdown, resolved through the exact same ladder. Browsers never ask for it, so this is invisible to human visitors. The response carries `Vary: Accept` so shared caches key on it correctly.
- **Discovery headers.** Every response advertises the index: `Link: </llms.txt>; rel="llms-txt"` and `X-Llms-Txt: /llms.txt`. An agent can find your `llms.txt` from any page without parsing the body.

### Negotiation rules

The `Accept` header is negotiated per [RFC 9110 §12.5.1](https://www.rfc-editor.org/rfc/rfc9110#section-12.5.1). Markdown is served when **both** hold:

1. `text/markdown` appears as an **exact media type** with `q > 0`. Matching is by type/subtype token, case-insensitive — never by substring — and wildcards (`*/*`, `text/*`) never select Markdown, so ordinary browser headers keep getting HTML.
2. Its q-weight is **at least** the effective weight of `text/html` (which *does* pick up wildcard ranges). Ties go to Markdown — the client asked for it explicitly.

In practice:

| `Accept` header | Serves | Why |
|---|---|---|
| `text/markdown` | Markdown | explicit, q defaults to 1.0 |
| `text/markdown, text/html` | Markdown | equal weight — explicit ask wins the tie |
| `text/html;q=0.9, text/markdown;q=0.8` | HTML | HTML weighted higher |
| `text/html, text/markdown;q=0` | HTML | `q=0` means *not acceptable* |
| `text/markdown;q=0.5, */*;q=0.9` | HTML | HTML is 0.9 via the wildcard |
| `text/html,application/xhtml+xml,…,*/*;q=0.8` (a browser) | HTML | no explicit `text/markdown` |
| `text/markdownish` or malformed garbage | HTML | no substring matching; parsing never raises |

q-values default to `1.0` and are clamped to `0..1`; malformed media-ranges are ignored and an unusable header falls back to HTML.

---

## Deployment

- A page's **own `to_markdown`** is compiled into your build and works anywhere `pyxle serve` runs.
- **Co-located `.md` files and `llms.py` handlers are source files.** They must be present alongside `pages/` at runtime — which they are for the common case of deploying your whole project directory. If they're ever absent, resolution simply falls through to the next rung (and ultimately the redirect), so nothing breaks; the page just isn't available as Markdown.
- **Caching.** `.md` responses aren't run through the page edge-cache. If you serve heavy handlers under load, cache at your CDN/reverse proxy keyed on the path (and `Vary: Accept` for the negotiated route).

---

## robots.txt and AI crawlers

The `.md`/`llms.txt` endpoints *offer* clean content; they don't gate crawling. If you want reach, **don't block the AI bots** in `public/robots.txt` — being read and cited by them is free distribution:

```
User-agent: GPTBot
User-agent: OAI-SearchBot
User-agent: ChatGPT-User
User-agent: ClaudeBot
User-agent: Claude-SearchBot
User-agent: PerplexityBot
User-agent: CCBot
Allow: /

Sitemap: https://example.com/sitemap.xml
```

Ship a normal XML sitemap alongside it. (OpenAI and Anthropic expose separate tokens for *training* vs *search* if you want to allow one and not the other — see their bot docs.)

---

## Recipes

**Serve docs from generated JSON** (as pyxle.dev does): one directory handler maps a slug to a pre-built Markdown field. See [directory handler](#3-a-directory-llmspy-handler-covers-a-route-subtree) above.

**Rewrite links for portability.** Internal links already point at `.md` renditions — [autoConvert](#autoconvert-the-lossy-fallback) and `html_to_markdown` rewrite them for you. But Markdown that travels (pasted into a chat, saved to disk) should also carry **absolute** links. Make them absolute when you generate or store the Markdown, so `[Routing](/docs/routing.md)` becomes `[Routing](https://example.com/docs/routing.md)` — the generated `/llms.txt` already does this.

**Add an agent search endpoint.** Agents can search by fetching `/llms-full.txt` and scanning it, but a dedicated endpoint is nicer. A plain `pages/api/*.py` route that ranks your content and returns Markdown links works well — then point to it from `wrap_markdown`:

```python
# pages/api/search.py  →  GET /api/search?q=...
from starlette.responses import PlainTextResponse

async def endpoint(request):
    q = request.query_params.get("q", "")
    hits = search_your_index(q)            # your ranking
    lines = [f"# Results for “{q}”", ""]
    lines += [f"- [{h.title}]({h.md_url})" for h in hits]
    return PlainTextResponse("\n".join(lines), media_type="text/markdown; charset=utf-8")
```

**Per-section handlers.** Give `pages/docs/llms.py` and `pages/blog/llms.py` different `to_markdown` handlers; each scopes to its subtree, and the root `pages/llms.py` catches anything else.

---

## FAQ

**Does this slow down my pages?** No. The `.md` routes are separate and only run for `.md`/`Accept: text/markdown` requests. The normal render path is untouched, and the whole feature is off unless you enable it.

**Do I have to write Markdown for every page?** No. Enable the feature and pages with no source simply redirect their `.md` URL to the page. Add `.md` files or handlers only where clean Markdown is worth it (usually docs and content pages).

**What about my interactive pages?** A dashboard or playground isn't meaningful as Markdown — either give it a short hand-written `.md` describing what it is, or let it redirect. `autoConvert` exists for a rough automatic version if you want one.

**Is this the same as Mintlify's `.md`/llms.txt?** Same idea, but built into the framework and applied to your *whole app*, not just a hosted docs site — and you control exactly where each page's Markdown comes from.

**Who reads all this?** Right now, the clearest win is direct: point any AI assistant — Claude, Cursor, ChatGPT — at a `.md` URL or your `llms.txt` and it gets clean, token-efficient context instead of scraped HTML. Beyond that, `.md` and [llms.txt](https://llmstxt.org/) are the conventions the AI ecosystem is standardizing on — so your app already speaks the format machine readers are moving toward, served to spec (per-page Markdown, RFC 9110 `Accept` negotiation, discovery headers) with nothing more to do as adoption grows.

---

## See also

- [Configuration → AI accessibility](../reference/configuration.md#ai-accessibility) — the config block.
- [Runtime API → AI accessibility hooks](../reference/runtime-api.md#ai-accessibility-hooks) — `to_markdown`, `wrap_markdown`, `llms_txt`, and the context objects.
- [Pyxle for AI coding agents](for-ai-agents.md) — the broader case for Pyxle in the AI era.
- The [llms.txt specification](https://llmstxt.org/).
