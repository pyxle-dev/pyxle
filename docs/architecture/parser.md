# The parser

> *"How does Pyxle know where the Python ends and the JSX begins?"*

The honest answer is: by **parsing**. The Pyxle parser feeds the source
to Python's `ast.parse` and uses the structure of the result — and the
structure of the failure when parsing fails — to find the boundary
between the two languages.

There are no fence markers, no comment directives, no per-line
heuristics, no whitespace conventions. There is only a walker, a
greedy strategy, and a small handful of carefully chosen guardrails.

This doc is the deepest technical doc in the architecture section. It's
written like a tour: we'll start with the problem, build up the
algorithm one step at a time, then catalog the edge cases that the
algorithm has to handle. By the end you'll be able to reason about any
`.pyxl` file the parser will see in the wild — and you'll know exactly
why each guardrail exists.

**File:** `pyxle/compiler/parser.py` (~1100 lines, the most sensitive
code in the framework).

---

## The problem in one sentence

> Given a string that contains some Python and some JSX, find the
> boundaries between them, extract metadata about the Python half, and
> emit clear errors when the file is broken.

Sounds simple. Let's see why it isn't.

---

## Why the obvious approaches don't work

You might reasonably ask: *"Why not just look for `import React`?
That's a JSX-only line."*

This is the first thing every framework that does this kind of split
tries. It works for the easy 95% and breaks on the hard 5%. Let's see
why.

### Approach 1: keyword matching

```python
def is_python(line):
    return line.lstrip().startswith(("import ", "from ", "def ",
                                      "class ", "@", "if ", "for "))
```

This was Pyxle's original approach (pre-v0.1.7). It's intuitive: Python
lines start with Python keywords. But it fails on:

```python
config = build_config()       # Python? Or JS bare assignment?
data = load_from_db()         # Python? Or JS bare assignment?
result = await compute()      # Python? Or JS top-level await?
```

`config = build_config()` is *valid in both languages*. You cannot tell
from the line alone which one it is. You need context.

It also fails on multi-line constructs like dictionary literals or
template strings:

```python
DATA = {
    "key": "value",   # Python? The line above had the indicator,
                      # but this line doesn't.
}
```

A pure line-based scanner would have to track open brackets, open
strings, indentation — and at that point you've reinvented half of a
Python parser, and it's still wrong.

### Approach 2: explicit fence markers

```pyxl
# --- server ---
@server
async def loader(request):
    return {}
# --- client ---
import React from 'react';
export default function P() { return <div />; }
```

Pyxle supported this for a while. It works perfectly, but it has two
problems:

1. **It's annoying.** Every file needs the marker comment, and
   forgetting it is a frustrating "why won't this compile?" error.
2. **It's fragile.** A `# --- client ---` comment inside a JS template
   literal (i.e., a string that mentions the marker) would be misread
   as a real fence. We had this exact bug in our own playground page —
   a code-display widget that quoted the marker as part of an example.
   The marker scanner would split the file in the wrong place.

### Approach 3: parse it

This is the approach Pyxle uses now. The insight is:

> **Python's `ast.parse` is the ultimate ground truth for "is this
> Python?".** If `ast.parse` says yes, it is. If it says no, it isn't.
> No heuristics needed.

Once you accept this, the algorithm almost writes itself.

---

## The greedy walker

Here's the core idea, in plain English:

> Start at line 0. Try to parse the **whole rest of the file** as
> Python. If it parses, you're done — the whole file is Python. If it
> fails, the parser tells you exactly which line it failed on. Walk
> back from that line until you find the largest valid Python prefix.
> Mark that prefix as a Python segment. Then look at where Python
> stopped: that line and onward is JSX. Walk forward through the JSX
> until you find a line where Python parses again. Mark that span as a
> JSX segment. Repeat.

Concretely:

```python
def auto_detect_segments(lines):
    segments = []
    cursor = 0
    while cursor < len(lines):
        # Skip leading blank lines.
        if not lines[cursor].strip():
            cursor += 1
            continue

        # Try to grow a Python segment from cursor.
        py_end = find_largest_python_at(lines, cursor, len(lines))
        if py_end > cursor:
            segments.append(("python", cursor, py_end))
            cursor = py_end
            continue

        # Otherwise, this region is JSX. Find where Python resumes.
        jsx_end = find_jsx_end_at(lines, cursor, len(lines))
        segments.append(("jsx", cursor, jsx_end))
        cursor = jsx_end

    return segments
```

Source: `compiler/parser.py:364-403`.

That's the entire walker. Eight lines of orchestration around two
helpers. The helpers do the interesting work.

---

## `find_largest_python_at`

This function answers: *"Starting at this line, how far can Python
take me?"*

The naive version would be: walk forward one line at a time, parse
each prefix, stop when one fails. That's O(n²) in the worst case.

The clever version uses CPython's own information: when `ast.parse`
fails on a multi-line input, the `SyntaxError.lineno` attribute usually
points exactly at the line where things first went wrong. We can jump
straight there.

```python
def find_largest_python_at(lines, start, n):
    if start >= n:
        return start

    rest = "\n".join(lines[start:n])
    if not rest.strip():
        return n

    # Optimistic: try the whole suffix.
    try:
        ast.parse(rest)
        return n              # Whole rest is Python. Common case!
    except SyntaxError as exc:
        first_failure = (exc.lineno or 1) - 1

    # Walk back from the failing line until we find a valid prefix.
    upper = min(first_failure + 1, n - start)
    while True:
        prefix = "\n".join(lines[start : start + upper])
        if not prefix.strip():
            return start
        try:
            ast.parse(prefix)
            return start + upper
        except SyntaxError:
            upper -= 1
```

Source: `compiler/parser.py:178-220`.

The walk-back is bounded by the SyntaxError lineno, so in practice this
finishes in 1-2 attempts, not n. The worst case is when the parser
emits a misleading lineno (it sometimes does — Python's error recovery
isn't perfect), but even then we walk back one line at a time and
terminate at the empty prefix.

Notice that we aren't doing anything fancy with the AST itself. We
don't traverse it, we don't extract symbols, we just ask `ast.parse`
*"does this string parse?"* and use the answer as a yes/no oracle.
The AST extraction comes later, after the segments are concatenated.

> **Why try the whole suffix first?** Because the most common case is
> "this file is mostly Python with a JSX section at the bottom". For
> that file, the optimistic parse on the first call is the *only*
> parse needed. The walker terminates after one `ast.parse` invocation
> and we move on. Most real `.pyxl` files take fewer than 10
> milliseconds to parse end-to-end.

---

## `find_jsx_end_at`

This function answers: *"I'm in a JSX section starting at this line.
Where does it end?"*

Simple version: walk forward one line at a time, calling
`find_largest_python_at` at each line, returning the first line where
Python becomes possible again. That works most of the time, but it has
a sneaky bug — the bug that sent us looking for a smarter approach.

### The bug

Consider this JSX:

```jsx
export default function CodeDisplay() {
    const PYTHON_DEMO = `
        @server
        async def loader(request):
            return {}
    `;
    return <pre>{PYTHON_DEMO}</pre>;
}
```

The lines `@server` and `async def loader(request):` are *inside a JS
template literal*. They look like Python. They even **parse as Python**.
But they aren't Python — they're string content.

A naive `find_jsx_end_at` would walk forward, hit the `@server` line,
ask `find_largest_python_at`, see "yes, this parses as Python", and
incorrectly split the JSX function in half.

### The fix

`find_jsx_end_at` walks the JSX **with structural awareness**. It
maintains a small state machine that tracks:

- Whether we're currently inside a JS string (`'`, `"`, or `` ` ``)
- Whether we're currently inside a `/* ... */` block comment
- The current brace, paren, and bracket nesting depth

A line is only considered as a "Python could resume here" candidate
if **all of those depths are zero and no string or comment is open**.
Inside the template literal, the brace and string state never returns
to clean, so the `@server` line is never considered as a Python
boundary.

Here's the loop:

```python
def find_jsx_end_at(lines, start, n):
    state = JsState()
    state.advance(lines[start])  # Seed with the starting line.

    for k in range(start + 1, n):
        if not lines[k].strip():
            continue   # Blank lines don't change state.

        if state.is_clean() and find_largest_python_at(lines, k, n) > k:
            return k   # Top-level JS *and* Python parses here. Switch.

        state.advance(lines[k])

    return n  # No Python resumption — rest of file is JSX.
```

Source: `compiler/parser.py:223-258`.

### The state machine

The `_JsState` dataclass is small:

```python
@dataclass(slots=True)
class _JsState:
    string: str | None = None       # ', ", `, or None
    block_comment: bool = False
    brace_depth: int = 0
    paren_depth: int = 0
    bracket_depth: int = 0
```

`advance(line)` walks the line character by character and updates
the state. The interesting subtleties:

- **Single and double-quoted strings reset at end of line.** JavaScript
  allows multi-line strings only inside backticks; `'hello\n'` is a
  syntax error in JS. So after each line, if we're inside a `'` or `"`
  string, we forget about it.
- **Backtick template literals span lines.** They stay open until the
  closing backtick.
- **`//` line comments terminate the line walk.** Anything after `//`
  is ignored for state tracking.
- **`/* ... */` block comments span lines.** They stay open until `*/`.
- **Backslash escapes inside strings consume the next character.** So
  `'hello\''` is a single string, not two.

It's not a complete JavaScript parser — it doesn't try to validate the
JS — but it tracks enough structure to know when we're at a clean
top-level position where Python *could* legitimately resume.

Source: `compiler/parser.py:261-345`.

> **Why not use Babel?** Babel would be a complete JS parser, and we
> already run it once per parse to extract JSX metadata. The reason we
> don't use it *here* is performance: it is a Node.js subprocess
> (~135 ms per call, measured) and the walker would have to call it
> inside its inner loop, once per candidate boundary. The state machine
> is ~80 lines of Python and runs in microseconds.

---

## Multi-section alternation in action

With the walker and the helpers in place, here's what happens for a
real four-section file:

```pyxl
# pages/dashboard.pyxl
from datetime import datetime         # ─┐
                                      #  │ Python segment 1:
def format(dt):                       #  │ lines 0-3
    return dt.isoformat()             # ─┘


import React from 'react';            # ─┐
                                      #  │ JSX segment 1:
function StatCard({ label, value }) { #  │ lines 5-8
    return <div>{label}: {value}</div>;
}                                     # ─┘


@server                               # ─┐
async def load(request):              #  │ Python segment 2:
    return {"now": format(datetime.now())}  # lines 10-12
                                      # ─┘

export default function Page({ data }) {  # ─┐
    return <StatCard label="Now" value={data.now} />;  # JSX segment 2:
}                                     # ─┘ lines 14-16
```

The walker:

1. **Cursor 0.** Skip blank lines. Try `find_largest_python_at(0)`.
   `ast.parse` of lines 0-16 fails at line 5 (`import React from
   'react';` is a `SyntaxError` because `from` followed by a string is
   invalid Python syntax). Walk back from line 5: lines 0-3 parse.
   Mark as Python segment 1. Cursor advances to 4.

2. **Cursor 4.** Skip blank line at 4. Try
   `find_largest_python_at(5)`. Lines 5-16 still fail (the JSX). Walk
   back: line 5 alone fails too (`from 'react'` is still bad syntax).
   Return cursor unchanged. So this region is JSX. Call
   `find_jsx_end_at(5)`, which walks forward through the JSX with
   state tracking, hits line 10 (`@server`), sees the state is clean
   (the `function StatCard` body closed at line 8), and asks "does
   Python parse here?" — `find_largest_python_at(10)` returns 13.
   So JSX segment 1 is lines 5-9. Cursor advances to 10.

3. **Cursor 10.** Try `find_largest_python_at(10)`. Lines 10-16 fail
   at line 14 (`export default` is invalid Python). Walk back: lines
   10-12 parse. Mark as Python segment 2. Cursor advances to 13.

4. **Cursor 13.** Skip blank line. Try
   `find_largest_python_at(14)`. Fails. JSX. `find_jsx_end_at(14)`
   walks to end of file without finding a Python resumption. Mark as
   JSX segment 2. Cursor reaches end of file.

The walker terminates after **four** `ast.parse` calls (one per
segment, with the walk-back amortized into each). On a 1500-line file
this typically completes in 5-15 milliseconds.

---

## Layer 2: catching broken Python in JSX segments

The walker has one more failure mode that took us a while to find.

Consider a syntax-broken Python file:

```python
x = "this string never closes
y = 1


import React from 'react';
export default function Page() { return <div />; }
```

The first line is *not* valid Python — the string is unterminated. The
walker tries to parse it and fails. So line 0 ends up classified as
JSX. The walker advances, finds line 1 (`y = 1`) which is valid
Python, and creates a tiny JSX segment containing just the broken
line.

The result: the broken Python silently gets concatenated into
`jsx_code` and shipped to the JSX compiler downstream, which then
fails with a confusing JSX error pointing at `x = "this string never
closes`. The user has no idea their Python is broken.

This is a real bug we discovered during the parser audit on
2026-04-08. The fix is **`_detect_broken_python_in_jsx_segments`**
(`compiler/parser.py:481-590`), a heuristic that runs after the walker
and inspects every JSX segment.

The heuristic flags a JSX segment as suspicious if its first non-blank
line:

1. **Is indented.** JSX top-level statements never start indented.
2. **Starts with a Python-only keyword.** `def`, `class`, `from`,
   decorators (`@`), `async def`, etc. — these are never valid at the
   top of a JSX statement.
3. **Doesn't start with any known JSX top-level token.** The
   whitelist is:
   ```python
   _JSX_TOPLEVEL_PREFIXES = (
       "import ", "import{", "import(", "import*", "import\"", "import'",
       "export ", "export{", "export*", "export(",
       "const ", "let ", "var ",
       "function ", "function(", "function*",
       "class ", "class{",
       "//", "/*",
       "<", "{", "}", "(", ")", "[", "]", ";",
   )
   ```
   A line that starts with anything else (e.g., a bare identifier
   `x = "..."`) is suspicious.

When any signal fires, the heuristic re-runs `ast.parse` on the
segment in isolation to recover the precise Python error message and
emits it as a `[python]` diagnostic, pointing at the right line in
the original `.pyxl` source.

### The false-positive guard

A naive version of the third signal would flag this valid JSX as
broken:

```jsx
config = <Provider value={ctx}>;
```

The first token is `config` (a bare identifier), which isn't in the
whitelist. But the line is legitimate JSX — it's a top-level
expression statement assigning a JSX element to a variable.

The fix: the helper `_contains_jsx_element_marker` (`parser.py:459`)
scans the line for `<` immediately followed by a letter, `/`, or `>`
(a JSX element tag start). If the line contains such a marker, it's
treated as legitimate JSX even though its first token is unrecognised.

The check intentionally *requires* the `<` to be followed by a
non-whitespace, non-operator character — because `x < 10` is a
less-than operator, not a JSX element. This distinction matters for
JSX lines like `guard = x < 10 ? <Warning /> : <Safe />;` — the
scanner walks past the first `<` (which is followed by space, so
it's not a tag), continues, and finds the second `<` (which is
followed by `W`, so it *is* a tag).

Source: `compiler/parser.py:459-475`.

### Cascade suppression

When `pyxle check` runs in tolerant mode with `validate_jsx=True`, it
runs **both** the parser AND Babel on every file. If the parser finds
a `[python]` error, the broken Python gets absorbed into `jsx_code`
and Babel will *also* fail on it — producing a noisy `[jsx]`
diagnostic that's really just a symptom of the underlying Python bug.

To avoid this, `parse_text()` skips the Babel validation step
whenever the diagnostic collector already has any `[python]` entry.
Fix Python first; JSX validation becomes meaningful again on the next
run.

Source: `compiler/parser.py:1080-1100`.

---

## Layer 3: AST metadata extraction

Once the segments are determined and the broken-Python check has
passed, the parser concatenates the Python segments into a single
`python_code` string and parses it once more — this time keeping the
AST. From the AST it extracts:

### Loaders (`_detect_loader`, `parser.py:567-636`)

The walker finds every function decorated with `@server`. It enforces:

- The function must be `async`. Sync `@server def` raises a structured
  error pointing at the line.
- The function must be at module scope (not nested inside a class).
- The function must accept exactly one positional argument named
  `request`.
- There must be at most one `@server` function per page.

The result is a frozen `LoaderDetails` dataclass with name, line
number, async flag, and parameter list. This metadata flows to the
dev server and the SSR pipeline so they can find and call the loader
without re-parsing the Python.

### Actions (`_detect_actions`, `parser.py:639-729`)

Same idea, for `@action`-decorated functions. Multiple actions per
page are allowed, but names must be unique. Validation includes:

- Async only.
- Module scope only.
- First positional argument named `request`.
- Cannot also be decorated with `@server` (that would create an
  ambiguity).
- Action names must be unique within the file.

The result is a tuple of `ActionDetails` dataclasses.

### HEAD elements (`_collect_head_elements`, `parser.py:760-800`)

The parser walks the top-level statements of the AST looking for
either:

- An assignment of `HEAD` to a string literal or list of string
  literals — extracted as `head_elements: tuple[str, ...]`.
- A `def HEAD(data):` or `async def HEAD(data):` — flagged as
  `head_is_dynamic = True`.

If neither pattern is found, `HEAD` is empty and `head_is_dynamic` is
False. Pages without a HEAD just use whatever the layout provides.

### JSX-side metadata (`_detect_script_declarations`, etc.)

After the Python AST extraction, the parser hands `jsx_code` to
`pyxle.compiler.jsx_parser`, which spawns a Node.js helper script
(`jsx_component_extractor.mjs`) that uses Babel to parse the JSX and
return information about specific component usages:

- `<Script>` declarations and their props (for runtime script loading)
- `<Image>` declarations and their props (for asset optimization)
- `<Head>` JSX blocks (their children get hoisted into the HTML head)

This is **not** a syntax check — it's a metadata extraction. If Babel
can't parse the JSX, it returns an error message which the parser
treats as "no metadata" (in non-validate mode) or as a `[jsx]`
diagnostic (in `validate_jsx=True` mode).

---

## Diagnostics: tolerant mode

Every error path in the parser flows through a single helper called
`_DiagnosticCollector` (`parser.py:124`). The collector has one
method, `emit`:

```python
def emit(self, message, line, *, section="python", column=None):
    if self.tolerant:
        self.diagnostics.append(PyxDiagnostic(...))
        return
    raise CompilationError(message, line)
```

In **strict mode** (`tolerant=False`, the default), every error
immediately raises `CompilationError` and the parse aborts. This is
what `pyxle dev` and `pyxle build` use — they want the build to
stop at the first error.

In **tolerant mode** (`tolerant=True`), errors are appended to a list
of `PyxDiagnostic` entries instead of raising. The parse continues as
far as it can, collecting every error along the way. The result is a
`PyxParseResult` with a populated `diagnostics: tuple[PyxDiagnostic,
...]` field. This is what `pyxle check` uses — it wants to report
*all* errors in *every* file in a single pass, so the user can see
the whole picture and fix everything at once.

A `PyxDiagnostic` looks like:

```python
@dataclass(frozen=True)
class PyxDiagnostic:
    section: Literal["python", "jsx"]
    severity: Literal["error", "warning"]
    message: str
    line: int | None
    column: int | None = None
```

The parser also accepts a `validate_jsx=True` flag in tolerant mode.
When set, it runs Babel on the JSX section and adds any Babel errors
as `[jsx]` diagnostics (subject to the cascade suppression described
above).

---

## Robustness: surviving pathological input

The parser is the entry point that processes **untrusted source code
from disk**. It needs to be robust against adversarial input that's
designed to crash CPython, not just merely broken input.

Two specific cases the parser defends against:

### Deep nesting → `MemoryError`

A `.pyxl` file containing 200 levels of nested list literals:

```pyxl
@server
async def loader(request):
    return [[[[[[[[[[ ... 200 levels ... ]]]]]]]]]]


import React from 'react';
export default function Page() { return <div />; }
```

When `find_largest_python_at` calls `ast.parse` on the whole file,
CPython's parser stack overflows and raises `MemoryError: Parser
stack overflowed`. (Curiously, this only triggers when the deeply
nested expression is followed by a section that *can't* parse as
Python — CPython's error recovery uses extra stack frames trying to
recover from the JSX.)

The parser catches `MemoryError` and `RecursionError` at the outer
boundary in `parse_text()` and emits a structured diagnostic:

```
[python] line 1: Python parser exhausted (MemoryError): source is too
deeply nested or too large for CPython to parse
```

It then returns an empty-but-valid `PyxParseResult` so the CLI can
keep scanning the rest of the project. Without this guard, a single
pathological file would crash `pyxle check` mid-scan, losing every
diagnostic for files that came after it alphabetically.

Source: `compiler/parser.py:1042-1090`.

### Null bytes and zero-width characters

CPython's `ast.parse` rejects source containing null bytes (`\x00`)
with a `ValueError: source code string cannot contain null bytes`.
It also rejects identifiers containing zero-width Unicode space
(`U+200B`) with `SyntaxError: invalid non-printable character
U+200B`. Both error paths flow through `_DiagnosticCollector.emit`
and produce structured diagnostics.

These are useful as security fixtures because they catch source files
that may have been intentionally crafted to confuse a developer or
slip past a code review. Pyxle just refuses to compile them.

---

## Public API

The parser exposes a single class:

```python
class PyxParser:
    def parse(
        self,
        source_path: Path,
        *,
        tolerant: bool = False,
        validate_jsx: bool = False,
    ) -> PyxParseResult:
        ...

    def parse_text(
        self,
        text: str,
        *,
        tolerant: bool = False,
        validate_jsx: bool = False,
    ) -> PyxParseResult:
        ...
```

Source: `compiler/parser.py:969`.

Every other function in the module is private (`_`-prefixed). The
contract is:

- **`parse(source_path, tolerant=False, validate_jsx=False)`** —
  Reads the file with `utf-8-sig` encoding (so a leading BOM is
  consumed transparently), then delegates to `parse_text`.
- **`parse_text(text, tolerant=False, validate_jsx=False)`** —
  Returns a `PyxParseResult`. In strict mode, raises `CompilationError`
  on the first error. In tolerant mode, returns a result with
  `diagnostics` populated.
- **`tolerant=True`** — Don't raise; collect diagnostics. Used by
  `pyxle check` and any future LSP integration.
- **`report_jsx_syntax=True`** — Surface a JSX section Babel could not
  parse as a `[jsx]` error located in the `.pyxl`. Free: the extractor
  pass that reads `<Head>`/`<Script>`/`<Image>`/`<Suspense>` runs on
  every parse anyway and has to parse the section to find them, so this
  only stops the result being discarded. A flag rather than always-on
  purely so the production build path keeps its existing behaviour —
  `pyxle dev` sets it, `pyxle build` and `pyxle serve` do not.
- **`validate_jsx=True`** — Re-run Babel over the JSX section explicitly.
  Now largely redundant with `report_jsx_syntax`, and skipped entirely
  when that already reported a syntax error, so it no longer spawns a
  second subprocess to rediscover the same failure.

The combinations that matter:

| `tolerant` | `report_jsx_syntax` | Behaviour |
|---|---|---|
| `False` | `False` | Strict Python parse. Raises on the first Python error. A JSX parse failure yields empty JSX metadata and no error. (Used by `pyxle build`, `pyxle serve`.) |
| `False` | `True` | Strict parse of both halves. Raises on the first error from either side. (Used by `pyxle dev`.) |
| `True` | `True` | Tolerant parse of both halves, with cascade suppression. (Used by `pyxle check`.) |

A JSX error is suppressed when the Python half already has one: broken
Python is absorbed into the JSX section by the walker, so the JSX
complaint would be a symptom rather than the cause.

---

## Performance characteristics

**One Node.js subprocess dominates every parse.** `_detect_jsx_metadata`
spawns the Babel extractor on every parse of a file with a JSX half, to
read `<Head>`, `<Script>`, `<Image>` and `<Suspense>` — unconditionally,
whether or not the page declares any of them. Measured on a 68-line
scaffold page: ~136 ms per parse, of which ~135 ms is that spawn. A full
incremental rebuild of one changed page measures ~136-150 ms, so the
Python-side work below is a rounding error against it.

For a **typical 200-line page** with one or two segments:
- 1-3 `ast.parse` calls
- 5-15 milliseconds of Python parsing
- plus the ~135 ms extractor spawn, which is the real cost

For a **complex 1500-line page** like the playground:
- 1-2 `ast.parse` calls (single Python section)
- 10-30 milliseconds total

For a **multi-section page** with 4-6 alternating segments:
- 4-8 `ast.parse` calls
- 20-50 milliseconds

The walker is O(n) in the typical case where each `ast.parse`
finishes quickly. The walk-back loop is bounded by the SyntaxError
lineno, so it doesn't degrade to O(n²) in practice. The state machine
in `_JsState.advance` is character-by-character but only runs on
lines that the walker visits — typically less than half of a file.

Reporting JSX syntax errors (`report_jsx_syntax=True`) adds nothing
measurable: the extractor spawn it reads from already happens. What
*would* cost is a second spawn, which is what `validate_jsx=True` used
to do on top of it — now skipped when the first pass already reported
the error. Counted directly: one spawn per compile in dev, in
production, and under `pyxle check`; two only on the old
`validate_jsx`-without-`report_jsx_syntax` path.

---

## How to read the source

If you want to read `compiler/parser.py` end to end, here's the
suggested order:

1. **Lines 1-100** — Module docstring, imports, and the public
   dataclasses (`LoaderDetails`, `ActionDetails`, `PyxDiagnostic`,
   `PyxParseResult`). This is the contract.

2. **Lines 110-175** — Internal helpers: `_Segment`,
   `_DiagnosticCollector`, `_normalize_newlines`, `_join_lines`,
   `_segment_has_content`. These are tiny; they exist to make the
   walker code readable.

3. **Lines 178-220** — `_find_largest_python_at`. The greedy Python
   prefix finder.

4. **Lines 223-345** — `_find_jsx_end_at` and `_JsState`. The
   JS-aware forward walker.

5. **Lines 364-403** — `_auto_detect_segments`. The walker that ties
   the two helpers together.

6. **Lines 405-595** — The broken-Python detector and its supporting
   helpers (`_PYTHON_ONLY_FIRST_TOKENS`, `_JSX_TOPLEVEL_PREFIXES`,
   `_contains_jsx_element_marker`, `_looks_like_jsx_toplevel`,
   `_detect_broken_python_in_jsx_segments`).

7. **Lines 597-810** — Metadata extraction:
   `_concat_segments`, `_map_lineno`, `_detect_loader`,
   `_detect_actions`, `_extract_head_literal`,
   `_collect_head_elements`.

8. **Lines 813-870** — JSX metadata extraction:
   `_detect_script_declarations`, `_detect_image_declarations`,
   `_detect_head_jsx_blocks`, `_validate_jsx_syntax`.

9. **Lines 880-1100** — `PyxParser` (the public class). Two methods:
   `parse` and `parse_text`. They orchestrate everything.

If you only have time for the most interesting 100 lines, read
points 3, 4, and 5. That's the algorithmic core.

---

## Test coverage

The parser is the most heavily tested module in Pyxle. The tests
live in:

- `tests/compiler/test_parser.py` — public API tests, segmentation
  behaviour tests, edge cases (empty file, whitespace-only file,
  pure-Python file, pure-JSX file, CRLF line endings, etc.).

- `tests/compiler/test_parser_diagnostics.py` — every diagnostic
  emission path (loader errors, action errors, HEAD errors, syntax
  errors), in both strict and tolerant modes.

- `tests/compiler/test_parser_hardening.py` — regression tests for
  every bug we've ever found in the parser. Notable entries: the
  unterminated-string test (the parser audit bug), the deeply-nested
  source test (the `MemoryError` guardrail), the JSX-element-marker
  tests (the `<` operator vs JSX element distinction), the async
  function recognition tests, and the playground template literal
  test.

Coverage of `parser.py` is currently **100%** with both line and
branch coverage. Every code path is exercised by at least one test.
This is enforced by the project-wide 95% threshold in
`pyproject.toml` plus careful per-module review.

---

## What this all adds up to

The Pyxle parser is **120 lines of orchestration around `ast.parse`**.
It has no fence markers, no per-line heuristics, no whitespace
conventions. The boundary between Python and JSX is found by parsing
both halves with their respective real parsers (CPython for Python,
Babel for JSX) and trusting their answers.

The complexity that does exist is concentrated in two places:

1. The **JS state machine** that prevents the walker from
   misclassifying content inside JS template literals as Python
   (~80 lines).

2. The **broken-Python heuristic** that catches the failure mode
   where invalid Python silently flows into the JSX section
   (~120 lines).

Both pieces exist because real-world `.pyxl` files include things like
syntax-highlighted code samples (the JS template literal case) and
in-progress code-with-typos (the broken-Python case). The parser is
written to be reliable on the median input *and* robust on the
adversarial tail.

When you write a `.pyxl` file, you don't have to think about any of
this. You just put the Python and the JSX in the same file, in any
order, with as many alternating sections as makes sense. The parser
figures it out.

---

## Where to read next

- **[The compiler](compiler.md)** — What happens *after* the parser:
  how `python_code` and `jsx_code` get written to disk as `.py` and
  `.jsx` artifacts, including the JSX import rewriter that turns
  `import './foo.pyxl'` into `import './foo.jsx'`.

- **[The CLI](cli.md)** — How `pyxle check` uses tolerant mode to
  surface every diagnostic in every file in one pass, and how the
  defensive per-file try/except keeps a single pathological file
  from aborting the entire scan.

- **[The runtime](runtime.md)** — The `@server` and `@action`
  decorators the parser detects, and the contract that lets them
  remain pure tags with no runtime wrapping.
