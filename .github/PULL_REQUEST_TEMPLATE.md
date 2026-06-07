<!-- What does this change, and why? Link any related issue. -->

## Checklist

- [ ] `pytest` passes and coverage stays ≥ 95%
- [ ] `ruff check pyxle/ tests/` is clean
- [ ] New behavior has tests in the matching `tests/` directory
- [ ] Touched the parser or SSR? Added regression tests and verified `pyxle init` + `pyxle dev` still work end-to-end
- [ ] One focused change; commit messages follow Conventional Commits (`scope`: `compiler`, `ssr`, `cli`, …)
