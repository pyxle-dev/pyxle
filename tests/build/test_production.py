"""Tests for the production app factory (``pyxle.build.production``).

Covers the single-process assembly helper, the asset/dist resolution mirrors,
the worker-subprocess env contract, and the importable ``create_app`` factory
used by ``pyxle serve --workers N``.
"""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from pyxle.build import production
from pyxle.build.production import (
    ENV_CONFIG,
    ENV_DIST,
    ENV_HOST,
    ENV_PORT,
    ENV_PROJECT_ROOT,
    ENV_SERVE_STATIC,
    ENV_SSR_WORKERS,
    FACTORY_IMPORT_STRING,
    ProductionServeError,
    _resolve_dist_directory,
    _resolve_global_script_entries,
    _resolve_global_style_entries,
    _resolve_pool_size,
    build_production_app,
    build_settings,
    create_app,
    serve_worker_env,
)
from pyxle.config import PyxleConfig


def _make_project(tmp_path: Path, *, with_manifest: bool = True) -> tuple[Path, Path]:
    """Create a minimal built project; return ``(project_root, dist)``."""
    project = tmp_path / "app"
    (project / "pages").mkdir(parents=True)
    (project / "public").mkdir(parents=True)
    dist = project / "dist"
    (dist / "public").mkdir(parents=True)
    (dist / "client").mkdir(parents=True)
    if with_manifest:
        (dist / "page-manifest.json").write_text(
            '{"pages": {}, "generated_at": "2024-01-01"}', encoding="utf-8"
        )
    return project, dist


def _stub_assembly(monkeypatch, captured: dict) -> None:
    """Replace the heavy assembly seams so tests need no Node/compilation."""
    monkeypatch.setattr(production, "build_metadata_registry", lambda settings: object())
    monkeypatch.setattr(production, "build_route_table", lambda registry: [])

    def fake_create_app(settings, routes, **kwargs):
        captured["settings"] = settings
        captured["routes"] = routes
        captured.update(kwargs)
        return SimpleNamespace(state=SimpleNamespace(pyxle_ready=False))

    monkeypatch.setattr(production, "create_starlette_app", fake_create_app)


# ── asset / dist resolution mirrors ──────────────────────────────────────────


def test_factory_import_string_is_stable() -> None:
    assert FACTORY_IMPORT_STRING == "pyxle.build.production:create_app"


def test_resolve_dist_directory_default_absolute_and_relative(tmp_path: Path) -> None:
    assert _resolve_dist_directory(tmp_path, None) == (tmp_path / "dist").resolve()
    absolute = (tmp_path / "out").resolve()
    assert _resolve_dist_directory(tmp_path, absolute) == absolute
    assert _resolve_dist_directory(tmp_path, Path("rel")) == (tmp_path / "rel").resolve()


def test_resolve_global_script_entries_dedupe(tmp_path: Path) -> None:
    config = PyxleConfig(global_scripts=(" scripts/a.js ", "", "scripts/a.js", "scripts/b.js"))
    assert _resolve_global_script_entries(tmp_path, config) == ("scripts/a.js", "scripts/b.js")


def test_resolve_global_style_entries_auto_detects_global_css(tmp_path: Path) -> None:
    (tmp_path / "styles").mkdir()
    (tmp_path / "styles" / "global.css").write_text("body{}", encoding="utf-8")
    assert _resolve_global_style_entries(tmp_path, PyxleConfig()) == ("styles/global.css",)


def test_resolve_global_style_entries_explicit_skips_autodetect(tmp_path: Path) -> None:
    (tmp_path / "styles").mkdir()
    (tmp_path / "styles" / "global.css").write_text("body{}", encoding="utf-8")
    config = PyxleConfig(global_styles=("styles/theme.css", "styles/theme.css"))
    assert _resolve_global_style_entries(tmp_path, config) == ("styles/theme.css",)


@pytest.mark.parametrize(
    "requested,expected",
    [(1, 1), (3, 3), (0, min(os.cpu_count() or 2, 4))],
)
def test_resolve_pool_size(requested: int, expected: int) -> None:
    assert _resolve_pool_size(requested) == expected


# ── build_settings ───────────────────────────────────────────────────────────


def test_build_settings_applies_overrides_and_styles(tmp_path: Path) -> None:
    project, _ = _make_project(tmp_path)
    (project / "styles").mkdir()
    (project / "styles" / "global.css").write_text("body{}", encoding="utf-8")

    settings = build_settings(project, host="0.0.0.0", port=9001, ssr_workers=2)

    assert settings.starlette_host == "0.0.0.0"
    assert settings.starlette_port == 9001
    assert settings.ssr_workers == 2
    assert settings.debug is False
    assert any("global.css" in str(s.source if hasattr(s, "source") else s) for s in settings.global_stylesheets)


# ── build_production_app ─────────────────────────────────────────────────────


def test_build_production_app_missing_manifest_raises(tmp_path: Path) -> None:
    project, dist = _make_project(tmp_path, with_manifest=False)
    settings = build_settings(project)
    with pytest.raises(ProductionServeError) as excinfo:
        build_production_app(settings, dist)
    assert "page-manifest.json not found" in str(excinfo.value)


def test_build_production_app_assembles_and_sizes_pool(tmp_path: Path, monkeypatch) -> None:
    project, dist = _make_project(tmp_path)
    settings = build_settings(project, ssr_workers=2)
    captured: dict = {}
    _stub_assembly(monkeypatch, captured)

    pool_args: dict = {}

    def fake_pool(**kwargs):
        pool_args.update(kwargs)
        return SimpleNamespace()

    monkeypatch.setattr("pyxle.ssr.worker_pool.SsrWorkerPool", fake_pool)

    app, pool_size = build_production_app(settings, dist, serve_static=True)

    assert app.state.pyxle_ready is True
    assert pool_size == 2
    assert pool_args["size"] == 2
    assert captured["public_static_dir"] == dist / "public"
    assert captured["client_static_dir"] == dist / "client"
    assert captured["serve_static"] is True
    assert captured["pool"] is not None


def test_build_production_app_serve_static_false_disables_mounts(tmp_path: Path, monkeypatch) -> None:
    project, dist = _make_project(tmp_path)
    settings = build_settings(project, ssr_workers=1)
    captured: dict = {}
    _stub_assembly(monkeypatch, captured)
    monkeypatch.setattr("pyxle.ssr.worker_pool.SsrWorkerPool", lambda **kw: SimpleNamespace())

    build_production_app(settings, dist, serve_static=False)

    assert captured["public_static_dir"] is None
    assert captured["client_static_dir"] is None
    assert captured["serve_static"] is False


def test_build_production_app_falls_back_when_dist_assets_missing(tmp_path: Path, monkeypatch) -> None:
    project, dist = _make_project(tmp_path)
    # Remove the built asset dirs to exercise the fallback/None branches.
    (dist / "public").rmdir()
    (dist / "client").rmdir()
    settings = build_settings(project, ssr_workers=1)
    captured: dict = {}
    _stub_assembly(monkeypatch, captured)
    monkeypatch.setattr("pyxle.ssr.worker_pool.SsrWorkerPool", lambda **kw: SimpleNamespace())

    build_production_app(settings, dist)

    assert captured["public_static_dir"] == settings.public_dir  # fell back to source
    assert captured["client_static_dir"] is None  # 404s for /client


def test_build_production_app_zero_ssr_workers_builds_no_pool_when_cpu_zero(
    tmp_path: Path, monkeypatch
) -> None:
    # When the resolved pool size is non-positive, no pool is constructed.
    project, dist = _make_project(tmp_path)
    settings = build_settings(project, ssr_workers=1)
    captured: dict = {}
    _stub_assembly(monkeypatch, captured)
    monkeypatch.setattr(production, "_resolve_pool_size", lambda _n: 0)

    def explode(**_kwargs):  # pragma: no cover - must not be called
        raise AssertionError("pool must not be built when size <= 0")

    monkeypatch.setattr("pyxle.ssr.worker_pool.SsrWorkerPool", explode)

    _, pool_size = build_production_app(settings, dist)

    assert pool_size == 0
    assert captured["pool"] is None


# ── serve_worker_env ─────────────────────────────────────────────────────────


def test_serve_worker_env_minimal(tmp_path: Path) -> None:
    env = serve_worker_env(
        tmp_path,
        config_path=None,
        dist_dir=None,
        host=None,
        port=None,
        ssr_workers=None,
        serve_static=False,
    )
    assert env == {ENV_PROJECT_ROOT: str(tmp_path), ENV_SERVE_STATIC: "0"}


def test_serve_worker_env_full(tmp_path: Path) -> None:
    env = serve_worker_env(
        tmp_path,
        config_path=Path("custom.json"),
        dist_dir=Path("out"),
        host="0.0.0.0",
        port=9000,
        ssr_workers=4,
        serve_static=True,
    )
    assert env[ENV_PROJECT_ROOT] == str(tmp_path)
    assert env[ENV_CONFIG] == "custom.json"
    assert env[ENV_DIST] == "out"
    assert env[ENV_HOST] == "0.0.0.0"
    assert env[ENV_PORT] == "9000"
    assert env[ENV_SSR_WORKERS] == "4"
    assert env[ENV_SERVE_STATIC] == "1"


# ── create_app (the uvicorn factory) ─────────────────────────────────────────


def test_create_app_rebuilds_from_env(tmp_path: Path, monkeypatch) -> None:
    project, dist = _make_project(tmp_path)
    captured: dict = {}
    _stub_assembly(monkeypatch, captured)
    monkeypatch.setattr("pyxle.ssr.worker_pool.SsrWorkerPool", lambda **kw: SimpleNamespace())

    monkeypatch.setenv(ENV_PROJECT_ROOT, str(project))
    monkeypatch.setenv(ENV_DIST, str(dist))
    monkeypatch.setenv(ENV_HOST, "127.0.0.1")
    monkeypatch.setenv(ENV_PORT, "8123")
    monkeypatch.setenv(ENV_SSR_WORKERS, "1")
    monkeypatch.setenv(ENV_SERVE_STATIC, "1")

    app = create_app()

    assert app.state.pyxle_ready is True
    assert captured["settings"].starlette_host == "127.0.0.1"
    assert captured["settings"].starlette_port == 8123
    assert captured["settings"].ssr_workers == 1
    assert captured["serve_static"] is True


def test_create_app_honours_serve_static_env(tmp_path: Path, monkeypatch) -> None:
    project, dist = _make_project(tmp_path)
    captured: dict = {}
    _stub_assembly(monkeypatch, captured)
    monkeypatch.setattr("pyxle.ssr.worker_pool.SsrWorkerPool", lambda **kw: SimpleNamespace())

    # Only the required project-root var; everything else defaults.
    monkeypatch.setenv(ENV_PROJECT_ROOT, str(project))
    monkeypatch.delenv(ENV_DIST, raising=False)
    monkeypatch.setenv(ENV_SERVE_STATIC, "0")

    create_app()

    assert captured["serve_static"] is False
    assert captured["public_static_dir"] is None
