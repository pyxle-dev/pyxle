# Installation

## Prerequisites

Pyxle requires:

- **Python 3.10+** (3.12 recommended)
- **Node.js 20.19+** (for Vite 7, React 19, and SSR). Node 18 is end-of-life and no longer supported.
- **npm** (ships with Node.js)

Verify your setup:

```bash
python --version   # Python 3.10 or later
node --version     # v20.19 or later
npm --version      # 10 or later
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

Node.js dependencies (React 19, Vite 7, and — if you opt in — Tailwind CSS v4) are installed per-project via `npm install` -- they are **not** global.

## Next steps

Create your first project: [Quick Start](quick-start.md)
