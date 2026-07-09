# Security

Pyxle includes CSRF protection, CORS support, and HEAD element sanitisation out of the box.

## CSRF protection

CSRF (Cross-Site Request Forgery) protection is **enabled by default** using a double-submit cookie pattern.

### How it works

1. Pyxle sets a CSRF cookie on every response. The default name is **namespaced by the app's bind port** — `pyxle-csrf-8000` for an app serving on port 8000
2. State-changing requests (`POST`, `PUT`, `PATCH`, `DELETE`) must echo the cookie value back, either in the `x-csrf-token` header or — for form-encoded submissions, such as a `<Form>` post before hydration — in a hidden `_csrf_token` form field
3. The framework validates that the submitted token matches the cookie using constant-time comparison
4. If they do not match, a `403 Forbidden` response is returned

The `<Form>` component and `useAction` hook handle this automatically.

### Why the cookie name carries the port

Browsers scope cookies to a **host, not a host+port**. With a fixed cookie name, two Pyxle apps on the same host — say two dev servers on `127.0.0.1:8000` and `127.0.0.1:8001` — would keep overwriting each other's token, and every action in the other app would fail with `403 CSRF token mismatch`. Namespacing the default name with the bind port (read from the ASGI connection scope) gives each app its own cookie. The bind port is stable per app in development *and* behind a production reverse proxy, where the public port (443) differs from the bind port but never varies per request.

Set `csrf.cookieName` to pin an explicit name instead — do this if you serve the same app from multiple ports and want one shared token, or if your production setup pre-renders pages with `pyxle build` and then serves them on a *different* port than `pyxle.config.json` declares (the pre-baked pages embed the cookie name). The same applies if you run the ASGI app over a **unix socket** (e.g. raw `uvicorn --uds`): with no bind port to namespace by, pin `cookieName` explicitly so the middleware and rendered pages always agree.

Upgrading from an older Pyxle: a leftover `pyxle-csrf` cookie is simply ignored — the token re-issues under the namespaced name on the next page load. No action needed.

### Form bodies and uploads

Pyxle never buffers an unbounded request body to find `_csrf_token`:

- `application/x-www-form-urlencoded` bodies are buffered up to 1 MiB; a larger body without the header token is rejected with `413`.
- `multipart/form-data` bodies are **stream-parsed only until the `_csrf_token` field is found** — file parts after the token are passed through to your handler untouched, never buffered by the CSRF layer. `<Form>` renders the hidden token field first, so an upload of any size works out of the box. The scan is capped at 1 MiB of pre-token data; if the field hasn't appeared by then (or isn't present at all), the request is rejected with `403` and a message telling you to send the token in the `x-csrf-token` header or move `_csrf_token` before the file fields.

### Configuration

```json
{
  "csrf": {
    "enabled": true,
    "cookieName": "my-app-csrf",
    "headerName": "x-csrf-token",
    "cookieSecure": false,
    "cookieSameSite": "lax",
    "exemptPaths": ["/api/webhooks"]
  }
}
```

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `enabled` | `boolean` | `true` | Enable/disable CSRF |
| `cookieName` | `string` | auto: `"pyxle-csrf-<port>"` | Cookie name; unset → namespaced by the bind port (see above) |
| `headerName` | `string` | `"x-csrf-token"` | Header name |
| `cookieSecure` | `boolean` | `false` | Set `Secure` flag (enable in production with HTTPS) |
| `cookieSameSite` | `string` | `"lax"` | `SameSite` attribute (`"strict"`, `"lax"`, or `"none"`) |
| `exemptPaths` | `string[]` | `[]` | Paths exempt from CSRF checks; an entry matches its exact path and anything beneath it at a `/` boundary (`/api/webhooks` does not exempt `/api/webhooks-admin`) — see [matching details](../reference/configuration.md#csrf) |

### Disabling CSRF

```json
{
  "csrf": false
}
```

Or with the object form:

```json
{
  "csrf": {
    "enabled": false
  }
}
```

### Token validation for custom fetch calls

If you make `POST` requests outside of `<Form>` or `useAction`, read the CSRF token from the cookie and include it as a header. Don't hardcode the cookie name — the default is port-namespaced, so resolve it from the `window.__PYXLE_CSRF_COOKIE__` global Pyxle injects into every page:

```javascript
function getCsrfToken() {
  const cookieName = window.__PYXLE_CSRF_COOKIE__ ?? 'pyxle-csrf';
  for (const part of document.cookie.split(';')) {
    const eq = part.indexOf('=');
    if (eq !== -1 && part.slice(0, eq).trim() === cookieName) {
      return decodeURIComponent(part.slice(eq + 1));
    }
  }
  return '';
}

await fetch('/api/data', {
  method: 'POST',
  headers: {
    'Content-Type': 'application/json',
    'x-csrf-token': getCsrfToken(),
  },
  body: JSON.stringify({ key: 'value' }),
});
```

A customised `csrf.headerName` is exposed the same way, as `window.__PYXLE_CSRF_HEADER__` (falling back to `x-csrf-token`). The globals always carry the *effective* names — including the auto port-namespaced cookie name, which client code cannot derive itself behind a reverse proxy.

## Signed cookies and tokens

`sign_cookie` and `verify_cookie` (importable from `pyxle`) attach a tamper-proof signature to a string and verify it later — the building block for a signed session id, a "remember me" cookie, or a stateless link such as an unsubscribe or password-reset URL. This is **signing, not encryption**: the value stays readable; a valid signature only proves it was produced by a holder of the secret.

```python
from pyxle import sign_cookie, verify_cookie

token = sign_cookie("user-42")          # "user-42.<hmac>"  (signs with PYXLE_SECRET_KEY)
verify_cookie(token)                    # "user-42"
verify_cookie("user-42.deadbeef")       # None  (bad signature → reject)
```

The signature is a full HMAC-SHA256 digest, compared in constant time. The secret is read from the `PYXLE_SECRET_KEY` environment variable, or passed explicitly as `secret_key=`. Signing is meaningless without a secret, so a missing one raises `MissingSecretKeyError` rather than returning an unprotected value — it **fails closed**. Import the exception from `pyxle.security` (it is not re-exported from the top-level `pyxle` package):

```python
from pyxle.security import MissingSecretKeyError
```

Use `salt=` to namespace signatures by purpose, so a token signed for one flow can't be replayed in another even under the same secret:

```python
reset = sign_cookie(email, salt="password-reset")
verify_cookie(reset, salt="login")      # None  (wrong salt → reject)
```

`verify_cookie` returns `None` on any signature or format failure. Because a validly-signed empty string is itself falsy, test the result with `is not None`, not truthiness:

```python
value = verify_cookie(token)
if value is not None:
    ...  # trust `value`
```

## CORS configuration

Configure Cross-Origin Resource Sharing for API access from other domains:

```json
{
  "cors": {
    "origins": ["https://app.example.com", "https://admin.example.com"],
    "methods": ["GET", "POST", "PUT", "DELETE"],
    "headers": ["Authorization", "Content-Type"],
    "credentials": true,
    "maxAge": 600
  }
}
```

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `origins` | `string[]` | `[]` | Allowed origins (CORS is disabled if empty) |
| `methods` | `string[]` | `["GET","POST","PUT","PATCH","DELETE","OPTIONS"]` | Allowed methods |
| `headers` | `string[]` | `[]` | Allowed request headers |
| `credentials` | `boolean` | `false` | Allow credentials (cookies, auth headers) |
| `maxAge` | `integer` | `600` | Preflight cache duration in seconds |

CORS is **disabled by default** (no `origins` configured). It is only active when you explicitly list allowed origins.

## Head element sanitisation

Dynamic head content (whether from a `<Head>` JSX block or the lower-level `HEAD` variable) is automatically sanitised to prevent XSS:

- **Tag allowlist**: only `<title>`, `<meta>`, `<link>`, `<script>`, and `<style>` may appear; any other tag (`<base>`, `<iframe>`, `<object>`, …) is dropped
- **Attribute escaping**: elements are parsed and rebuilt with every attribute value HTML-escaped, so a quote in dynamic data cannot break out of its attribute and inject markup
- **Title text escaping**: `<` and `>` inside `<title>` elements are escaped to `&lt;` and `&gt;`, and markup injected after a closing `</title>` is discarded
- **Event handler stripping**: `onclick`, `onerror`, `onload`, and all `on*` attributes are removed
- **Dangerous URL neutralisation**: `javascript:`, `vbscript:`, and `data:` URLs in `href`, `src`, and `action` attributes are emptied
- **Meta refresh rejected**: `<meta http-equiv="refresh">` is dropped as an open-redirect vector

The text content of inline `<script>`/`<style>` head elements is trusted author code and is **not** sanitised. Never interpolate user-supplied data into inline script or style content.

This sanitisation is applied to all head elements from every source — layout `<Head>` blocks, page `<Head>` blocks, and the legacy `HEAD` Python variable.

### Best practice

React already escapes text content by default when you interpolate into JSX, so the `<Head>` component inherits XSS protection automatically:

```jsx
import { Head } from 'pyxle/client';

export default function Page({ data }) {
  return (
    <>
      <Head>
        <title>{data.userSubmittedTitle}</title>
      </Head>
      {/* ... */}
    </>
  );
}
```

React escapes `data.userSubmittedTitle` in the text node, and Pyxle's head sanitiser provides a second layer of defense. You don't need `html.escape()` for JSX-driven head content.

For the lower-level `HEAD` Python variable with user-supplied data, escape explicitly:

```python
from html import escape

def HEAD(data):
    return f'<title>{escape(data["title"])}</title>'
```

## Security headers

In production, configure your reverse proxy (Nginx, Caddy, etc.) to add security headers:

```
X-Content-Type-Options: nosniff
X-Frame-Options: DENY
Referrer-Policy: strict-origin-when-cross-origin
Content-Security-Policy: default-src 'self'
```

## Rate limiting

Pyxle ships a built-in token-bucket rate limiter, off by default. Enable it from `pyxle.config.json` to cap how fast a single client can hit the app and blunt brute-force and scraping abuse:

```json
{
  "rateLimit": {
    "requests": 100,
    "window": 60,
    "exemptPaths": ["/health"]
  }
}
```

A client may burst up to `requests` and then sustains `requests / window` per second; over-limit requests get `429 Too Many Requests` with a `Retry-After` header. The store is in-memory and **per-process**, so for a single shared limit across `--workers` or hosts, rate-limit at your reverse proxy. Full details: [middleware guide](middleware.md#rate-limiting) and [configuration reference](../reference/configuration.md#rate-limiting).

## Environment variable safety

- Variables without the `PYXLE_PUBLIC_` prefix are **server-only** and never appear in client bundles
- Loader and action return values are serialised to JSON and sent to the client -- never include secrets in return values
- Use `.env.local` for secrets and add it to `.gitignore`

## Next steps

- Deploy to production: [Deployment](deployment.md)
- Full config reference: [Configuration](../reference/configuration.md)
