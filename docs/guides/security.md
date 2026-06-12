# Security

Pyxle includes CSRF protection, CORS support, and HEAD element sanitisation out of the box.

## CSRF protection

CSRF (Cross-Site Request Forgery) protection is **enabled by default** using a double-submit cookie pattern.

### How it works

1. Pyxle sets a `pyxle-csrf` cookie on every response
2. State-changing requests (`POST`, `PUT`, `PATCH`, `DELETE`) must echo the cookie value back, either in the `x-csrf-token` header or — for form-encoded submissions, such as a `<Form>` post before hydration — in a hidden `_csrf_token` form field
3. The framework validates that the submitted token matches the cookie using constant-time comparison
4. If they do not match, a `403 Forbidden` response is returned

The `<Form>` component and `useAction` hook handle this automatically.

Pyxle buffers at most 1 MB of a form-encoded body to find `_csrf_token`. A larger `application/x-www-form-urlencoded` or `multipart/form-data` body that doesn't carry the header token is rejected with `413` — for large uploads, send the token via the `x-csrf-token` header instead.

### Configuration

```json
{
  "csrf": {
    "enabled": true,
    "cookieName": "pyxle-csrf",
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
| `cookieName` | `string` | `"pyxle-csrf"` | Cookie name |
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

If you make `POST` requests outside of `<Form>` or `useAction`, read the CSRF token from the cookie and include it as a header:

```javascript
function getCsrfToken() {
  const match = document.cookie.match(/pyxle-csrf=([^;]+)/);
  return match ? match[1] : '';
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

If you've customised `csrf.cookieName` or `csrf.headerName`, use your configured names instead — Pyxle also injects non-default names into every page as `window.__PYXLE_CSRF_COOKIE__` and `window.__PYXLE_CSRF_HEADER__`, so custom code can read those globals (falling back to `pyxle-csrf` / `x-csrf-token`) rather than hardcoding.

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

## Environment variable safety

- Variables without the `PYXLE_PUBLIC_` prefix are **server-only** and never appear in client bundles
- Loader and action return values are serialised to JSON and sent to the client -- never include secrets in return values
- Use `.env.local` for secrets and add it to `.gitignore`

## Next steps

- Deploy to production: [Deployment](deployment.md)
- Full config reference: [Configuration](../reference/configuration.md)
