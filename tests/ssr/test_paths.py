"""Tests for :mod:`pyxle.ssr.paths`.

The memo exists so the SSR render path stops making a blocking ``realpath(3)``
walk per request (CLAUDE.md rules 8 and 15). These tests pin both halves of
that: the value must stay identical to ``Path.resolve()`` including symlink
normalisation, and the hot path must not re-resolve a path it has already seen.
"""

from __future__ import annotations

import asyncio
import json
import sys
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pyxle.ssr.paths import (
    RESOLVED_PATH_CACHE_MAXSIZE,
    clear_resolved_paths,
    resolve_component_path,
)
from pyxle.ssr.renderer import ComponentRenderer, RenderResult
from pyxle.ssr.worker_pool import SsrWorkerPool


@pytest.fixture
def anyio_backend() -> str:  # pragma: no cover - fixture wiring
    return "asyncio"


def _echo_proc():
    """Fake Node worker that answers every request with a successful render."""
    read_queue: asyncio.Queue = asyncio.Queue()
    proc = MagicMock()
    proc.stdin = MagicMock()
    proc.stdin.is_closing.return_value = False
    proc.stdin.close = MagicMock()
    proc.stdin.drain = AsyncMock()

    def capture_write(data: bytes) -> None:
        payload = json.loads(data.decode().strip())
        reply = {"id": payload["id"], "ok": True, "html": "<div/>"}
        read_queue.put_nowait((json.dumps(reply) + "\n").encode())

    proc.stdin.write = MagicMock(side_effect=capture_write)

    async def fake_read(n: int = -1) -> bytes:
        return await read_queue.get()

    proc.stdout = MagicMock()
    proc.stdout.read = fake_read
    proc.wait = AsyncMock(return_value=0)
    proc.kill = MagicMock()
    return proc


@pytest.fixture(autouse=True)
def _clean_cache():
    """Every test starts and ends with an empty memo."""
    clear_resolved_paths()
    yield
    clear_resolved_paths()


def test_matches_path_resolve(tmp_path: Path) -> None:
    component = tmp_path / "pages" / "index.js"
    component.parent.mkdir(parents=True)
    component.write_text("export default () => null;")

    assert resolve_component_path(component) == component.resolve()


@pytest.mark.skipif(
    sys.platform == "win32", reason="symlink creation needs elevation on Windows"
)
def test_follows_symlinks_like_resolve(tmp_path: Path) -> None:
    """The whole reason this is ``resolve()`` and not ``abspath()``.

    A build directory reached through a symlink must canonicalise to the real
    location -- the resolved path is handed to a Node subprocess as the module
    it has to import.
    """
    real_build = tmp_path / "real-build" / "client" / "pages"
    real_build.mkdir(parents=True)
    component = real_build / "index.js"
    component.write_text("export default () => null;")

    link = tmp_path / "current"
    link.symlink_to(tmp_path / "real-build", target_is_directory=True)
    via_link = link / "client" / "pages" / "index.js"

    resolved = resolve_component_path(via_link)

    assert resolved == component.resolve()
    assert "current" not in resolved.parts, "symlink was not normalised away"


@pytest.mark.skipif(
    sys.platform == "win32", reason="symlink creation needs elevation on Windows"
)
def test_distinct_inputs_sharing_a_target_are_not_conflated(tmp_path: Path) -> None:
    """Two different input paths that resolve to the same file both work."""
    target_dir = tmp_path / "real"
    target_dir.mkdir()
    component = target_dir / "index.js"
    component.write_text("export default () => null;")
    link = tmp_path / "alias"
    link.symlink_to(target_dir, target_is_directory=True)

    assert resolve_component_path(component) == component.resolve()
    assert resolve_component_path(link / "index.js") == component.resolve()


def test_memoises_repeat_lookups(tmp_path: Path) -> None:
    component = tmp_path / "index.js"
    component.write_text("export default () => null;")

    resolve_component_path(component)
    before = resolve_component_path.cache_info()
    for _ in range(50):
        resolve_component_path(component)
    after = resolve_component_path.cache_info()

    assert after.hits - before.hits == 50
    assert after.misses == before.misses, "a warm path must not re-resolve"


def test_cache_is_bounded(tmp_path: Path) -> None:
    """CLAUDE.md rule 17: the memo must not grow without bound."""
    assert resolve_component_path.cache_info().maxsize == RESOLVED_PATH_CACHE_MAXSIZE
    assert RESOLVED_PATH_CACHE_MAXSIZE > 0

    for index in range(RESOLVED_PATH_CACHE_MAXSIZE + 25):
        resolve_component_path(tmp_path / f"page-{index}.js")

    assert resolve_component_path.cache_info().currsize == RESOLVED_PATH_CACHE_MAXSIZE


def test_clear_drops_memoised_entries(tmp_path: Path) -> None:
    component = tmp_path / "index.js"
    component.write_text("export default () => null;")
    resolve_component_path(component)
    assert resolve_component_path.cache_info().currsize == 1

    clear_resolved_paths()

    assert resolve_component_path.cache_info().currsize == 0


def test_missing_path_still_canonicalises(tmp_path: Path) -> None:
    """``resolve()`` is non-strict; a path that does not exist still resolves."""
    missing = tmp_path / "nope" / "gone.js"
    assert resolve_component_path(missing) == missing.resolve()


# --- Regression guard: no blocking realpath(3) on the SSR render path -------
#
# This is the test that would have caught the original defect. A raw
# ``Path.resolve()`` costs ~18.7us of on-CPU event-loop stall; at 100
# concurrent renders each per-request call added ~1.9ms of head-of-line delay
# to every other in-flight request. If someone reintroduces one on a warm
# render, these fail.


@contextmanager
def _count_raw_resolves():
    """Count real ``Path.resolve()`` calls, bypassing the memo."""
    calls: list[Path] = []
    real = Path.resolve

    def counting(self, *args, **kwargs):
        calls.append(self)
        return real(self, *args, **kwargs)

    with patch.object(Path, "resolve", counting):
        yield calls


@pytest.mark.anyio
async def test_component_renderer_does_not_resolve_per_render(tmp_path: Path) -> None:
    component = tmp_path / "pages" / "index.js"
    component.parent.mkdir(parents=True)
    component.write_text("export default () => null;")

    async def _render(_props, **_kwargs):
        return RenderResult(html="<div/>", inline_styles=(), head_elements=())

    renderer = ComponentRenderer(factory=lambda _path: _render)
    await renderer.render(component, {})  # warm

    with _count_raw_resolves() as calls:
        for _ in range(5):
            await renderer.render(component, {})

    assert calls == [], f"warm render still called Path.resolve(): {calls}"


@pytest.mark.anyio
async def test_worker_pool_render_does_not_resolve_per_render(tmp_path: Path) -> None:
    project_root = tmp_path / "p"
    client_root = project_root / ".pyxle-build" / "client"
    component = client_root / "pages" / "page.jsx"
    component.parent.mkdir(parents=True)
    component.touch()

    proc = _echo_proc()
    with (
        patch("pyxle.ssr.worker_pool.shutil.which", return_value="/usr/bin/node"),
        patch(
            "pyxle.ssr.worker_pool.asyncio.create_subprocess_exec",
            new_callable=AsyncMock,
            return_value=proc,
        ),
        patch("pyxle.ssr.worker_pool.Path.exists", return_value=True),
    ):
        pool = SsrWorkerPool(size=1, project_root=project_root, client_root=client_root)
        await pool.start()
        await pool.render(component, {})  # warm

        with _count_raw_resolves() as calls:
            for _ in range(5):
                await pool.render(component, {})

        assert calls == [], f"warm pool render still called Path.resolve(): {calls}"

        # ...and the path it sends the worker is still the canonical one.
        sent = json.loads(proc.stdin.write.call_args_list[-1][0][0].decode().strip())
        assert sent["componentPath"] == str(component.resolve())

        await pool.stop()
