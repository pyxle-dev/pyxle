# pyxle-auth

`pyxle-auth` is Pyxle's official authentication plugin: email + password accounts, sliding sessions, password-reset and email-verification flows, role-based access control, and scoped API tokens — wired into your app with one config entry. It never sends email and never renders UI; it gives you hardened primitives and stays out of your templates.

> **Version 0.2.0.** Runs on [pyxle-db](pyxle-db.md) (SQLite, PostgreSQL, or MySQL — the full account lifecycle is tested against real servers in CI), or on any database layer satisfying the `DatabaseLike` contract.

## Install

```bash
pip install pyxle-auth
```

This pulls in `pyxle-db` and `argon2-cffi`. There are no other dependencies.

## Quickstart

List `pyxle-db` **before** `pyxle-auth` in `pyxle.config.json` — the auth services run on the database that plugin opens:

```json
{
  "plugins": [
    "pyxle-db",
    "pyxle-auth"
  ]
}
```

That's the whole wire-up. At startup the plugin applies its bundled migrations (idempotent, checksum-tracked, in its own `schema_migrations_pyxle_auth` tracking table so they never collide with your app's migrations) and registers its services.

Protect a page with a guard in its `@server` loader:

```python
# pages/dashboard.pyxl — Python section
from pyxle.runtime import server
from pyxle_auth import require_user_page


@server
async def load(request):
    user = await require_user_page(request)   # 401 → error boundary when signed out
    return {"email": user.email, "plan": user.plan}
```

Sign-in must put a `Set-Cookie` header on the response, so it lives in an [API route](../guides/api-routes.md) — actions return plain JSON payloads and can't attach cookies:

```python
# pages/api/sign_in.py
from starlette.requests import Request
from starlette.responses import JSONResponse

from pyxle_auth import AuthError, RateLimited, get_auth_service


async def endpoint(request: Request) -> JSONResponse:
    body = await request.json()
    auth = get_auth_service()
    try:
        user, cookie = await auth.sign_in(
            email=body["email"],
            password=body["password"],
            ip=request.client.host,
            user_agent=request.headers.get("user-agent", ""),
        )
    except RateLimited as exc:
        return JSONResponse(
            {"ok": False, "error": str(exc)},
            status_code=429,
            headers={"Retry-After": str(exc.retry_after_seconds)},
        )
    except AuthError as exc:
        # InvalidCredentials and friends share one deliberately vague
        # message — don't replace it with something more "helpful".
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=401)

    response = JSONResponse({"ok": True, "userId": user.id})
    response.set_cookie(**cookie.kwargs())
    return response
```

`sign_up` has the same shape and also returns `(user, cookie)`. `sign_out(cookie_value=...)` returns a cookie that clears the browser's copy — set it the same way.

## Plugin services

| Service name | Type | What it does |
|---|---|---|
| `auth.service` | `AuthService` | Accounts, sessions, password flows. |
| `auth.rbac` | `RoleService` | Roles and permissions. |
| `auth.tokens` | `TokenService` | Single-use tokens (resets, invites, magic links). |
| `auth.api_tokens` | `ApiTokenService` | Long-lived `pyxle_pat_` bearer tokens. |
| `auth.settings` | `AuthSettings` | The resolved configuration. |

`get_auth_service()` and `get_auth_settings()` are typed shortcuts for the first and last.

## How sessions work

- The browser holds 32 random bytes (256-bit entropy) in an `HttpOnly` cookie — default name `pyxle_session`.
- The database stores only the **SHA-256 hash** of that value, so a leaked database cannot resurrect sessions.
- Sessions are **sliding**: each resolved request can extend the expiry (`resolve_session(cookie_value=..., extend=True)`), up to an absolute maximum age. Defaults: 30-day sliding window, 90-day absolute cap.
- `revoke_all_sessions(user_id=...)` signs out everywhere; `list_sessions` / `revoke_session` power a "manage devices" page. Password changes and resets revoke every session automatically.

## Session middleware, `request.user`, and `useAuth()`

Listing the plugin installs **`AuthSessionMiddleware`** automatically. On every request it resolves the session cookie and sets `request.user` (a `User`, or `None` when anonymous) — so loaders and actions can read the signed-in user without calling a guard. A request **without** the cookie does zero database work; one **with** it does a single indexed lookup that the guards then reuse, so a guarded loader never resolves the session twice.

The middleware also serves the endpoints the client [`useAuth()`](../reference/client-api.md#useauth) hook talks to, under `authPathPrefix` (default `/auth`):

| Endpoint | Purpose |
|---|---|
| `GET /auth/me` | The current user as JSON. |
| `POST /auth/login` | Sign in with `{ email, password }`. *(opt-out)* |
| `POST /auth/signup` | Create an account with `{ email, password }`. *(opt-out)* |
| `POST /auth/logout` | Revoke the session and clear the cookie. |

`/login` and `/signup` reuse `AuthService.sign_in` / `sign_up` — same rate limiting, same enumeration-safe errors — and map failures to status codes (`401` invalid credentials, `409` account exists, `422` weak password, `403` unverified email, `429` rate limited with `Retry-After`). They are state-changing POSTs, so the framework's CSRF protection applies; `useAuth` sends the token for you. Set `enableCredentialsApi: false` to turn them off and drive sign-in from your own `@action` instead (then call `useAuth().refresh()`); `/me` and `/logout` stay available.

```jsx
// A whole auth UI, client-side:
import { useAuth } from 'pyxle/client';

function Account() {
  const { user, isAuthenticated, login, logout } = useAuth();
  return isAuthenticated
    ? <button onClick={() => logout()}>Sign out {user.email}</button>
    : <button onClick={() => login({ email, password })}>Sign in</button>;
}
```

The signed-in user is **seeded into the server render** (`window.__PYXLE_AUTH__`), so `useAuth` shows the right state on the first frame — no flash of "logged out", no extra round-trip.

## Guards

Drop-in checks for loaders, actions, and API routes:

| Guard | Behaviour |
|---|---|
| `current_user(request)` | `User` or `None` — never raises. |
| `require_user_page(request)` | `User`, or raises `LoaderError(401)` for `@server` loaders. |
| `require_user_action(request)` | `User`, or raises `ActionError(401)` for `@action` handlers. |
| `require_permission_page(request, permission)` | User must hold the permission, else 401/403. |
| `require_permission_action(request, permission)` | Same, for actions. |
| `bearer_token(request)` | Extracts a `Bearer` token from the `Authorization` header, or `None`. |

The roadmap-named aliases `login_required` / `login_required_action` and `permission_required` / `permission_required_action` are the same functions as `require_user_*` / `require_permission_*` — call them at the top of a loader or action (Pyxle guards are awaited, not wrapping decorators):

```python
from pyxle_auth import login_required

@server
async def load(request):
    user = await login_required(request)   # raises LoaderError(401) when anonymous
    return {"email": user.email}
```

## Passwords

Hashing is **argon2id** via `argon2-cffi`. Defaults: time cost 3, memory 64 MiB, parallelism 2. In strict mode (the default) the settings refuse to construct with parameters below the floor (time cost ≥ 2, memory ≥ 19 MiB), so a typo'd config can't silently weaken hashing.

Two deliberate guards worth knowing about:

- Passwords longer than `passwordMaxLength` (default 1024) are rejected **before** hashing — unbounded input would make argon2 itself a denial-of-service vector.
- Sign-in burns the same argon2 verification cost whether or not the account exists, so response timing doesn't leak which emails are registered.

## Password reset and email verification

pyxle-auth never sends email. Flows that need delivery return a raw, single-use token exactly once; your app puts it in a link and hands it to whatever mailer it already uses:

```python
# pages/api/forgot_password.py
async def endpoint(request: Request) -> JSONResponse:
    body = await request.json()
    auth = get_auth_service()
    result = await auth.request_password_reset(
        email=body["email"], ip=request.client.host
    )
    if result is not None:
        user, token = result
        await my_mailer.send(
            to=user.email,
            subject="Reset your password",
            body=f"https://example.com/reset?token={token}",
        )
    # Same response whether the account exists or not — this endpoint
    # must not be usable to probe for accounts.
    return JSONResponse({"ok": True, "message": "Check your inbox."})
```

The user completes the flow with `reset_password(raw_token=..., new_password=...)`, which burns the token and revokes every session. Email verification mirrors the pattern: `request_email_verification(user_id=...)` returns a token, `confirm_email(raw_token=...)` redeems it. Both raise `InvalidToken` for anything stale, used, unknown, or wrong-purpose — indistinguishably.

Tokens are stored hashed (SHA-256), are single-use, and requesting again invalidates earlier tokens for the same purpose. The unknown-email path of `request_password_reset` performs the same committed token write a real account does, so even its timing doesn't enumerate accounts.

For your own flows (invite links, magic links), the same machinery is exposed as `auth.tokens`: `issue(purpose=..., user_id=..., ttl_seconds=...)`, `consume(purpose=..., raw_token=...)`, `sweep_expired()`.

## Roles and permissions

A small RBAC layer with wildcard grants:

```python
from pyxle.plugins import plugin

rbac = plugin("auth.rbac")

await rbac.define_role(name="admin", permissions=["projects.*", "billing.read"])
await rbac.grant_role(user_id=user.id, role_name="admin")

await rbac.has_permission(user_id=user.id, permission="projects.deploy")  # True
```

Permissions are dot-separated strings; a role can hold exact permissions, `"prefix.*"` wildcards, or the global `"*"`. Also available: `revoke_role`, `delete_role`, `roles_for`, `users_with_role`, and `permissions_for` (the user's full effective set). Granting twice is idempotent.

## API tokens

Long-lived bearer credentials for CLIs and CI, prefixed `pyxle_pat_` so secret scanners can recognise them:

```python
api_tokens = plugin("auth.api_tokens")

token, raw = await api_tokens.create(
    user_id=user.id,
    name="deploy bot",
    scopes=["deploy"],
    expires_in_days=90,            # optional — None means no expiry
    max_tokens_per_user=10,        # optional plan cap, enforced atomically
)
# `raw` is shown exactly once; only its hash is stored.

resolved = await api_tokens.resolve(raw_token=raw, required_scope="deploy")
```

`resolve` returns the token metadata only if the token exists, is not revoked or expired, and carries the required scope — otherwise `None`, with no reason disclosed. `list_for_user`, `revoke`, and `revoke_all` complete the lifecycle. When `max_tokens_per_user` is exceeded, `create` raises `TokenLimitReached`; the count-and-insert is race-safe on PostgreSQL and MySQL.

In an API route, pair it with the guard helper:

```python
raw = bearer_token(request)
token = raw and await api_tokens.resolve(raw_token=raw, required_scope="deploy")
if token is None:
    return JSONResponse({"error": "unauthorized"}, status_code=401)
```

## Rate limiting

Sign-in, sign-up, and password-reset are rate-limited per identifier (email and, when provided, IP) with hourly buckets stored in the database — no Redis required. Limits are settings (defaults: 10 sign-ins, 5 sign-ups, 3 resets per hour). One deliberate subtlety: a **correct** password is never blocked by the per-email bucket — only failed attempts count toward it — so an attacker hammering an inbox can't lock the real owner out of signing in.

## Settings

Configure in `pyxle.config.json` (camelCase), override per environment with `PYXLE_AUTH_*` variables. Precedence: **config > environment > default**.

```json
{
  "plugins": [
    "pyxle-db",
    {
      "name": "pyxle-auth",
      "settings": { "cookieName": "myapp_session", "sessionTtlSeconds": 1209600 }
    }
  ]
}
```

| Config key | Env variable | Default |
|---|---|---|
| `argonTimeCost` | `PYXLE_AUTH_ARGON_T` | `3` |
| `argonMemoryKib` | `PYXLE_AUTH_ARGON_M` | `65536` |
| `argonParallelism` | `PYXLE_AUTH_ARGON_P` | `2` |
| `passwordMinLength` | `PYXLE_AUTH_PW_MIN` | `8` |
| `sessionTtlSeconds` | `PYXLE_AUTH_SESSION_TTL` | `2592000` (30 d) |
| `sessionAbsoluteMaxSeconds` | `PYXLE_AUTH_SESSION_ABS_MAX` | `7776000` (90 d) |
| `cookieName` | `PYXLE_AUTH_COOKIE_NAME` | `"pyxle_session"` |
| `cookieSecure` | `PYXLE_AUTH_COOKIE_SECURE` | `true` |
| `cookieSameSite` | `PYXLE_AUTH_COOKIE_SAMESITE` | `"Lax"` |
| `cookieDomain` | `PYXLE_AUTH_COOKIE_DOMAIN` | unset |
| `authPathPrefix` | `PYXLE_AUTH_PATH_PREFIX` | `"/auth"` |
| `enableCredentialsApi` | `PYXLE_AUTH_ENABLE_CREDENTIALS_API` | `true` |
| `passwordResetTtlSeconds` | `PYXLE_AUTH_PASSWORD_RESET_TTL_SECONDS` | `1800` (30 min) |
| `emailVerifyTtlSeconds` | `PYXLE_AUTH_EMAIL_VERIFY_TTL_SECONDS` | `86400` (24 h) |
| `rateLimitSignInPerHour` | `PYXLE_AUTH_RL_SIGN_IN_PER_HOUR` | `10` |
| `rateLimitSignUpPerHour` | `PYXLE_AUTH_RL_SIGN_UP_PER_HOUR` | `5` |
| `rateLimitPasswordResetPerHour` | `PYXLE_AUTH_RATE_LIMIT_PASSWORD_RESET_PER_HOUR` | `3` |
| `requireEmailVerified` | `PYXLE_AUTH_REQUIRE_VERIFIED` | `false` |
| `strict` | `PYXLE_AUTH_STRICT` | `true` |

**Strict mode** is the production posture and the default: it requires `cookieSecure: true` and enforces the argon2 strength floors, refusing to boot otherwise. Local HTTP development relaxes it through the environment — never in the committed config:

```bash
# .env for local dev only
PYXLE_AUTH_STRICT=false
PYXLE_AUTH_COOKIE_SECURE=false
```

## Bring your own database

pyxle-auth binds to the **`db.database` plugin service**, not to the pyxle-db package. Any plugin that registers an object satisfying [`pyxle_db.DatabaseLike`](pyxle-db.md) can back it — an adapter over another engine, a test fake. The replacement must also translate unique-constraint violations into `pyxle_db.IntegrityError` (that's how duplicate sign-ups become `AccountExists`) and report a `dialect.name` the DDL helpers understand. The package's `tests/test_database_contract.py` runs the entire lifecycle against a deliberately foreign database object — it is both the executable specification and a template for writing an adapter.

## Errors

All inherit from `AuthError`:

| Exception | Meaning |
|---|---|
| `InvalidCredentials` | Wrong email/password — message intentionally vague. |
| `AccountExists` | Duplicate sign-up. |
| `WeakPassword` | Below `passwordMinLength` (or absurdly long). |
| `RateLimited` | Bucket exhausted; carries `retry_after_seconds`. |
| `EmailNotVerified` | Sign-in blocked while `requireEmailVerified` is on. |
| `InvalidToken` | Reset/verify token stale, used, unknown, or wrong-purpose. |
| `TokenLimitReached` | API-token cap hit. |
| `RoleNotFound` | Granting an undefined role. |

## OAuth sign-in (Google, GitHub, Discord)

Social sign-in ships in the `pyxle_auth.oauth` subpackage behind the `[oauth]`
extra (`pip install 'pyxle-auth[oauth]'`). Enable it with the `oauth` setting:

```json
{
  "plugins": [
    "pyxle-db",
    {
      "name": "pyxle-auth",
      "settings": {
        "oauth": {
          "providers": ["google", "github"],
          "failureRedirect": "/login"
        }
      }
    }
  ]
}
```

Client credentials are read **from the environment only** — never put them in
`pyxle.config.json`:

```bash
PYXLE_AUTH_OAUTH_GOOGLE_CLIENT_ID=...
PYXLE_AUTH_OAUTH_GOOGLE_CLIENT_SECRET=...
PYXLE_AUTH_SECRET=...   # signs the OAuth state cookie (required in strict mode)
```

A sign-in link is just an anchor to the start endpoint:

```jsx
<a href="/auth/oauth/google/start?next=/dashboard">Continue with Google</a>
```

The middleware redirects to the provider, handles the callback, creates or
links the local account, sets the session cookie, and sends the user to `next`.
On failure it redirects to `failureRedirect` with `?oauth_error=<reason>`
(`state`, `denied`, `email_unverified`, `exchange`, `unknown_provider`).

**Security model** — the callback is a `GET` carrying an attacker-influenceable
`?code&state`, so the defenses are deliberate:

- **PKCE `S256` is mandatory**; the verifier lives only in the signed cookie.
- A **signed, single-use, HttpOnly `state` cookie** binds the flow to the
  browser; the `state` the provider echoes must equal the cookie's nonce
  (constant-time) — this is the login-CSRF defense.
- An identity links to an existing account **only when the provider says the
  email is verified** — otherwise an attacker could pre-register the victim's
  address at the provider and hijack the account.
- `next` is **same-origin path only** (open-redirect guard).
- Secrets come from the environment, are redacted in `repr`, and never reach a
  log or the browser.

Set `redirectBaseUrl` when behind a reverse proxy / on a custom domain so the
`redirect_uri` matches what you registered with the provider.

## JWT for API & mobile clients

For clients that send `Authorization: Bearer` instead of a cookie, enable JWT
(`[jwt]` extra) with the `jwt` setting:

```json
{ "name": "pyxle-auth", "settings": { "jwt": { "accessTtlSeconds": 900 } } }
```

Two endpoints appear (sign with `PYXLE_AUTH_SECRET` / `PYXLE_SECRET_KEY`):

| Endpoint | Body | Returns |
|---|---|---|
| `POST /auth/token` | `{ email, password }` | `{ accessToken, refreshToken, expiresIn }` |
| `POST /auth/token/refresh` | `{ refreshToken }` | a rotated pair |

- **Access token** — a short-lived signed JWT (HS256), verified statelessly.
- **Refresh token** — a long-lived **opaque** string stored only as its hash.
  Refresh **rotates**: each use issues a new token and invalidates the old one.
  Replaying a rotated token **revokes the whole family** (theft detection).

> **CSRF:** these endpoints authenticate from the request body (not a cookie),
> so they aren't CSRF-vulnerable — but the framework's CSRF middleware still
> guards POSTs. Add `/auth/token` and `/auth/token/refresh` to
> `csrf.exempt_paths` so non-browser clients can reach them.

Resolve a bearer token in a loader or API route with the guards, which try
**JWT then PAT** (and `authenticate` tries the **session** first):

```python
from pyxle_auth import bearer_user, authenticate

user = await bearer_user(request)              # JWT access token → PAT
user = await authenticate(request)             # session → JWT → PAT
```

`JWTService` is also usable directly (`auth.jwt` service): `issue_pair`,
`verify_access`, `refresh`, `revoke_family`, `revoke_all_for_user`.

## What pyxle-auth is not (yet)

Honest scope, so you can plan around it:

- **No email delivery** — by design, permanently. Bring your mailer.
- **No multi-factor authentication** (TOTP, WebAuthn) — not implemented yet.

The building blocks (sessions, `TokenService`, guards) compose underneath whatever you add on top; contributions are welcome.

## See also

- [pyxle-db](pyxle-db.md) — the database layer underneath, and the `DatabaseLike` contract.
- [Plugins guide](../guides/plugins.md) — plugin ordering and service resolution.
- [API routes](../guides/api-routes.md) — where cookie-setting endpoints live.
- [Security guide](../guides/security.md) — CSRF and the framework's request protections.
