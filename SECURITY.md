# Security Policy

## Supported versions

Pyxle is pre-1.0. Only the latest released version receives security fixes — we
fix forward rather than backporting to older `0.x` releases.

| Version         | Supported |
| --------------- | --------- |
| latest release  | ✅        |
| older `0.x`     | ❌        |

## Reporting a vulnerability

**Please do not open a public issue for security reports.** Public disclosure
before a fix is available puts every user at risk.

Report privately through either channel:

- **GitHub private advisory (preferred):** [Report a vulnerability](https://github.com/pyxle-dev/pyxle/security/advisories/new) — this opens a private thread with the maintainers.
- **Email:** **security@pyxle.dev**

Please include:

- The affected version (`pyxle --version`) and environment (OS, Python, Node.js).
- A description of the issue and its impact.
- Steps to reproduce — a minimal proof of concept helps a lot.

You will get an acknowledgement within **72 hours**. For confirmed issues we aim
to ship a fix and publish an advisory within **14 days**; we will keep you
updated and credit you in the advisory unless you ask otherwise.

## Scope notes

Pyxle compiles `.pyxl` files to Python and JavaScript and renders React on the
server. Reports that are in scope include:

- **Server-side rendering escapes** — user-controlled data reaching the HTML
  document or `<Head>` without escaping, or SSR leaking server-only state
  (environment variables without the `PYXLE_PUBLIC_` prefix, loader/action
  internals) into the client bundle or serialized props.
- **CSRF** — bypasses of the built-in double-submit token protection on
  state-changing requests.
- **Path traversal** — reading or serving files outside the project's public
  and build directories.
- **Production error hygiene** — stack traces, file paths, or internal state
  appearing in production error responses (`debug=false`).
- **Compiler / dev-server** — code execution or file-write primitives reachable
  from untrusted `.pyxl` input or a malicious project on disk.

Out of scope: vulnerabilities in *your* application code (e.g. string-building
SQL, unescaped `dangerouslySetInnerHTML`), and issues that require an already
fully-compromised host.
