# Pyxle charting example: Recharts from a Python loader

One `.pyxl` file. A Python `@server` loader reads a 2,316-row request log,
aggregates it with `csv` and `statistics`, and returns a dict. A
[Recharts](https://recharts.org) chart — an ordinary npm package — renders that
dict. There is no API route, no `fetch`, no serializer and no client-side data
layer between the two.

```
npm install recharts@^2.15  →  import { ComposedChart } from 'recharts';
@server def load(request)   →  export default function Latency({ data })
```

> **Pin Recharts to 2.x.** Recharts 3.x pulls in a CommonJS-only dependency that
> calls `require('react')`, which cannot be linked into Pyxle's ES-module server
> bundle, so the page fails to server-render. This example's `package.json`
> already caps at `^2.15.0`, so cloning it is safe; the pin above matters when
> you add Recharts to your own app. To use 3.x anyway, render the chart inside a
> [`<ClientOnly>`](https://pyxle.dev/docs/guides/client-components.md) boundary so
> it never runs during the server render.

## Run it

```bash
pyxle install   # or: npm install
pyxle dev
```

Then open <http://127.0.0.1:8000>.

## What it demonstrates

- **A real third-party React library, not a toy.** Recharts brings its own
  component tree, SVG rendering, refs and layout maths.
- **It server-renders.** The chart's `<path>`, bars and axis labels are in the
  HTML before any JavaScript runs:

  ```bash
  pyxle build
  PYXLE_SECRET_KEY=$(openssl rand -hex 32) pyxle serve

  # then, in another terminal:
  curl -s http://127.0.0.1:8000/ | grep -o 'recharts-line-curve" d="M[0-9.,]*'
  ```

  `pyxle serve` refuses to start without `PYXLE_SECRET_KEY` — it is what signs
  CSRF tokens and cookies in production. `pyxle dev` does not need one.

- **It is live after hydration.** Switching the metric re-renders the chart, and
  hovering a day shows a tooltip. Both need React to have attached.
- **Realistic Python.** `summarize()` computes per-day request counts, median and
  95th-percentile latency and a 5xx rate — the kind of aggregation you would
  never want to ship to the browser.

## The interesting part: making a DOM-measuring library hydrate

Recharts decides several things by **measuring the DOM**. The server has no DOM,
so it measures nothing, and the two renders can disagree — which React rejects as
a hydration mismatch. What that costs depends on *what* disagreed, and the two
cases behave nothing alike:

- **An attribute** — a coordinate, a `width`, an `href`. React keeps the
  server's value and tells you so: *"A tree hydrated but some attributes of the
  server rendered HTML didn't match the client properties. **This won't be
  patched up.**"* Nothing is re-rendered: the page hydrates, stays interactive,
  and carries a value the client never agreed to. A later re-render need not
  correct it either, because React diffs against what it believes it rendered,
  not against the DOM. Here that is a tick a few pixels out; elsewhere it is a
  stale `href` or `aria-*`.
- **Text or structure** — a label the browser wraps and the server does not, an
  element one side emits and the other does not. React cannot patch either in
  place, and it distinguishes them: *"Hydration failed because the server
  rendered **text** didn't match the client"* when the two sides disagree about a
  string, and *"Hydration failed because the server rendered **HTML** didn't
  match the client"* when one side emits an element the other does not. Both
  continue *"As a result this tree will be regenerated on the client."* The entry
  animation in the table below is the second kind. Either way React throws
  away the server HTML for the **whole root** and re-renders it in the browser.
  The page still ends up interactive — but the server render you paid for was
  discarded, so the first paint is rebuilt from scratch on the client, which is
  the thing SSR exists to avoid.

Neither kind produces the symptom people expect. A hydration mismatch does not
leave you with a page that renders correctly and responds to nothing: React
attaches in both cases, and the chart's metric toggle and tooltip keep working
even through the whole-root re-render. A page that really is inert never ran its
JavaScript — a different fault with a different cause, which `pyxle dev` warns
about directly.

Each row below is a one-line fix, and every one of them is in the chart:

| What measures the DOM | What goes wrong | Fix used here |
|---|---|---|
| Tick layout on **any** axis | Unless `interval` is a number, Recharts measures every label: it drops the ones that will not fit, and pulls the end tick inwards so its text cannot overflow the axis. The server measures nothing and skips the pass, so it kept all 30 `<XAxis>` dates that the browser — left to itself, on a 1280px-wide window — thinned to 8; and it put the top `<YAxis>` tick at `y=8` where the browser wanted `y=12.796875` ([why that number](#where-y12796875-comes-from)) | Give **every** axis a numeric `interval`. A number is the one value that makes the browser skip the same pass the server skips; `interval={0}` keeps all the ticks |
| Default tick text | Handed an axis `width`, Recharts' `<Text>` measures word widths and breaks any label that overflows it onto one `<tspan>` per line — two for a two-word label; the server measures nothing, so it emits exactly one | Keep labels to one short line, or draw the `<text>` yourself |
| Bar/line entry animation | Adds a wrapper element on the client that the server never emits | `isAnimationActive={false}` |

The tick-text row is the one this chart never actually trips: swap its custom
ticks for Recharts' own `<Text>` and hydration still comes out clean, so the
custom `tick` here earns its place on colour and offset rather than on hydration.
Narrow an axis and put a space in a label and you will see it: drop the custom
`tick` from the traffic `<YAxis>`, so Recharts' own `<Text>` runs, and give it
`width={40}` plus a `tickFormatter` that turns `120` into `120 req`. The server
sends one `<tspan>` per tick, five in all. The browser sends nine: `30 req`
through `120 req` each split into two — `30` and `req` on separate lines — while
`0 req` still fits and stays on one. The exception is the rule working, not an exception to it: the
break is decided per label, by measurement, and only the shortest label survives
40px. Either way the two renders differ, and this row is the second kind of
mismatch: React cannot patch a differing `<tspan>` in place, so it regenerates
the whole root on the client.

Two more things Recharts measures are not mismatches — the measurement lands
after mount, so both renders agree while React is hydrating — but they still
decide what the server can produce. `<ResponsiveContainer>` renders an empty
`<div>` on the server and the chart appears only once JS has run, which is why
this example passes an explicit `width` instead
([`lib/use-measured-width.js`](lib/use-measured-width.js)). `<Legend>` feeds its
own measured height back into the plot area, which lands 26px shorter in the
browser than on the server; the legend here is plain markup so that it doesn't.

### Where `y=12.796875` comes from

Half of `25.59375` — and that is not the tick's own box. Recharts sizes a label
in `getStringSize` (`recharts/util/DOMUtils`, v2.15): it appends a hidden
`<span id="recharts_measurement_span">` to `document.body`, puts the text on it
and reads `getBoundingClientRect()`. The span is styled from the axis' font
size, which `CartesianAxis` only learns in `componentDidMount` — so on the
render that hydrates there is no font size to give it, and the span inherits the
page's `font: 16px/1.6` for a `25.59375px` line box. The end tick is then placed
half a label below the top of the chart's viewBox, which is `y=0` here:
`12.796875`.

Those two numbers are the coordinates Recharts computes. Reproduce the mismatch
by deleting `interval={0}` from the traffic `<YAxis>` and the console diff reads
`y="12"` against `y={16.796875}`, because the `AxisTick` in this file adds its
own `offsetY={4}` to whatever Recharts hands it. Drop the custom `tick` as well
and you see the `8` and the `12.796875` unmodified.

`getBBox()` on the rendered `<text>` disagrees, and should: that element is not
the one the layout measured, and it is drawn at `font-size: 12` rather than the
body's 16px.

One more, unrelated to measuring: Recharts names its `<clipPath>` from a
**module-level counter**. An SSR worker process serves many requests, so that
counter keeps climbing (`recharts1-clip`, `recharts6-clip`, …) while the browser
always starts from one. Passing a stable `id` to the chart fixes it — and this
one is worth knowing about generally, because any library with module-global
mutable state behaves this way under SSR.

None of this is special to Pyxle; the same list applies to any React framework
that server-renders. It is written down here because "it works" is less useful
than knowing exactly *what* to reach for when it doesn't.

The list is what to check against, not a guarantee. React only *names* a
mismatch — which elements, which values — in a development build, so the honest
test is `pyxle dev` with the browser console open. Read the **whole** console, not
the top of it: React logs the mismatch *after* its own DevTools notice, so a
broken load and a clean one are indistinguishable until you scroll past it. Filter
the console for `hydrat` and you get a straight answer — no match means clean, and
a match is one of three messages: `A tree hydrated but some attributes … won't be
patched up` (an attribute), `Hydration failed because the server rendered text
didn't match the client` (a string differs), or `Hydration failed because the
server rendered HTML didn't match the client` (one side emitted an element the
other did not — the kind an entry animation produces). Add an axis, a label or a
series and look again.

A production build is not a substitute, and neither is reading the finished DOM.
An attribute mismatch leaves *no* trace in production — nothing is logged, the
page is fully interactive, and the DOM you are inspecting is the server's value,
which is precisely the bug. Before `interval={0}` was added to the two `<YAxis>`
elements above, this example carried exactly that mismatch, and it survived a
production build, a browser and a click-through with nothing to show for it; it
was only ever visible in `pyxle dev`. The chart in this repository no longer has
it — the point is how little the production build would have told you if it did.

## Files

| File | What's in it |
|---|---|
| `pages/index.pyxl` | The whole thing — Python loader and React component |
| `lib/use-measured-width.js` | The `ResponsiveContainer` replacement that survives SSR |
| `data/requests.csv` | 30 days of synthetic request logs, one row per request |
| `pages/styles/app.css` | Plain CSS |
| `public/favicon.ico` | Served at `/favicon.ico`; without it every load logs a 404 |

## Related docs

- [Third-party packages](../../docs/guides/third-party-packages.md) — npm and pip
  packages, the import alias, shadcn/ui, and charting libraries
- [Data Loading](../../docs/core-concepts/data-loading.md) — `@server` loaders
- [Client Components](../../docs/guides/client-components.md) — `<ClientOnly>`
  for libraries that cannot render on the server at all
