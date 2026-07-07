from __future__ import annotations

import json
from pathlib import Path

from pyxle.devserver.build import (
    BUILD_CACHE_SCHEMA_VERSION,
    BuildMetadata,
    BuildPaths,
    ensure_fresh_build_cache,
    initialize_build_directories,
    load_build_metadata,
    save_build_metadata,
)
from pyxle.devserver.settings import DevServerSettings


def make_settings(tmp_path: Path) -> DevServerSettings:
    project = tmp_path / "app"
    project.mkdir()
    return DevServerSettings.from_project_root(project)


def test_initialize_build_directories_creates_structure(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)

    paths = initialize_build_directories(settings)

    assert isinstance(paths, BuildPaths)
    assert paths.build_root.exists()
    assert (paths.client_root / "pages").exists()
    assert (paths.server_root / "pages").exists()
    assert paths.metadata_root.exists()


def test_initialize_build_directories_is_idempotent(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)

    # Create directories manually to simulate prior runs.
    settings.client_build_dir.mkdir(parents=True, exist_ok=True)
    settings.server_build_dir.mkdir(parents=True, exist_ok=True)

    paths_first = initialize_build_directories(settings)
    paths_second = initialize_build_directories(settings)

    assert paths_first == paths_second
    assert (settings.build_root / "client/pages").exists()
    assert (settings.build_root / "server/pages").exists()


def test_ensure_fresh_build_cache_writes_metadata(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)

    paths, metadata = ensure_fresh_build_cache(settings, schema_version="42")

    metadata_path = paths.build_root / "meta.json"
    assert metadata_path.exists()
    with metadata_path.open("r", encoding="utf-8") as file:
        payload = json.load(file)

    assert payload["schema_version"] == "42"
    assert payload["sources"] == {}
    assert isinstance(metadata, BuildMetadata)
    assert metadata.schema_version == "42"
    assert metadata.sources == {}


def test_ensure_fresh_build_cache_preserves_existing_files_when_schema_matches(
    tmp_path: Path,
) -> None:
    settings = make_settings(tmp_path)
    paths, _ = ensure_fresh_build_cache(settings)

    sentinel = paths.client_root / "pages" / "sentinel.txt"
    sentinel.write_text("keep me")

    ensure_fresh_build_cache(settings, schema_version=BUILD_CACHE_SCHEMA_VERSION)

    assert sentinel.exists()


def test_ensure_fresh_build_cache_recreates_cache_on_schema_change(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    paths, _ = ensure_fresh_build_cache(settings, schema_version="1")

    sentinel = paths.server_root / "pages" / "stale.py"
    sentinel.parent.mkdir(parents=True, exist_ok=True)
    sentinel.write_text("# stale artifact")

    ensure_fresh_build_cache(settings, schema_version="2")

    assert not sentinel.exists()
    metadata_path = settings.build_root / "meta.json"
    with metadata_path.open("r", encoding="utf-8") as file:
        payload = json.load(file)

    assert payload["schema_version"] == "2"


def test_ensure_fresh_build_cache_handles_corrupt_metadata(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    paths, _ = ensure_fresh_build_cache(settings, schema_version="1")
    metadata_path = paths.build_root / "meta.json"
    metadata_path.write_text("not valid json", encoding="utf-8")

    ensure_fresh_build_cache(settings, schema_version="3")

    with metadata_path.open("r", encoding="utf-8") as file:
        payload = json.load(file)

    assert payload["schema_version"] == "3"


    def test_load_build_metadata_returns_empty_when_missing(tmp_path: Path) -> None:
        settings = make_settings(tmp_path)

        metadata = load_build_metadata(settings.build_root)

        assert metadata.schema_version == BUILD_CACHE_SCHEMA_VERSION
        assert metadata.sources == {}


    def test_save_build_metadata_writes_payload(tmp_path: Path) -> None:
        settings = make_settings(tmp_path)
        paths = initialize_build_directories(settings)
        metadata = BuildMetadata.empty(schema_version="9")

        save_build_metadata(paths.build_root, metadata)

        with (paths.build_root / "meta.json").open("r", encoding="utf-8") as file:
            payload = json.load(file)

        assert payload == {"schema_version": "9", "sources": {}}


def test_save_build_metadata_skips_identical_content(tmp_path: Path) -> None:
    """Rewriting unchanged metadata must not touch the file (mtime stays)."""
    import time

    settings = make_settings(tmp_path)
    initialize_build_directories(settings)
    metadata = BuildMetadata(
        schema_version=BUILD_CACHE_SCHEMA_VERSION,
        sources={},
    )

    save_build_metadata(settings.build_root, metadata)
    meta_path = settings.build_root / "meta.json"
    before = meta_path.stat().st_mtime_ns
    time.sleep(0.02)

    save_build_metadata(settings.build_root, metadata)

    assert meta_path.stat().st_mtime_ns == before
    assert not (settings.build_root / "meta.json.tmp").exists()


def test_save_build_metadata_replaces_changed_content_atomically(tmp_path: Path) -> None:
    """Changed metadata is rewritten (via temp + rename) with no temp leftover."""
    from pyxle.devserver.build import CachedSourceRecord

    settings = make_settings(tmp_path)
    initialize_build_directories(settings)
    save_build_metadata(
        settings.build_root,
        BuildMetadata(schema_version=BUILD_CACHE_SCHEMA_VERSION, sources={}),
    )

    updated = BuildMetadata(
        schema_version=BUILD_CACHE_SCHEMA_VERSION,
        sources={"index.pyxl": CachedSourceRecord(kind="page", content_hash="abc")},
    )
    save_build_metadata(settings.build_root, updated)

    meta_path = settings.build_root / "meta.json"
    payload = json.loads(meta_path.read_text(encoding="utf-8"))
    assert payload["sources"] == {"index.pyxl": {"kind": "page", "hash": "abc"}}
    assert not (settings.build_root / "meta.json.tmp").exists()
