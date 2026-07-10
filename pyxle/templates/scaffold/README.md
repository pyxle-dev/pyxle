# $project_name

A [Pyxle](https://pyxle.dev) app — Python and React in one file. `@server` loaders
and `@action` mutations live in Python right beside your React components in a
single `.pyxl` file: file-based routing, server-side rendering, no separate API
layer to wire up.

## Prerequisites

- **Python 3.10+**
- **Node.js 20.19+** (with npm)

Check with `python --version` and `node --version`.

## Getting started

Install dependencies (Python + Node) and start the dev server:

```bash
pyxle install      # pip install -r requirements.txt  +  npm install
pyxle dev          # http://localhost:8000, with hot reload
```

## Project structure

```
pages/            File-based routes
  layout.pyxl     Root layout (wraps every page)
  index.pyxl      Home page — a @server loader + a React component
  api/            Plain API routes (e.g. api/pulse.py -> GET /api/pulse)
public/           Static assets, served at /
pyxle.config.json Configuration (plugins, CSRF, caching, …)
requirements.txt  Python dependencies
package.json      Node dependencies (React, Vite)
```

A `.pyxl` file has two parts: Python at the top (your `@server`/`@action`
functions), then the React component below.

## Common commands

```bash
pyxle dev                 # Development server with hot reload
pyxle check               # Validate .pyxl syntax, semantics, and JSX
pyxle build               # Build production assets into dist/
pyxle serve --skip-build  # Serve the production build
```

Set `PYXLE_SECRET_KEY` before serving in production — `pyxle serve` requires it.

## Learn more

- **Docs:** https://pyxle.dev/docs
- **Quick start:** https://pyxle.dev/docs/getting-started/quick-start
- **Deployment:** https://pyxle.dev/docs/guides/deployment

Working with an AI coding agent? See `AGENTS.md` in this project for the
conventions it should follow.
