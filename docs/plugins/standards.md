# Plugin Standards

The bar a plugin must clear to be listed in the [pyxle.dev plugin directory](/plugins). It exists so that anything a Pyxle user installs from the directory behaves the way the official plugins do — predictable lifecycle, honest docs, fail-secure defaults. The official packages ([pyxle-db](pyxle-db.md), [pyxle-auth](pyxle-auth.md)) are held to every rule on this page; read their source when a rule's intent is unclear, it is the reference implementation of all of them.

> Community plugins are listed under their author's name and are not security-audited by the Pyxle team. The directory label says exactly that.

## Naming

- The bare `pyxle-<name>` namespace is the **official** namespace. We don't pre-register or squat names — an official plugin is published when it actually ships, with real code behind it. The namespace is protected by PyPI's [name-dispute process (PEP 541)](https://peps.python.org/pep-0541/): if someone uploads a confusing or malicious `pyxle-*` package, we dispute it on brand grounds, not by parking empty packages ahead of time.
- Community plugins ship under **your own PyPI name** — `acme-mail`, `mailpyx`, anything honest. The directory lists you under the name you chose; the name has no bearing on review.
- A founding-cohort plugin that clears the bar **may be granted a canonical `pyxle-<name>`** at the team's discretion — fulfilled by publishing your (real, working) plugin under that name, not by us holding it empty for you.
- Whatever you pick, don't imply officialness (`pyxle-core-*`, `pyxle-official-*` will be asked to rename).
- One plugin, one job. "Auth plus storage plus cron" is three plugins.

## Packaging

Everything on this list ships in the wheel — a user who runs `pip install` and never visits your repo still gets all of it:

- **`pyproject.toml`** with real metadata: description, license, classifiers, `requires-python`, project URLs. Depend on `pyxle-framework` with an honest floor (the version whose APIs you actually use).
- **`LICENSE`** file (OSI-approved) inside the package directory, not just the repo root.
- **`py.typed`** marker and full type hints on the public API.
- **`README.md`** with: a config-snippet quickstart a stranger can follow in two minutes, a settings table (key, default, what it does), and a *scope-honesty* section — what the plugin deliberately does **not** do. The official plugins' "What it is not (yet)" sections exist because unstated scope becomes angry issues.
- **`CHANGELOG.md`** (Keep a Changelog format) and semantic versioning. Pre-1.0 is fine — say so.
- Optional heavy dependencies go in **extras** (`pyxle-storage[s3]`), never in the base install.

## Behaviour

- Subclass `PyxlePlugin` and do real work in `on_startup` — open clients, validate settings, register services. **Fail loud at startup**: a misconfigured plugin should refuse to boot with an actionable message, not limp into requests.
- Register services under your **namespace**: `mail.service`, `storage.client` — and document every name you register. Consumers reach them via `plugin("mail.service")`.
- **Never monkey-patch Pyxle internals.** Use the public plugin API only. If the API can't do what you need, that's an issue (or an [RFC comment](rfc-plugin-pages.md)) — not a patch.
- Don't depend on sibling plugins unless the dependency *is the point* and is documented — the way `pyxle-auth` depends on `pyxle-db`. When you need a database, build against [`pyxle_db.DatabaseLike`](pyxle-db.md), not the concrete class, and say so.
- Settings come from the plugin's `settings` block in `pyxle.config.json`; **secrets come from the environment** (support `env:VAR` indirection or read env directly). A committed config file must never need to contain a credential.
- If your plugin owns database tables, ship checksum-tracked migrations with a namespaced tracking table (`tracking_table="schema_migrations_pyxle_<name>"`) so they coexist with the host app's migrations.

## Testing

- A real test suite, runnable with `pytest` from the package directory, green in CI on every supported Python version.
- Test the **plugin lifecycle**, not just the library: startup with valid/invalid settings, service registration, shutdown.
- If you claim support for an external system (a database, an email provider, a queue), back the claim with a **conformance test** that runs against the real thing in CI — service containers are cheap. A claim no test enforces will eventually be false.

## Security

- **Fail-secure defaults.** The lazy path must be the safe path; relaxation is explicit and documented (the way pyxle-auth requires `PYXLE_AUTH_STRICT=false` to weaken cookies for local dev).
- Credentials and tokens are hashed or encrypted at rest; raw values are shown once and never logged.
- Document your attack surface: what the plugin exposes, what it trusts, what the operator must protect.
- Provide a security contact in your README and respond to reports. Directory-listed plugins with an unpatched, reported vulnerability are delisted until fixed.

## Maintenance

A directory listing is a small ongoing promise:

- Keep CI green against the latest Pyxle minor (we announce breaking changes in the [changelog](../changelog.md); pre-1.0 minors may include them).
- Respond to security reports within a reasonable window.
- If you're done maintaining it, archive the repo and tell us — an honestly-archived plugin is respectable; a silently dead one gets delisted.

## The review

Submissions (see [the directory page](/plugins) for how) get a review against exactly this page — nothing unwritten. We check the packaging list mechanically, read the source for the behaviour and security sections, and run the suite. Outcome is a listing, a short list of blockers, or (rarely) a "this should be an app pattern, not a plugin." Reviews are public; expect honest notes.
