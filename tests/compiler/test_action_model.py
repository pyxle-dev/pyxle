"""Tests for ActionDeclaration and PageMetadata actions support."""

from __future__ import annotations

from pyxle.compiler.model import ActionDeclaration, PageMetadata


def _base_metadata(**kwargs) -> PageMetadata:
    defaults = dict(
        route_path="/",
        alternate_route_paths=(),
        client_path="/pages/index.jsx",
        server_path="/pages/index.py",
        loader_name=None,
        loader_line=None,
        head_elements=(),
        head_is_dynamic=False,
    )
    defaults.update(kwargs)
    return PageMetadata(**defaults)


class TestActionDeclaration:
    def test_to_json(self) -> None:
        decl = ActionDeclaration(name="save", line=10)
        assert decl.to_json() == {"name": "save", "line": 10}

    def test_to_json_no_line(self) -> None:
        decl = ActionDeclaration(name="delete", line=None)
        assert decl.to_json() == {"name": "delete", "line": None}

    def test_is_frozen(self) -> None:

        assert ActionDeclaration.__dataclass_params__.frozen  # type: ignore[attr-defined]


class TestPageMetadataActions:
    def test_has_actions_false_by_default(self) -> None:
        meta = _base_metadata()
        assert meta.has_actions is False

    def test_has_actions_true_when_present(self) -> None:
        meta = _base_metadata(
            actions=(ActionDeclaration(name="save", line=5),)
        )
        assert meta.has_actions is True

    def test_to_json_includes_actions(self) -> None:
        meta = _base_metadata(
            actions=(
                ActionDeclaration(name="create", line=3),
                ActionDeclaration(name="remove", line=8),
            )
        )
        payload = meta.to_json()
        assert "actions" in payload
        assert payload["actions"] == [
            {"name": "create", "line": 3},
            {"name": "remove", "line": 8},
        ]

    def test_to_json_empty_actions(self) -> None:
        meta = _base_metadata()
        payload = meta.to_json()
        assert payload["actions"] == []

    def test_to_json_includes_cache_revalidate(self) -> None:
        assert _base_metadata().to_json()["cache_revalidate"] is None
        assert _base_metadata(cache_revalidate=60.0).to_json()["cache_revalidate"] == 60.0
