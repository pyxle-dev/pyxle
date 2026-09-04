# The road to a fully-typed Pyxle

> **Status: roadmap.** This page describes where Pyxle's type story is going, not
> a feature that ships today. For what's available now — typed editor support,
> generated declarations, and the `pyxle typecheck` gate — see the
> [TypeScript guide](typescript.md). Nothing here changes the fact that **JavaScript
> is fully first-class today**: every npm package works in a `.pyxl` JSX block, and
> nothing is blocked.

Pyxle is Python-first. That is a deliberate choice, not a limitation — but it
raises an obvious question for anyone who has felt the pull of end-to-end types
in a TypeScript stack: *if my server is Python and my client is React, where do
my types come from?*

This is our answer, and the direction we are building toward.

## The vision: one source of truth, typed across the boundary

In a Pyxle app the shape of your data is **defined once, in Python** — a loader
returns a Pydantic model, a dataclass, a `TypedDict`, or an annotated `dict`; an
`@action` accepts a validated body. The endgame is that this shape **flows to the
client as a real type**, so the `data` your component receives is typed *from the
Python that produced it*, and a mismatch is caught before the page ever renders.

```
@server loader returns  User          ──▶  component receives  data: User
@action accepts         CreatePost    ──▶  useAction is typed to CreatePost
```

No hand-written client interfaces to keep in sync. No duplicated shapes drifting
apart. Python stays the single source of truth, and the type crosses the
Python↔React boundary with it. That is the thing a Python-first full-stack
framework is uniquely positioned to offer — and it's what "a fully-typed Pyxle"
means.

## What's already true today

This isn't a green field. The framework already understands the boundary the
type bridge will cross:

- **`pyxle check`** catches undefined names and semantic mistakes in your `.pyxl`
  files before they run.
- **`pyxle typecheck`** generates a full set of `.d.ts` declarations for the
  `pyxle/client` runtime and type-checks your client against them with `tsc` —
  so the framework surface is already typed in your editor and gateable in CI.
- **`@action` bodies validate against Pydantic models** at runtime today, and
  `pyxle openapi` emits an OpenAPI 3.1 schema straight from those models. The
  server already treats your Python types as the contract.
- **The compiler extracts loader and action metadata** at build time. The
  information the type bridge needs to reason about is already on the table.

So the direction below isn't a rewrite — it's connecting foundations that are
already in place.

## Where it's going

Two pieces, in roughly this order:

1. **Author your client in TypeScript.** Write the JSX half of a `.pyxl` file in
   TSX — annotations, `interface`, generics — with full editor intelligence.
   (Today the client block is plain JSX that gets type-*checked* but not
   type-*annotated*; this lifts that restriction.)

2. **Forward Python types to the client.** Derive a TypeScript type from a
   loader's or action's Python return/parameter type and thread it into the
   component's props, kept in sync automatically as your Python changes. This is
   the differentiator, and the harder half — it's where most of the design work
   lives.

We're describing the *direction*, not committing to a specific mechanism yet.
The right inference strategy, code-generation approach, and editor integration
are exactly what a dedicated design pass will settle — and we'd rather ship that
properly than nail a half-answer to a page.

## Principles we're holding to

- **Python is the source of truth.** Types are *derived* from your Python, never
  hand-duplicated on the client.
- **TypeScript is opt-in, not a tax.** JavaScript stays fully first-class. A JS
  Pyxle app is a real, supported Pyxle app — today and after this lands.
- **No lock-in and no magic.** Generated types are inspectable; nothing hides
  behavior — in keeping with Pyxle's "no magic" design principle.
- **Honest tooling.** A green type-check will mean what it says.

## A taste of what it should feel like

*Illustrative — this is the target ergonomics, not current syntax.*

```pyxl
# posts/[slug].pyxl

class Post(BaseModel):
    slug: str
    title: str
    body: str

@server
async def load(request) -> Post:          # Python defines the shape, once
    return await db.get_post(request.path_params["slug"])

// the type comes from the loader, for free
export default function PostPage({ data }: { data: Post }) {
  //                                          ^^^^ derived from the Python above
  return <article><h1>{data.title}</h1>{/* data.tital → caught before render */}</article>
}
```

The point isn't the syntax — it's that `Post` was written **once, in Python**,
and the client got it for free.

## Status and how to follow along

This is **planned**, not scheduled — we're deliberately not attaching a version
or date to it, because the design pass comes first and we won't pre-commit to a
mechanism we haven't validated. What you can rely on in the meantime:

- JavaScript is fully supported, with no capability or package gaps.
- The typed-editor-support and `pyxle typecheck` features on the
  [TypeScript guide](typescript.md) are here today.
- When the type bridge lands, it will be **additive** — it won't break your JS
  app.

If typed end-to-end data is the thing that would make Pyxle a yes for you, this
page is our commitment that it's where we're headed — and that we intend to do
it right.
