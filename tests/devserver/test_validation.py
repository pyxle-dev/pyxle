"""Tests for the @action Pydantic body-validation engine."""

from __future__ import annotations

from typing import Annotated, Optional

import pytest
from pydantic import BaseModel, Field

from pyxle.devserver import validation
from pyxle.devserver.validation import (
    PydanticNotInstalledError,
    ResolvedBody,
    get_cached_body_model,
    resolve_body_model,
    translate_validation_error,
    validate_body,
)
from pyxle.runtime import ValidationActionError


class Address(BaseModel):
    zip: str


class UserBody(BaseModel):
    email: str
    age: int = Field(gt=0)
    address: Address
    tags: list[str]


# `from __future__ import annotations` makes these annotations strings, so
# resolution exercises typing.get_type_hints (the path the codebase relies on).
async def act_plain(request, body: UserBody):  # noqa: ANN001
    return {}


async def act_annotated(request, body: Annotated[UserBody, "doc"]):  # noqa: ANN001
    return {}


async def act_optional(request, body: Optional[UserBody] = None):  # noqa: ANN001
    return {}


async def act_pipe_optional(request, body: UserBody | None = None):  # noqa: ANN001
    return {}


async def act_optional_annotated(  # noqa: ANN001
    request, body: Optional[Annotated[UserBody, "doc"]] = None
):
    return {}


async def act_multi_union(request, body: UserBody | int = 0):  # noqa: ANN001
    return {}


async def act_unannotated_body(request, body):  # noqa: ANN001
    return {}


async def act_no_body(request):  # noqa: ANN001
    return {}


async def act_nonmodel_param(request, limit: int = 10):  # noqa: ANN001
    return {}


# ---------------------------------------------------------------------------
# resolve_body_model


def test_resolves_plain_model() -> None:
    resolved = resolve_body_model(act_plain)
    assert resolved == ResolvedBody(param_name="body", model=UserBody)


def test_resolves_annotated_model() -> None:
    assert resolve_body_model(act_annotated) == ResolvedBody("body", UserBody)


@pytest.mark.parametrize("fn", [act_optional, act_pipe_optional])
def test_resolves_optional_model(fn) -> None:
    assert resolve_body_model(fn) == ResolvedBody("body", UserBody)


def test_resolves_optional_annotated_model() -> None:
    # Optional[Annotated[Model, ...]] unwraps both layers down to the model.
    assert resolve_body_model(act_optional_annotated) == ResolvedBody("body", UserBody)


def test_multi_type_union_is_not_a_body_model() -> None:
    # A union with more than one non-None member isn't a single body model.
    assert resolve_body_model(act_multi_union) is None


def test_unannotated_body_param_returns_none() -> None:
    # A present-but-unannotated extra parameter isn't a body model.
    assert resolve_body_model(act_unannotated_body) is None


async def act_unresolvable_hint(request, body: "NotARealType"):  # noqa: ANN001, F821
    return {}


def test_unresolvable_annotation_falls_back_and_returns_none() -> None:
    # ``get_type_hints`` raises NameError on the undefined forward reference;
    # resolution falls back to the raw string annotation, which isn't a model.
    assert resolve_body_model(act_unresolvable_hint) is None


def test_non_weakly_referenceable_callable_resolves_without_caching() -> None:
    # A callable whose instances can't be weak-referenced (``__slots__`` with no
    # ``__weakref__``) can't be a WeakKeyDictionary key — ``get_cached_body_model``
    # must fall back to resolving directly instead of raising ``TypeError``.
    class Action:
        __slots__ = ()

        async def __call__(self, request):  # noqa: ANN001
            return {}

    action = Action()
    # No body parameter -> None, and crucially it doesn't raise on the
    # unhashable-for-weakref key.
    assert get_cached_body_model(action) is None
    assert get_cached_body_model(action) == resolve_body_model(action)


def test_no_body_param_returns_none() -> None:
    assert resolve_body_model(act_no_body) is None


def test_non_model_param_returns_none() -> None:
    # A typed-but-not-a-model second param (with a default) isn't a body model.
    assert resolve_body_model(act_nonmodel_param) is None


def test_pydantic_absent_with_required_body_raises(monkeypatch) -> None:
    monkeypatch.setattr(validation, "_try_import_pydantic", lambda: None)
    with pytest.raises(PydanticNotInstalledError):
        resolve_body_model(act_plain)


def test_pydantic_absent_without_body_is_fine(monkeypatch) -> None:
    monkeypatch.setattr(validation, "_try_import_pydantic", lambda: None)
    assert resolve_body_model(act_no_body) is None
    # An optional (defaulted) extra param doesn't force pydantic.
    assert resolve_body_model(act_nonmodel_param) is None


# ---------------------------------------------------------------------------
# caching


def test_cache_is_per_function_object() -> None:
    first = get_cached_body_model(act_plain)
    second = get_cached_body_model(act_plain)
    assert first is second  # memoised, not re-resolved

    # A different function object resolves independently.
    assert get_cached_body_model(act_no_body) is None


# ---------------------------------------------------------------------------
# validate_body + translate_validation_error


def test_validate_body_success() -> None:
    instance = validate_body(
        UserBody,
        {"email": "a@b.c", "age": 5, "address": {"zip": "12345"}, "tags": ["x"]},
    )
    assert isinstance(instance, UserBody)
    assert instance.email == "a@b.c"


def test_validate_body_raises_with_field_paths() -> None:
    with pytest.raises(ValidationActionError) as exc_info:
        validate_body(
            UserBody,
            {"age": 0, "address": {}, "tags": [1]},  # missing email, bad age/zip/tags
        )
    fields = exc_info.value.fields
    assert exc_info.value.status_code == 422
    # Top-level missing field.
    assert "email" in fields
    # Nested model field uses a dotted path.
    assert "address.zip" in fields
    # List item uses an index path.
    assert "tags.0" in fields
    # Constraint failure carries a message.
    assert any("greater than 0" in m for m in fields.get("age", []))


def test_translate_groups_multiple_messages_per_field() -> None:
    class M(BaseModel):
        n: int

    from pydantic import ValidationError

    try:
        M.model_validate({"n": "not-an-int"})
    except ValidationError as exc:
        fields = translate_validation_error(exc)
        assert "n" in fields and len(fields["n"]) >= 1
