"""Tests for action-related import helpers in writers.py."""

from __future__ import annotations

from textwrap import dedent

from pyxle.compiler.writers import (
    ensure_action_error_import,
    ensure_action_import,
    ensure_server_action_import,
    ensure_validation_action_error_import,
)


class TestEnsureActionImport:
    def test_adds_import_when_missing(self) -> None:
        source = "async def save(request):\n    pass\n"
        result = ensure_action_import(source)
        assert "from pyxle.runtime import action" in result

    def test_no_duplicate_when_already_present(self) -> None:
        source = "from pyxle.runtime import action\nasync def save(request):\n    pass\n"
        result = ensure_action_import(source)
        assert result.count("from pyxle.runtime import action") == 1

    def test_empty_source_unchanged(self) -> None:
        result = ensure_action_import("")
        assert result == ""

    def test_preserves_trailing_newline(self) -> None:
        source = "async def save(request):\n    pass\n"
        result = ensure_action_import(source)
        assert result.endswith("\n")

    def test_no_trailing_newline_preserved(self) -> None:
        source = "async def save(request):\n    pass"
        result = ensure_action_import(source)
        assert not result.endswith("\n")

    def test_no_import_when_action_defined_locally(self) -> None:
        source = "def action(fn):\n    return fn\nasync def save(request):\n    pass\n"
        result = ensure_action_import(source)
        assert result.count("import action") == 0

    def test_no_import_when_action_assigned(self) -> None:
        source = "action = lambda fn: fn\nasync def save(request):\n    pass\n"
        result = ensure_action_import(source)
        assert result.count("import action") == 0

    def test_no_import_when_action_annotated_assigned(self) -> None:
        source = "action: object = lambda fn: fn\nasync def save(request):\n    pass\n"
        result = ensure_action_import(source)
        assert result.count("import action") == 0

    def test_no_import_when_action_bare_imported(self) -> None:
        source = "import action\nasync def save(request):\n    pass\n"
        result = ensure_action_import(source)
        assert result.count("import action") == 1  # the bare import, not a new one

    def test_adds_import_when_different_module_imported(self) -> None:
        """import os should not satisfy the action import requirement."""
        source = "import os\nasync def save(request):\n    pass\n"
        result = ensure_action_import(source)
        assert "from pyxle.runtime import action" in result

    def test_adds_import_with_tuple_assignment_target(self) -> None:
        """Tuple unpacking assignment should not suppress the action import."""
        source = "x, y = 1, 2\nasync def save(request):\n    pass\n"
        result = ensure_action_import(source)
        assert "from pyxle.runtime import action" in result

    def test_adds_import_after_docstring_only_module(self) -> None:
        """Module with only a docstring should get the import inserted after it."""
        source = '"""Module docstring."""\nasync def save(request):\n    pass\n'
        result = ensure_action_import(source)
        assert "from pyxle.runtime import action" in result


class TestEnsureServerActionImport:
    def test_combined_import_when_both_missing(self) -> None:
        source = dedent(
            """
            @server
            async def load(request):
                return {}

            @action
            async def save(request):
                pass
            """
        ).strip() + "\n"
        result = ensure_server_action_import(source)
        assert "from pyxle.runtime import server, action" in result

    def test_only_server_when_action_present(self) -> None:
        source = dedent(
            """
            from pyxle.runtime import action

            @server
            async def load(request):
                return {}
            """
        ).strip() + "\n"
        result = ensure_server_action_import(source)
        assert "from pyxle.runtime import server" in result
        # Should not add combined import
        assert result.count("from pyxle.runtime import server, action") == 0

    def test_only_action_when_server_present(self) -> None:
        source = dedent(
            """
            from pyxle.runtime import server

            @action
            async def save(request):
                pass
            """
        ).strip() + "\n"
        result = ensure_server_action_import(source)
        assert "from pyxle.runtime import action" in result

    def test_no_change_when_both_present(self) -> None:
        source = dedent(
            """
            from pyxle.runtime import server, action

            @server
            async def load(request):
                return {}

            @action
            async def save(request):
                pass
            """
        ).strip() + "\n"
        result = ensure_server_action_import(source)
        assert result == source

    def test_empty_source_unchanged(self) -> None:
        result = ensure_server_action_import("")
        assert result == ""

    def test_no_trailing_newline_preserved(self) -> None:
        source = "async def save(request):\n    pass"
        result = ensure_server_action_import(source)
        assert not result.endswith("\n")


class TestEnsureActionErrorImport:
    """Pages with ``@action`` get ``ActionError`` auto-imported so
    the idiomatic ``raise ActionError(...)`` pattern works without the
    user remembering to add the import. This mirrors :func:`ensure_action_import`."""

    def test_adds_import_when_missing(self) -> None:
        source = "async def save(request):\n    pass\n"
        result = ensure_action_error_import(source)
        assert "from pyxle.runtime import ActionError" in result

    def test_no_duplicate_when_already_present(self) -> None:
        source = (
            "from pyxle.runtime import ActionError\n"
            "async def save(request):\n    pass\n"
        )
        result = ensure_action_error_import(source)
        assert result.count("from pyxle.runtime import ActionError") == 1

    def test_empty_source_returned_unchanged(self) -> None:
        assert ensure_action_error_import("") == ""

    def test_respects_existing_combined_import(self) -> None:
        source = (
            "from pyxle.runtime import action, ActionError\n"
            "async def save(request):\n    pass\n"
        )
        result = ensure_action_error_import(source)
        # No extra import line was added.
        assert result == source

    def test_does_not_shadow_local_class(self) -> None:
        # A user-defined ActionError class takes precedence — no injection.
        source = (
            "class ActionError(Exception):\n    pass\n"
            "async def save(request):\n    pass\n"
        )
        result = ensure_action_error_import(source)
        assert "from pyxle.runtime import ActionError" not in result

    def test_honours_alias(self) -> None:
        source = (
            "from pyxle.runtime import ActionError as PyxleActionError\n"
            "ActionError = PyxleActionError\n"
            "async def save(request):\n    pass\n"
        )
        result = ensure_action_error_import(source)
        # Module-level ``ActionError = ...`` assignment satisfies the
        # need; helper returns source unchanged.
        assert result == source

    def test_inserted_after_docstring_and_future(self) -> None:
        source = dedent(
            '''\
            """Module docstring."""
            from __future__ import annotations

            async def save(request):
                pass
            '''
        )
        result = ensure_action_error_import(source)
        lines = result.splitlines()
        # Should come after the __future__ import, before the function.
        future_idx = next(i for i, line in enumerate(lines) if "from __future__" in line)
        import_idx = next(
            i for i, line in enumerate(lines) if "from pyxle.runtime import ActionError" in line
        )
        func_idx = next(i for i, line in enumerate(lines) if "async def save" in line)
        assert future_idx < import_idx < func_idx


class TestEnsureValidationActionErrorImport:
    """Pages with ``@action`` also get ``ValidationActionError`` auto-imported,
    so a handler can ``raise ValidationActionError(fields=...)`` for hand-rolled
    field errors without an import. Independent of ``ActionError`` — a
    user-owned name suppresses only its own injection."""

    def test_adds_import_when_missing(self) -> None:
        source = "async def save(request):\n    pass\n"
        result = ensure_validation_action_error_import(source)
        assert "from pyxle.runtime import ValidationActionError" in result

    def test_no_duplicate_when_already_present(self) -> None:
        source = (
            "from pyxle.runtime import ValidationActionError\n"
            "async def save(request):\n    pass\n"
        )
        result = ensure_validation_action_error_import(source)
        assert result.count("from pyxle.runtime import ValidationActionError") == 1

    def test_empty_source_returned_unchanged(self) -> None:
        assert ensure_validation_action_error_import("") == ""

    def test_respects_existing_combined_import(self) -> None:
        source = (
            "from pyxle.runtime import action, ValidationActionError\n"
            "async def save(request):\n    pass\n"
        )
        assert ensure_validation_action_error_import(source) == source

    def test_does_not_shadow_local_class(self) -> None:
        source = (
            "class ValidationActionError(Exception):\n    pass\n"
            "async def save(request):\n    pass\n"
        )
        result = ensure_validation_action_error_import(source)
        assert "from pyxle.runtime import ValidationActionError" not in result

    def test_independent_of_action_error(self) -> None:
        # A page that imports ActionError but not ValidationActionError still
        # gets the validation subclass injected (and ActionError untouched).
        source = (
            "from pyxle.runtime import ActionError\n"
            "async def save(request):\n    pass\n"
        )
        result = ensure_validation_action_error_import(source)
        assert "from pyxle.runtime import ValidationActionError" in result
        assert result.count("from pyxle.runtime import ActionError") == 1
