"""Tests for ``ArtifactWriter``'s debugger artifacts (``pyxle/compiler/writers.py``).

Covers the server-module debug footer (the persisted emitted-line → ``.pyxl``
map) and the client source-map sidecar consumed by the generated Vite plugin.
Import-injection behaviour itself is covered in ``test_compile.py`` /
``test_action_compile.py``.
"""

from __future__ import annotations

import json
from pathlib import Path
from textwrap import dedent

import pytest

from pyxle.compiler.linemap import LINE_MAP_DUNDER, SOURCE_DUNDER, extract_debug_info
from pyxle.compiler.parser import PyxParser, PyxParseResult
from pyxle.compiler.writers import (
    CLIENT_SOURCEMAP_SIDECAR,
    ArtifactWriter,
    _atomic_write_replace,
    _relative_posix,
    reconcile_client_sourcemap_sidecar,
)


def _make_writer(tmp_path: Path) -> ArtifactWriter:
    build_root = tmp_path / ".pyxle-build"
    return ArtifactWriter(
        build_root=build_root,
        client_root=build_root / "client",
        server_root=build_root / "server",
        metadata_root=build_root / "metadata",
    )


def _write_source(tmp_path: Path, name: str, content: str) -> Path:
    source = tmp_path / "pages" / name
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text(content, encoding="utf-8")
    return source


def _compile(tmp_path: Path, name: str, content: str):
    source = _write_source(tmp_path, name, content)
    writer = _make_writer(tmp_path)
    parse_result = PyxParser().parse(source)
    result = writer.write(
        source_path=source,
        page_relative_path=Path(name),
        route_path="/" + Path(name).stem,
        alternate_route_paths=None,
        parse_result=parse_result,
    )
    return source, parse_result, result


_LOADER_PAGE = dedent(
    """\
    @server
    async def load(request):
        return {"n": 1}

    import React from 'react';

    export default function Page({ data }) {
        return <div>{data.n}</div>;
    }
    """
)


# ---------------------------------------------------------------------------
# Debug footer
# ---------------------------------------------------------------------------


def test_writer_appends_debug_footer_with_relative_posix_source(tmp_path: Path) -> None:
    source, _, result = _compile(tmp_path, "demo.pyxl", _LOADER_PAGE)

    server_code = result.server_output.read_text(encoding="utf-8")
    info = extract_debug_info(server_code)
    assert info is not None
    # server module lives at .pyxle-build/server/pages/demo.py — the source
    # reference is relative to that directory, as POSIX, never absolute.
    assert info.source_relative_posix == "../../../pages/demo.pyxl"
    assert "\\" not in info.source_relative_posix
    resolved = (result.server_output.parent / info.source_relative_posix).resolve()
    assert resolved == source.resolve()


def test_writer_footer_spans_reproduce_pyxl_lines_exactly(tmp_path: Path) -> None:
    """Every mapped emitted line must be byte-identical to the ``.pyxl`` line
    the span points at, and injected import lines must stay unmapped."""
    source, _, result = _compile(tmp_path, "spans.pyxl", _LOADER_PAGE)

    server_lines = result.server_output.read_text(encoding="utf-8").splitlines()
    pyxl_lines = source.read_text(encoding="utf-8").splitlines()
    info = extract_debug_info("\n".join(server_lines) + "\n")
    assert info is not None
    assert info.spans

    mapped_emitted: set[int] = set()
    for emitted_start, source_start, length in info.spans:
        for offset in range(length):
            emitted = emitted_start + offset
            mapped_emitted.add(emitted)
            assert server_lines[emitted - 1] == pyxl_lines[source_start - 1 + offset]

    # The auto-injected runtime imports are writer artifacts — not .pyxl lines.
    for index, line in enumerate(server_lines, start=1):
        if line.startswith("from pyxle.runtime import"):
            assert index not in mapped_emitted


def test_writer_static_stub_gets_no_footer(tmp_path: Path) -> None:
    _, _, result = _compile(
        tmp_path,
        "static.pyxl",
        "import React from 'react';\n\nexport default function P() { return <p/>; }\n",
    )
    server_code = result.server_output.read_text(encoding="utf-8")
    assert SOURCE_DUNDER not in server_code
    assert LINE_MAP_DUNDER not in server_code
    assert extract_debug_info(server_code) is None


def test_writer_skips_footer_when_no_line_maps(tmp_path: Path) -> None:
    """Python code with no recoverable line map (empty spans) gets no footer —
    a footer with an empty map would be dead weight in the artifact."""
    source = _write_source(
        tmp_path,
        "unmapped.pyxl",
        "export default function P() { return <p/>; }\n",
    )
    parse_result = PyxParseResult(
        python_code="value = 1\n",
        jsx_code="export default function P() { return <p/>; }\n",
        loader=None,
        python_line_numbers=(),  # nothing survives → no spans
        jsx_line_numbers=(1,),
        head_elements=(),
        head_is_dynamic=False,
    )
    writer = _make_writer(tmp_path)
    result = writer.write(
        source_path=source,
        page_relative_path=Path("unmapped.pyxl"),
        route_path="/unmapped",
        alternate_route_paths=None,
        parse_result=parse_result,
    )
    server_code = result.server_output.read_text(encoding="utf-8")
    assert SOURCE_DUNDER not in server_code
    assert LINE_MAP_DUNDER not in server_code


# ---------------------------------------------------------------------------
# Client source-map sidecar
# ---------------------------------------------------------------------------


def test_writer_records_jsx_page_in_sourcemap_sidecar(tmp_path: Path) -> None:
    source, parse_result, result = _compile(tmp_path, "demo.pyxl", _LOADER_PAGE)

    sidecar_path = tmp_path / ".pyxle-build/client" / CLIENT_SOURCEMAP_SIDECAR
    entries = json.loads(sidecar_path.read_text(encoding="utf-8"))
    entry = entries["pages/demo.jsx"]
    assert entry["pyxl"] == "../../pages/demo.pyxl"
    assert entry["lines"] == list(parse_result.jsx_line_numbers)
    resolved = (sidecar_path.parent / entry["pyxl"]).resolve()
    assert resolved == source.resolve()
    # Atomic replace leaves no temp file behind.
    assert not sidecar_path.with_suffix(".json.tmp").exists()


def test_writer_sidecar_aggregates_pages_and_updates_entries(tmp_path: Path) -> None:
    _compile(tmp_path, "one.pyxl", _LOADER_PAGE)
    _compile(tmp_path, "two.pyxl", _LOADER_PAGE)

    sidecar_path = tmp_path / ".pyxle-build/client" / CLIENT_SOURCEMAP_SIDECAR
    entries = json.loads(sidecar_path.read_text(encoding="utf-8"))
    assert set(entries) == {"pages/one.jsx", "pages/two.jsx"}

    # Recompiling a page with a shifted JSX section replaces its entry in
    # place — one key per page, always describing the latest build.
    _, parse_result, _ = _compile(tmp_path, "one.pyxl", "\n\n" + _LOADER_PAGE)
    entries = json.loads(sidecar_path.read_text(encoding="utf-8"))
    assert set(entries) == {"pages/one.jsx", "pages/two.jsx"}
    assert entries["pages/one.jsx"]["lines"] == list(parse_result.jsx_line_numbers)


@pytest.mark.parametrize("corrupt_payload", ["{ not json", "[]", "42"])
def test_writer_sidecar_recovers_from_corrupt_payload(
    tmp_path: Path, corrupt_payload: str
) -> None:
    sidecar_path = tmp_path / ".pyxle-build/client" / CLIENT_SOURCEMAP_SIDECAR
    sidecar_path.parent.mkdir(parents=True)
    sidecar_path.write_text(corrupt_payload, encoding="utf-8")

    _compile(tmp_path, "fresh.pyxl", _LOADER_PAGE)

    entries = json.loads(sidecar_path.read_text(encoding="utf-8"))
    assert set(entries) == {"pages/fresh.jsx"}


def test_writer_sidecar_skipped_for_python_only_page(tmp_path: Path) -> None:
    _compile(
        tmp_path,
        "pyonly.pyxl",
        "@server\nasync def load(request):\n    return {}\n",
    )
    assert not (tmp_path / ".pyxle-build/client" / CLIENT_SOURCEMAP_SIDECAR).exists()


def test_writer_sidecar_entry_removed_when_page_drops_all_jsx(tmp_path: Path) -> None:
    """A page that once had a client component and is edited down to pure Python
    still writes a stub ``.jsx``, so reconcile can't prune it — the writer must
    drop the now-stale sidecar entry itself, or breakpoints bind to a map whose
    generated lines no longer exist."""
    sidecar_path = tmp_path / ".pyxle-build/client" / CLIENT_SOURCEMAP_SIDECAR

    # First build has JSX → an entry is recorded.
    _compile(tmp_path, "shrinks.pyxl", _LOADER_PAGE)
    assert "pages/shrinks.jsx" in json.loads(sidecar_path.read_text(encoding="utf-8"))

    # Recompile the same page with every bit of JSX removed.
    _compile(
        tmp_path,
        "shrinks.pyxl",
        "@server\nasync def load(request):\n    return {}\n",
    )
    entries = json.loads(sidecar_path.read_text(encoding="utf-8"))
    assert "pages/shrinks.jsx" not in entries
    assert not sidecar_path.with_suffix(".json.tmp").exists()


def test_writer_sidecar_removal_ignores_non_dict_payload(tmp_path: Path) -> None:
    # A corrupt sidecar that is valid JSON but not an object must not crash the
    # JSX-less write path — the removal simply no-ops and leaves it as-is.
    sidecar_path = tmp_path / ".pyxle-build/client" / CLIENT_SOURCEMAP_SIDECAR
    sidecar_path.parent.mkdir(parents=True)
    sidecar_path.write_text("[]", encoding="utf-8")

    _compile(
        tmp_path,
        "pyonly.pyxl",
        "@server\nasync def load(request):\n    return {}\n",
    )
    assert sidecar_path.read_text(encoding="utf-8") == "[]"


def test_writer_sidecar_removal_is_noop_when_entry_absent(tmp_path: Path) -> None:
    # Compiling a JSX-less page whose key was never recorded leaves an existing
    # sidecar (describing other pages) byte-for-byte untouched.
    _compile(tmp_path, "keeps.pyxl", _LOADER_PAGE)
    sidecar_path = tmp_path / ".pyxle-build/client" / CLIENT_SOURCEMAP_SIDECAR
    before = sidecar_path.read_text(encoding="utf-8")

    _compile(
        tmp_path,
        "pyonly.pyxl",
        "@server\nasync def load(request):\n    return {}\n",
    )
    assert sidecar_path.read_text(encoding="utf-8") == before


def test_writer_sidecar_lands_before_the_client_jsx(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Vite may re-transform the module the instant the ``.jsx`` changes, so
    the sidecar it reads at that moment must already describe the new build."""
    write_order: list[str] = []
    real_write_text = Path.write_text

    def recording_write_text(self: Path, *args, **kwargs):
        write_order.append(self.name)
        return real_write_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", recording_write_text)
    _compile(tmp_path, "ordered.pyxl", _LOADER_PAGE)

    sidecar_write = write_order.index(CLIENT_SOURCEMAP_SIDECAR + ".tmp")
    jsx_write = write_order.index("ordered.jsx")
    assert sidecar_write < jsx_write


# ---------------------------------------------------------------------------
# reconcile_client_sourcemap_sidecar
# ---------------------------------------------------------------------------


def _client_root(tmp_path: Path) -> Path:
    return tmp_path / ".pyxle-build" / "client"


def test_reconcile_drops_entries_for_deleted_jsx(tmp_path: Path) -> None:
    _compile(tmp_path, "one.pyxl", _LOADER_PAGE)
    _compile(tmp_path, "two.pyxl", _LOADER_PAGE)
    client_root = _client_root(tmp_path)
    sidecar_path = client_root / CLIENT_SOURCEMAP_SIDECAR
    assert set(json.loads(sidecar_path.read_text(encoding="utf-8"))) == {
        "pages/one.jsx",
        "pages/two.jsx",
    }

    # A page whose source (and thus generated .jsx) was removed this pass.
    (client_root / "pages" / "one.jsx").unlink()
    reconcile_client_sourcemap_sidecar(client_root)

    entries = json.loads(sidecar_path.read_text(encoding="utf-8"))
    assert set(entries) == {"pages/two.jsx"}  # the survivor is kept intact
    assert entries["pages/two.jsx"]["pyxl"].endswith("two.pyxl")
    # Atomic replace leaves no temp file behind.
    assert not sidecar_path.with_suffix(".json.tmp").exists()


def test_reconcile_missing_sidecar_is_a_noop(tmp_path: Path) -> None:
    client_root = _client_root(tmp_path)
    client_root.mkdir(parents=True)
    reconcile_client_sourcemap_sidecar(client_root)  # must not raise
    assert not (client_root / CLIENT_SOURCEMAP_SIDECAR).exists()


@pytest.mark.parametrize("payload", ["{ not json", "[]", "42"])
def test_reconcile_malformed_sidecar_is_a_noop(tmp_path: Path, payload: str) -> None:
    client_root = _client_root(tmp_path)
    client_root.mkdir(parents=True)
    sidecar_path = client_root / CLIENT_SOURCEMAP_SIDECAR
    sidecar_path.write_text(payload, encoding="utf-8")

    reconcile_client_sourcemap_sidecar(client_root)

    # A corrupt or non-object payload is left exactly as-is, never rewritten.
    assert sidecar_path.read_text(encoding="utf-8") == payload


def test_reconcile_no_change_leaves_file_untouched(tmp_path: Path) -> None:
    _compile(tmp_path, "one.pyxl", _LOADER_PAGE)
    client_root = _client_root(tmp_path)
    sidecar_path = client_root / CLIENT_SOURCEMAP_SIDECAR

    before = sidecar_path.read_text(encoding="utf-8")
    mtime_before = sidecar_path.stat().st_mtime_ns

    # Every .jsx still exists → nothing to prune, so the file is not rewritten.
    reconcile_client_sourcemap_sidecar(client_root)

    assert sidecar_path.read_text(encoding="utf-8") == before
    assert sidecar_path.stat().st_mtime_ns == mtime_before
    assert not sidecar_path.with_suffix(".json.tmp").exists()


# ---------------------------------------------------------------------------
# Cross-platform path handling
# ---------------------------------------------------------------------------


def test_relative_posix_falls_back_to_absolute_across_drives(monkeypatch, tmp_path: Path) -> None:
    """On Windows, os.path.relpath raises ValueError when target and base are on
    different drives; _relative_posix must not crash the compile — it falls back
    to an absolute POSIX path."""
    import os as _os

    target = tmp_path / "pages" / "demo.pyxl"

    def _raise(*_a, **_k):
        raise ValueError("path is on mount 'C:', start on mount 'D:'")

    monkeypatch.setattr(_os.path, "relpath", _raise)
    result = _relative_posix(target, tmp_path / "build" / "client")

    # An absolute POSIX path (no relpath), never a crash.
    assert result.endswith("pages/demo.pyxl")
    assert "\\" not in result


def test_atomic_write_replace_retries_then_succeeds_on_windows_lock(
    monkeypatch, tmp_path: Path
) -> None:
    """os.replace can raise PermissionError transiently on Windows; the helper
    retries and eventually writes the file rather than propagating."""
    target = tmp_path / "sidecar.json"
    target.write_text("old", encoding="utf-8")

    calls = {"n": 0}
    real_replace = Path.replace

    def flaky_replace(self, dst):
        calls["n"] += 1
        if calls["n"] < 3:
            raise PermissionError("target busy")
        return real_replace(self, dst)

    monkeypatch.setattr(Path, "replace", flaky_replace)
    monkeypatch.setattr("pyxle.compiler.writers.time.sleep", lambda _s: None)

    _atomic_write_replace(target, "new")

    assert calls["n"] == 3
    assert target.read_text(encoding="utf-8") == "new"
    assert not target.with_suffix(".json.tmp").exists()


def test_atomic_write_replace_gives_up_after_retries(monkeypatch, tmp_path: Path) -> None:
    """After exhausting retries the helper re-raises and cleans up the temp file."""
    target = tmp_path / "sidecar.json"

    def always_busy(self, dst):
        raise PermissionError("target busy")

    monkeypatch.setattr(Path, "replace", always_busy)
    monkeypatch.setattr("pyxle.compiler.writers.time.sleep", lambda _s: None)

    with pytest.raises(PermissionError):
        _atomic_write_replace(target, "new")

    assert not target.with_suffix(".json.tmp").exists()
