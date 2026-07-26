from __future__ import annotations

import json
from pathlib import Path

import pytest

from pyxle.devserver.builder import build_once
from pyxle.devserver.settings import DevServerSettings


@pytest.fixture
def project(tmp_path: Path) -> DevServerSettings:
    root = tmp_path / "project"
    (root / "pages").mkdir(parents=True)
    (root / "public").mkdir()
    settings = DevServerSettings.from_project_root(root)
    create_sample_sources(settings)
    return settings


def create_sample_sources(settings: DevServerSettings) -> None:
    write_file(
        settings.pages_dir / "about.pyxl",
        "import React from 'react';\n\nexport default function About() {\n  return <div>About</div>;\n}\n",
    )
    write_file(
        settings.pages_dir / "api/pulse.py",
        "async def endpoint(request):\n    return {'message': 'hi'}\n",
    )
    write_file(
        settings.pages_dir / "components/layout.jsx",
        "export const Layout = ({ children }) => <div>{children}</div>;\n",
    )


def write_file(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def read_meta(settings: DevServerSettings) -> dict[str, object]:
    meta_path = settings.build_root / "meta.json"
    with meta_path.open("r", encoding="utf-8") as file:
        return json.load(file)


def test_build_once_compiles_pages_and_copies_api(project: DevServerSettings) -> None:
    summary = build_once(project)

    assert summary.compiled_pages == ["about.pyxl"]
    assert summary.copied_api_modules == ["api/pulse.py"]
    assert summary.copied_client_assets == ["components/layout.jsx"]
    assert summary.skipped == []
    assert summary.removed == []

    assert (project.build_root / "client/pages/about.jsx").exists()
    assert (project.build_root / "client/pages/components/layout.jsx").exists()
    assert (project.build_root / "server/pages/about.py").exists()
    assert (project.build_root / "metadata/pages/about.json").exists()
    assert (project.build_root / "server/api/pulse.py").exists()

    metadata = read_meta(project)
    assert set(metadata["sources"].keys()) == {"about.pyxl", "api/pulse.py", "components/layout.jsx"}


def test_build_once_skips_unchanged_sources(project: DevServerSettings) -> None:
    build_once(project)

    summary = build_once(project)

    assert summary.compiled_pages == []
    assert summary.copied_api_modules == []
    assert summary.copied_client_assets == []
    assert summary.removed == []
    assert set(summary.skipped) == {"about.pyxl", "api/pulse.py", "components/layout.jsx"}


def test_build_once_reacts_to_changes_and_deletions(project: DevServerSettings) -> None:
    build_once(project)

    # Modify the page and remove the API module.
    write_file(
        project.pages_dir / "about.pyxl",
        "import React from 'react';\n\nexport default function About() {\n  return <div>Updated</div>;\n}\n",
    )
    (project.pages_dir / "api/pulse.py").unlink()

    summary = build_once(project)

    assert summary.compiled_pages == ["about.pyxl"]
    assert summary.copied_api_modules == []
    assert summary.copied_client_assets == []
    assert summary.removed == ["api/pulse.py"]
    assert summary.skipped == ["components/layout.jsx"]

    # The API artifact should be removed while the page artifacts remain.
    assert not (project.build_root / "server/api/pulse.py").exists()
    assert (project.build_root / "server/pages/about.py").exists()

    metadata = read_meta(project)
    assert set(metadata["sources"].keys()) == {"about.pyxl", "components/layout.jsx"}


def test_build_once_force_rebuild_reprocesses_all_sources(project: DevServerSettings) -> None:
    build_once(project)

    summary = build_once(project, force_rebuild=True)

    assert summary.compiled_pages == ["about.pyxl"]
    assert summary.copied_api_modules == ["api/pulse.py"]
    assert summary.copied_client_assets == ["components/layout.jsx"]
    assert summary.skipped == []


def test_build_once_handles_page_removal(project: DevServerSettings) -> None:
    build_once(project)

    # The page's .jsx is recorded in the client source-map sidecar.
    sidecar_path = project.build_root / "client/pyxl-sourcemaps.json"
    assert "pages/about.jsx" in json.loads(sidecar_path.read_text(encoding="utf-8"))

    # Remove the page source to trigger artifact cleanup.
    (project.pages_dir / "about.pyxl").unlink()

    summary = build_once(project)

    assert summary.removed == ["about.pyxl"]
    assert not (project.build_root / "client/pages/about.jsx").exists()
    assert not (project.build_root / "server/pages/about.py").exists()
    assert not (project.build_root / "metadata/pages/about.json").exists()
    # …and the reconcile pass drops its now-stale sidecar entry.
    assert "pages/about.jsx" not in json.loads(
        sidecar_path.read_text(encoding="utf-8")
    )


def test_build_once_tracks_client_asset_changes(project: DevServerSettings) -> None:
    build_once(project)

    asset_path = project.pages_dir / "components/layout.jsx"
    asset_path.write_text("export const Layout = () => <main />;\n", encoding="utf-8")

    summary = build_once(project)

    assert summary.compiled_pages == []
    assert summary.copied_api_modules == []
    assert summary.copied_client_assets == ["components/layout.jsx"]
    assert summary.removed == []

    compiled_asset = project.build_root / "client/pages/components/layout.jsx"
    assert compiled_asset.exists()

    asset_path.unlink()
    summary = build_once(project)
    assert summary.removed == ["components/layout.jsx"]
    assert not compiled_asset.exists()


def test_build_once_syncs_global_stylesheets(tmp_path: Path) -> None:
    root = tmp_path / "styled"
    (root / "pages").mkdir(parents=True)
    (root / "public").mkdir()
    write_file(
        root / "pages" / "index.pyxl",
        "import React from 'react';\n\nexport default function Home() {\n  return <div>Home</div>;\n}\n",
    )
    style_path = root / "styles" / "global.css"
    style_path.parent.mkdir(parents=True, exist_ok=True)
    style_path.write_text("body { color: #333; }\n", encoding="utf-8")

    settings = DevServerSettings.from_project_root(
        root,
        global_stylesheets=("styles/global.css",),
    )

    summary = build_once(settings)
    assert summary.synced_stylesheets == ["styles/global.css"]
    generated = settings.client_build_dir / settings.global_stylesheets[0].client_relative_path
    assert generated.exists()
    assert "#333" in generated.read_text(encoding="utf-8")

    summary = build_once(settings)
    assert summary.synced_stylesheets == []

    style_path.write_text("body { color: #111; }\n", encoding="utf-8")
    summary = build_once(settings)
    assert summary.synced_stylesheets == ["styles/global.css"]
    assert "#111" in generated.read_text(encoding="utf-8")


def test_build_once_syncs_global_scripts(tmp_path: Path) -> None:
    root = tmp_path / "scripted"
    (root / "pages").mkdir(parents=True)
    (root / "public").mkdir()
    write_file(
        root / "pages" / "index.pyxl",
        "import React from 'react';\n\nexport default function Home() {\n  return <div>Home</div>;\n}\n",
    )
    script_path = root / "scripts" / "analytics.js"
    script_path.parent.mkdir(parents=True, exist_ok=True)
    script_path.write_text("console.log('analytics');\n", encoding="utf-8")

    settings = DevServerSettings.from_project_root(
        root,
        global_scripts=("scripts/analytics.js",),
    )

    summary = build_once(settings)
    assert summary.synced_scripts == ["scripts/analytics.js"]
    generated = settings.client_build_dir / settings.global_scripts[0].client_relative_path
    assert generated.exists()
    assert "analytics" in generated.read_text(encoding="utf-8")

    summary = build_once(settings)
    assert summary.synced_scripts == []

    script_path.write_text("console.log('updated');\n", encoding="utf-8")
    summary = build_once(settings)
    assert summary.synced_scripts == ["scripts/analytics.js"]
    assert "updated" in generated.read_text(encoding="utf-8")


def test_build_once_leaves_unchanged_generated_files_untouched(
    project: DevServerSettings,
) -> None:
    """A rebuild must not rewrite stable generated artifacts.

    Rewriting ``vite.config.js`` with identical content still bumps its mtime,
    and Vite answers a config-file change with a full dev-server restart — so
    an unchanged config (and unchanged ``meta.json``) must keep its mtime.
    """
    import time

    from pyxle.devserver.client_files import VITE_CONFIG_FILENAME

    build_once(project, force_rebuild=True)

    vite_config = project.client_build_dir / VITE_CONFIG_FILENAME
    meta_json = project.build_root / "meta.json"
    assert vite_config.exists()
    assert meta_json.exists()

    before = {
        path: path.stat().st_mtime_ns for path in (vite_config, meta_json)
    }
    time.sleep(0.02)

    build_once(project)

    for path, mtime in before.items():
        assert path.stat().st_mtime_ns == mtime, f"{path.name} was rewritten"


def test_build_once_serializes_concurrent_invocations(
    project: DevServerSettings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Overlapping build_once calls (debounce-timer race) run one at a time.

    A concurrent pass used to read ``meta.json`` while another pass was
    mid-write, misdiagnose the torn read as a schema mismatch, and wipe the
    build cache (recreating ``vite.config.js`` → spurious Vite restart).
    """
    import threading
    import time

    from pyxle.devserver import builder as builder_module

    active = 0
    max_active = 0
    gauge_lock = threading.Lock()
    real_scan = builder_module.scan_source_tree

    def tracking_scan(settings: DevServerSettings):
        nonlocal active, max_active
        with gauge_lock:
            active += 1
            max_active = max(max_active, active)
        time.sleep(0.05)
        with gauge_lock:
            active -= 1
        return real_scan(settings)

    monkeypatch.setattr(builder_module, "scan_source_tree", tracking_scan)

    errors: list[BaseException] = []

    def run_build() -> None:
        try:
            build_once(project)
        except BaseException as exc:  # pragma: no cover - fails the assertion below
            errors.append(exc)

    threads = [threading.Thread(target=run_build) for _ in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert errors == []
    assert max_active == 1, "build passes overlapped"
