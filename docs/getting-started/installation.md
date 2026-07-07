# Installation

## Prerequisites

Pyxle requires:

- **Python 3.10+** (3.12 recommended)
- **Node.js 18+** (for Vite, React, and SSR)
- **npm** (ships with Node.js)

Verify your setup:

```bash
python --version   # Python 3.10 or later
node --version     # v18 or later
npm --version      # 9 or later
```

## Install Pyxle

Install Pyxle from PyPI:

```bash
pip install pyxle-framework
```

This installs the `pyxle` CLI and the framework runtime. Confirm it works:

```bash
pyxle --version
```

### Installing in a virtual environment (recommended)

```bash
python -m venv venv
source venv/bin/activate   # macOS / Linux
# venv\Scripts\activate    # Windows
pip install pyxle-framework
```

## What gets installed

The `pyxle` package includes:

| Component | Purpose |
|-----------|---------|
| `pyxle` CLI | Project scaffolding, dev server, build pipeline |
| `pyxle.runtime` | `@server` and `@action` decorators for your `.pyxl` files |
| `pyxle.config` | Configuration loading and validation |
| Starlette | ASGI web server (installed as a dependency) |
| Uvicorn | ASGI server runner (installed as a dependency) |
| `pyxle-langkit` | Language toolkit powering `pyxle check`'s JSX validation (installed as a dependency) |

Node.js dependencies (React, Vite, Tailwind) are installed per-project via `npm install` -- they are **not** global.

## Next steps

Create your first project: [Quick Start](quick-start.md)
