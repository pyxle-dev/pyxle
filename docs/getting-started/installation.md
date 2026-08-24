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
python3 -m venv venv && source venv/bin/activate   # PEP 668: needed on Debian/Ubuntu
pip install pyxle-framework
```

This installs the `pyxle` CLI and the framework runtime. Confirm it works:

```bash
pyxle --version
```

### Virtual environments in detail

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

## Troubleshooting

These failures all come from an incomplete or out-of-date toolchain. Pyxle needs **Python 3.10+** and **Node.js 20.19+ with npm**.

### `pip install` says "No matching distribution found for pyxle-framework"

```
ERROR: Could not find a version that satisfies the requirement pyxle-framework (from versions: none)
ERROR: No matching distribution found for pyxle-framework
```

Your `pip` is running on **Python older than 3.10** (the stock `python3` on macOS is 3.9). Pyxle ships no wheels for end-of-life Pythons, so pip reports "none available." Check your version, then install with a supported interpreter:

```bash
python3 --version          # must be 3.10 or later
```

Install a supported Python (via [python.org](https://www.python.org/downloads/), `pyenv`, or Homebrew's `python@3.12`) and create the virtual environment with **that** interpreter:

```bash
python3.12 -m venv venv && source venv/bin/activate
pip install pyxle-framework
```

### `pip install` says "error: externally-managed-environment"

```
error: externally-managed-environment
× This environment is externally managed
```

Debian and Ubuntu (24.04 and newer), and some Homebrew and Linux distribution
Pythons, mark the system interpreter as **externally managed** ([PEP 668](https://peps.python.org/pep-0668/)),
so `pip install` outside a virtual environment refuses to run. This is the
operating system protecting its own Python, not a problem with Pyxle — and every
Python package hits it, not just this one.

Create a virtual environment, which is the recommended path anyway:

```bash
python3 -m venv venv && source venv/bin/activate
pip install pyxle-framework
```

If `python3 -m venv` itself fails on Debian or Ubuntu, install the venv module
first with `sudo apt install python3-venv`.

Avoid `pip install --break-system-packages`: it does what it says, and a broken
system Python is a much worse afternoon than a virtual environment.

### `pyxle dev` / `pyxle build` stops with a Node.js version error

If `node --version` is **below v20.19**, Pyxle stops with a clear message before starting Vite:

```
Node.js 20.19+ is required, but 20.16.0 is installed.
```

Vite 7 — which Pyxle uses to build and serve your React code — requires Node.js 20.19 or newer; older 20.x releases crash at startup. Upgrade Node and re-run:

```bash
node --version             # must be v20.19 or later
nvm install 20             # if you use nvm
# or download the latest LTS from https://nodejs.org
```

### `pyxle build` stops with "npx was not found on your PATH"

```
npx was not found on your PATH — cannot build the client bundle.
```

Node.js is installed but **npm is not**, so Pyxle cannot run Vite to bundle your
pages. `pyxle build` stops here on purpose: continuing would produce a `dist/`
with no browser JavaScript, and `pyxle serve` refuses to start on one.

This is most common on Debian and Ubuntu, where the `nodejs` package does not
include npm, and in slim container images that ship the `node` binary alone:

```bash
npx --version              # nothing? npm is missing
sudo apt install npm       # Debian / Ubuntu
# or install Node.js from https://nodejs.org (or nvm / fnm) — npm ships with those
```

## Next steps

Create your first project: [Quick Start](quick-start.md)
