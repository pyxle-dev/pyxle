"""Tests for pyxle.security — HMAC signing for cookies and opaque values."""

from __future__ import annotations

import re

import pytest

from pyxle.security import (
    MissingSecretKeyError,
    sign_cookie,
    verify_cookie,
)

_KEY = "test-signing-secret"
_HEX_64 = re.compile(r"^[0-9a-f]{64}$")


@pytest.fixture(autouse=True)
def _clear_secret_env(monkeypatch):
    """Isolate every test from a PYXLE_SECRET_KEY that may exist on the host.

    Tests that exercise environment resolution set it explicitly.
    """
    monkeypatch.delenv("PYXLE_SECRET_KEY", raising=False)


class TestSignAndVerifyRoundTrip:
    def test_round_trip_returns_original_value(self):
        token = sign_cookie("user-42", _KEY)
        assert verify_cookie(token, _KEY) == "user-42"

    def test_signed_format_is_value_dot_hex(self):
        token = sign_cookie("hello", _KEY)
        value, _, signature = token.rpartition(".")
        assert value == "hello"
        assert _HEX_64.match(signature), "signature must be a full 64-char hex digest"

    def test_value_containing_dots_round_trips(self):
        original = "a.b.c.d"
        token = sign_cookie(original, _KEY)
        # rpartition keeps the last dot as the separator, so dotted values survive.
        assert verify_cookie(token, _KEY) == original

    def test_empty_value_round_trips(self):
        token = sign_cookie("", _KEY)
        assert token.startswith(".")
        assert verify_cookie(token, _KEY) == ""

    def test_distinct_values_get_distinct_signatures(self):
        sig_a = sign_cookie("a", _KEY).rpartition(".")[2]
        sig_b = sign_cookie("b", _KEY).rpartition(".")[2]
        assert sig_a != sig_b

    def test_signing_is_deterministic(self):
        assert sign_cookie("x", _KEY) == sign_cookie("x", _KEY)


class TestTamperRejection:
    def test_tampered_value_rejected(self):
        token = sign_cookie("admin=false", _KEY)
        _, _, signature = token.rpartition(".")
        forged = f"admin=true.{signature}"
        assert verify_cookie(forged, _KEY) is None

    def test_tampered_signature_rejected(self):
        token = sign_cookie("payload", _KEY)
        assert verify_cookie(token[:-1] + ("0" if token[-1] != "0" else "1"), _KEY) is None

    def test_wrong_secret_rejected(self):
        token = sign_cookie("payload", _KEY)
        assert verify_cookie(token, "a-different-secret") is None

    def test_no_separator_returns_none(self):
        assert verify_cookie("no-signature-here", _KEY) is None

    def test_empty_input_returns_none(self):
        assert verify_cookie("", _KEY) is None

    def test_empty_signature_segment_returns_none(self):
        assert verify_cookie("value.", _KEY) is None

    def test_unrelated_string_with_dot_returns_none(self):
        assert verify_cookie("value.deadbeef", _KEY) is None


class TestSaltNamespacing:
    def test_same_salt_round_trips(self):
        token = sign_cookie("email@x.com", _KEY, salt="password-reset")
        assert verify_cookie(token, _KEY, salt="password-reset") == "email@x.com"

    def test_wrong_salt_rejected(self):
        token = sign_cookie("email@x.com", _KEY, salt="password-reset")
        assert verify_cookie(token, _KEY, salt="login") is None

    def test_default_salt_differs_from_named_salt(self):
        default = sign_cookie("v", _KEY).rpartition(".")[2]
        salted = sign_cookie("v", _KEY, salt="ns").rpartition(".")[2]
        assert default != salted


class TestSecretResolution:
    def test_env_secret_used_when_key_omitted(self, monkeypatch):
        monkeypatch.setenv("PYXLE_SECRET_KEY", "env-secret")
        token = sign_cookie("v")
        assert verify_cookie(token) == "v"

    def test_explicit_key_overrides_env(self, monkeypatch):
        monkeypatch.setenv("PYXLE_SECRET_KEY", "env-secret")
        token = sign_cookie("v", "explicit-secret")
        # The env secret must NOT validate a token signed with the explicit key.
        assert verify_cookie(token) is None
        assert verify_cookie(token, "explicit-secret") == "v"

    def test_sign_without_secret_raises(self):
        with pytest.raises(MissingSecretKeyError):
            sign_cookie("v")

    def test_verify_without_secret_raises(self):
        # Fails closed: no secret rejects loudly rather than accepting anything.
        with pytest.raises(MissingSecretKeyError):
            verify_cookie("v.deadbeef")

    def test_explicit_empty_secret_is_a_misconfiguration(self):
        with pytest.raises(MissingSecretKeyError):
            sign_cookie("v", "")


class TestPackageReExport:
    def test_exported_from_top_level_package(self):
        import pyxle

        assert pyxle.sign_cookie is sign_cookie
        assert pyxle.verify_cookie is verify_cookie
        assert "sign_cookie" in pyxle.__all__
        assert "verify_cookie" in pyxle.__all__


class TestEdgeCaseInputs:
    def test_unicode_value_salt_and_secret_round_trip(self):
        value = "café ☕ → 世界 🚀"
        token = sign_cookie(value, "sünrïse-🔑", salt="námespace-✨")
        assert verify_cookie(token, "sünrïse-🔑", salt="námespace-✨") == value

    def test_large_value_round_trips(self):
        value = "x" * 1_000_000
        assert verify_cookie(sign_cookie(value, _KEY), _KEY) == value

    def test_signature_shaped_value_round_trips(self):
        # A value that itself looks like "<text>.<64-hex>" must survive: rpartition
        # splits on the LAST dot, so the real signature is appended after it and
        # the whole crafted string is recovered intact.
        crafted = "x." + "a" * 64
        assert verify_cookie(sign_cookie(crafted, _KEY), _KEY) == crafted

    def test_signature_shaped_value_is_not_self_validating(self):
        # The same crafted string presented directly, with no real signature,
        # must be rejected — appending plausible hex can't forge a token.
        assert verify_cookie("x." + "a" * 64, _KEY) is None


class TestUntrustedInputNeverRaises:
    """`verify_cookie` is handed cookie values straight from a request, so it
    has to be total: the documented contract is `None` for any signature or
    format failure. `hmac.compute_digest` on a non-ASCII `str` raises
    TypeError instead, which would surface as a 500 on a tampered cookie.
    """

    def test_non_ascii_signature_segment_returns_none(self):
        assert verify_cookie("user-42.caf\xe9", _KEY) is None

    def test_replacement_character_signature_returns_none(self):
        assert verify_cookie("user-42.�" * 2, _KEY) is None

    def test_lone_surrogate_returns_none(self):
        assert verify_cookie("user-42.\ud800", _KEY) is None

    def test_a_valid_token_still_verifies(self):
        assert verify_cookie(sign_cookie("user-42", _KEY), _KEY) == "user-42"


class TestConstantTimeEquals:
    def test_equal_ascii(self):
        from pyxle.security import constant_time_equals

        assert constant_time_equals("abc", "abc") is True

    def test_unequal_ascii(self):
        from pyxle.security import constant_time_equals

        assert constant_time_equals("abc", "xyz") is False

    def test_non_ascii_compares_instead_of_raising(self):
        from pyxle.security import constant_time_equals

        assert constant_time_equals("caf\xe9", "caf\xe9") is True
        assert constant_time_equals("caf\xe9", "cafe") is False

    def test_lone_surrogate_compares_instead_of_raising(self):
        from pyxle.security import constant_time_equals

        assert constant_time_equals("\ud800", "\ud800") is True
        assert constant_time_equals("\ud800", "x") is False
