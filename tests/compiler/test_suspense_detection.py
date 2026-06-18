"""Tests for ``<Suspense>`` detection — the implicit streaming-SSR opt-in.

A page that renders a ``<Suspense>`` boundary needs the streaming render path
(``renderToString`` only emits fallbacks for async boundaries), so the compiler
records ``uses_suspense`` on the parse result. These exercise the real Babel
extractor used by every JSX-metadata pass.
"""

from __future__ import annotations

from pyxle.compiler.parser import PyxParser


class TestSuspenseDetection:
    def test_named_import_suspense_is_detected(self) -> None:
        source = """
import React, { Suspense } from 'react';

export default function Page() {
  return (
    <main>
      <Suspense fallback={<p>loading</p>}>
        <Slow />
      </Suspense>
    </main>
  );
}
"""
        result = PyxParser().parse_text(source)
        assert result.uses_suspense is True

    def test_react_member_suspense_is_detected(self) -> None:
        source = """
import React from 'react';

export default function Page() {
  return (
    <React.Suspense fallback={null}>
      <div />
    </React.Suspense>
  );
}
"""
        result = PyxParser().parse_text(source)
        assert result.uses_suspense is True

    def test_deeply_nested_suspense_is_detected(self) -> None:
        source = """
import React, { Suspense } from 'react';

export default function Page() {
  return (
    <main>
      <section>
        <article>
          <Suspense fallback={<span>...</span>}>
            <Comments />
          </Suspense>
        </article>
      </section>
    </main>
  );
}
"""
        result = PyxParser().parse_text(source)
        assert result.uses_suspense is True

    def test_page_without_suspense_is_not_flagged(self) -> None:
        source = """
import React from 'react';

export default function Page() {
  return <main><h1>Hello</h1></main>;
}
"""
        result = PyxParser().parse_text(source)
        assert result.uses_suspense is False

    def test_suspense_in_a_string_is_not_a_false_positive(self) -> None:
        # The word appears only in text content, never as an element.
        source = """
import React from 'react';

export default function Page() {
  return <main><p>This page does not use Suspense at all.</p></main>;
}
"""
        result = PyxParser().parse_text(source)
        assert result.uses_suspense is False

    def test_consolidated_pass_still_detects_scripts_alongside_suspense(self) -> None:
        # The single Babel pass targets Script/Image/Head/Suspense at once;
        # detecting one must not suppress the others.
        source = """
import React, { Suspense } from 'react';
import { Script } from 'pyxle/client';

export default function Page() {
  return (
    <>
      <Script src="https://example.com/sdk.js" />
      <Suspense fallback={<p>loading</p>}>
        <Slow />
      </Suspense>
    </>
  );
}
"""
        result = PyxParser().parse_text(source)
        assert result.uses_suspense is True
        assert len(result.script_declarations) == 1
        assert result.script_declarations[0]["src"] == "https://example.com/sdk.js"
