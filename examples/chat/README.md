# Pyxle WebSocket example: realtime chat

A two-file realtime chat. `pages/chat/[room].pyxl` serves both the chat **page**
(over HTTP) and a **WebSocket** (at the same path) — its `async def websocket(ws)`
joins a per-room broadcast channel from `pyxle.realtime`, and the client uses the
`useWebSocket()` hook.

## Run it

```bash
pyxle install   # or: npm install
pyxle dev
```

Open two browser windows at <http://127.0.0.1:8000/chat/lobby> — a message typed
in one appears in the other instantly. `/chat/general` is a separate room.

## How it works

- **Server** (`pages/chat/[room].pyxl`): the `websocket` handler subscribes the
  connection to `room:<room>` via `channel(ws, ...)` and re-publishes each
  message it receives. The broker fans it out to every other connection in that
  room.
- **Client**: `useWebSocket('/chat/' + room)` connects to the page's own path,
  appends each received message, and `send()`s the input. It auto-reconnects and
  never opens a socket during SSR.

> The default broker is **in-process**. Under `pyxle serve --workers N` it
> doesn't span workers — use a shared (e.g. Redis) broker or sticky sessions for
> multi-worker realtime. See the [WebSockets guide](../../docs/guides/websockets.md).
