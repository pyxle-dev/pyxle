"""``STANDALONE = True`` on a layout: the root of its own chain.

The case this exists for is a section of a site that is not part of the app
around it — a public status page inside an admin console, a print view, an
embedded widget. Without it the only options are to wrap that section in the
app's chrome, or to teach the outer layout to recognise each such section and
render nothing: a conditional that grows a branch per child and puts knowledge
of every one of them in the parent.
"""

from __future__ import annotations

import json

import pytest

from pyxle.compiler.core import compile_file


def write(tmp_path, relative: str, source: str):
    path = tmp_path / "pages" / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source)
    return path


PLAIN_LAYOUT = """@server
async def load(request):
    return {"who": "root"}


import React from 'react';

export default function Layout({ children }) {
    return <div className="shell">{children}</div>;
}
"""

STANDALONE_LAYOUT = """@server
async def load(request):
    return {"who": "section"}


STANDALONE = True


import React from 'react';

export default function Layout({ children }) {
    return <>{children}</>;
}
"""


class TestTheDirectiveIsExtracted:
    def test_a_layout_can_declare_itself_standalone(self, tmp_path):
        source = write(tmp_path, "public/layout.pyxl", STANDALONE_LAYOUT)
        result = compile_file(source, build_root=tmp_path / ".pyxle-build")
        assert result.metadata.standalone is True

    def test_a_plain_layout_is_not_standalone(self, tmp_path):
        source = write(tmp_path, "layout.pyxl", PLAIN_LAYOUT)
        result = compile_file(source, build_root=tmp_path / ".pyxle-build")
        assert result.metadata.standalone is False

    def test_it_survives_the_json_round_trip(self, tmp_path):
        """The composer and the loader chain both read it back from metadata on
        disk, so the flag has to be in the emitted JSON."""
        source = write(tmp_path, "public/layout.pyxl", STANDALONE_LAYOUT)
        compile_file(source, build_root=tmp_path / ".pyxle-build")

        emitted = json.loads(
            (tmp_path / ".pyxle-build" / "metadata" / "pages" / "public" / "layout.json")
            .read_text()
        )
        assert emitted["standalone"] is True

    def test_false_is_accepted_explicitly(self, tmp_path):
        source = write(
            tmp_path, "public/layout.pyxl",
            STANDALONE_LAYOUT.replace("STANDALONE = True", "STANDALONE = False"),
        )
        result = compile_file(source, build_root=tmp_path / ".pyxle-build")
        assert result.metadata.standalone is False

    @pytest.mark.parametrize("bad", ['STANDALONE = "yes"', "STANDALONE = 1"])
    def test_a_non_boolean_is_refused_rather_than_guessed(self, tmp_path, bad):
        """Guessing would mean `STANDALONE = 0` silently meaning True, and a
        section quietly losing the app shell is a confusing way to find out."""
        from pyxle.compiler.exceptions import CompilationError

        source = write(
            tmp_path, "public/layout.pyxl",
            STANDALONE_LAYOUT.replace("STANDALONE = True", bad),
        )
        with pytest.raises(CompilationError, match="STANDALONE must be True or False"):
            compile_file(source, build_root=tmp_path / ".pyxle-build")

    def test_a_page_without_the_directive_compiles_unchanged(self, tmp_path):
        """Back-compatibility: every existing `.pyxl` in the world has no
        STANDALONE, and must behave exactly as before."""
        source = write(tmp_path, "index.pyxl", """@server
async def load(request):
    return {}


import React from 'react';

export default function Page() {
    return <p>hello</p>;
}
""")
        result = compile_file(source, build_root=tmp_path / ".pyxle-build")
        assert result.metadata.standalone is False
