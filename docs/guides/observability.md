# Observability

Pyxle gives every request a **correlation id** and measures how long it takes —
on by default, with no setup. Both are cheap enough (a `uuid4` and two
`perf_counter` reads, no I/O) to run on every request in production.

Heavier, exporter-style observability — structured logs, a Prometheus metrics
endpoint, OpenTelemetry tracing — is configured under the same
[`observability`](../reference/configuration.md#observability) block and stays
**off** until you opt in.

## Request IDs

Every request is assigned a correlation id and the response carries it as the
`X-Request-Id` header:

```
$ curl -i http://localhost:8000/
HTTP/1.1 200 OK
x-request-id: 3f9a1c0b8e7d4a2f9c1b6e5d4a3f2e1d
...
```

The same id is available to your server code on `request.state.request_id`, so
you can log it alongside your own messages and tie a user-reported error back to
a single request:

```python
@server
async def load_dashboard(request):
    rid = request.state.request_id
    logger.info("loading dashboard", extra={"request_id": rid})
    return {"widgets": await fetch_widgets()}
```

```python
from pyxle.runtime import ActionError

@action
async def charge(request, body: ChargeRequest):
    if not await payments.ok(body):
        # The id ends up in the response header too, so a user can quote it.
        raise ActionError(f"Payment failed (ref {request.state.request_id})")
    ...
```

A helper is also exported for code that may run before `request.state` is
populated:

```python
from pyxle.observability import get_request_id

rid = get_request_id(request)  # str, or None if observability is disabled
```

### Trusting an upstream id

By default Pyxle **generates a fresh id** and ignores any `X-Request-Id` the
client sent — echoing a client-supplied value into your logs is a
log-injection and spoofing vector. When Pyxle runs behind a trusted reverse
proxy or gateway that already stamps a request id, opt in so the id flows
through end to end:

```json
{
  "observability": {
    "trustIncomingRequestId": true
  }
}
```

Even then, an incoming id is only honoured if it is well-formed
(`[A-Za-z0-9._-]`, at most 128 characters); anything else is replaced with a
generated id.

### Custom header name

```json
{
  "observability": {
    "requestIdHeader": "X-Trace-Id"
  }
}
```

## Request timing

With `timing` enabled (the default), Pyxle records each request's wall-clock
duration. The measurement is taken at the outermost layer, so it reflects the
full time the framework spent on the request (to the point the response starts).
This timing feeds the metrics and structured-logging features; it is the
foundation the rest of this guide builds on.

## Metrics

Pyxle keeps an in-process registry of request, SSR-render, `@server`-loader, and
`@action` durations (as fixed-bucket histograms) plus page-cache hit/stale/miss
counts. Recording is always on and cheap — a handful of integer adds per request,
no per-request allocation, bounded memory.

Exposing those metrics is **opt-in** (the endpoint reveals internal state). Turn
on a Prometheus endpoint:

```json
{
  "observability": {
    "metricsEndpoint": true
  }
}
```

```
$ curl http://localhost:8000/api/__pyxle/metrics
# TYPE pyxle_requests_total counter
pyxle_requests_total{worker="40123"} 1843
# TYPE pyxle_cache_hit_ratio gauge
pyxle_cache_hit_ratio{worker="40123"} 0.92
# TYPE pyxle_render_duration_ms histogram
pyxle_render_duration_ms_bucket{worker="40123",le="50"} 1201
...
```

Guard it with a bearer token, and serve it on a custom path, if you like:

```json
{
  "observability": {
    "metricsEndpoint": true,
    "metricsEndpointPath": "/internal/metrics",
    "metricsEndpointToken": "from-an-env-var"
  }
}
```

With a token set, the endpoint requires `Authorization: Bearer <token>` (compared
in constant time). The page-cache metrics require the [server-side page
cache](caching.md), which runs under `pyxle serve`.

> **Multi-worker note.** Under `pyxle serve --workers N` each worker process keeps
> its own registry, so the endpoint reports that worker's numbers — every series
> carries a `worker="<pid>"` label. Aggregate across workers at your Prometheus
> scraper (e.g. `sum without (worker) (...)`). A shared cross-worker registry is
> planned for a later release.

## Health probes

Pyxle serves two probes for orchestrators (Kubernetes, load balancers):

- **`GET /healthz`** — *liveness*. Always `200` while the process is up; the body
  reports `status`, `uptime`, and a `ready` flag.
- **`GET /readyz`** — *readiness*. `200` once the server has warmed up **and**
  every dependency check passes, otherwise `503`. The body lists each check:

```
$ curl http://localhost:8000/readyz
{
  "status": "ok",
  "ready": true,
  "uptime": 123.4,
  "checks": { "ssr_pool": { "ok": true, "alive": 4, "size": 4 } },
  "metrics": { "requests_total": 1843, "cache_hit_ratio": 0.92 }
}
```

The dependency checks are fast attribute reads (never a network round-trip), so
the probe stays cheap. Today `/readyz` verifies the SSR worker pool has at least
one live worker; point your readiness probe at it so traffic is only routed to a
worker that can actually render.

## Turning it off

Request-id and timing are individually toggleable, and a bare `false` disables
both:

```json
{ "observability": false }
```

See the [configuration reference](../reference/configuration.md#observability)
for every key and default.
