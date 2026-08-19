# CLAUDE.md — Pyxle Core Framework

This file covers technical rules for the Pyxle core framework.
Every rule here exists to keep Pyxle enterprise-grade, stable, and maintainable.

---

## Project Overview

Pyxle is a Python-first full-stack web framework. `.pyxl` files colocate Python server
logic (`@server` loaders, `@action` mutations) with React/JSX components. The stack is
Starlette (ASGI), Vite (bundling), React 19 (rendering), and esbuild (SSR transpilation).

**Key files to read first:**
- `ROADMAP.md` — current phase, pending tasks, design principles
- `PYXLE_AUDIT.md` — architectural strengths, risks, and bottlenecks
- `pyproject.toml` — dependencies, test config, coverage thresholds
- `pyxle/runtime.py` — the `@server` and `@action` decorator contracts
- `pyxle/compiler/parser.py` — the `.pyxl` parser (most complex module)
- `pyxle/devserver/starlette_app.py` — request routing and middleware stack
- `pyxle/ssr/renderer.py` — SSR rendering pipeline (performance-critical)

---

## Mandatory Rules

### 1. Run Tests After Every Change

**This is non-negotiable.** Every code change must be followed by running the test suite.

```bash
# Run the full test suite
pytest

# The above command uses pyproject.toml defaults:
#   --strict-markers --strict-config
#   --cov=pyxle.build --cov=pyxle.cli --cov=pyxle.compiler --cov=pyxle.devserver --cov=pyxle.ssr
#   --cov-report=term-missing
```

- **Coverage threshold is 95%.** The build fails below this. Do not lower it.
- **All tests must pass.** Zero test failures are acceptable.
- **If you break a test, fix it before moving on.** Do not leave failing tests for later.
- **If you add a feature, add tests for it** in the same change. No feature ships without tests.

### 2. Write Tests First When Possible

Prefer test-driven development:
1. Write a failing test that describes the expected behavior
2. Implement the minimum code to make it pass
3. Refactor while keeping tests green

### 3. Never Skip or Weaken Tests

- DO NOT add `@pytest.mark.skip`, `pytest.mark.xfail`, or `# pragma: no cover` to dodge coverage
- DO NOT delete tests to make the suite pass
- DO NOT lower `fail_under = 95` in `pyproject.toml`
- DO NOT remove modules from the `--cov=` list

### 4. Test File Location Convention

Tests mirror the source tree:
```
pyxle/cli/           -> tests/cli/
pyxle/compiler/      -> tests/compiler/
pyxle/devserver/     -> tests/devserver/
pyxle/ssr/           -> tests/ssr/
pyxle/build/         -> tests/build/
pyxle/config.py      -> tests/test_config.py
```

When creating a new module at `pyxle/foo/bar.py`, create `tests/foo/test_bar.py`.

---

## Architecture Rules

### 5. Respect Module Boundaries

The codebase has clear separation of concerns:

| Module | Responsibility | May Import From |
|--------|---------------|-----------------|
| `pyxle/cli/` | CLI commands, user-facing I/O | Everything below |
| `pyxle/devserver/` | Dev server, Vite proxy, file watcher | compiler, ssr, routing, config |
| `pyxle/ssr/` | Server-side rendering, head merging | compiler (models only), config |
| `pyxle/compiler/` | `.pyxl` parsing, code generation | Nothing from pyxle (standalone) |
| `pyxle/routing/` | File-based route calculation | Nothing from pyxle (standalone) |
| `pyxle/build/` | Production build pipeline | compiler, devserver, config |
| `pyxle/config.py` | Configuration parsing | Nothing from pyxle (standalone) |
| `pyxle/runtime.py` | `@server`, `@action` decorators | Nothing from pyxle (standalone) |
| `pyxle/client/` | Client-side JS/JSX components | N/A (not Python -- JS only) |

**DO NOT** introduce circular imports. **DO NOT** have `compiler` depend on `devserver`.
**DO NOT** have `runtime.py` import anything from the framework -- it must stay zero-dependency
because it's injected into user code.

### 6. Frozen Dataclasses Everywhere

All data-carrying classes must be frozen dataclasses:

```python
# CORRECT
@dataclass(frozen=True)
class PageRoute:
    path: str
    module_key: str
    has_loader: bool

# WRONG -- mutable state causes bugs in async code
@dataclass
class PageRoute:
    path: str
    module_key: str
    has_loader: bool
```

Use `slots=True` for internal-only dataclasses that benefit from memory efficiency.

### 7. Use `Sequence` and `tuple` for Immutable Collections

```python
# CORRECT -- signals immutability
def process_routes(routes: Sequence[PageRoute]) -> tuple[str, ...]: ...

# WRONG -- signals mutability
def process_routes(routes: list[PageRoute]) -> list[str]: ...
```

Store collection fields as `tuple`, not `list`, in frozen dataclasses.

### 8. Async by Default

All I/O operations must be async. Never block the event loop.

If wrapping a synchronous call (like `subprocess.run`), use `asyncio.to_thread()`.

### 9. Use Structured Error Types

Every error that a user or developer might see needs a specific exception class.
Error classes live in the module they belong to (e.g., `pyxle/compiler/exceptions.py`).
Error messages must be specific, actionable, and include context (file path, line number, etc.).

### 10. No Magic, No Hidden Behavior

Decorators add metadata. They do NOT wrap, transform, or hide behavior.
The same principle applies to `@action` and any future decorators.

---

## Code Quality Rules

### 11. Type Hints on All Public APIs

Every public function, method, and class must have complete type hints.
Internal helpers may omit return types if the logic is trivial, but parameters must always be typed.

### 12. Docstrings on Public APIs

Every public function, class, and module needs a docstring. Keep them concise -- describe
*what* and *why*, not *how*.

### 13. No Print Statements

Use the CLI logger (`pyxle/cli/logger.py`) for user-facing output. Use Python `logging`
module for internal diagnostics. Never use `print()`.

### 14. Run Ruff Before Committing

```bash
ruff check pyxle/ tests/
```

Fix all lint errors. Do not add `# noqa` unless there's a documented reason.

---

## Performance Rules

### 15. SSR is the Hot Path

`pyxle/ssr/` is the most performance-critical code. Every millisecond matters.

- **DO NOT** add synchronous I/O to the SSR request path
- **DO NOT** add new imports to modules loaded on every request
- **DO NOT** grow caches without eviction policies
- **DO** profile before and after any SSR change
- **DO** consider the cost at 100 concurrent requests, not just 1

### 16. Lazy Imports for Heavy Modules

Modules that are only needed in specific code paths should be imported lazily.
This keeps CLI startup fast and avoids circular import issues.

### 17. No Unbounded Caches

Every cache must have a max size or TTL. Document the eviction strategy.

---

## Security Rules

### 18. Never Trust User Input

- Escape all dynamic content injected into HTML (especially HEAD elements)
- Validate file paths -- never allow path traversal (`../`)
- Use parameterized queries -- never string-interpolate SQL
- Sanitize route parameters
- Never expose stack traces, file paths, or internal state in production error responses

### 19. Subprocess Safety

- Build command arrays programmatically -- never use `shell=True` with user input
- Set timeouts on all subprocess calls
- Capture stderr and handle errors
- Clean up temp files in `finally` blocks

### 20. Secrets Stay Server-Side

- Environment variables without `PYXLE_PUBLIC_` prefix must NEVER appear in client bundles
- Loader and action return values are serialized to JSON and sent to the client -- never
  include secrets, tokens, or internal IDs that shouldn't be exposed

---

## Development Workflow

### 21. Branch and Change Management

- Read `ROADMAP.md` before starting work -- find the relevant phase and task
- Work on one task at a time -- complete it (including tests) before moving to the next
- Mark completed tasks in `ROADMAP.md` by changing `[ ]` to `[x]`
- Keep commits focused: one logical change per commit

### 22. Adding a New Feature -- Checklist

Before writing code:
1. Identify the relevant `ROADMAP.md` task
2. Understand how it fits in the architecture (which modules are affected?)
3. Check for existing patterns to follow (look at similar completed features)

While writing code:
4. Add/update frozen dataclasses in the relevant `model.py`
5. Implement the logic following module boundary rules
6. Write tests in the matching `tests/` directory
7. Run `pytest` -- all tests must pass, coverage must stay above 95%
8. Run `ruff check` -- zero lint errors

After writing code:
9. Mark the task as `[x]` in `ROADMAP.md`
10. If you discovered new work needed, add it to the appropriate phase in `ROADMAP.md`

### 23. Commit Scope Convention

Use the primary module changed as the commit scope: `compiler`, `devserver`, `ssr`, `cli`,
`runtime`, `client`, `build`, `routing`, `tests`, `scaffold`.

### 24. Modifying the Compiler or Parser

The parser (`pyxle/compiler/parser.py`) is the most sensitive code. Changes here can
break every `.pyxl` file in existence.

- **Always** add regression tests for the specific input pattern you're handling
- **Never** remove an existing test
- **Test edge cases**: empty files, files with only Python, files with only JSX,
  multiline strings, nested brackets, decorator chains, comments that look like code
- After changes, run the full compiler test suite AND manually compile the scaffold
  templates to verify they still work

### 25. Modifying the SSR Pipeline

Changes to `pyxle/ssr/` affect every page render.

- **Benchmark** before and after: measure render time for simple and complex pages
- **Test error paths**: loader failure, render failure, head evaluation failure
- **Test with missing data**: what happens when a loader returns `None`? Empty dict?
- **Verify the error overlay** still receives correct breadcrumbs after your change

---

## Testing Patterns

### 26. Use Fixtures, Not Setup Methods

### 27. Use Parametrize for Variant Testing

### 28. Mock External Dependencies

Node.js, npm, Vite, and file system operations should be mocked in unit tests.
Use `tmp_path` for any test that creates files. Never write to the real filesystem.

### 29. Test Error Messages, Not Just Error Types

---

## DO NOT List

- **DO NOT** lower the coverage threshold below 95%
- **DO NOT** skip or delete tests to make the suite pass
- **DO NOT** add `print()` statements (use logger or `logging`)
- **DO NOT** introduce circular imports between modules
- **DO NOT** make `runtime.py` import anything from the framework
- **DO NOT** use mutable dataclasses for data-carrying types
- **DO NOT** block the async event loop with synchronous I/O
- **DO NOT** grow caches without eviction policies
- **DO NOT** use `shell=True` in subprocess calls
- **DO NOT** expose internal error details in production responses
- **DO NOT** hardcode host/port/path values (use config or env vars)
- **DO NOT** commit `.env` files, secrets, or credentials
- **DO NOT** add dependencies to `pyproject.toml` without explicit need and version pinning
- **DO NOT** break backward compatibility without updating `ROADMAP.md` migration notes
- **DO NOT** modify templates without verifying `pyxle init` + `pyxle dev` still works end-to-end
- **DO NOT** write framework code that only works on macOS/Linux -- support Windows paths

---

## Quick Reference

### Running Tests
```bash
pytest                          # Full suite with coverage
pytest tests/compiler/          # Just compiler tests
pytest -x                       # Stop on first failure
pytest -k "test_parser"         # Run tests matching pattern
pytest --no-cov                 # Skip coverage (faster for iteration)
```

### Linting
```bash
ruff check pyxle/ tests/
```

### Key Paths
```
pyxle/                          # Framework source
|-- cli/                        # CLI commands (Typer)
|-- compiler/                   # .pyxl -> .py + .jsx compiler
|   |-- parser.py               # State-machine parser (most complex)
|   |-- writers.py              # Server/client code emission
|   |-- model.py                # Compilation data models
|   |-- jsx_parser.py           # Babel-based JSX validation
|   +-- jsx_imports.py          # Import specifier rewriter
|-- devserver/                  # Development server
|   |-- starlette_app.py        # ASGI app assembly
|   |-- vite.py                 # Vite subprocess management
|   |-- builder.py              # Incremental build orchestration
|   |-- proxy.py                # Vite asset proxy
|   |-- routes.py               # Route descriptors
|   |-- registry.py             # Page metadata registry
|   |-- middleware.py            # Middleware loading
|   |-- layouts.py              # Layout/template composition
|   |-- scanner.py              # Source file discovery
|   +-- overlay.py              # WebSocket error overlay
|-- ssr/                        # Server-side rendering
|   |-- renderer.py             # Component render orchestration
|   |-- render_component.mjs    # Node.js SSR runtime (esbuild + React)
|   |-- view.py                 # Page response building
|   |-- head_merger.py          # Head element deduplication
|   +-- template.py             # HTML document assembly
|-- build/                      # Production build pipeline
|-- routing/                    # File-based route calculation
|-- client/                     # Client-side JS components (not Python)
|-- config.py                   # Configuration parsing + validation
|-- runtime.py                  # @server, @action decorators (zero deps)
+-- templates/scaffold/         # pyxle init project template

tests/                          # Mirrors pyxle/ structure
docs/                           # Framework documentation
```

### Design Principles (from ROADMAP.md)
1. **Python-first, not Python-only** -- great Python AND great React
2. **Convention over configuration** -- zero config for common cases
3. **Compiler-driven** -- extract metadata at build time, not runtime
4. **No magic** -- decorators add metadata, not hidden behavior
5. **Progressive disclosure** -- simple things simple, complex things possible
6. **Batteries includable** -- ship hooks and integration points, not opinions
7. **AI-first DX** -- predictable patterns, strong types, clear errors
