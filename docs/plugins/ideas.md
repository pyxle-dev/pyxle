# Plugin Ideas

Capabilities the ecosystem needs, scoped to what the plugin API supports **today** — services and middleware (see [the plugins guide](../guides/plugins.md); page contribution is [Phase B, in RFC](rfc-plugin-pages.md)). Every idea below is buildable right now, and each names the contract it builds against. If you take one on, read the [standards](standards.md) first and check the [directory](/plugins) so two people don't race the same idea blind.

> **The first idea here shipped.** Mail — once *"the most wanted one"* on this list — is now the official [pyxle-mail](pyxle-mail.md) plugin (transactional email over SMTP, Resend, or the console). This list is where official plugins start.

Sizes are honest estimates for a working, standards-meeting v0.1: **S** ≈ a weekend, **M** ≈ a week of evenings, **L** ≈ a real project.

> **On names:** these are *capabilities*, not package names to grab — ship under **your own** PyPI name (`pyxle-*` is the [official namespace](standards.md), and a founding-cohort plugin may later be granted a canonical name by publishing under it). The `service.method` identifiers below are the suggested *API* a plugin registers, not its package.

## Storage (M)

File storage: `storage.client` with `put/get/delete/presign`, S3/R2/GCS behind extras, local-filesystem backend for dev (mirroring how pyxle-db treats SQLite as the zero-config default).
**Builds against:** the service registry; `env:` indirection for credentials.

## Caching (S)

A TTL cache service: `cache.get/set/delete` plus a `@cached(ttl=...)` helper for loader functions. In-memory backend by default, Redis behind an extra.
**Builds against:** the service registry.

## Observability (S)

Sentry (or OpenTelemetry) wiring: middleware that captures unhandled exceptions with request context, an `obs.capture` service for manual events, scrubbing rules for secrets. Small surface, huge production value.
**Builds against:** plugin `middleware()` contribution + the service registry.

## Rate limiting (S)

Reusable rate limiting: a middleware for path-pattern limits and a `ratelimit.check(key, scope)` service for in-action checks. Database-backed buckets via `DatabaseLike` (no Redis required), Redis behind an extra. pyxle-auth ships exactly this pattern internally — generalise it.
**Builds against:** [`pyxle_db.DatabaseLike`](pyxle-db.md), middleware contribution.

## Feature flags (S)

Feature flags on a `DatabaseLike` table: `flags.enabled(name, user_id=None)`, percentage rollouts, an `@server`-friendly API. Pairs naturally with pyxle-auth's `User`.
**Builds against:** `DatabaseLike`, optionally pyxle-auth's user model.

## Payments (M)

Stripe (or similar): `billing.service` for checkout/portal sessions and a **webhook-verification helper** (constant-time HMAC, timestamp skew, idempotency-key tracking via `DatabaseLike`) that apps call from their own API route. Webhook *handling* stays in the app; the hard, easy-to-get-wrong crypto lives in the plugin.
**Builds against:** the service registry, `DatabaseLike` for idempotency.

## Search (M)

Meilisearch/Typesense client as a service: index management, a `search.query` API shaped for loaders, reindex helpers. The docs-site search on pyxle.dev is hand-rolled today — this is the generalisation.
**Builds against:** the service registry.

## Background jobs (L)

A `DatabaseLike`-backed queue: `jobs.enqueue(name, payload, run_at=...)`, a worker entrypoint (`python -m <yourpkg>.worker`), retries with backoff, dead-letter table. The hardest and most valuable item on this list — talk to us before starting; it's a strong Phase B co-design seat.
**Builds against:** `DatabaseLike` (transactions, portable schema rules).

## OAuth / social login (L)

OAuth/OIDC sign-in helpers: provider services (`oauth.google`, `oauth.github`) implementing the code flow, state/PKCE handling, and a documented recipe for the app-side API routes that exchange the code and call `pyxle-auth` to establish the session. Fills pyxle-auth's [explicitly-out-of-scope](pyxle-auth.md) gap without forking it. UI pages stay in the app until [Phase B](rfc-plugin-pages.md).
**Builds against:** the service registry, pyxle-auth's session API.

---

Building one? Open a submission issue early (see [the directory](/plugins)) so it's visible — and so we can offer review while you build rather than after.
