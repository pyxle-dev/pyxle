# Third-party packages

A Pyxle project has **two** dependency sets: Python packages (used by your
`@server` loaders, `@action` mutations, and `pages/api/*.py` endpoints) and Node
packages (used by your React/JSX components). Add to whichever side you need.

> **After installing a new Node package, restart `pyxle dev`.** Until you do,
> the page can fail to hydrate, with a misleading React error in the console —
> `Invalid hook call` or `more than one copy of React` — while the server-side
> render still looks fine. Nothing is wrong with your React versions: Vite's
> dependency cache predates the new package, and the restart rebuilds it. This
> bites hardest right after following an example, because the install is the
> step you just did.

## Python packages (pip)

Add the package to `requirements.txt` and install:

```bash
echo "pydantic>=2" >> requirements.txt
pip install -r requirements.txt
```

Import it in the **Python** section of a `.pyxl` file (above the first JS
`import`) or in a `pages/api/*.py` module:

```python
from pydantic import BaseModel
```

## Node packages (npm)

Install with npm and import it in the **JSX** section of a `.pyxl` file:

```bash
npm install zustand
```

```jsx
import { create } from 'zustand';
```

Vite bundles it for the browser and Pyxle's SSR runtime bundles it for the
server, so most packages "just work" on both. Prefer packages with **SSR
support** (they render the same markup on the server and client) — a
browser-only package should be used inside a `useEffect`, an event handler, or a
`<ClientOnly>` boundary. See [Client Components](client-components.md).

Run both installers at once with `pyxle install`.

### CommonJS packages and SSR

The server render bundles your page as an ES module, with React provided by the
runtime rather than bundled in. Pyxle resolves dependencies **ESM-first** (it
prefers a package's `module`/ESM entry over its CommonJS `main`), so libraries
that ship both — including `lucide-react` and most of the shadcn/ui ecosystem —
render on the server with no extra configuration.

A package that ships **only** CommonJS and calls `require('react')` internally
can't be linked into the ES-module server bundle. If one does, the server
render fails with an actionable error that names the package's `require(...)`
and your page file, and suggests the fix: use a version that ships an ES module,
or render that part of the page client-only with `<ClientOnly>` so it never runs
during the server render.

### The import alias

`pyxle init` sets up an import alias (default `@/*`) in `jsconfig.json`, and
Pyxle wires the same alias into the Vite config **and** the SSR runtime. So
`@/lib/format` resolves to `lib/format.js` from anywhere, on both server and
client:

```jsx
import { formatDate } from '@/lib/format';
```

## Charts, and other libraries that measure the DOM

A `@server` loader returns a dict; a third-party React component renders it.
Nothing sits in between — no API route, no `fetch`, no serializer:

```python
@server
async def load(request):
    days = await asyncio.to_thread(summarize, LOG)   # plain Python
    return {"days": days}
```

> **Install Recharts as `recharts@^2.15`.** Version 3.x brings in a CommonJS-only
> dependency that calls `require('react')`, so a page importing it fails to
> server-render with the error described under
> [CommonJS packages and SSR](#commonjs-packages-and-ssr) above. Pin 2.x, or keep
> 3.x and render the chart inside a `<ClientOnly>` boundary.

```jsx
import { ComposedChart, Bar, Line, XAxis, YAxis } from 'recharts';

export default function Latency({ data }) {
  return (
    <ComposedChart id="latency-chart" width={880} height={340} data={data.days}>
      <XAxis dataKey="day" interval={2} />
      <YAxis interval={0} />
      <Bar dataKey="requests" isAnimationActive={false} />
      <Line dataKey="p95" isAnimationActive={false} />
    </ComposedChart>
  );
}
```

That renders to real SVG **on the server** — the plotted path, the bars and the
axis labels are all in the HTML before any JavaScript runs — and it is a live
React tree once hydrated. A complete, runnable version is in
[`examples/charts`](https://github.com/pyxle-dev/pyxle/tree/main/examples/charts).

### Why the extra props

Charting libraries are the one category where SSR needs care, because they work
out their layout by **measuring the DOM** — and during a server render there is
no DOM to measure. The server and the browser can then produce different markup,
and React treats that as a hydration mismatch.

What a mismatch costs you depends on what disagreed, and the two cases are worth
telling apart before you go looking for one:

- **An attribute** — a coordinate, a `width`, an `href`. React keeps the
  **server's** value and says so: *"A tree hydrated but some attributes of the
  server rendered HTML didn't match the client properties. This won't be patched
  up."* Nothing is re-rendered and the page is fully interactive; you are simply
  left with a value the client never agreed to, and a later re-render will not
  necessarily correct it, because React is comparing against what it thinks it
  already rendered. This is the quiet one.
- **Text or structure** — a label the browser wraps onto a second line, an
  element one side emits and the other does not. React cannot patch either in
  place, and it distinguishes them: *"Hydration failed because the server
  rendered **text** didn't match the client"* when the two sides disagree about a
  string, and *"Hydration failed because the server rendered **HTML** didn't
  match the client"* when one side emits an element the other does not — which is
  the kind the entry animations below produce. Both continue *"As a result this
  tree will be regenerated on the client."* Either way React discards
  the server HTML for the **whole root** and re-renders it in the browser. The
  page ends up interactive, but the server render is thrown away — you pay for
  SSR and the visitor gets a client render anyway.

Only that second kind — text or structure — is reported outside a development
build, and then only as a minified error code. See
[Verifying it hydrates](#verifying-it-hydrates) below.

The fix for both is the same shape — **give the library the number instead of
letting it measure**:

| Measures the DOM | Symptom | Fix |
|---|---|---|
| Axis tick layout | Server keeps every label and leaves the end tick where the scale put it; the browser measures, drops labels that will not fit and nudges the end tick inwards | Give **every** axis a numeric `interval` (see below) |
| Default axis tick text | A tick label with a space in it wraps in the browser whenever its measured word widths exceed the axis `width` — one `<tspan>` per line, two for a two-word label; the server measures nothing, so it emits exactly one | Keep labels to one short line, or supply a custom `tick` renderer |
| Entry animations | Extra wrapper element on the client only | `isAnimationActive={false}` |

Two other things Recharts measures are **not** mismatches, because the
measurement lands after mount rather than during hydration.
`<ResponsiveContainer>` renders an empty `<div>` on the server and the chart
appears only once JS has run — see
[Staying responsive without `ResponsiveContainer`](#staying-responsive-without-responsivecontainer).
`<Legend>` feeds its own measured height back into the plot area, which comes out
26px shorter in the browser than on the server; render the legend as your own
markup if you want the server's layout to be the final one.

The tick row catches people out because a vertical axis looks like it has nothing
to thin. Recharts runs one tick pass for every axis, and `interval` decides which
one: a **number** means "take these ticks as they are", anything else — including
the `preserveEnd` default — means "measure the labels first". Measuring is what
the server cannot do, so it always takes the first path. Leave a `<YAxis>` on its
default and the server leaves the end tick where the scale put it — against the
chart's top margin, so `y=5` with Recharts' own defaults — while the browser
pulls it in to `y=12.796875`, and React names the mismatch in the console on
every development load. It is an attribute mismatch, so what ships is the
server's coordinate, on a chart that is otherwise working perfectly.
`interval={0}` keeps every tick and pins both sides to the same path.

That second number is worth a look, because it says what "measuring" means here.
Recharts sizes a label in `getStringSize` (`recharts/util/DOMUtils`): it appends
a hidden `<span id="recharts_measurement_span">` to `document.body`, sets the
text on it and reads `getBoundingClientRect()`. The span takes its font from the
axis, which `CartesianAxis` only reads in `componentDidMount` — so on the render
that hydrates it has none to take and inherits the page's, giving a line box as
tall as the body's `line-height`. The end tick is then placed half a label below
the top of the chart's viewBox. With a `16px/1.6` body that box is `25.59375px`
and the tick lands at `12.796875`. `getBBox()` on the rendered `<text>` reports
something else entirely — a different element, in the SVG, at whatever
`font-size` the tick draws with — so halving *that* will never give you the
coordinate. The element the layout measured is not the one you are looking at.
The same span also decides where a tick label wraps, so a label can be measured
in one font and painted in another.

Separately, watch for **module-level counters**. Recharts names its `<clipPath>`
from one, and an SSR worker serves many requests, so the counter keeps climbing
(`recharts1-clip`, `recharts6-clip`, …) while the browser always starts from one.
Passing a stable `id` to the chart pins it. Any library that keeps mutable
module-global state behaves this way under SSR.

None of this is specific to Pyxle — it applies to any framework that renders
React on the server.

### Verifying it hydrates

Run `pyxle dev`, open the browser console, and read **all** of it. React reports a
mismatch *after* its own DevTools notice, not before it, so a page that is broken
and a page that is clean look identical at the top of the console. Filter the
console for `hydrat` instead — that one word separates the three cases:

- **Clean** — no match. What is left is `[vite] connecting...`,
  `[vite] connected.`, the DevTools notice, and whatever your own page logs.
  Judge by the absence of a match, not by the console being empty — your app's
  own warnings are not hydration failures. Clear them anyway, so the console
  stays readable: a missing `public/favicon.ico` logs a 404 on every load, and
  noise you have taught yourself to scroll past is noise you will scroll past
  again.
- **An attribute mismatch** — `A tree hydrated but some attributes of the server
  rendered HTML didn't match the client properties. This won't be patched up.`
- **A text mismatch** — an uncaught `Error: Hydration failed because the server
  rendered text didn't match the client. As a result this tree will be
  regenerated on the client.`
- **A structure mismatch** — an uncaught `Error: Hydration failed because the
  server rendered HTML didn't match the client. As a result this tree will be
  regenerated on the client.` `text` and `HTML` are the only words that differ
  between these two, and the second is the one an entry animation produces.

All three messages continue with the component path and a `+`/`-` diff of the two
renders, which is what tells you the prop to pin and the component to pin it on.

That is the only test that finds both kinds. A production build is not a
substitute:

- An **attribute** mismatch is completely silent in production. Nothing is
  logged, the page is interactive, and it behaves identically to a page with no
  mismatch at all.
- Comparing the production DOM against the server HTML does not find one either.
  React kept the server's value, so the two agree *because* of the bug.
- A **text or structure** mismatch does surface in production, as a thrown
  minified React error — `#418` for the wrapped-label case above — carrying no
  detail. That tells you a mismatch exists, not where.

So verify in `pyxle dev`, then treat the production build as a build, not a
check.

One symptom this check is *not* for: a page that renders correctly and responds
to nothing. Neither mismatch does that. Both leave the page interactive — the
text/structure one re-renders the whole root in the browser and you still end up
with a working tree, just without the server render you paid for. A page that is
genuinely inert never ran its JavaScript at all, which is a different problem
with a different cause; see
[Which browsers the dev server trusts](../architecture/dev-server.md#which-browsers-the-dev-server-trusts).

### Staying responsive without `ResponsiveContainer`

`<ResponsiveContainer>` is the one you will miss. Replace it with a hook that
renders at a fixed width on the server and measures once mounted. The fallback
width is used for the server render *and* the first client render, so the two
agree and hydration stays clean:

```jsx
export function useMeasuredWidth(fallback) {
  const ref = useRef(null);
  const [width, setWidth] = useState(fallback);

  useEffect(() => {
    const node = ref.current;
    if (!node || typeof ResizeObserver === 'undefined') return undefined;
    const observer = new ResizeObserver(([entry]) => {
      setWidth(Math.round(entry.contentRect.width));
    });
    observer.observe(node);
    return () => observer.disconnect();
  }, []);

  return [ref, width];
}
```

```jsx
const [chartBox, width] = useMeasuredWidth(880);

<div ref={chartBox}>
  <ComposedChart width={width} height={340} data={data.days}>{/* … */}</ComposedChart>
</div>
```

Anything derived from `width` — a tick `interval`, say — is safe too, because at
hydration time `width` is the fallback on both sides and only changes afterwards.

### When a library cannot render on the server at all

Some libraries touch `window` or `document` at module scope and will never
server-render. Those belong in a `<ClientOnly>` boundary, which skips them during
SSR and mounts them in the browser — you lose the server-rendered markup for that
subtree, but nothing breaks. See [Client Components](client-components.md).

## shadcn/ui

[shadcn/ui](https://ui.shadcn.com) is a collection of components you copy into
your project (not an installed dependency). Enable it when you scaffold:

```bash
pyxle init my-app --shadcn        # implies Tailwind
# or answer "y" to the shadcn prompt in interactive init
```

The scaffold pre-configures everything shadcn needs — `components.json`,
`jsconfig.json` (the `@` alias), `lib/utils.js`, and a Tailwind v4 stylesheet
with the shadcn theme tokens — so you **don't** need to run `shadcn init`. Add
components directly:

```bash
cd my-app
npm install
npx shadcn@latest add button
```

This drops `components/ui/button.jsx` into your project (JavaScript, not
TypeScript). Import it via the alias and use it in any page:

```jsx
// pages/index.pyxl (JSX section)
import { Button } from '@/components/ui/button';

export default function Home() {
  return <Button>Click me</Button>;
}
```

`pyxle build` bundles it for production and it renders server-side like any
other component.

> **Verified flow.** The steps above were verified end to end on Node 22 with
> `shadcn@latest`: scaffold with `--shadcn` → `npm install` → `npx shadcn@latest
> add button` → import via `@/components/ui/button` → `pyxle build` +
> `pyxle serve`. Because the scaffold ships a ready `components.json`, running
> `shadcn init` is unnecessary — it would only offer to overwrite the config the
> scaffold already wrote.

### Adding shadcn to an existing project

If you scaffolded without it, enable Tailwind first (see
[Styling](styling.md#adding-tailwind-to-an-existing-project)), then create a
`components.json` at your project root (`npx shadcn@latest init` can generate it
in **JavaScript** mode — answer *no* to TypeScript), pointing its
`tailwind.css` at your CSS entry (`pages/styles/app.css`) and its aliases at
your import alias.

## Next steps

- Styling and Tailwind: [Styling](styling.md)
- Browser-only libraries: [Client Components](client-components.md)
- Validate action bodies with Pydantic: [Server Actions](../core-concepts/server-actions.md)
