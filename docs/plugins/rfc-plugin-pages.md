# RFC: Plugin Routes & Pages (Phase B)

**Status: draft, open for comment.** This is the design discussion for the next phase of the plugin system — plugins contributing routes and, eventually, whole pages. Nothing here is shipped; signatures are sketches. Comment by opening an issue on [pyxle-dev/pyxle](https://github.com/pyxle-dev/pyxle/issues) titled `RFC(plugin-pages): …`. Authors in the [Founding Plugin Program](/plugins) co-design this — their build experience against today's API is the primary input.

## Where the line is today

Phase A (shipped, stable) lets a plugin run lifecycle hooks, register named services, and contribute ASGI **middleware** — see the [plugins guide](../guides/plugins.md). What it cannot do is own a URL: no API endpoints, no pages. Every route in a Pyxle app comes from the app's own `pages/` tree.

That line is why [pyxle-auth](pyxle-auth.md)'s quickstart still asks you to write `pages/api/sign_in.py` yourself, and why an admin-panel plugin — the Django-admin moment this framework should eventually have — can't exist yet.

## Phase B1 — plugin API routes (proposed, concrete)

The smaller, earlier slice: plugins contribute **API endpoints** (Starlette-style handlers — no `.pyxl` compilation, no client bundling).

```python
class PyxleAuthPlugin(PyxlePlugin):
    def routes(self) -> Sequence[RouteSpec]:
        return [
            RouteSpec("POST", "/sign-in",  self.sign_in_endpoint,
                      csrf="same-origin"),
            RouteSpec("POST", "/sign-out", self.sign_out_endpoint,
                      csrf="same-origin"),
        ]
```

Proposed rules, each open to challenge:

- **Namespacing by default.** Plugin routes mount under `/api/<plugin-shortname>/…` (`/api/auth/sign-in`). A host app can remount via config; collisions between two plugins are a startup error, not a silent override.
- **Host wins.** If the app's `pages/api/` tree defines the same path, the app's endpoint is used and a startup warning names the shadowed plugin route.
- **CSRF is declared, not assumed.** Each route declares its posture: `"token"` (default — framework CSRF applies), `"same-origin"` (Origin/Referer check for pre-auth endpoints that can't have a token yet), or `"exempt"` (signature-verified webhooks). The config's `csrf.exemptPaths` mechanism stays for app routes; plugin routes carry their posture with them so installing a plugin can't silently widen the app's CSRF holes.
- **Introspectable.** `pyxle routes` (CLI) lists every route with its origin — app file or plugin name — because debuggability is where mounted-route systems usually rot.

**What B1 unlocks immediately:** pyxle-auth ships its own sign-in/sign-up/sign-out/reset endpoints and its quickstart shrinks to config plus a `<Form>`; webhook-receiving plugins (Stripe) own their endpoint with the right CSRF posture out of the box.

## Phase B2 — plugin pages (design space, deliberately unresolved)

Plugins shipping real `.pyxl` pages — sign-in screens, admin panels. The hard questions, with current leanings:

1. **Compilation.** Host-build compiles plugin-shipped `.pyxl` sources (leaning: yes — plugins ship sources, the host's `pyxle build` discovers and compiles them; precompiled artifacts would freeze plugins to compiler versions).
2. **Bundling.** Plugin client code joins the host's Vite graph — one dedup'd React, plugin chunks hashed like app chunks. This is the riskiest area (version skew, CSS collisions) and needs a prototype before promises.
3. **Mounting & precedence.** Same model as B1: prefix-mounted by default (`/auth/sign-in`), host remounts via config, host pages shadow plugin pages with a warning.
4. **Layouts & theming.** Plugin pages render inside the host's root layout by default (they should look like *your* app, not the plugin author's). Styling contract — tokens/CSS variables vs. utility classes — is an open question; this is where founding-author input matters most.
5. **Security.** A plugin page runs the plugin's loaders/actions in your process; B2 doesn't change the trust model (installing a plugin always meant running its code at startup), but route-owning plugins make the audit surface more visible. The directory's review bar (see [standards](standards.md)) tightens for page-shipping plugins.

## Sequencing

- **B1** is targeted for an upcoming minor; it's mostly registry plumbing plus the CSRF-posture mechanism.
- **B2** lands after the founding cohort's feedback and a bundling prototype — a wrong API here would be very expensive to walk back, and we'd rather be slow than stuck.

## Open questions (comment on these)

1. Should B1 routes be able to declare middleware-style guards (auth required) declaratively, or is "call the guard in the handler" enough?
2. Route remounting config: per-plugin prefix, per-route overrides, or both?
3. For B2, do plugin pages get access to host services by name only (`plugin("db.database")`), or a declared-dependency mechanism with startup validation?
4. Is "host page shadows plugin page" ever the wrong default? (e.g. a security-critical plugin page an app shouldn't be able to accidentally replace.)
