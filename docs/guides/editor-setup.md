# Editor setup

Pyxle Language Tools brings first-class IDE support to `.pyxl` files — syntax
highlighting, diagnostics, completions, hover documentation, go-to-definition,
and formatting. Everything works out of the box with a single install.

> **Beta release.** The language toolkit is production-usable but still
> evolving. [Report issues on GitHub.](https://github.com/pyxle-dev/pyxle-langkit/issues)

---

## VS Code (recommended)

### 1. Install the extension

Search for **"Pyxle"** in the VS Code Extensions panel, or install from
the terminal:

```bash
code --install-extension pyxle.pyxle-language-tools
```

The extension is also available on the
[VS Code Marketplace](https://marketplace.visualstudio.com/items?itemName=pyxle.pyxle-language-tools).

### 2. Install the language server

The VS Code extension is a thin client — the actual intelligence comes from
the `pyxle-langserver` process. Install it alongside your project:

```bash
pip install pyxle-langkit
```

Or install it as an optional extra with the framework:

```bash
pip install pyxle-framework[langkit]
```

That's it. Open any `.pyxl` file and you'll see the **✓ Pyxle** indicator
in the status bar when the language server connects.

---

## Features

### Syntax highlighting

`.pyxl` files get dual-language highlighting — Python sections are
highlighted as Python, JSX sections as JavaScript/React. The grammar
correctly handles template literals, so code snippets inside backtick
strings won't confuse the highlighter.

### Diagnostics

Errors and warnings appear inline as you type:

- **Python analysis** — powered by [pyflakes](https://pypi.org/project/pyflakes/)
  with 40+ rules. Catches undefined names, unused imports, syntax errors,
  unreachable code, and more.
- **JSX analysis** — powered by [Babel](https://babeljs.io/). Catches
  syntax errors in your JSX code.
- **Pyxle-specific rules** — validates `@server` loaders (async, request
  parameter, return statement), `@action` functions, `<Script>` and
  `<Image>` component props, `HEAD` element format, and default export
  requirements.

### Completions

Intelligent autocompletion for both sections:

- **Python** — powered by [Jedi](https://jedi.readthedocs.io/). Full
  module completions, function signatures, attribute access, import
  suggestions.
- **JSX** — powered by TypeScript's language service. React component
  props, DOM attributes, imported symbols, and local variables.
- **Pyxle-specific** — `@server` and `@action` decorator snippets,
  `HEAD` boilerplate, Pyxle component names (`<Link>`, `<Script>`,
  `<Image>`, `<Head>`, `<Slot>`, `<ClientOnly>`, `<Form>`), and
  `data.` property completions inferred from your loader's return
  statement.

### Hover documentation

Hover over any symbol to see its type and documentation:

- **Python symbols** — function signatures, docstrings, and type info
  from Jedi.
- **JSX symbols** — type information from TypeScript.
- **Pyxle decorators** — hover `@server` or `@action` for a quick
  reference of the decorator contract.
- **Pyxle components** — hover `Link`, `Script`, `Image`, etc. for
  prop documentation.

### Go-to-definition

**Cmd+Click** (macOS) or **Ctrl+Click** (Windows/Linux) to jump to
definitions:

- **Python symbols** — jump to function definitions, class declarations,
  and imported modules via Jedi.
- **JSX symbols** — jump to component definitions, imported functions,
  and type declarations via TypeScript.
- **Cross-section** — click `data.title` in JSX to jump to the
  `"title"` key in your `@server` loader's return statement.

### Document symbols

Open the **Outline** panel (Cmd+Shift+O) to see all symbols in the
current file: loaders, actions, functions, classes, and JSX exports.

### Formatting

Format `.pyxl` files with section-aware formatting:

- **Python sections** — formatted with [ruff](https://docs.astral.sh/ruff/)
  (or optionally [black](https://black.readthedocs.io/)).
- **JSX sections** — formatted with [prettier](https://prettier.io/).

Each section is formatted independently, preserving the file structure.

### Semantic highlighting

Enhanced syntax coloring beyond the TextMate grammar — decorators,
function parameters, built-in calls, and constants get distinct colors
based on AST analysis.

---

## Configuration

The extension exposes these settings (VS Code Settings → search "Pyxle"):

| Setting | Default | Description |
|---------|---------|-------------|
| `pyxle.langserver.command` | `pyxle-langserver` | Command to launch the language server |
| `pyxle.langserver.args` | `["--stdio"]` | Arguments for the language server |
| `pyxle.formatting.python` | `ruff` | Python formatter (`ruff`, `black`, or `none`) |
| `pyxle.formatting.jsx` | `prettier` | JSX formatter (`prettier` or `none`) |
| `pyxle.diagnostics.pyflakes` | `true` | Enable pyflakes diagnostics |
| `pyxle.diagnostics.react` | `true` | Enable Babel-based JSX analysis |

### Using a virtual environment

If `pyxle-langserver` is installed in a virtual environment, point the
extension to the full path:

```json
{
  "pyxle.langserver.command": "/path/to/venv/bin/pyxle-langserver"
}
```

---

## CLI tools

The language toolkit also provides command-line tools for CI pipelines
and scripting:

```bash
# Parse a .pyxl file and output its structure as JSON
pyxle-langkit parse pages/index.pyxl

# Lint a .pyxl file (exit code 1 on errors)
pyxle-langkit lint pages/index.pyxl

# Show the symbol outline
pyxle-langkit outline pages/index.pyxl

# Format a .pyxl file
pyxle-langkit format pages/index.pyxl

# Check if a file needs formatting (without modifying it)
pyxle-langkit format pages/index.pyxl --check
```

### Lint in CI

Add to your CI pipeline to catch issues before merge:

```bash
pip install pyxle-langkit
pyxle-langkit lint pages/*.pyxl
```

---

## Other editors

The language server speaks the standard
[Language Server Protocol](https://microsoft.github.io/language-server-protocol/)
and works with any LSP-compatible editor.

### Neovim

Using [nvim-lspconfig](https://github.com/neovim/nvim-lspconfig):

```lua
vim.api.nvim_create_autocmd("FileType", {
  pattern = "pyxle",
  callback = function()
    vim.lsp.start({
      name = "pyxle-langserver",
      cmd = { "pyxle-langserver", "--stdio" },
      root_dir = vim.fs.dirname(
        vim.fs.find({ "pyxle.config.json", "pyproject.toml" }, { upward = true })[1]
      ),
    })
  end,
})
```

You'll also need to register the `.pyxl` filetype:

```lua
vim.filetype.add({ extension = { pyxl = "pyxle" } })
```

### Sublime Text

Using [LSP](https://github.com/sublimelsp/LSP):

```json
{
  "clients": {
    "pyxle": {
      "enabled": true,
      "command": ["pyxle-langserver", "--stdio"],
      "selector": "source.pyxle"
    }
  }
}
```

### JetBrains IDEs

JetBrains IDEs have limited LSP support. A dedicated plugin is planned
for a future release.

---

## Troubleshooting

### "pyxle-langserver: command not found"

The language server isn't on your PATH. Either:
- Install globally: `pip install pyxle-langkit`
- Or set the full path in VS Code settings:
  `"pyxle.langserver.command": "/path/to/venv/bin/pyxle-langserver"`

### No JSX completions

JSX completions require **Node.js** and **TypeScript** installed in your
project's `node_modules`. The language server spawns a TypeScript
language service worker for JSX intelligence.

```bash
npm install typescript
```

### No Python completions

Python completions are powered by Jedi, which is installed automatically
with `pyxle-langkit`. If completions are missing, verify the install:

```bash
python -c "import jedi; print(jedi.__version__)"
```

### Status bar shows "✗ Pyxle"

The language server failed to start. Check the **Output** panel in
VS Code (View → Output → select "Pyxle Language Server") for error
details.

### Formatting doesn't change anything

Formatting requires the formatter binaries to be on your PATH:
- Python: `pip install ruff` (or `pip install black`)
- JSX: `npm install -g prettier` (or install in your project)

---

## Architecture

The language toolkit has a server-driven architecture — all intelligence
lives in the Python LSP server:

- **Python analysis** — [Jedi](https://jedi.readthedocs.io/) runs
  in-process for completions, hover, and go-to-definition.
- **JSX analysis** — An embedded TypeScript language service runs as a
  Node.js subprocess, communicating via NDJSON over stdin/stdout.
- **Diagnostics** — [pyflakes](https://pypi.org/project/pyflakes/) for
  Python static analysis, [Babel](https://babeljs.io/) for JSX
  validation, plus Pyxle-specific rules.
- **No virtual files** — unlike some language tools that write temporary
  files to disk, Pyxle's language server keeps everything in memory.

The VS Code extension is a thin LSP client (~100 lines of TypeScript)
that connects to the server and manages the status bar. All the
intelligence is in the server, making it easy to support other editors.

Source code: [github.com/pyxle-dev/pyxle-langkit](https://github.com/pyxle-dev/pyxle-langkit)
