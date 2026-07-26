# Pyxle Studio

Pyxle Studio is the dashboard built into `pyxle dev` — one local page that shows everything the development server knows about your app: every route with its loader and actions, an interactive tester that runs them with real inputs, a live request feed, latency metrics, the effective configuration, and `pyxle check` diagnostics. Nothing to install, nothing to configure, and **dev-only by construction** — a production `pyxle serve` process doesn't contain it.

```bash
pyxle studio
```

---

## Opening Studio

Two ways in:

- **`pyxle studio [directory]`** — runs the same development server as `pyxle dev` and opens your browser on the dashboard once it's ready. It takes the server flags (`--host`, `--port`, `--vite-host`, `--vite-port`, `--ssr-workers`, `--tailwind/--no-tailwind`, `--config`, `--verbose`), the debugger flags `--inspect` and `--inspect-port`, and `--open/--no-open`. The command also enables the dashboard for that run even when the config turns it off. Full flag list: [CLI reference](../reference/cli.md#pyxle-studio).
- **Any running `pyxle dev`** already serves it — visit `http://localhost:8000/__pyxle/studio`.

Studio updates itself live: the request feed streams over Server-Sent Events, and a hot rebuild refreshes the route data, so you can keep it open in a second window while you work.

---

## The panels

### Routes

The route table as the server actually resolved it. For every page: its URL pattern and source file, the `@server` loader (name and line), each `@action` with the **real endpoint URL** it's served at, any `websocket` handler, the cache posture (the loader's `revalidate` and the route's edge TTL from the [`cache`](../reference/configuration.md#edge-caching) block), the layout loaders that run above the page, and its `loading.pyxl`/`error.pyxl` boundaries. API routes are listed below the pages.

Every source reference is a click-to-open link into your editor — `vscode://` deep links by default, with Cursor or copy-the-path selectable from the header.

### Tester

Run any loader or action against the live server without writing a scratch page. The two halves are deliberately different in how faithful they are:

- **Loaders run in-process.** Pick a page, fill in its path params and query string, and Studio calls the `@server` function directly, timed. Every result shape renders as it would resolve — a plain data dict, a cache-shaped `{"data": ..., "revalidate": N}` return, a raised `LoaderError` (status and message), or a timeout. The synthesized request is deliberately minimal — no cookies, no session, no caller headers — the tester exercises the *loader*, not a login flow.
- **Actions go through their real HTTP endpoint**, exactly like a browser would. Studio generates an input form from the action's [Pydantic body model](../core-concepts/server-actions.md#validating-request-bodies-with-pydantic)'s JSON schema (a free-form JSON editor when there isn't one) and POSTs to the action's URL — so the CSRF token, Pydantic validation, and any [`routeMiddleware.actions`](middleware.md#route-level-hooks) auth hooks are all exercised. A `401` from your auth hook or a `422` field map in the tester is precisely what a client would get. The response view also surfaces the `x-pyxle-invalidate` header, so you can confirm an action's `invalidate_routes(...)` is telling the client cache what you meant.

### Requests

A live feed of the requests your dev server is handling — method, path, status, duration, the route target that handled it, and the request's correlation id — streamed as they happen. It's a bounded window (the most recent 200), not a log store. Studio's own traffic is excluded from both the feed **and** the metrics, so the dashboard never counts itself.

### Metrics

A view over the same in-process [metrics registry](observability.md#metrics) the observability stack records into: request, SSR-render, loader, and action latency histograms, request totals, and server uptime. No exporter or Prometheus setup needed in dev — this is the zero-config way to see where a slow page spends its time.

### Config

The effective merged settings and every config block (`cors`, `csrf`, `cache`, `observability`, …) as the server is actually running them — the fastest answer to "did my config change take effect?". Secret-shaped values (keys matching `secret`, `token`, `password`, `apiKey`, …) are redacted before they reach the browser.

### Check

The same three-level diagnostics as [`pyxle check`](../reference/cli.md#pyxle-check) — Python syntax and structure, Python semantics, JSX — run on demand from the browser, with each finding linking straight to the offending file and line in your editor.

---

## Configuration

Studio is configured under the `"studio"` block in `pyxle.config.json`. It's on by default in development; a bare boolean is accepted as shorthand (`"studio": false` turns it off).

```json
{
  "studio": {
    "enabled": true,
    "allowedHosts": ["my-machine.local"]
  }
}
```

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `studio` | `object` \| `boolean` | `{}` | Studio settings. `false` disables the dashboard; `true` (or omitting the block) enables it. |
| `studio.enabled` | `boolean` | `true` | Serve the dashboard at `/__pyxle/studio` under `pyxle dev`. The `pyxle studio` command re-enables it for that run even when this is `false`. |
| `studio.allowedHosts` | `string[]` | `[]` | Extra hostnames allowed to reach Studio, extending the built-in `Host`-header allowlist (loopback names and the configured server host are always allowed). |

---

## Security model

Studio displays your app's internals, so even as a local tool it is hardened:

- **Dev-only by construction.** The code that mounts Studio only exists in the debug-mode dev server. `pyxle serve` never constructs it, so no configuration mistake can expose the dashboard in production.
- **A `Host`-header allowlist on every Studio endpoint** — the standard defence against DNS rebinding, where an attacker's domain resolves to `127.0.0.1` so their page can read your local server. Only loopback names (`localhost`, `127.0.0.1`, `[::1]`) and the configured server host are accepted; add `allowedHosts` entries when you genuinely reach the dev server through another name (a LAN IP while testing on a phone, a `*.local` alias).
- **Mutating endpoints are `POST` and require `Content-Type: application/json`**, so a cross-site form or no-CORS fetch can't invoke the tester — and with [CSRF protection](security.md#csrf-protection) enabled (the default), they ride the app's double-submit token too.
- **Secrets are redacted** in the config view, and every Studio response is served `Cache-Control: no-store`.

---

## The reserved `__pyxle` namespace

The `/__pyxle` URL prefix is reserved for framework endpoints — Studio lives under it, and the dev server's Vite asset proxy never forwards paths beneath it to Vite. Don't create pages or API routes that map into it.

---

## See also

- [Debugging](debugging.md) — breakpoints in `.pyxl` source; `pyxle studio --inspect` combines both tools
- [Observability](observability.md) — the metrics registry Studio reads, and production-grade exporters
- [CLI reference → `pyxle studio`](../reference/cli.md#pyxle-studio) — every flag
- [Configuration reference → Studio](../reference/configuration.md#studio) — the config block
