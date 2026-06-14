# pyxle-mail

`pyxle-mail` is Pyxle's official email plugin: one `mail.service` your loaders, actions, and API routes call as `await mail.send(...)`, delivered over SMTP, [Resend](https://resend.com), or any backend satisfying the `MailProvider` contract. You write the send once and choose the provider by config, not code. With no configuration it **logs the email instead of sending it**, so local development works with zero setup.

> **Version 0.1.0.** SMTP and the console provider need nothing beyond the package; Resend ships as an extra. It depends on no other plugin — no database, no auth — so its position in the `plugins` list doesn't matter.

## Install

```bash
pip install pyxle-mail                # SMTP + console, zero extra deps
pip install "pyxle-mail[resend]"      # + httpx, for the Resend provider
```

## Quickstart

Add the plugin to `pyxle.config.json` (see the [plugins guide](../guides/plugins.md) for how plugin loading works):

```json
{
  "plugins": ["pyxle-mail"]
}
```

That's the whole wire-up. At startup the plugin builds a provider from your settings and registers the `mail.service`. With no configuration it registers the **console** provider — it logs a summary and sends nothing, so there's no account, key, or SMTP server to stand up for local work.

Send from any loader, action, or API route through `get_mail_service()`:

```python
from pyxle.runtime import action
from pyxle_mail import get_mail_service


@action
async def invite(request):
    await get_mail_service().send(
        to="user@example.com",
        subject="You're invited",
        html="<p>Welcome aboard.</p>",
        text="Welcome aboard.",
    )
    return {"ok": True}
```

Configure a provider (below) when you want real delivery.

## Plugin services

The plugin registers two services:

| Service name | Type | What it does |
|---|---|---|
| `mail.service` | `MailService` | The consumer-facing surface — builds, validates, and delivers messages. |
| `mail.settings` | `MailSettings` | The resolved configuration. |

`get_mail_service()` is the typed shortcut for the first; it raises `pyxle.plugins.PluginServiceError` if `pyxle-mail` isn't in your `plugins` list.

## Providers

A provider is what actually puts a message on the wire. The service wraps one provider and is what your app calls; swapping providers is a config change, never a code change.

| Provider | When | Needs |
|---|---|---|
| `console` | Local dev, dry-run — logs a summary, sends nothing. **The default.** | nothing |
| `smtp` | Any mail server (Gmail, Fastmail, MailHog, a corporate relay). | host (+ usually user/password) |
| `resend` | [Resend](https://resend.com) — modern transactional API. | `pyxle-mail[resend]` + API key |

`dryRun: true` forces the console provider regardless of `provider` — the safe switch for staging or a first deploy, where you want the wiring exercised but no mail to actually leave.

### Example: Resend in production

```bash
PYXLE_MAIL_PROVIDER=resend
PYXLE_MAIL_RESEND_API_KEY=re_...
PYXLE_MAIL_FROM=hello@yourdomain.com
PYXLE_MAIL_FROM_NAME="Your App"
```

Resend requires a verified sender domain (SPF/DKIM DNS records) before it will deliver from anything but its test address.

## Sending mail

```python
mail = get_mail_service()

result = await mail.send(
    to=["a@example.com", "b@example.com"],   # str or list
    subject="Monthly digest",
    html="<h1>Hi</h1>",
    text="Hi",                               # at least one of html / text
    reply_to="support@yourdomain.com",
    cc="manager@example.com",                # str or list
    headers={"List-Unsubscribe": "<https://you/u?t=…>"},   # one-click unsubscribe
)
result.message_id  # the provider's id (Resend id, SMTP Message-ID, …)
result.provider    # "console" | "smtp" | "resend" | a community name
result.accepted    # the recipients the provider took responsibility for
```

The sender (`from_address` / `from_name`) and `reply_to` fall back to the configured defaults, so most calls pass only `to`, `subject`, and a body. Recipients are validated and normalised before they reach the provider.

`send` raises `InvalidMessage` for a bad message — no recipient, no body, an empty subject, or a malformed address — *before* any network call, and `SendError` if the provider rejects the message or the transport fails. Both inherit `MailError`.

When you'd rather build the message yourself, `send_message(EmailMessage)` is the lower-level entry point; it applies the default sender the same way.

## The MailProvider contract

`pyxle-mail`'s swappable piece is the **provider** — the analogue of pyxle-db's `DatabaseLike`. Any object satisfying `pyxle_mail.MailProvider` can back the service: the bundled SMTP/Resend/console providers, or a community adapter for SendGrid, Mailgun, SES, Postmark, and so on. The protocol has two members:

```python
from pyxle_mail import EmailMessage, MailProvider, SendError, SendResult


class MyProvider:
    name = "myprovider"   # short id, used in logs and SendResult

    async def send(self, message: EmailMessage) -> SendResult:
        # message.from_address is already filled from settings.
        # Deliver it, or raise SendError on rejection / transport failure.
        ...
        return SendResult(message_id="…", provider=self.name, accepted=message.to)
```

The protocol is `runtime_checkable`, so `isinstance(obj, MailProvider)` verifies the members are present (signatures are checked statically):

```python
assert isinstance(MyProvider(), MailProvider)
```

`EmailMessage` and `SendResult` are both frozen and provider-agnostic, so the same message round-trips through any provider unchanged.

## Settings

Configure in `pyxle.config.json` (camelCase), override per environment with `PYXLE_MAIL_*` variables. Precedence: **config > environment > default**. Keep secrets (SMTP password, Resend API key) in the environment, never in the committed config.

```json
{
  "plugins": [
    {
      "name": "pyxle-mail",
      "settings": { "provider": "smtp", "smtpHost": "smtp.example.com", "fromAddress": "hello@example.com" }
    }
  ]
}
```

| Config key | Env variable | Default | What it does |
|---|---|---|---|
| `fromAddress` | `PYXLE_MAIL_FROM` | — | Default sender. **Required** for any real provider. |
| `fromName` | `PYXLE_MAIL_FROM_NAME` | — | Optional display name. |
| `replyTo` | `PYXLE_MAIL_REPLY_TO` | — | Default `Reply-To`. |
| `provider` | `PYXLE_MAIL_PROVIDER` | `"console"` | `console` \| `smtp` \| `resend`. |
| `dryRun` | `PYXLE_MAIL_DRY_RUN` | `false` | Force the console provider regardless of `provider`. |
| `smtpHost` | `PYXLE_MAIL_SMTP_HOST` | — | SMTP server host. |
| `smtpPort` | `PYXLE_MAIL_SMTP_PORT` | `587` | SMTP port. |
| `smtpUsername` | `PYXLE_MAIL_SMTP_USERNAME` | — | SMTP auth user. |
| `smtpPassword` | `PYXLE_MAIL_SMTP_PASSWORD` | — | SMTP auth password (**env only**). |
| `smtpUseTls` | `PYXLE_MAIL_SMTP_TLS` | `true` | STARTTLS (port 587). |
| `smtpUseSsl` | `PYXLE_MAIL_SMTP_SSL` | `false` | Implicit TLS (port 465). |
| `resendApiKey` | `PYXLE_MAIL_RESEND_API_KEY` | — | Resend API key (**env only**). |

A misconfigured real provider **fails loud at startup**, not on the first send: `resend` with no key, `smtp` with no host, or any real provider with no `fromAddress` refuses to boot. Unknown settings keys are rejected too, so a typo can't silently take the default.

## Errors

All inherit from `MailError`:

| Exception | Raised when |
|---|---|
| `MailError` | Base class for everything below. |
| `MailConfigError` | Unknown provider, or a real provider missing its key/host/from-address. |
| `InvalidMessage` | No recipient, no body, empty subject, or a malformed address. |
| `SendError` | The provider rejected the message, or the transport failed. |

## What pyxle-mail is not

Honest scope, so you can plan around it:

- **Not a newsletter / campaign system.** It sends transactional and one-off mail. Audiences, scheduling, and analytics are out of scope.
- **Not a template engine.** Pass rendered HTML/text — use Pyxle components, Jinja, or f-strings to produce it.
- **Not a queue.** A `send` awaits the provider. For high volume or retries, enqueue with a background-jobs plugin and send from the worker.

## See also

- [pyxle-auth](pyxle-auth.md) — its password-reset and email-verification flows hand you a token *for your mailer*; this is that mailer.
- [Plugin standards](standards.md) — the bar every official plugin meets.
- [Plugins guide](../guides/plugins.md) — how plugin loading, ordering, and services work.
- [API routes](../guides/api-routes.md) — where cookie-setting endpoints live.
- The [plugins directory](/plugins) — every official and community plugin.
</content>
</invoke>
