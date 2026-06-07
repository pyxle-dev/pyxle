# Contributing to Pyxle

Thanks for your interest in Pyxle! It's early (0.3.x) and feedback, bug reports, and pull
requests are all genuinely welcome.

## Ways to help

- **Try it and report friction.** `pyxle init` → build something → tell us where it breaks.
  Rough edges in the first 15 minutes are the most valuable thing you can report.
- **File issues** for bugs and feature requests (templates guide you).
- **Improve the docs** at [pyxle.dev/docs](https://pyxle.dev/docs).
- **Send a PR** — see the workflow below.

## Development setup

```bash
git clone https://github.com/pyxle-dev/pyxle.git
cd pyxle
python -m venv venv && source venv/bin/activate   # Python 3.10+
pip install -e ".[dev]"
pytest                                             # full suite + coverage
```

You'll also need **Node.js 18+** on your `PATH` (some tests shell out to Babel/esbuild for
JSX validation and SSR).

## The bar for a PR

Pyxle aims to stay small, fast, and predictable. A PR is ready when:

1. **All tests pass and coverage stays ≥ 95%.** Run `pytest`. New behavior ships with new
   tests in the matching `tests/` directory. Don't `skip`/`xfail`/`# pragma: no cover` to
   dodge coverage, and don't lower the threshold.
2. **`ruff check pyxle/ tests/` is clean.** Fix lint rather than adding `# noqa`.
3. **It respects the architecture.** Modules have clear boundaries (`compiler` and `runtime`
   are standalone — no framework imports; no circular deps). Data-carrying classes are
   **frozen dataclasses** with `tuple`/`Sequence` for collections. I/O is `async`. No `print`
   (use the logger). No magic — decorators add metadata, they don't wrap behavior.
4. **It's focused.** One logical change per PR/commit.

The parser (`pyxle/compiler/parser.py`) and the SSR pipeline (`pyxle/ssr/`) are the most
sensitive code — changes there need regression tests for the exact input/case, and you should
manually verify `pyxle init` + `pyxle dev` still work end-to-end.

## Commits

Conventional Commits, scoped to the primary module changed:

```
feat(compiler): support optional catch-all routes
fix(ssr): dedupe <title> across nested layouts
docs(actions): add a form-submission example
```

Scopes: `compiler`, `ssr`, `devserver`, `cli`, `runtime`, `client`, `build`, `routing`,
`tests`, `scaffold`, `docs`.

## Reporting security issues

Please **don't** open a public issue for security vulnerabilities. Email **dev@pyxle.dev**
with the details and we'll respond promptly.

## License

By contributing, you agree your contributions are licensed under the [MIT License](LICENSE).
