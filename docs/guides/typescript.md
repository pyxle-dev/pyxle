# TypeScript

Pyxle gives you typed editor support and an opt-in type-check gate **without** asking you to write TypeScript in your `.pyxl` files. The model is **consumption, not authoring**: your server code is typed Python, your client code is plain JSX, and Pyxle generates the TypeScript declarations and config that make the framework surface fully typed in your editor.

## What "TypeScript support" means in Pyxle today

Three concrete things, all generated for you on every `pyxle dev` / `pyxle build`:

1. A **`tsconfig.json`** under `.pyxle-build/client/`, with strict mode and the right path aliases.
2. A full set of **`.d.ts` declarations** for every `pyxle/client` hook and component, so imports resolve to real types.
3. The **`pyxle typecheck`** command, which runs `tsc --noEmit` over the generated client so you can gate CI on type errors.

What it does **not** include is TypeScript *syntax* in the JSX block of a `.pyxl` file. The compiler emits the client component as `.jsx`, and esbuild (used by both the SSR runtime and Vite) loads it with its **JSX loader, not the TypeScript loader** — so type annotations, `interface`, generics, and `x as T` casts in a client block aren't transpiled. Pyxle flags them at **compile time** with a clear error pointing at your `.pyxl` file, rather than letting them fail later in the bundler (see [Limitations](#limitations)). In short: the client block is plain JSX that gets type-*checked*, never type-*annotated*.

## Server-side types (Python)

The `@server` and `@action` halves of a `.pyxl` file are ordinary Python — type them with normal Python hints. An `@action` body parameter can be annotated with a [Pydantic](https://docs.pydantic.dev/) model for automatic request validation, and `pyxle openapi` generates an OpenAPI 3.1 document straight from those models. This is independent of the client TypeScript toolchain below. See [Server actions → Validating request bodies](../core-concepts/server-actions.md#validating-request-bodies-with-pydantic).

## The generated client project

Every build writes a self-contained TypeScript project under `.pyxle-build/client/`. It is **framework-owned and regenerated each run** — don't edit it. It contains the `tsconfig.json`, the compiled `.jsx` pages, and a `pyxle/` folder of declarations:

| Declaration | Types |
|-------------|-------|
| `pyxle/index.d.ts` | the navigation runtime: `navigate`, `prefetch`, `refresh`, `invalidate`, `getRouter`, `PyxleRouter`, `NavigationOptions` (re-exports the component types below) |
| `pyxle/link.d.ts` | `Link`, `LinkProps` |
| `pyxle/image.d.ts` | `Image`, `ImageProps`, `ImageLoader` |
| `pyxle/head.d.ts` | `Head`, `HeadProps` |
| `pyxle/script.d.ts` | `Script`, `ScriptProps` |
| `pyxle/client-only.d.ts` | `ClientOnly`, `ClientOnlyProps` |
| `pyxle/slot.d.ts` | `Slot`, `SlotProvider`, `useSlot`, `useSlots`, slot types |
| `pyxle/use-action.d.ts` | `useAction<TData>`, `ActionInvoker`, `ActionResult`, `ActionFieldErrors` |
| `pyxle/use-pathname.d.ts` | `usePathname()` |
| `pyxle/use-auth.d.ts` | `useAuth()`, `PyxleUser`, `AuthResult` |
| `pyxle/use-websocket.d.ts` | `useWebSocket()`, `UseWebSocketResult`, `WebSocketStatus` |
| `pyxle/form.d.ts` | `Form`, `FormProps` |

## Typed framework imports

Because of those declarations, importing from `pyxle/client` gives you full completions, hover, and signature help:

```jsx
import { useAction, Link, Form } from 'pyxle/client';

export default function Page({ data }) {
  // useAction is generic in the action's return type:
  const subscribe = useAction('subscribe');
  return (
    <Form action="subscribe" onError={(message, fields) => console.log(fields)}>
      <input name="email" />
      <button disabled={subscribe.pending}>Subscribe</button>
    </Form>
  );
}
```

`useAction<TData>` is generic in the data your action returns. For Pydantic `422` validation errors, the `useAction` invoker exposes a reactive `fields` map, and `<Form>` delivers field errors to its `onError(message, fields)` callback. These types describe the **framework surface** — they do not infer the shape of your own loader's `data` (see [Limitations](#limitations)).

## The generated `tsconfig.json`

The scaffolded config turns on strict mode and wires the import aliases Pyxle uses:

```jsonc
{
  "compilerOptions": {
    "strict": true,
    "jsx": "react-jsx",
    "module": "ESNext",
    "moduleResolution": "Node",
    "allowJs": true,            // your pages are .jsx, checked against the .d.ts
    "skipLibCheck": true,
    "types": ["vite/client"],
    "baseUrl": ".",
    "paths": {
      "/pages/*": ["pages/*"],
      "/routes/*": ["routes/*"],
      "pyxle/client": ["pyxle/client"],
      "pyxle/client/*": ["pyxle/*"]
    }
  },
  "include": ["./client-entry.js", "./pages/**/*.jsx", "./pyxle/**/*"]
}
```

Two honest notes on coverage:

- `checkJs` is **not** enabled, so your own page JSX isn't deeply type-checked on its own. The real value is that **misuse of the typed framework imports is caught** — wrong props on `<Image>`, a bad `useAction` call, etc.
- `include` covers `./pages/**/*.jsx` but not `./routes/**/*.jsx`, so files under a `routes/` source root currently fall outside the type-check glob.

## `pyxle typecheck`

`pyxle typecheck` compiles your `.pyxl` files to `.jsx` and runs `tsc --noEmit` against the generated `tsconfig.json`, surfacing any type errors against the framework declarations. It looks for `tsc` in your local `node_modules/.bin`, then globally, then via `npx`, so install `typescript` (locally is recommended) to use it. It's the recommended CI gate for catching framework-API misuse. See the [CLI reference](../reference/cli.md).

## Editor setup

The [pyxle-langkit](editor-setup.md) language server runs a TypeScript language service over the JSX section of your `.pyxl` files, so you get completions and hover from the generated declarations as you type. It needs Node and `typescript` available in the project. See [Editor setup](editor-setup.md).

## Limitations

TypeScript support today is **consumption-only**. The following are not yet available and are tracked for a later phase — they are roadmap items, not bugs:

- **No TypeScript syntax in the client block.** The compiler emits `.jsx` and the bundlers use a JSX loader, so `const x: number = 1`, `interface`, generics, and `as` casts aren't supported in a `.pyxl` JSX block — keep the client half plain JSX. Pyxle's compiler flags TypeScript syntax there with a clear, source-located error.
- **No `.tsx` authoring.** You can't write the client half as full `.tsx`/TypeScript yet.
- **No per-page loader-type generation.** Your loader's `data` arrives on the client as an untyped object — there's no generated `.d.ts` describing a specific loader's return shape.
- **No OpenAPI → TypeScript client generation** from your action models.
- **No single `pyxle/client.d.ts` barrel.** Types are per-module plus `index.d.ts`; the `pyxle/client.js` barrel itself resolves as plain JS via `allowJs`.

These are tracked for a later phase. The strict `tsconfig` scaffold and the framework `.d.ts` set are delivered today; loader-type generation and `.tsx` authoring remain pending.

## Next steps

- Configure your editor: [Editor setup](editor-setup.md)
- Validate action inputs with types: [Server actions](../core-concepts/server-actions.md#validating-request-bodies-with-pydantic)
- CLI reference for `pyxle typecheck`: [CLI](../reference/cli.md)
