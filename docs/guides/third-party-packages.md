# Third-party packages

A Pyxle project has **two** dependency sets: Python packages (used by your
`@server` loaders, `@action` mutations, and `pages/api/*.py` endpoints) and Node
packages (used by your React/JSX components). Add to whichever side you need.

## Python packages (pip)

Add the package to `requirements.txt` and install:

```bash
echo "pydantic>=2" >> requirements.txt
pip install -r requirements.txt
```

Import it in the **Python** section of a `.pyxl` file (above the first JS
`import`) or in a `pages/api/*.py` module:

```python
from pydantic import BaseModel
```

## Node packages (npm)

Install with npm and import it in the **JSX** section of a `.pyxl` file:

```bash
npm install zustand
```

```jsx
import { create } from 'zustand';
```

Vite bundles it for the browser and Pyxle's SSR runtime bundles it for the
server, so most packages "just work" on both. Prefer packages with **SSR
support** (they render the same markup on the server and client) — a
browser-only package should be used inside a `useEffect`, an event handler, or a
`<ClientOnly>` boundary. See [Client Components](client-components.md).

Run both installers at once with `pyxle install`.

### The import alias

`pyxle init` sets up an import alias (default `@/*`) in `jsconfig.json`, and
Pyxle wires the same alias into the Vite config **and** the SSR runtime. So
`@/lib/format` resolves to `lib/format.js` from anywhere, on both server and
client:

```jsx
import { formatDate } from '@/lib/format';
```

## shadcn/ui

[shadcn/ui](https://ui.shadcn.com) is a collection of components you copy into
your project (not an installed dependency). Enable it when you scaffold:

```bash
pyxle init my-app --shadcn        # implies Tailwind
# or answer "y" to the shadcn prompt in interactive init
```

The scaffold pre-configures everything shadcn needs — `components.json`,
`jsconfig.json` (the `@` alias), `lib/utils.js`, and a Tailwind v4 stylesheet
with the shadcn theme tokens — so you **don't** need to run `shadcn init`. Add
components directly:

```bash
cd my-app
npm install
npx shadcn@latest add button
```

This drops `components/ui/button.jsx` into your project (JavaScript, not
TypeScript). Import it via the alias and use it in any page:

```jsx
// pages/index.pyxl (JSX section)
import { Button } from '@/components/ui/button';

export default function Home() {
  return <Button>Click me</Button>;
}
```

`pyxle build` bundles it for production and it renders server-side like any
other component.

> **Verified flow.** The steps above were verified end to end on Node 22 with
> `shadcn@latest`: scaffold with `--shadcn` → `npm install` → `npx shadcn@latest
> add button` → import via `@/components/ui/button` → `pyxle build` +
> `pyxle serve`. Because the scaffold ships a ready `components.json`, running
> `shadcn init` is unnecessary — it would only offer to overwrite the config the
> scaffold already wrote.

### Adding shadcn to an existing project

If you scaffolded without it, enable Tailwind first (see
[Styling](styling.md#adding-tailwind-to-an-existing-project)), then create a
`components.json` at your project root (`npx shadcn@latest init` can generate it
in **JavaScript** mode — answer *no* to TypeScript), pointing its
`tailwind.css` at your CSS entry (`pages/styles/app.css`) and its aliases at
your import alias.

## Next steps

- Styling and Tailwind: [Styling](styling.md)
- Browser-only libraries: [Client Components](client-components.md)
- Validate action bodies with Pydantic: [Server Actions](../core-concepts/server-actions.md)
