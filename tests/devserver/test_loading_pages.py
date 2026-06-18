"""Tests for pyxle.devserver.loading_pages — loading.pyxl boundary resolution."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from pyxle.devserver.loading_pages import (
    LoadingBoundaryRegistry,
    build_loading_boundary_registry,
    is_loading_file,
)


class TestFilenameClassification:
    @pytest.mark.parametrize(
        "path,expected",
        [
            ("loading.pyxl", True),
            ("dashboard/loading.pyxl", True),
            ("deep/nested/dir/loading.pyxl", True),
            ("index.pyxl", False),
            ("error.pyxl", False),
            ("loading.py", False),
            ("loading.tsx", False),
            ("my-loading.pyxl", False),
            ("loadings.pyxl", False),
        ],
    )
    def test_is_loading_file(self, path: str, expected: bool):
        assert is_loading_file(path) == expected

    def test_case_insensitive(self):
        assert is_loading_file("Loading.pyxl")
        assert is_loading_file("LOADING.PYXL")


def _stub_page(relative_path: str, path: str = "/") -> MagicMock:
    mock = MagicMock()
    mock.source_relative_path = Path(relative_path)
    mock.path = path
    return mock


class TestBuildLoadingBoundaryRegistry:
    def test_empty_input(self):
        registry = build_loading_boundary_registry([])
        assert registry.loading_pages == {}
        assert not registry.has_loading_pages

    def test_root_loading_page(self):
        page = _stub_page("loading.pyxl", "/loading")
        registry = build_loading_boundary_registry([page])
        assert registry.loading_pages["."] is page
        assert registry.has_loading_pages

    def test_nested_loading_page(self):
        page = _stub_page("dashboard/loading.pyxl", "/dashboard/loading")
        registry = build_loading_boundary_registry([page])
        assert "dashboard" in registry.loading_pages

    def test_deeply_nested(self):
        page = _stub_page("dashboard/settings/loading.pyxl")
        registry = build_loading_boundary_registry([page])
        assert "dashboard/settings" in registry.loading_pages

    def test_non_loading_pages_are_ignored(self):
        index = _stub_page("index.pyxl", "/")
        error = _stub_page("error.pyxl", "/error")
        registry = build_loading_boundary_registry([index, error])
        assert not registry.has_loading_pages


class TestFindLoadingBoundary:
    def _registry(self) -> LoadingBoundaryRegistry:
        return LoadingBoundaryRegistry(
            loading_pages={
                ".": _stub_page("loading.pyxl"),
                "dashboard": _stub_page("dashboard/loading.pyxl"),
                "dashboard/settings": _stub_page("dashboard/settings/loading.pyxl"),
            }
        )

    def test_root_route_finds_root_loading(self):
        result = self._registry().find_loading_boundary("/")
        assert result.source_relative_path == Path("loading.pyxl")

    def test_dashboard_child_finds_dashboard_loading(self):
        result = self._registry().find_loading_boundary("/dashboard/users")
        assert result.source_relative_path == Path("dashboard/loading.pyxl")

    def test_settings_child_finds_settings_loading(self):
        result = self._registry().find_loading_boundary("/dashboard/settings/profile")
        assert result.source_relative_path == Path("dashboard/settings/loading.pyxl")

    def test_unrelated_route_falls_back_to_root(self):
        result = self._registry().find_loading_boundary("/about")
        assert result.source_relative_path == Path("loading.pyxl")

    def test_trailing_slash_normalised(self):
        result = self._registry().find_loading_boundary("/dashboard/")
        assert result.source_relative_path == Path("dashboard/loading.pyxl")

    def test_no_boundary_returns_none(self):
        empty = LoadingBoundaryRegistry(loading_pages={})
        assert empty.find_loading_boundary("/anything") is None
