# Example applications

Two complete, runnable applications live in the
[`examples/`](https://github.com/pyxle-dev/pyxle/tree/main/examples) directory of
the repository. They are not decoration. Each one exists to answer a question
that prose cannot answer credibly — *does a real npm package work?*, *can one URL
be both a page and a socket?* — by being a program you can run.

Both are small enough to read in a sitting, and both are built and tested the
same way the framework is.

---

## Charts — a real charting library, driven from a Python loader

**[`examples/charts`](https://github.com/pyxle-dev/pyxle/tree/main/examples/charts)**

One `.pyxl` file. A Python `@server` loader reads a 2,316-row request log,
aggregates it with the standard library's `csv` and `statistics`, and returns a
dict. A [Recharts](https://recharts.org) chart — an ordinary npm package,
installed with `npm install recharts@^2.15` — renders that dict directly.

```
npm install recharts@^2.15  →  import { ComposedChart } from 'recharts';
@server def load(request)   →  export default function Latency({ data })
```

There is no API route, no `fetch`, no serializer and no client-side data layer in
between. The chart's SVG is in the server-rendered HTML — visible in
`view-source`, before any JavaScript runs — and the tree is a live React
component once it hydrates: the metric toggle re-renders the chart from new
state.

This is the example to read if you are evaluating the central claim, because it
is the one that could most easily have been faked. It imports the library the way
its own documentation tells you to. Nothing wraps it, nothing reimplements it.

**It also documents the hard part rather than avoiding it.** Recharts decides
some of its layout by *measuring the DOM* — and during a server render there is
no DOM to measure. That is a genuine constraint of server-rendering any library
of this kind, not a Pyxle bug and not something an example should quietly sidestep.
The example's README and
[Third-party packages](guides/third-party-packages.md#charts-and-other-libraries-that-measure-the-dom)
name each thing Recharts measures, the mismatch each one produces, the fix, and
how to check your own charts with the browser console. If you are putting any
DOM-measuring library behind SSR, read that section before you start.

---

## Chat — one path serving both a page and a WebSocket

**[`examples/chat`](https://github.com/pyxle-dev/pyxle/tree/main/examples/chat)**

A two-file realtime chat. `pages/chat/[room].pyxl` serves the chat **page** over
HTTP *and* a **WebSocket** at the same path: an `async def websocket(ws)` joins a
per-room broadcast channel from `pyxle.realtime`, and the client subscribes with
the `useWebSocket()` hook.

The point is that the route is one file. The page and the socket that updates it
are not two services to deploy, two routers to keep in step, or two places to
remember a room name.

See [WebSockets](guides/websockets.md) for the full API.

---

## Running one

Each example is a standalone Pyxle project. From inside its directory:

```bash
pyxle install   # installs Python and npm dependencies
pyxle dev
```

`pyxle install` needs [Node.js](getting-started/installation.md) on your `PATH`
for the JavaScript half of the build; `pyxle dev` prints the URL to open.

To run one as a production build instead, see
[Deployment](guides/deployment.md) — and note that `pyxle serve` requires
`PYXLE_SECRET_KEY` to be set, which `pyxle dev` does not.
