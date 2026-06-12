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

## What pyxle-auth is not (yet)

Honest scope, so you can plan around it:

- **No email delivery** — by design, permanently. Bring your mailer.
- **No OAuth / OIDC sign-in** (Google, GitHub) — not implemented yet.
- **No multi-factor authentication** (TOTP, WebAuthn) — not implemented yet.

The building blocks (sessions, `TokenService`, guards) compose underneath whatever you add on top; contributions are welcome.

## See also

- [pyxle-db](pyxle-db.md) — the database layer underneath, and the `DatabaseLike` contract.
- [Plugins guide](../guides/plugins.md) — plugin ordering and service resolution.
- [API routes](../guides/api-routes.md) — where cookie-setting endpoints live.
- [Security guide](../guides/security.md) — CSRF and the framework's request protections.
