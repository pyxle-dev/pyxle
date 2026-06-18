# WebSockets

A Pyxle page can serve a **WebSocket** alongside its HTML. Add a module-level
`async def websocket(ws)` to a page's Python section and Pyxle registers a
WebSocket route at the page's path — the same file serves both the rendered page
over HTTP and a live connection over WS.

```python
# pages/chat/[room].pyxl

from pyxle.realtime import channel

async def websocket(ws):
    await ws.accept()
    room = ws.path_params["room"]
    async with channel(ws, f"room:{room}") as live:
        async for message in ws.iter_text():
            await live.publish(message)


# --- JavaScript/PSX (Client + Server) ---

import React from 'react';
import { useWebSocket } from 'pyxle/client';

export default function Chat() {
  const { status, send, lastMessage } = useWebSocket(window.location.pathname);
  // …render the chat UI…
}
```

Open `/chat/lobby` and you get the page; the client hook upgrades the **same
path** to a WebSocket. Two browsers in `/chat/lobby` exchange messages in real
time; `/chat/general` is a separate room.

## The `websocket` handler

Detection is by **convention**, not a decorator: a module-scope coroutine named
`websocket` is the handler. (This matches how `pages/api/**` modules already
expose a `websocket` callable, and keeps `pyxle.runtime` free of new symbols.)

- It must be `async def` at module scope — a `def websocket` or a `class
  websocket` is a compile error (almost always a mistyped handler).
- Its first argument is the Starlette
  [`WebSocket`](https://www.starlette.io/websockets/) — name it whatever you
  like (`ws`, `socket`, …); it is **not** a `Request`.
- Dynamic segments resolve into `ws.path_params` (e.g. `ws.path_params["room"]`
  for `pages/chat/[room].pyxl`), exactly like a page loader.

You call `await ws.accept()` yourself, then read with `ws.receive_text()` /
`ws.iter_text()` and write with `ws.send_text()` / `ws.send_json()`. The handler
owns the full connection lifecycle.

> A page WebSocket handler runs **without middleware** — no auth population, no
> CSRF, no route hooks. Starlette runs HTTP middleware only for `http` scope.
> See [Authentication](#authentication-in-a-websocket-handler) below.

## Channels (pub/sub)

`pyxle.realtime` provides a small broadcast layer so you don't hand-roll
connection bookkeeping. `channel(ws, name)` subscribes the connection to a named
channel for the life of the `async with` block (unsubscribing on disconnect),
and gives you a handle to `publish` to everyone in it:

```python
from pyxle.realtime import channel

async def websocket(ws):
    await ws.accept()
    async with channel(ws, "notifications") as feed:
        await feed.publish({"type": "joined"})       # dict → JSON frame
        async for raw in ws.iter_text():
            await feed.publish(raw)                   # str → text frame
```

A published message reaches **every** subscriber, including the sender — a chat
UI that doesn't want to echo the sender's own messages filters them client-side.
`bytes` are delivered as binary frames, `str` as text, anything else as JSON.

`channel()` accepts a keyword-only `broker=` to use an explicit broker for that
call — `channel(ws, name, broker=my_broker)` — useful in tests or against a
non-default backend without touching `app.state`.

The broker lives on `app.state.pyxle_broker` (one per process). Reach it inside a
handler with `broker_for(ws)` (the intended accessor) rather than digging into
`ws.app.state`. The default `InProcessBroker` also offers `broadcast(message)`
(send to every connection across all channels) and `channel_count()` for
introspection — extras beyond the core `Broker` protocol. For a custom backend,
implement the `Broker` protocol (`subscribe` / `unsubscribe` / `publish`) and set
your own:

```python
from pyxle.realtime import broker_for

async def websocket(ws):
    await ws.accept()
    await broker_for(ws).broadcast({"type": "ping"})
```

> **Multi-worker caveat.** The default `InProcessBroker` lives in **one
> process**. Under `pyxle serve --workers N`, each worker has its own broker —
> a client on worker 1 never receives a message published on worker 2 (Pyxle
> logs a warning at startup when this combination is detected). For cross-worker
> realtime, use a shared backend (Redis pub/sub) or sticky-session routing at
> the load balancer. The default is correct for `pyxle dev` and single-worker
> `pyxle serve`.
>
> **Why you may not see this locally.** Workers share one listening socket and the
> OS load-balances new connections across them via `accept()`. Under light,
> bursty-by-hand load — opening a couple of browser tabs — the kernel keeps
> handing consecutive connections to the **same** "hot" worker, so two tabs almost
> always land together and you'll see them share state (e.g. a presence count of
> 2). The split across brokers only shows up reliably under **concurrent** load.
> So "it works on my machine with `--workers 2`" is not evidence that cross-worker
> realtime is safe — assume separate workers can't see each other and plan a
> shared broker accordingly.

### Cross-worker realtime with Redis

For multi-worker (or multi-machine) realtime, switch the broker to Redis — no
code change, just configuration:

```bash
pip install 'pyxle-framework[redis]'

export PYXLE_REALTIME_BROKER=redis
export PYXLE_REALTIME_REDIS_URL=redis://localhost:6379   # default if unset
pyxle serve --workers 4
```

Every worker now relays `channel()` / `room.publish()` traffic through Redis
pub/sub: a message published on any worker reaches every subscriber on every
worker (and every machine pointed at the same Redis), delivered to each
connection exactly once. Your handler code is **identical** — `channel(ws,
"room")` and `room.publish(...)` work the same; only the broker behind
`app.state.pyxle_broker` changes. The connection is pinged at startup, so a
wrong URL fails loudly rather than silently dropping messages, and the listener
reconnects automatically if Redis blips.

| Env var | Default | Purpose |
|---|---|---|
| `PYXLE_REALTIME_BROKER` | `memory` | `memory` (in-process) or `redis` |
| `PYXLE_REALTIME_REDIS_URL` | `redis://localhost:6379` | Redis connection URL |
| `PYXLE_REALTIME_CHANNEL_PREFIX` | `pyxle:rt:` | namespace for the Redis channels |

> **Presence counts are still per-process unless you share that state too.** The
> broker makes *message delivery* span workers, but a "users online" counter you
> keep in a module-level dict is per-process — derive presence from a shared
> store (e.g. a Redis counter) if you need an exact cross-worker count. The Redis
> broker is the default in-process broker's drop-in replacement (`PSUBSCRIBE`s the
> namespace and filters locally); for very high channel counts, a per-channel
> subscription variant is a future optimisation.

## Authentication in a WebSocket handler

Because the auth middleware never runs for a WebSocket upgrade, `request.user`
is **not** available. Resolve the session yourself with
`authenticate_websocket`, which reads the session cookie off the handshake and
returns the user (or `None`):

```python
from pyxle.realtime import authenticate_websocket

async def websocket(ws):
    user = await authenticate_websocket(ws)
    if user is None:
        await ws.close(code=4401)   # unauthorized
        return
    await ws.accept()
    # …user is the signed-in pyxle-auth user…
```

It returns `None` (doing zero work) when [`pyxle-auth`](../plugins/pyxle-auth.md)
isn't installed or no session cookie is present.

**Gating specific actions on a public socket.** A socket can stay open to
everyone (read-only) while restricting *writes* to authenticated users. Call
`authenticate_websocket(ws)` when a privileged frame arrives, and take the
identity from the session — never from the client payload, which can be forged:

```python
from pyxle.realtime import authenticate_websocket, channel

async def websocket(ws):
    await ws.accept()                         # anyone may connect and watch
    async with channel(ws, "incident:42") as room:
        async for raw in ws.iter_text():
            msg = json.loads(raw)
            if msg["type"] == "update":       # posting is privileged
                user = await authenticate_websocket(ws)
                if user is None:
                    continue                  # ignore (or close) anonymous writes
                await room.publish({"type": "update",
                                    "author": user.email,   # trust the session, not msg
                                    "text": msg["text"]})
```

**Origin checking.** CSRF doesn't apply to a WebSocket upgrade, so a cross-site
origin check is the equivalent guard against a hostile page opening a socket
with the victim's cookie. Enforce it when the socket carries privileged state:

```python
from pyxle.realtime import origin_allowed

async def websocket(ws):
    if not origin_allowed(ws, ["https://app.example.com"]):
        await ws.close(code=4403)
        return
    ...
```

## The `useWebSocket()` client hook

`useWebSocket(path, options?)` connects from the browser with auto-reconnect,
JSON parsing, and connection state. It **never connects during SSR** and
reconnects with exponential backoff (capped, with jitter).

```jsx
import { useWebSocket } from 'pyxle/client';

function Chat({ room }) {
  const { status, send, lastMessage, error } = useWebSocket(`/chat/${room}`, {
    onMessage(data) {
      // data is JSON-parsed when the frame is valid JSON, else the raw string
    },
  });

  return (
    <div>
      <span>{status}</span>                         {/* 'connecting' | 'open' | 'closed' */}
      <button disabled={status !== 'open'} onClick={() => send({ text: 'hi' })}>
        Send
      </button>
      {error && <p role="alert">{error}</p>}
    </div>
  );
}
```

| Returns | Type | Description |
|---|---|---|
| `status` | `'connecting' \| 'open' \| 'closed'` | Connection state |
| `send(data)` | `(data) => boolean` | Send a string as-is, or JSON-encode anything else; `false` if not open |
| `lastMessage` | `unknown` | The most recent received message (JSON-parsed when possible) |
| `error` | `string \| null` | The last error message |

Options: `onMessage(data, event)`, `protocols`, `reconnect` (default `true`),
`maxRetries` (default `Infinity`). A relative `path` is resolved against the
current origin with the matching scheme (`wss:` on `https:`); an absolute
`ws://` / `wss://` URL passes through.

## See also

- [API routes](api-routes.md) — `pages/api/**` modules can export a `websocket`
  callable too (and an `endpoint` for HTTP on the same path).
- [pyxle-auth](../plugins/pyxle-auth.md) — the session the WS auth helper reads.
- [Deployment](deployment.md) — multi-core serving and the broker caveat.
