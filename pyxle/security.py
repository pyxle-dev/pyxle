"""Tamper-proof signing for cookies and other opaque values.

Sign a string with the application secret and later verify it was not altered —
the building block for tamper-proof cookies (a signed session id, a
"remember me" token, a stateless unsubscribe link). This is **signing, not
encryption**: the value stays human-readable; a valid signature only proves it
was produced by someone holding the secret. Despite the ``cookie`` in the
function names, the value can be any string — a session id, a password-reset or
unsubscribe link, a signed UUID — not only a cookie value.

The secret comes from the ``secret_key`` argument or, when that is omitted, the
``PYXLE_SECRET_KEY`` environment variable. Signing is meaningless without a
secret, so both functions raise :class:`MissingSecretKeyError` rather than
silently returning an unprotected value — a missing secret fails closed.

Example::

    from pyxle import sign_cookie, verify_cookie

    token = sign_cookie("user-42", secret_key="s3cret")   # "user-42.<hmac>"
    verify_cookie(token, secret_key="s3cret")             # "user-42"
    verify_cookie("user-42.deadbeef", secret_key="s3cret")  # None (bad signature)

Use ``salt`` to namespace signatures so a value signed for one purpose cannot
be replayed for another, even under the same secret::

    reset = sign_cookie(email, secret_key=k, salt="password-reset")
    verify_cookie(reset, secret_key=k, salt="login")      # None (wrong salt)
"""

from __future__ import annotations

import hashlib
import hmac
import os

__all__ = ["sign_cookie", "verify_cookie", "MissingSecretKeyError"]

_SECRET_ENV_VAR = "PYXLE_SECRET_KEY"
_SEPARATOR = "."


class MissingSecretKeyError(RuntimeError):
    """Raised when signing or verifying is attempted with no secret available.

    Signing without a secret offers no protection, so Pyxle fails loud instead
    of returning (or accepting) an unsigned value. Pass ``secret_key=``
    explicitly, or set the ``PYXLE_SECRET_KEY`` environment variable.
    """


def _resolve_secret(secret_key: str | None) -> str:
    """Return the secret to sign with, or raise if none is configured.

    ``None`` means "read ``PYXLE_SECRET_KEY`` from the environment"; an explicit
    empty string is treated as a misconfiguration, not as "no secret".
    """
    secret = (
        secret_key if secret_key is not None else os.environ.get(_SECRET_ENV_VAR, "")
    )
    if not secret:
        raise MissingSecretKeyError(
            "No signing secret available. Pass secret_key=... or set the "
            f"{_SECRET_ENV_VAR} environment variable."
        )
    return secret


def _signature(value: str, secret: str, salt: str) -> str:
    """HMAC-SHA256 of *value* under a key derived from *secret* and *salt*.

    Deriving a per-salt key (rather than mixing the salt into the signed
    message) keeps signatures for different salts cryptographically
    independent: a value signed under one salt can never validate under
    another, even for the same value and secret. The signature is the full
    64-character hex digest — no truncation — so it carries the full 256 bits
    of HMAC strength.
    """
    derived_key = hmac.new(secret.encode(), salt.encode(), hashlib.sha256).digest()
    return hmac.new(derived_key, value.encode(), hashlib.sha256).hexdigest()


def sign_cookie(value: str, secret_key: str | None = None, *, salt: str = "") -> str:
    """Append an HMAC signature to *value*, returning ``"<value>.<hmac>"``.

    *value* may be any string and stays readable in the result (this is signing,
    not encryption). The only way to produce a string that :func:`verify_cookie`
    accepts is to hold the same secret and salt.

    Raises :class:`MissingSecretKeyError` if no secret is available (see the
    module docstring for how the secret is resolved).
    """
    secret = _resolve_secret(secret_key)
    return f"{value}{_SEPARATOR}{_signature(value, secret, salt)}"


def verify_cookie(
    signed_value: str, secret_key: str | None = None, *, salt: str = ""
) -> str | None:
    """Return the original value if the signature is valid, else ``None``.

    ``None`` is returned for a signature/format failure — empty input, no
    signature segment, or a signature that does not match (wrong secret, wrong
    salt, or a tampered value). The comparison is constant-time. A *missing*
    secret is a different case: it raises :class:`MissingSecretKeyError` (just
    like :func:`sign_cookie`), so a misconfigured server fails closed and loudly
    rather than silently treating every token as invalid.

    The recovered value may itself be an empty string (a validly-signed ``""``),
    which is falsy — test the result with ``is not None``, not truthiness::

        value = verify_cookie(token, key)
        if value is not None:
            ...  # trust `value`
    """
    secret = _resolve_secret(secret_key)
    if not signed_value or _SEPARATOR not in signed_value:
        return None
    value, _, signature = signed_value.rpartition(_SEPARATOR)
    if not signature:
        return None
    expected = _signature(value, secret, salt)
    if hmac.compare_digest(signature, expected):
        return value
    return None
