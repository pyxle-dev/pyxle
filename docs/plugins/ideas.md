# Plugin Ideas

Plugins the ecosystem needs, scoped to what the plugin API supports **today** — services and middleware (see [the plugins guide](../guides/plugins.md); page contribution is [Phase B, in RFC](rfc-plugin-pages.md)). Every idea below is buildable right now, and each names the contract it builds against. If you take one on, read the [standards](standards.md) first and check the [directory](/plugins) so two people don't race the same idea blind.

Sizes are honest estimates for a working, standards-meeting v0.1: **S** ≈ a weekend, **M** ≈ a week of evenings, **L** ≈ a real project.

> **On the names:** the canonical `pyxle-<name>` titles below are [reserved on PyPI](standards.md) as documented placeholders. Build under **your own package name**; a plugin accepted into the Founding Program may be granted the canonical name along with the badge.

## pyxle-mail — *the most wanted one* (M)

Email delivery as a service: `mail.service` with `send(to, subject, html, text)`, providers behind extras (`[smtp]`, `[resend]`, `[ses]`), per-environment dry-run mode that logs instead of sending.

**Why it matters:** [pyxle-auth](pyxle-auth.md) deliberately never sends email — its password-reset and verification flows return a raw token for *your mailer*. Today every app hand-rolls that mailer. `pyxle-mail` completes the story: `pyxle-db` + `pyxle-auth` + `pyxle-mail` would be a full accounts stack in three config lines.
**Builds against:** the plugin service registry; consumed alongside pyxle-auth's token flows.

## pyxle-storage (M)

File storage: `storage.client` with `put/get/delete/presign`, S3/R2/GCS behind extras, local-filesystem backend for dev (mirroring how pyxle-db treats SQLite as the zero-config default).
**Builds against:** the service registry; `env:` indirection for credentials.

## pyxle-cache (S)

A TTL cache service: `cache.get/set/delete` plus a `@cached(ttl=...)` helper for loader functions. In-memory backend by default, Redis behind an extra.
**Builds against:** the service registry.

## pyxle-observability (S)

Sentry (or OpenTelemetry) wiring: middleware that captures unhandled exceptions with request context, an `obs.capture` service for manual events, scrubbing rules for secrets. Small surface, huge production value.
**Builds against:** plugin `middleware()` contribution + the service registry.

## pyxle-ratelimit (S)

Reusable rate limiting: a middleware for path-pattern limits and a `ratelimit.check(key, scope)` service for in-action checks. Database-backed buckets via `DatabaseLike` (no Redis required), Redis behind an extra. pyxle-auth ships exactly this pattern internally — generalise it.
**Builds against:** [`pyxle_db.DatabaseLike`](pyxle-db.md), middleware contribution.

## pyxle-flags (S)

Feature flags on a `DatabaseLike` table: `flags.enabled(name, user_id=None)`, percentage rollouts, an `@server`-friendly API. Pairs naturally with pyxle-auth's `User`.
**Builds against:** `DatabaseLike`, optionally pyxle-auth's user model.

## pyxle-stripe (M)

Payments: `billing.service` for checkout/portal sessions and a **webhook-verification helper** (constant-time HMAC, timestamp skew, idempotency-key tracking via `DatabaseLike`) that apps call from their own API route. Webhook *handling* stays in the app; the hard, easy-to-get-wrong crypto lives in the plugin.
**Builds against:** the service registry, `DatabaseLike` for idempotency.

## pyxle-search (M)

Meilisearch/Typesense client as a service: index management, a `search.query` API shaped for loaders, reindex helpers. The docs-site search on pyxle.dev is hand-rolled today — this is the generalisation.
**Builds against:** the service registry.

## pyxle-jobs (L)

Background jobs with a `DatabaseLike`-backed queue: `jobs.enqueue(name, payload, run_at=...)`, a worker entrypoint (`python -m pyxle_jobs.worker`), retries with backoff, dead-letter table. The hardest and most valuable item on this list — talk to us before starting; it's a strong Phase B co-design seat.
**Builds against:** `DatabaseLike` (transactions, portable schema rules).

## pyxle-oauth (L)

OAuth/OIDC sign-in helpers: provider services (`oauth.google`, `oauth.github`) implementing the code flow, state/PKCE handling, and a documented recipe for the app-side API routes that exchange the code and call `pyxle-auth` to establish the session. Fills pyxle-auth's [explicitly-out-of-scope](pyxle-auth.md) gap without forking it. UI pages stay in the app until [Phase B](rfc-plugin-pages.md).
**Builds against:** the service registry, pyxle-auth's session API.

---

Claimed one? Open a submission issue early (see [the directory](/plugins)) so it's visible — and so we can offer review while you build rather than after.
