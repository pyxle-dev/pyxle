# Pyxle vs. other frameworks

Pyxle isn't the only way to build a full-stack app in Python, and it isn't the right tool for every job. This page is an honest map of the landscape — what each alternative is genuinely great at, and when you should reach for it **instead of** Pyxle.

The short version: Pyxle's niche is **real React UI plus real Python server logic, in one file and one service**, with conventions predictable enough that an AI coding agent can ship a whole feature in a single pass. If you never want to write JavaScript, if you're building a data dashboard, or if you need a decade-mature ecosystem at large-team scale today, one of the tools below is probably a better fit — and we'll say so plainly.

## At a glance

| | UI you write | Languages | Services to run | Real React ecosystem | Sweet spot |
|---|---|---|---|---|---|
| **Pyxle** | React / JSX | Python + JS | **one** | ✅ full | React apps, solo & small teams, AI-assisted dev |
| Next.js + FastAPI | React / JSX | Python + TS | two | ✅ full | Large teams, maximum ecosystem & scale |
| Reflex | Python | Python | one | ⚠️ wrapped | Teams that would rather not touch JS |
| Django | Templates (or + React SPA) | Python (+ JS) | one (or two) | ❌ (or two-service) | Content / CRUD apps, mature batteries |
| NiceGUI | Python | Python | one | ❌ (Vue under the hood) | Internal tools & dashboards, fast |
| Streamlit | Python | Python | one | ❌ | Data / ML dashboards & demos |

"Real React ecosystem" means you can `npm install` any React library and use it directly, rather than through a framework-specific wrapper.

Worked example: [`examples/charts`](https://github.com/pyxle-dev/pyxle/tree/main/examples/charts) is a [Recharts](https://recharts.org) chart rendered from data a Python `@server` loader aggregated — one `.pyxl` file, `npm install recharts`, no wrapper and no API route in between. It server-renders to SVG and is a live React tree after hydration. See [Third-party packages](third-party-packages.md#charts-and-other-libraries-that-measure-the-dom).

## Pyxle vs. Next.js + FastAPI

This is the stack Pyxle most directly replaces: a Next.js (React/TypeScript) frontend talking to a FastAPI (Python) backend over an API.

It's the gold standard for a reason — both halves are mature, battle-tested, and have enormous ecosystems, and the separation scales cleanly to large teams who *want* a hard frontend/backend boundary. The cost is everything in the seam: two languages, two runtimes, two deploys, a hand-written API contract, CORS, and the type drift between your Pydantic models and your TypeScript types.

Pyxle collapses that seam. A `@server` loader's return value *is* your component's props; an `@action` *is* the endpoint, called with `useAction` instead of `fetch`. One file, one service, no glue.

**Use Next.js + FastAPI instead** when you have a large team that benefits from a strict frontend/backend split, you need the maturity and scale guarantees of the two most-proven tools in their categories, or you're already invested in that stack. **Reach for Pyxle** when the two-service tax is pure overhead for your team size and you'd rather ship one thing.

## Pyxle vs. Reflex

Reflex lets you build the entire app — UI included — in pure Python; it compiles your Python components down to a React/Next.js app under the hood. For a team that never wants to write JavaScript, that's a genuinely compelling promise, and Reflex executes it well.

The trade is indirection: you're expressing your UI through Reflex's component abstractions rather than React itself, so reaching for an arbitrary React library or a bit of custom client behavior means going through (or around) that layer.

Pyxle makes the opposite bet: you write **real React/JSX**, so the entire npm React ecosystem is available with no wrapper — but you *do* write some JavaScript.

**Use Reflex instead** when "zero JavaScript, ever" is a hard requirement for your team. **Reach for Pyxle** when you want the real React ecosystem and are comfortable writing JSX next to your Python.

## Pyxle vs. Django

Django is the batteries-included Python web framework: a superb ORM, the legendary admin, auth, migrations, and a vast, mature ecosystem. For server-rendered, content- and CRUD-heavy applications it's still one of the best choices in any language.

But Django's native UI story is server-rendered templates. The moment you want a rich React frontend, you're back to a two-service setup (Django REST Framework + a separate React SPA) — the very split Pyxle exists to avoid.

**Use Django instead** when you want a mature ORM and admin out of the box, your UI is well served by server-rendered templates, or you're building something content-heavy where its ecosystem shines. **Reach for Pyxle** when you specifically want a React UI without bolting on a second service to serve it.

## Pyxle vs. NiceGUI

NiceGUI is a delightful way to build web UIs in pure Python (on top of Vue/Quasar and FastAPI). For internal tools, control panels, and dashboards, it's fast to write and gets you a working interface in minutes.

Like Reflex, the UI is defined in Python rather than React, which is exactly what makes it quick for tooling — and exactly why it's a less natural fit for a polished, public-facing product where you want full control over the React component tree.

**Use NiceGUI instead** for internal tools and dashboards you want to stand up quickly in Python. **Reach for Pyxle** for public-facing apps where you want a real, hand-authored React frontend.

## Pyxle vs. Streamlit

Streamlit is phenomenal at what it's built for: turning a Python data script into an interactive app. For data exploration, ML demos, and analytics dashboards, nothing gets you there faster.

Its execution model — the script re-runs top to bottom on every interaction — is the source of that magic, and also the reason it's not aimed at general-purpose web apps with custom routing, fine-grained interactivity, and a bespoke UI.

**Use Streamlit instead** for data and ML dashboards and quick analytical apps. **Reach for Pyxle** when you're building a general-purpose web application rather than a data tool.

## What Pyxle doesn't have yet

Pyxle is young and deliberately dependency-light, so some things you may expect from a mature React stack aren't built in yet. Here's an honest list and how to bridge each one today:

- **Authoring your client in TypeScript.** Pyxle already gives you *end-to-end Python types* (typed `@server` loaders and Pydantic-validated `@action` bodies) and consumes typed npm packages, and `pyxle check` / `pyxle typecheck` validate your code. What's not here yet is writing the client block itself in **TSX** with types forwarded across the Python↔React boundary. That's the next milestone — see [the road to a fully-typed Pyxle](fully-typed-pyxle.md). JavaScript is fully first-class in the meantime.
- **A first-class test client.** You can unit-test loaders and actions and write end-to-end tests today (see [Testing](testing.md)); an in-process `PyxleTestClient` that drives pages and actions without managing a server is on the roadmap.
- **Automatic image byte-optimization and font optimization.** The built-in [`<Image>`](build-optimization.md) gives you responsive `srcset`, lazy-loading, and priority hints (layout parity with `next/image`), but *byte* optimization (resizing/format conversion) is opt-in via an image loader/CDN, not automatic. There's no `next/font` equivalent yet — self-host fonts with `@font-face` and `font-display: swap`.
- **Internationalization.** There's no built-in i18n routing or message system yet. Use a React i18n library (e.g. `react-i18next`) in your client components, with the locale resolved in a `@server` loader.

None of these block shipping a real app — they're the honest edges of a fast-moving 0.x, listed so you know exactly where you stand.

## So which should you choose?

**Choose Pyxle when** you want a real React frontend *and* a Python backend without running two services, you value predictable conventions (for yourself or your AI agent), and you're happy being an early adopter of a framework that's moving quickly.

**Choose something else when:**

- You never want to write JavaScript → **Reflex** or **NiceGUI**
- You're building a data or ML dashboard → **Streamlit**
- You need a mature, proven ecosystem at large-team scale *today* → **Next.js + FastAPI** or **Django**
- Your app is content-heavy CRUD and you want an admin + ORM out of the box → **Django**

A fair caveat: Pyxle is **0.x and evolving** — APIs may still shift before 1.0. If you need rock-solid stability for something mission-critical shipping next week, pick a mature option or wait for 1.0. But if you're starting something new and the one-file Python + React workflow sounds like how you *want* to build, that's exactly who Pyxle is for.

Curious about the AI-agent angle specifically? See [Pyxle for AI coding agents](for-ai-agents.md). Ready to try it? The [Quick Start](../getting-started/quick-start.md) takes about five minutes.
